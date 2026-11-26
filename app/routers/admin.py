"""Administrative endpoints: invites, member management, the admin-only note.

Reaching any route here requires the ``admin`` or ``root`` role. Root-only
routes are marked as such; they cover operations that must not spread sideways,
like promoting someone to root or touching a root account.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from .. import academic, editorials
from ..cleanup import delete_user_accounts, delete_user_references
from ..db import get_session
from ..deps import require_admin, require_origin_secret
from ..models import (
    Account,
    DailyProblem,
    DailyProgress,
    Editorial,
    EditorialStatus,
    Invite,
    ProblemCache,
    PushSubscription,
    ReviewRequest,
    ReviewStatus,
    Role,
    SolvedSnapshot,
    User,
    UserSession,
    UserStatus,
    utcnow,
)
from ..reviews import (
    apply_display_name_change,
    cancel_pending_reviews_for_user,
    complete_registration_reviews_for_user,
)
from ..schemas import (
    AcademicOptions,
    AdminAccountBrief,
    AdminAccountDetail,
    AdminCreateUserRequest,
    AdminMemberDetail,
    AdminUserPage,
    InviteCreate,
    InvitePublic,
    UpdateUserAdminRequest,
    UserAdmin,
)
from ..security import hash_password, new_invite_code, new_temporary_password
from ..student_id import StudentIdError, parse_external_username, parse_student_id

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_origin_secret), Depends(require_admin)],
)


# --------------------------------------------------------------------------- #
# Invites
# --------------------------------------------------------------------------- #


@router.get("/invites", response_model=list[InvitePublic])
def list_invites(
    include_dead: bool = Query(default=True, description="是否包含已失效的邀请码"),
    db: Session = Depends(get_session),
) -> list[InvitePublic]:
    statement = select(Invite).order_by(Invite.created_at.desc())  # type: ignore[attr-defined]
    rows = list(db.exec(statement).all())
    if not include_dead:
        rows = [row for row in rows if row.is_usable]

    # 一次查出所有创建者，而不是每个邀请码查一次。
    creator_ids = {row.created_by for row in rows if row.created_by is not None}
    creators: dict[int, User] = {}
    if creator_ids:
        found = db.exec(select(User).where(User.id.in_(creator_ids))).all()  # type: ignore[attr-defined]
        creators = {user.id: user for user in found}

    out: list[InvitePublic] = []
    for row in rows:
        item = InvitePublic.model_validate(row)
        creator = creators.get(row.created_by) if row.created_by else None
        if creator is not None:
            item.created_by_name = creator.display_name
            item.created_by_signature = creator.signature
            item.created_by_student_id = creator.student_id
        out.append(item)
    return out


@router.post("/invites", response_model=InvitePublic, status_code=status.HTTP_201_CREATED)
def create_invite(
    payload: InviteCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> Invite:
    """Mint an invite code.

    管理员之间是平级的：任何管理员都能发管理员邀请码。root 是唯一的例外，
    不能通过邀请码授予——root 只有一个，要转让得走别的路。
    """
    if payload.grants_role == Role.ROOT:
        raise HTTPException(status_code=403, detail="不能通过邀请码授予 root")

    # Retry on the vanishingly unlikely collision rather than trusting luck.
    for _ in range(5):
        code = new_invite_code()
        if db.exec(select(Invite).where(Invite.code == code)).first() is None:
            break
    else:  # pragma: no cover - would need a broken RNG
        raise HTTPException(status_code=500, detail="无法生成邀请码")

    invite = Invite(
        code=code,
        grants_role=payload.grants_role,
        max_uses=payload.max_uses,
        use_count=0,
        expires_at=utcnow() + timedelta(hours=payload.ttl_hours),
        note=payload.note,
        created_by=admin.id,
        created_at=utcnow(),
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)

    # 建完立刻返回，前端要直接插进列表里。创建者信息一并带上，否则新出现的
    # 那一行跟其他行长得不一样，得刷新才有。
    item = InvitePublic.model_validate(invite)
    item.created_by_name = admin.display_name
    item.created_by_signature = admin.signature
    item.created_by_student_id = admin.student_id
    return item


@router.delete("/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invite(
    invite_id: int,
    db: Session = Depends(get_session),
) -> Response:
    """Revoke an invite without deleting it, so the audit trail survives."""
    invite = db.get(Invite, invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    if invite.revoked_at is None:
        invite.revoked_at = utcnow()
        db.add(invite)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #


@router.get("/users", response_model=AdminUserPage)
def list_users(
    db: Session = Depends(get_session),
    q: str | None = Query(default=None, description="按学号或姓名模糊搜索"),
    role: str | None = Query(default=None),
    status: str | None = Query(default=None),
    enroll_year: int | None = Query(default=None),
    college_code: str | None = Query(default=None),
    has_account: bool | None = Query(default=None, description="是否已绑定平台账号"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    sort: str = Query(
        default="enroll_year", description="enroll_year|student_id|display_name|created_at"
    ),
) -> AdminUserPage:
    """成员列表，支持搜索、筛选、排序与分页。

    过滤都在数据库里做，不是取全部再在 Python 里筛——成员多起来之后
    后者会随人数线性变慢。
    """
    statement = select(User)

    if q and q.strip():
        # 学号按前缀匹配，姓名和曾用名按包含匹配：学号输一半或查旧名字都能搜到。
        needle = q.strip()
        statement = statement.where(
            or_(
                User.student_id.startswith(needle),  # type: ignore[union-attr]
                User.display_name.contains(needle),  # type: ignore[union-attr]
                User.former_names_json.contains(needle),  # type: ignore[union-attr]
            )
        )

    if role:
        if role not in Role.ALL:
            raise HTTPException(status_code=400, detail="未知的角色")
        statement = statement.where(User.role == role)

    if status:
        statement = statement.where(User.status == status)

    if enroll_year is not None:
        statement = statement.where(User.enroll_year == enroll_year)

    if college_code:
        statement = statement.where(User.college_code == college_code)

    if has_account is not None:
        # 用子查询而不是 join：一个人绑了多个账号时 join 会出现重复行。
        bound = select(Account.user_id).where(Account.verified_at.is_not(None))  # type: ignore[union-attr]
        if has_account:
            statement = statement.where(User.id.in_(bound))  # type: ignore[attr-defined]
        else:
            statement = statement.where(User.id.not_in(bound))  # type: ignore[attr-defined]

    # 总数要在分页之前算，但只取 COUNT，不能把所有匹配成员加载到内存。
    total = db.exec(select(func.count()).select_from(statement.subquery())).one()

    sort_columns = {
        "enroll_year": (User.enroll_year.desc(), User.student_id),  # type: ignore[union-attr]
        "student_id": (User.student_id,),
        "display_name": (User.display_name,),
        "created_at": (User.created_at.desc(),),  # type: ignore[union-attr]
    }
    order_by = sort_columns.get(sort, sort_columns["enroll_year"])
    statement = statement.order_by(*order_by).offset((page - 1) * page_size).limit(page_size)

    items = list(db.exec(statement).all())

    # 供前端渲染筛选下拉，免得选项写死在两处。
    years = sorted(
        {y for y in db.exec(select(User.enroll_year).distinct()).all() if y},  # type: ignore[attr-defined]
        reverse=True,
    )
    colleges = sorted({c for c in db.exec(select(User.college_code).distinct()).all() if c})  # type: ignore[attr-defined]

    # 一次把这一页所有成员的账号查出来，避免逐个查库（N+1）。
    # 只取列表要显示的几个字段，不拉详情页那一整套。
    accounts_by_user: dict[int, list[Account]] = {}
    if items:
        user_ids = [u.id for u in items if u.id]
        for account in db.exec(
            select(Account)
            .where(Account.user_id.in_(user_ids))  # type: ignore[attr-defined]
            .order_by(Account.platform, Account.identifier)  # type: ignore[attr-defined]
        ).all():
            accounts_by_user.setdefault(account.user_id, []).append(account)

    def to_admin(u: User) -> UserAdmin:
        # 用 from_attributes 逐字段构造，而不是 model_validate(u)：
        # User 上有 accounts 这个 relationship，直接验证的话会把 Account
        # 对象原样塞进 accounts 字段，类型对不上直接 500。
        view = UserAdmin.model_validate(
            {k: getattr(u, k) for k in UserAdmin.model_fields if k != "accounts"}
        )
        view.accounts = [
            AdminAccountBrief(
                id=a.id or 0,
                platform=a.platform,
                identifier=a.identifier,
                display_name=a.display_name,
                verified_at=a.verified_at,
            )
            for a in accounts_by_user.get(u.id or 0, [])
        ]
        return view

    return AdminUserPage(
        items=[to_admin(u) for u in items],
        total=total,
        page=page,
        page_size=page_size,
        enroll_years=years,
        college_codes=colleges,
    )


@router.post("/users", response_model=UserAdmin, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: AdminCreateUserRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> User:
    """管理员直接建号，不用邀请码。

    权限规则与邀请码一致：不能造 root，管理员之间平级。
    """
    if payload.role == Role.ROOT:
        raise HTTPException(status_code=403, detail="不能直接创建 root 账号")

    if payload.role == Role.EXTERNAL:
        try:
            username = parse_external_username(payload.student_id)
        except StudentIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if db.exec(select(User).where(User.student_id == username)).first() is not None:
            raise HTTPException(status_code=409, detail="该用户名已注册")
        user = User(
            student_id=username,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name,
            enroll_year=0,
            level=0,
            college_code="",
            major_code="",
            class_no=0,
            role=Role.EXTERNAL,
            status=UserStatus.ACTIVE,
            admin_note=payload.admin_note,
            created_at=utcnow(),
        )
    else:
        try:
            parsed = parse_student_id(payload.student_id)
        except StudentIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if db.exec(select(User).where(User.student_id == parsed.raw)).first() is not None:
            raise HTTPException(status_code=409, detail="该学号已注册")

        user = User(
            student_id=parsed.raw,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name,
            enroll_year=parsed.enroll_year,
            level=parsed.level,
            college_code=parsed.college_code,
            major_code=parsed.major_code,
            class_no=parsed.class_no,
            role=payload.role,
            status=UserStatus.ACTIVE,
            admin_note=payload.admin_note,
            created_at=utcnow(),
        )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该学号已注册") from exc
    db.refresh(user)
    return user


@router.get("/users/{user_id}", response_model=UserAdmin)
def get_user(user_id: int, db: Session = Depends(get_session)) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    return user


@router.patch("/users/{user_id}", response_model=UserAdmin)
def update_user(
    user_id: int,
    payload: UpdateUserAdminRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> User:
    """Edit a member's role, status, or the admin-only note.

    ``admin_note`` is writable here and readable only from admin endpoints;
    the member-facing schemas do not carry the field at all.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="成员不存在")

    if payload.role is not None and payload.role != user.role:
        if user.is_root:
            raise HTTPException(status_code=403, detail="不能修改 root 的角色")
        if user.is_external or payload.role == Role.EXTERNAL:
            raise HTTPException(status_code=400, detail="校外账号与校内成员身份不能直接互转")
        if user.id == admin.id and payload.role not in Role.ADMINISTRATIVE:
            raise HTTPException(status_code=400, detail="不能撤销自己的管理员权限")
        if payload.role == Role.ROOT and not admin.is_root:
            raise HTTPException(status_code=403, detail="只有 root 可以授予 root")
        # 管理员之间平级：任何管理员都能授予或撤销管理员。
        user.role = payload.role

    if payload.status is not None and payload.status != user.status:
        if user.is_root:
            raise HTTPException(status_code=403, detail="不能停用 root")
        if user.id == admin.id and payload.status == UserStatus.DISABLED:
            raise HTTPException(status_code=400, detail="不能停用自己")
        was_pending = user.status == UserStatus.PENDING
        user.status = payload.status
        if payload.status == UserStatus.DISABLED:
            # A disabled account should not keep working sessions around.
            for row in db.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
                db.delete(row)
        elif was_pending and payload.status == UserStatus.ACTIVE:
            complete_registration_reviews_for_user(db, user, admin)

    if payload.admin_note is not None:
        user.admin_note = payload.admin_note

    if payload.display_name is not None:
        apply_display_name_change(user, payload.display_name)

    if payload.can_post is not None:
        if user.is_external and payload.can_post:
            raise HTTPException(status_code=400, detail="校外账号不能授予发文章权限")
        user.can_post = payload.can_post

    # 空串表示恢复成按学号查表。
    if payload.college_override is not None:
        user.college_override = payload.college_override.strip() or None
    if payload.major_override is not None:
        user.major_override = payload.major_override.strip() or None

    if payload.class_no is not None:
        user.class_no = payload.class_no

    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> Response:
    """删除成员，连同其会话与平台账号。

    只能删成员：管理员和 root 要先降级再删，避免一次误操作把管理员清空。
    也不能删自己，否则会当场把自己踢出去。
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    if user.is_root:
        raise HTTPException(status_code=403, detail="不能删除 root 账号")
    # 管理员之间平级，所以能互相删除。

    # 会话、账号、以及指向该成员的其他记录都要一起清掉。漏掉任何一张带
    # 外键的表，删除都会被数据库拦下。
    cancel_pending_reviews_for_user(db, user, admin)
    for row in db.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
        db.delete(row)
    delete_user_accounts(db, user.id)
    delete_user_references(db, user.id, reassign_to_user_id=admin.id)
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> dict:
    """Reset a member's password to a random value and evict their sessions.

    There is no self-service recovery: accounts are keyed on a student id with
    no email attached, so a forgotten password means asking an administrator.
    The new password is returned exactly once, in this response, and is not
    recoverable afterwards.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    if user.is_root and not admin.is_root:
        raise HTTPException(status_code=403, detail="只有 root 可以重置 root 的密码")

    temporary = new_temporary_password()
    user.password_hash = hash_password(temporary)
    db.add(user)
    for row in db.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
        db.delete(row)
    db.commit()

    return {
        "student_id": user.student_id,
        "display_name": user.display_name,
        "temporary_password": temporary,
    }


@router.get("/academics", response_model=AcademicOptions)
def academic_options() -> AcademicOptions:
    """学院与专业的候选列表，供管理员改学院/专业时选。

    数据是静态的（学号码表），不用查库。
    """
    colleges = sorted(set(academic.COLLEGES.values()))
    majors: dict[str, list[str]] = {name: [] for name in colleges}
    for (college_code, _), major in academic.MAJORS.items():
        college = academic.COLLEGES.get(college_code)
        if college and major not in majors[college]:
            majors[college].append(major)
    for names in majors.values():
        names.sort()
    return AcademicOptions(colleges=colleges, majors=majors)


@router.get("/stats")
def stats(db: Session = Depends(get_session)) -> dict:
    """Counts for the admin dashboard."""
    total = db.exec(select(func.count()).select_from(User)).one()
    active = db.exec(
        select(func.count()).select_from(User).where(User.status == UserStatus.ACTIVE)
    ).one()
    admins = db.exec(
        select(func.count()).select_from(User).where(User.role.in_(Role.ADMINISTRATIVE))  # type: ignore[attr-defined]
    ).one()
    externals = db.exec(
        select(func.count()).select_from(User).where(User.role == Role.EXTERNAL)
    ).one()
    pending_users = db.exec(
        select(func.count()).select_from(User).where(User.status == UserStatus.PENDING)
    ).one()
    pending_reviews = db.exec(
        select(func.count())
        .select_from(ReviewRequest)
        .where(ReviewRequest.status == ReviewStatus.PENDING)
    ).one()
    now = utcnow()
    live_invites = db.exec(
        select(func.count())
        .select_from(Invite)
        .where(
            Invite.revoked_at.is_(None),  # type: ignore[union-attr]
            Invite.expires_at > now,
            Invite.use_count < Invite.max_uses,
        )
    ).one()
    # 开了推送的成员数：按 user_id 去重（一人多设备只算一次）。
    push_users = db.exec(select(func.count(func.distinct(PushSubscription.user_id)))).one()
    push_devices = db.exec(select(func.count()).select_from(PushSubscription)).one()
    return {
        "user_count": int(total),
        "active_count": int(active),
        "admin_count": int(admins),
        "external_count": int(externals),
        "pending_user_count": int(pending_users),
        "pending_review_count": int(pending_reviews),
        "usable_invite_count": int(live_invites),
        "push_subscriber_count": int(push_users),
        "push_device_count": int(push_devices),
    }


@router.get("/members/{user_id}", response_model=AdminMemberDetail)
def get_member_detail(
    user_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> AdminMemberDetail:
    """成员详情的管理员视角。

    比公开信息多出一块排查用的数据：仅管理员可见的备注、最后登录时间、
    每个平台账号的同步健康度（抓取时间、失败次数、最近错误）。

    这些不是「更多信息」，而是回答一个具体问题用的：**这个人为什么没数据**。
    常见原因是账号验证没过、平台抓取一直失败、或者人本来就不做题了——
    这三者从公开信息里看不出来。
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="成员不存在")

    import json as _json

    from ..sync import current_practice_day

    accounts = list(db.exec(select(Account).where(Account.user_id == user_id)).all())
    account_ids = [a.id for a in accounts if a.id is not None]

    # 已过题目数与练习天数，一次查完，别在循环里逐个查。
    solved_by_account: dict[int, int] = {}
    if account_ids:
        for snapshot in db.exec(
            select(SolvedSnapshot).where(
                SolvedSnapshot.account_id.in_(account_ids)  # type: ignore[attr-defined]
            )
        ).all():
            try:
                solved_by_account[snapshot.account_id] = len(
                    _json.loads(snapshot.problems_json or "[]")
                )
            except (TypeError, ValueError):
                solved_by_account[snapshot.account_id] = snapshot.problem_count

    active_days_by_account: dict[int, int] = {}
    if account_ids:
        rows = db.exec(
            select(DailyProgress.account_id, func.count())  # type: ignore[call-overload]
            .where(
                DailyProgress.account_id.in_(account_ids),  # type: ignore[attr-defined]
                DailyProgress.solved_count > 0,
            )
            .group_by(DailyProgress.account_id)  # type: ignore[arg-type]
        ).all()
        for account_id, count in rows:
            active_days_by_account[account_id] = count

    details = [
        AdminAccountDetail(
            id=a.id or 0,
            platform=a.platform,
            identifier=a.identifier,
            display_name=a.display_name,
            verified_at=a.verified_at,
            verify_attempts=a.verify_attempts,
            verify_attempted_at=a.verify_attempted_at,
            rating=a.rating,
            max_rating=a.max_rating,
            rating_fetched_at=a.rating_fetched_at,
            solved_fetched_at=a.solved_fetched_at,
            last_active_at=a.last_active_at,
            last_error=a.last_error,
            consecutive_failures=a.consecutive_failures,
            rating_failures=a.rating_failures,
            sync_flag=a.sync_flag,
            created_at=a.created_at,
            solved_count=solved_by_account.get(a.id or 0, 0),
            active_days=active_days_by_account.get(a.id or 0, 0),
        )
        for a in accounts
    ]

    # 最近 30 天的活跃天数：按「人」算，同一天多个账号只算一天。
    today = date.fromisoformat(current_practice_day())
    cutoff = (today.fromordinal(today.toordinal() - 30)).isoformat()
    recent_days: set[str] = set()
    last_date: str | None = None
    if account_ids:
        for row in db.exec(
            select(DailyProgress).where(
                DailyProgress.account_id.in_(account_ids),  # type: ignore[attr-defined]
                DailyProgress.solved_count > 0,
            )
        ).all():
            if row.date >= cutoff:
                recent_days.add(row.date)
            if last_date is None or row.date > last_date:
                last_date = row.date

    return AdminMemberDetail(
        id=user.id or 0,
        display_name=user.display_name,
        signature=user.signature,
        student_id=user.student_id,
        enroll_year=user.enroll_year,
        level=user.level,
        college_code=user.college_code,
        major_code=user.major_code,
        college_name=user.college_name,
        major_name=user.major_name,
        class_label=user.class_label,
        role=user.role,
        status=user.status,
        admin_note=user.admin_note,
        former_names=user.former_names,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        accounts=details,
        total_solved=sum(d.solved_count for d in details),
        active_days_30=len(recent_days),
        last_practice_date=last_date,
        failing_accounts=sum(1 for d in details if d.consecutive_failures > 0),
    )


# --------------------------------------------------------------------------- #
# Editorials
# --------------------------------------------------------------------------- #


class AdminEditorial(BaseModel):
    """管理员看到的题解。比成员多几个字段：状态、模型、失败原因。"""

    id: int
    daily_problem_id: int
    day: str
    platform: str
    key: str
    name: str = ""
    rating: int | None = None

    status: str
    error: str = ""
    model: str = ""

    hints: str = ""
    body: str = ""
    code: dict[str, str] = Field(default_factory=dict)

    generated_at: datetime | None = None
    published_at: datetime | None = None

    #: 算出来的公布时刻，前端用来显示「几点公布」。
    publish_at: datetime


class AdminEditorialPage(BaseModel):
    items: list[AdminEditorial] = Field(default_factory=list)
    total: int = 0


class EditorialPatch(BaseModel):
    """改题解。只传要改的字段。"""

    hints: str | None = None
    body: str | None = None
    code: dict[str, str] | None = None


def _admin_editorial(row: Editorial, name: str, rating: int | None) -> AdminEditorial:
    return AdminEditorial(
        id=row.id or 0,
        daily_problem_id=row.daily_problem_id,
        day=row.day,
        platform=row.platform,
        key=row.key,
        name=name,
        rating=rating,
        status=row.status,
        error=row.error,
        model=row.model,
        hints=row.hints,
        body=row.body,
        code={
            "cpp20": row.code_cpp20,
            "py3": row.code_py3,
            "java17": row.code_java17,
        },
        generated_at=row.generated_at,
        published_at=row.published_at,
        publish_at=editorials.publish_at_for(row.day),
    )


@router.get("/editorials", response_model=AdminEditorialPage)
def list_editorials(
    day: str | None = Query(default=None, description="按日期筛，不传则全部"),
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, description="题号或题名关键词"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_session),
) -> AdminEditorialPage:
    """题解列表。

    不按日期筛时返回全部，从新到旧。管理员要能一眼看到哪些没生成、
    哪些生成失败了。
    """
    statement = select(Editorial)
    if day:
        statement = statement.where(Editorial.day == day)
    if status_filter:
        statement = statement.where(Editorial.status == status_filter)
    if q:
        like = f"%{q}%"
        matched_keys = select(ProblemCache.key).where(
            ProblemCache.platform == "codeforces",  # type: ignore[arg-type]
            ProblemCache.name.ilike(like),  # type: ignore[attr-defined]
        )
        statement = statement.where(
            or_(
                Editorial.key.ilike(like),  # type: ignore[attr-defined]
                Editorial.day.ilike(like),  # type: ignore[attr-defined]
                Editorial.key.in_(matched_keys),  # type: ignore[attr-defined]
            )
        )

    total = int(db.exec(select(func.count()).select_from(statement.subquery())).one())
    page = list(
        db.exec(
            statement.order_by(Editorial.day.desc(), Editorial.id.desc())  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        ).all()
    )

    # 一次把题目元信息查出来，避免逐条查库。
    keys = [row.key for row in page]
    cache: dict[str, ProblemCache] = {}
    if keys:
        for item in db.exec(
            select(ProblemCache).where(
                ProblemCache.platform == "codeforces",  # type: ignore[arg-type]
                ProblemCache.key.in_(keys),  # type: ignore[attr-defined]
            )
        ).all():
            cache[item.key] = item

    return AdminEditorialPage(
        items=[
            _admin_editorial(
                row,
                cache[row.key].name if row.key in cache else "",
                cache[row.key].rating if row.key in cache else None,
            )
            for row in page
        ],
        total=total,
    )


@router.patch("/editorials/{editorial_id}", response_model=AdminEditorial)
def update_editorial(
    editorial_id: int,
    payload: EditorialPatch,
    db: Session = Depends(get_session),
) -> AdminEditorial:
    """改题解的正文、hint 或代码。

    改完不重置公布状态——已经对成员可见的题解，改错字不该让它消失。
    校验规则和生成器走同一套（``editorials.upsert`` 里的那几个），
    否则管理员能存进生成器存不进的东西。
    """
    row = db.get(Editorial, editorial_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有这篇题解")

    if payload.body is not None:
        body = payload.body.strip()
        if len(body) < editorials.MIN_BODY_CHARS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"正文太短（{len(body)} 字符，至少 {editorials.MIN_BODY_CHARS}）",
            )
        if len(body) > editorials.MAX_BODY_CHARS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"正文太长（{len(body)} 字符，最多 {editorials.MAX_BODY_CHARS}）",
            )
        row.body = body

    if payload.hints is not None:
        row.hints = payload.hints.strip()

    if payload.code is not None:
        for lang in payload.code:
            if lang not in editorials.LANGUAGES:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"不支持的语言：{lang}",
                )
        for lang in editorials.LANGUAGES:
            source = payload.code.get(lang)
            if source is None:
                continue
            if len(source) > editorials.MAX_CODE_CHARS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{lang} 的代码太长（{len(source)} 字符）",
                )
            setattr(row, f"code_{lang}", source.strip())

    row.updated_at = utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)

    cache = db.exec(
        select(ProblemCache).where(
            ProblemCache.platform == "codeforces",  # type: ignore[arg-type]
            ProblemCache.key == row.key,
        )
    ).first()
    return _admin_editorial(row, cache.name if cache else "", cache.rating if cache else None)


@router.post("/editorials/{editorial_id}/publish", response_model=AdminEditorial)
def publish_editorial(
    editorial_id: int,
    db: Session = Depends(get_session),
) -> AdminEditorial:
    """立刻公布一篇题解，不等 20:00。"""
    row = db.get(Editorial, editorial_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有这篇题解")
    if row.status in (EditorialStatus.FAILED, EditorialStatus.PENDING) or not row.body.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="题解尚未生成或正文为空，不能公布"
        )

    now = utcnow()
    if row.published_at is None:
        row.published_at = now
    # 和 publish_due 保持一致：status 描述的就是「公布了没有」。
    row.status = EditorialStatus.PUBLISHED
    row.updated_at = now
    db.add(row)
    db.commit()
    db.refresh(row)

    cache = db.exec(
        select(ProblemCache).where(
            ProblemCache.platform == "codeforces",  # type: ignore[arg-type]
            ProblemCache.key == row.key,
        )
    ).first()
    return _admin_editorial(row, cache.name if cache else "", cache.rating if cache else None)


@router.post("/editorials/{editorial_id}/unpublish", response_model=AdminEditorial)
def unpublish_editorial(
    editorial_id: int,
    db: Session = Depends(get_session),
) -> AdminEditorial:
    """收回一篇题解，让成员看不到。"""
    row = db.get(Editorial, editorial_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有这篇题解")

    row.published_at = None
    # 收回后回到草稿。生成失败的不动——那不是「收回」，是没成功过。
    if row.status == EditorialStatus.PUBLISHED:
        row.status = EditorialStatus.DRAFT
    row.updated_at = utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)

    cache = db.exec(
        select(ProblemCache).where(
            ProblemCache.platform == "codeforces",  # type: ignore[arg-type]
            ProblemCache.key == row.key,
        )
    ).first()
    return _admin_editorial(row, cache.name if cache else "", cache.rating if cache else None)


@router.delete("/editorials/{editorial_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_editorial(
    editorial_id: int,
    db: Session = Depends(get_session),
) -> Response:
    """删掉一篇题解。

    删掉之后那道题会回到「还没生成」，下次生成任务会重写。
    """
    row = db.get(Editorial, editorial_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有这篇题解")
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class MissingEditorial(BaseModel):
    """还没生成题解的题。"""

    daily_problem_id: int
    day: str
    band: str
    key: str
    name: str = ""
    rating: int | None = None

    #: 是不是今天的题。
    #:
    #: 今天的题在生成任务跑之前本来就还没题解，那是正常的；往日的没有
    #: 才是真漏了。前端靠这个区分显示，不然管理员会把正常情况当成故障。
    is_today: bool = False


@router.get("/editorials/missing", response_model=list[MissingEditorial])
def list_missing_editorials(
    days: int = Query(default=30, ge=1, le=365, description="往前查几天"),
    db: Session = Depends(get_session),
) -> list[MissingEditorial]:
    """哪些题还没有题解，含今天。

    管理员靠这个知道生成任务漏了哪些。

    今天的题也列出来——不然管理员在生成之前完全看不到今天出了什么题。
    用 ``is_today`` 标出来让前端分开显示：今天的还没到生成时间是正常的，
    往日的没有才是真漏了。
    """
    from .. import daily as daily_mod

    today = daily_mod.workshop_today()
    cutoff = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    problems = db.exec(
        select(DailyProblem)
        .where(DailyProblem.day >= cutoff)  # type: ignore[operator]
        .order_by(DailyProblem.day.desc(), DailyProblem.id.desc())  # type: ignore[attr-defined]
    ).all()
    if not problems:
        return []

    have = {
        row.daily_problem_id
        for row in db.exec(select(Editorial)).all()
    }

    keys = [p.key for p in problems]
    cache: dict[str, ProblemCache] = {}
    for item in db.exec(
        select(ProblemCache).where(
            ProblemCache.platform == "codeforces",  # type: ignore[arg-type]
            ProblemCache.key.in_(keys),  # type: ignore[attr-defined]
        )
    ).all():
        cache[item.key] = item

    return [
        MissingEditorial(
            daily_problem_id=p.id or 0,
            day=p.day,
            band=p.band,
            key=p.key,
            name=cache[p.key].name if p.key in cache else "",
            rating=cache[p.key].rating if p.key in cache else None,
            is_today=p.day == today,
        )
        for p in problems
        if (p.id or 0) not in have
    ]
