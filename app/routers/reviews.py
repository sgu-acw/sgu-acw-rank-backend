"""审核工作台与成员资料修改申请接口。

路由：
- ``GET /auth/academics``：登录成员获取学院与专业候选表，用于资料修改表单。
- ``GET /auth/me/change-requests``：成员查看自己提交过的资料修改申请。
- ``POST /auth/me/change-requests``：成员提交改名/更改学籍资料申请。
- ``DELETE /auth/me/change-requests/{request_id}``：撤回自己的待审核申请。
- ``GET /admin/reviews``：管理员分页查询注册与资料修改审核单。
- ``POST /admin/reviews/{request_id}/approve``：管理员通过审核。
- ``POST /admin/reviews/{request_id}/reject``：管理员驳回审核。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_
from sqlmodel import Session, select

from .. import academic
from ..db import get_session
from ..deps import get_current_user, require_admin, require_origin_secret
from ..models import ReviewKind, ReviewRequest, ReviewStatus, User
from ..reviews import (
    approve_review_request,
    create_profile_change_review,
    reject_review_request,
    serialize_review,
    sync_pending_review_notification,
)
from ..schemas import (
    AcademicOptions,
    AdminReviewPage,
    CreateProfileChangeRequest,
    ReviewDecisionRequest,
    ReviewRequestPublic,
)

router = APIRouter(
    tags=["reviews"],
    dependencies=[Depends(require_origin_secret)],
)


def _load_reviewers(db: Session, rows: list[ReviewRequest]) -> dict[int, User]:
    reviewer_ids = {r.reviewed_by for r in rows if r.reviewed_by is not None}
    if not reviewer_ids:
        return {}
    return {
        u.id: u
        for u in db.exec(
            select(User).where(User.id.in_(reviewer_ids))  # type: ignore[attr-defined]
        ).all()
        if u.id is not None
    }


@router.get("/auth/academics", response_model=AcademicOptions)
def member_academic_options(
    _user: User = Depends(get_current_user),
) -> AcademicOptions:
    """登录成员获取学院与专业候选列表。"""
    colleges = sorted(set(academic.COLLEGES.values()))
    majors: dict[str, list[str]] = {name: [] for name in colleges}
    for (college_code, _), major in academic.MAJORS.items():
        college = academic.COLLEGES.get(college_code)
        if college and major not in majors[college]:
            majors[college].append(major)
    for names in majors.values():
        names.sort()
    return AcademicOptions(colleges=colleges, majors=majors)


@router.get("/auth/me/change-requests", response_model=list[ReviewRequestPublic])
def list_my_change_requests(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ReviewRequestPublic]:
    """查看当前用户最近的资料修改申请记录。"""
    rows = list(
        db.exec(
            select(ReviewRequest)
            .where(
                ReviewRequest.user_id == user.id,
                ReviewRequest.kind == ReviewKind.PROFILE_CHANGE,
            )
            .order_by(ReviewRequest.created_at.desc(), ReviewRequest.id.desc())  # type: ignore[attr-defined]
            .limit(20)
        ).all()
    )
    reviewers = _load_reviewers(db, rows)
    return [serialize_review(r, reviewers, user) for r in rows]


@router.post(
    "/auth/me/change-requests",
    response_model=ReviewRequestPublic,
    status_code=status.HTTP_201_CREATED,
)
def submit_my_change_request(
    payload: CreateProfileChangeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ReviewRequestPublic:
    """提交改名或更改学籍资料申请，等待管理员审核。"""
    row = create_profile_change_review(db, user, payload)
    db.commit()
    db.refresh(row)
    return serialize_review(row, {}, user)


@router.delete(
    "/auth/me/change-requests/{request_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def cancel_my_change_request(
    request_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> Response:
    """撤回自己尚未审核的资料修改申请。"""
    row = db.get(ReviewRequest, request_id)
    if row is None or row.user_id != user.id or row.kind != ReviewKind.PROFILE_CHANGE:
        raise HTTPException(status_code=404, detail="申请不存在")
    if row.status != ReviewStatus.PENDING:
        raise HTTPException(status_code=409, detail="该申请已处理，无法撤回")

    row.status = ReviewStatus.CANCELLED
    db.add(row)
    db.flush()
    sync_pending_review_notification(db)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/admin/reviews", response_model=AdminReviewPage)
def list_admin_reviews(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
    status_filter: str = Query(default="pending", alias="status"),
    kind: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> AdminReviewPage:
    """管理员分页查询所有注册审核与资料修改审核单。"""
    statement = select(ReviewRequest)

    if status_filter and status_filter != "all":
        if status_filter not in ReviewStatus.ALL:
            raise HTTPException(status_code=400, detail="未知的审核状态")
        statement = statement.where(ReviewRequest.status == status_filter)

    if kind:
        if kind not in ReviewKind.ALL:
            raise HTTPException(status_code=400, detail="未知的审核类型")
        statement = statement.where(ReviewRequest.kind == kind)

    if q and q.strip():
        needle = q.strip()
        statement = statement.where(
            or_(
                ReviewRequest.student_id.contains(needle),  # type: ignore[union-attr]
                ReviewRequest.current_display_name.contains(needle),  # type: ignore[union-attr]
                ReviewRequest.after_json.contains(needle),  # type: ignore[union-attr]
                ReviewRequest.before_json.contains(needle),  # type: ignore[union-attr]
            )
        )

    total = int(db.exec(select(func.count()).select_from(statement.subquery())).one())

    pending_reg = int(
        db.exec(
            select(func.count())
            .select_from(ReviewRequest)
            .where(
                ReviewRequest.status == ReviewStatus.PENDING,
                ReviewRequest.kind == ReviewKind.REGISTRATION,
            )
        ).one()
    )
    pending_prof = int(
        db.exec(
            select(func.count())
            .select_from(ReviewRequest)
            .where(
                ReviewRequest.status == ReviewStatus.PENDING,
                ReviewRequest.kind == ReviewKind.PROFILE_CHANGE,
            )
        ).one()
    )

    # 待审核按先进先出排序方便依次审批；历史记录按最新倒序。
    if status_filter == ReviewStatus.PENDING:
        statement = statement.order_by(ReviewRequest.created_at.asc(), ReviewRequest.id.asc())  # type: ignore[attr-defined]
    else:
        statement = statement.order_by(ReviewRequest.created_at.desc(), ReviewRequest.id.desc())  # type: ignore[attr-defined]

    rows = list(db.exec(statement.offset((page - 1) * page_size).limit(page_size)).all())
    reviewers = _load_reviewers(db, rows)

    applicant_ids = {row.user_id for row in rows if row.user_id is not None}
    applicants = (
        {
            user.id: user
            for user in db.exec(
                select(User).where(User.id.in_(applicant_ids))  # type: ignore[attr-defined]
            )
        }
        if applicant_ids
        else {}
    )
    return AdminReviewPage(
        items=[serialize_review(r, reviewers, applicants.get(r.user_id)) for r in rows],
        total=total,
        pending_count=pending_reg + pending_prof,
        pending_registration_count=pending_reg,
        pending_profile_change_count=pending_prof,
        page=page,
        page_size=page_size,
    )


@router.post("/admin/reviews/{request_id}/approve", response_model=ReviewRequestPublic)
def approve_review(
    request_id: int,
    payload: ReviewDecisionRequest | None = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> ReviewRequestPublic:
    """通过审核单并生效变更。"""
    note = payload.review_note if payload is not None else ""
    row = approve_review_request(db, request_id, admin, review_note=note)
    db.commit()
    db.refresh(row)
    reviewers = {admin.id: admin} if admin.id is not None else {}
    return serialize_review(row, reviewers, db.get(User, row.user_id) if row.user_id else None)


@router.post("/admin/reviews/{request_id}/reject", response_model=ReviewRequestPublic)
def reject_review(
    request_id: int,
    payload: ReviewDecisionRequest | None = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> ReviewRequestPublic:
    """驳回审核单。"""
    note = payload.review_note if payload is not None else ""
    row = reject_review_request(db, request_id, admin, review_note=note)
    db.commit()
    db.refresh(row)
    reviewers = {admin.id: admin} if admin.id is not None else {}
    return serialize_review(row, reviewers, db.get(User, row.user_id) if row.user_id else None)
