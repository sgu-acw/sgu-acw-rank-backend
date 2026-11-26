"""审核队列业务逻辑与改名曾用名归档。

覆盖两类需要管理员介入的动作：
1. 新用户注册审核（校内邀请码注册 + 校外自助注册）。
2. 成员改名 / 更改个人学籍信息（学院、专业、班级）申请。

另外集中处理 ``apply_display_name_change``：无论通过审核生效、管理员后台
直接修改，还是管理员自助改名，旧姓名都会自动沉淀进 ``former_names_json``
并递增 ``honor_revision``，确保永远可追溯成员改名前是谁。
"""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy import func, update
from sqlmodel import Session, select

from . import academic, notifications
from .cleanup import delete_user_accounts, delete_user_references
from .models import (
    Invite,
    InviteUse,
    ReviewKind,
    ReviewRequest,
    ReviewStatus,
    User,
    UserSession,
    UserStatus,
    utcnow,
)
from .schemas import CreateProfileChangeRequest, ReviewRequestPublic

PENDING_REVIEW_FINGERPRINT = "review:pending"


def apply_display_name_change(user: User, new_name: str) -> bool:
    """修改成员姓名，同时自动将旧姓名归档到 ``former_names_json``。

    返回 True 表示姓名实际发生了变化。
    """
    cleaned = new_name.strip()
    old_name = (user.display_name or "").strip()
    if not cleaned or cleaned == old_name:
        return False

    history = list(user.former_names)
    if old_name and old_name not in history:
        history.append(old_name)
        user.former_names_json = json.dumps(history, ensure_ascii=False)

    user.display_name = cleaned
    user.honor_revision += 1
    return True


def _load_json_dict(raw: str | None) -> dict[str, object]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pending_counts(db: Session) -> tuple[int, int, int]:
    """返回 ``(总待审, 注册待审, 资料修改待审)``。"""
    reg = int(
        db.exec(
            select(func.count())
            .select_from(ReviewRequest)
            .where(
                ReviewRequest.status == ReviewStatus.PENDING,
                ReviewRequest.kind == ReviewKind.REGISTRATION,
            )
        ).one()
    )
    prof = int(
        db.exec(
            select(func.count())
            .select_from(ReviewRequest)
            .where(
                ReviewRequest.status == ReviewStatus.PENDING,
                ReviewRequest.kind == ReviewKind.PROFILE_CHANGE,
            )
        ).one()
    )
    return reg + prof, reg, prof


def sync_pending_review_notification(db: Session) -> int:
    """同步管理员站内信：有待审项时上报聚合提醒，清空后自动标记已解决。"""
    total, reg, prof = _pending_counts(db)
    if total == 0:
        notifications.resolve(db, PENDING_REVIEW_FINGERPRINT)
        return 0

    parts: list[str] = []
    if reg > 0:
        parts.append(f"注册申请 {reg} 条")
    if prof > 0:
        parts.append(f"改名/资料修改申请 {prof} 条")
    body = "当前待处理：" + "、".join(parts) + "。请前往管理工作台审核。"
    notifications.report(
        db,
        PENDING_REVIEW_FINGERPRINT,
        title=f"有 {total} 条待审核申请",
        body=body,
        href="/admin/reviews",
    )
    return total


def create_registration_review(
    db: Session,
    user: User,
    *,
    invite_code: str | None = None,
    reason: str = "",
) -> ReviewRequest:
    """为新注册的待审核账号建立审核单。"""
    after: dict[str, object] = {
        "student_id": user.student_id,
        "display_name": user.display_name,
        "role": user.role,
    }
    if not user.is_external:
        after["enroll_year"] = user.enroll_year
        after["college_name"] = user.college_name
        after["major_name"] = user.major_name
        after["class_no"] = user.class_no
        after["class_label"] = user.class_label
    if invite_code:
        after["invite_code"] = invite_code

    row = ReviewRequest(
        kind=ReviewKind.REGISTRATION,
        status=ReviewStatus.PENDING,
        user_id=user.id,
        student_id=user.student_id,
        current_display_name=user.display_name,
        role=user.role,
        invite_code=invite_code,
        before_json="{}",
        after_json=json.dumps(after, ensure_ascii=False),
        reason=reason.strip(),
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()
    sync_pending_review_notification(db)
    return row


def create_profile_change_review(
    db: Session,
    user: User,
    payload: CreateProfileChangeRequest,
) -> ReviewRequest:
    """成员提交改名 / 更改个人资料审核单。"""
    existing = db.exec(
        select(ReviewRequest).where(
            ReviewRequest.user_id == user.id,
            ReviewRequest.kind == ReviewKind.PROFILE_CHANGE,
            ReviewRequest.status == ReviewStatus.PENDING,
        )
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="你已有一条待审核的资料修改申请，请等待处理或撤回后重新提交",
        )

    if user.is_external and (
        payload.college_override is not None
        or payload.major_override is not None
        or payload.class_no is not None
    ):
        raise HTTPException(status_code=400, detail="校外账号仅支持申请修改昵称")

    before: dict[str, object] = {}
    after: dict[str, object] = {}

    if payload.display_name is not None and payload.display_name != user.display_name:
        before["display_name"] = user.display_name
        after["display_name"] = payload.display_name

    if not user.is_external:
        if payload.college_override is not None:
            next_college_override = payload.college_override.strip() or None
            resolved_next = next_college_override or academic.college_name(user.college_code)
            if (
                next_college_override != user.college_override
                and (resolved_next != user.college_name or next_college_override is None)
            ):
                before["college_name"] = user.college_name or ""
                after["college_override"] = next_college_override or ""
                after["college_name"] = resolved_next or ""

        if payload.major_override is not None:
            next_major_override = payload.major_override.strip() or None
            resolved_next = next_major_override or academic.major_name(
                user.college_code, user.major_code
            )
            if (
                next_major_override != user.major_override
                and (resolved_next != user.major_name or next_major_override is None)
            ):
                before["major_name"] = user.major_name or ""
                after["major_override"] = next_major_override or ""
                after["major_name"] = resolved_next or ""

        if payload.class_no is not None and payload.class_no != user.class_no:
            before["class_no"] = user.class_no
            after["class_no"] = payload.class_no

    if not after:
        raise HTTPException(status_code=400, detail="未检测到需要修改的内容")

    row = ReviewRequest(
        kind=ReviewKind.PROFILE_CHANGE,
        status=ReviewStatus.PENDING,
        user_id=user.id,
        student_id=user.student_id,
        current_display_name=user.display_name,
        role=user.role,
        before_json=json.dumps(before, ensure_ascii=False),
        after_json=json.dumps(after, ensure_ascii=False),
        reason=payload.reason,
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()
    sync_pending_review_notification(db)
    return row


def _claim_pending_review(
    db: Session,
    review_id: int,
    *,
    next_status: str,
    admin_id: int,
    review_note: str,
) -> ReviewRequest:
    """原子占用待审单，避免多名管理员并发点击造成重复处理。"""
    now = utcnow()
    result = db.exec(  # type: ignore[call-overload]
        update(ReviewRequest)
        .where(
            ReviewRequest.id == review_id,
            ReviewRequest.status == ReviewStatus.PENDING,
        )
        .values(
            status=next_status,
            reviewed_by=admin_id,
            reviewed_at=now,
            review_note=review_note.strip(),
        )
    )
    if result.rowcount != 1:
        existing = db.get(ReviewRequest, review_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="审核单不存在")
        raise HTTPException(status_code=409, detail="该审核单已处理，无法重复操作")
    row = db.get(ReviewRequest, review_id, populate_existing=True)
    assert row is not None
    return row


def approve_review_request(
    db: Session,
    review_id: int,
    admin: User,
    *,
    review_note: str = "",
) -> ReviewRequest:
    """通过审核单并应用对应变更。"""
    assert admin.id is not None
    row = _claim_pending_review(
        db,
        review_id,
        next_status=ReviewStatus.APPROVED,
        admin_id=admin.id,
        review_note=review_note,
    )

    if row.user_id is None:
        raise HTTPException(status_code=404, detail="申请对应的账号已不存在")
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="申请对应的账号已不存在")

    if row.kind == ReviewKind.REGISTRATION:
        if user.status == UserStatus.PENDING:
            user.status = UserStatus.ACTIVE
            db.add(user)
    elif row.kind == ReviewKind.PROFILE_CHANGE:
        after = _load_json_dict(row.after_json)
        new_name = after.get("display_name")
        if isinstance(new_name, str) and new_name.strip():
            apply_display_name_change(user, new_name)
        if not user.is_external:
            if "college_override" in after:
                raw_college = after.get("college_override")
                user.college_override = (
                    raw_college.strip() or None if isinstance(raw_college, str) else None
                )
            if "major_override" in after:
                raw_major = after.get("major_override")
                user.major_override = (
                    raw_major.strip() or None if isinstance(raw_major, str) else None
                )
            if "class_no" in after and after["class_no"] is not None:
                user.class_no = int(after["class_no"])  # type: ignore[arg-type]
        db.add(user)

    db.flush()
    sync_pending_review_notification(db)
    return row


def reject_review_request(
    db: Session,
    review_id: int,
    admin: User,
    *,
    review_note: str = "",
) -> ReviewRequest:
    """驳回审核单；若是注册审核则释放占位账号与邀请码名额。"""
    assert admin.id is not None
    row = _claim_pending_review(
        db,
        review_id,
        next_status=ReviewStatus.REJECTED,
        admin_id=admin.id,
        review_note=review_note,
    )

    if row.kind == ReviewKind.REGISTRATION and row.user_id is not None:
        pending_user = db.get(User, row.user_id)
        if pending_user is not None and pending_user.status == UserStatus.PENDING:
            # 退回本次注册占用的邀请码名额，避免填错姓名被驳回后浪费邀请码。
            uses = list(
                db.exec(select(InviteUse).where(InviteUse.user_id == pending_user.id)).all()
            )
            for use in uses:
                invite = db.get(Invite, use.invite_id)
                if invite is not None and invite.use_count > 0:
                    invite.use_count -= 1
                    db.add(invite)
                db.delete(use)

            # 先解绑审核单外键再删待审核占位用户，保留完整的审核日志快照。
            row.user_id = None
            db.add(row)
            db.flush()

            for sess in db.exec(
                select(UserSession).where(UserSession.user_id == pending_user.id)
            ).all():
                db.delete(sess)
            delete_user_accounts(db, pending_user.id)
            delete_user_references(db, pending_user.id, reassign_to_user_id=admin.id)
            db.delete(pending_user)
            db.flush()

    sync_pending_review_notification(db)
    return row


def complete_registration_reviews_for_user(
    db: Session,
    user: User,
    admin: User,
    *,
    review_note: str = "管理员在成员列表中直接启用",
) -> int:
    """当管理员直接在成员表启用待审账号时，同步完结其待审注册单。"""
    if user.id is None or admin.id is None:
        return 0
    rows = list(
        db.exec(
            select(ReviewRequest).where(
                ReviewRequest.user_id == user.id,
                ReviewRequest.kind == ReviewKind.REGISTRATION,
                ReviewRequest.status == ReviewStatus.PENDING,
            )
        ).all()
    )
    if not rows:
        return 0
    now = utcnow()
    for row in rows:
        row.status = ReviewStatus.APPROVED
        row.reviewed_by = admin.id
        row.reviewed_at = now
        row.review_note = review_note
        db.add(row)
    db.flush()
    sync_pending_review_notification(db)
    return len(rows)


def cancel_pending_reviews_for_user(
    db: Session,
    user: User,
    admin: User,
    *,
    review_note: str = "管理员直接删除了该账号",
) -> int:
    """删除成员前将其所有待审核申请结案为驳回，并在待审注册被删时退回邀请码名额。"""
    if user.id is None or admin.id is None:
        return 0

    if user.status == UserStatus.PENDING:
        uses = list(db.exec(select(InviteUse).where(InviteUse.user_id == user.id)).all())
        for use in uses:
            invite = db.get(Invite, use.invite_id)
            if invite is not None and invite.use_count > 0:
                invite.use_count -= 1
                db.add(invite)
            db.delete(use)

    rows = list(
        db.exec(
            select(ReviewRequest).where(
                ReviewRequest.user_id == user.id,
                ReviewRequest.status == ReviewStatus.PENDING,
            )
        ).all()
    )
    if not rows:
        return 0
    now = utcnow()
    for row in rows:
        row.status = ReviewStatus.REJECTED
        row.reviewed_by = admin.id
        row.reviewed_at = now
        row.review_note = review_note
        db.add(row)
    db.flush()
    sync_pending_review_notification(db)
    return len(rows)


def serialize_review(
    row: ReviewRequest,
    reviewers: dict[int, User],
    applicant: User | None = None,
) -> ReviewRequestPublic:
    reviewer = reviewers.get(row.reviewed_by) if row.reviewed_by is not None else None
    return ReviewRequestPublic(
        id=row.id or 0,
        kind=row.kind,
        status=row.status,
        user_id=row.user_id,
        student_id=row.student_id,
        current_display_name=row.current_display_name,
        signature=applicant.signature if applicant else "",
        role=row.role,
        invite_code=row.invite_code,
        before=_load_json_dict(row.before_json),
        after=_load_json_dict(row.after_json),
        reason=row.reason,
        review_note=row.review_note,
        reviewed_by=row.reviewed_by,
        reviewer_name=reviewer.display_name if reviewer else None,
        reviewer_signature=reviewer.signature if reviewer else "",
        reviewer_student_id=reviewer.student_id if reviewer else None,
        created_at=row.created_at,
        reviewed_at=row.reviewed_at,
    )
