"""平台账号的绑定与验证。

流程是「先声明、再证明」：

1. 成员提交平台与标识（handle 或 uid），后端生成一个 ``ACW-xxxx`` 验证码
   并记下有效期，此时账号处于未验证状态。
2. 成员把这个码填到自己在该平台的签名里（CF 的 organization、AtCoder 的
   Affiliation、洛谷的 slogan、牛客的 coder-brief）。
3. 成员点验证，后端去读那份签名，看到码就算通过。

这样证明的是「这个人控制那个账号」，而不是「这个人是工作坊的人」——后者
需要学校配合，前者正是绑定时真正需要确认的事。

校验用「包含」而非「相等」，因为成员的签名里往往本来就有别的内容。
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..cleanup import delete_account_data
from ..config import get_settings
from ..db import get_session
from ..deps import require_campus_member, require_origin_secret
from ..models import Account, Platform, User, utcnow
from ..platforms import FetchError, check_signature, fetch_profile
from ..schemas import (
    AccountCreate,
    AccountPublic,
    AccountVerifyResult,
    UserPublic,
)
from ..security import new_verify_token

router = APIRouter(
    prefix="/accounts",
    tags=["accounts"],
    dependencies=[Depends(require_origin_secret)],
)

#: 验证码的有效期。够成员去平台改一次签名，又不至于长期挂着。
VERIFY_TTL_HOURS = 24


def _own_account(db: Session, user: User, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None or account.user_id != user.id:
        # 不区分「不存在」和「不是你的」，避免探测别人有哪些账号。
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


@router.get("", response_model=list[AccountPublic])
def list_my_accounts(
    user: User = Depends(require_campus_member),
    db: Session = Depends(get_session),
) -> list[Account]:
    statement = (
        select(Account)
        .where(Account.user_id == user.id)
        .order_by(Account.platform, Account.created_at)  # type: ignore[attr-defined]
    )
    return list(db.exec(statement).all())


@router.post("", response_model=AccountPublic, status_code=status.HTTP_201_CREATED)
def add_account(
    payload: AccountCreate,
    user: User = Depends(require_campus_member),
    db: Session = Depends(get_session),
) -> Account:
    """声明一个平台账号，并返回待填入的验证码。

    此时只确认了「这个标识在平台上存在」，归属尚未证明，所以
    ``verified_at`` 仍为空。
    """
    if payload.platform not in Platform.ALL:
        raise HTTPException(status_code=400, detail="不支持的平台")

    identifier = payload.identifier.strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="请填写账号标识")

    # 先确认账号真实存在，避免把拼错的名字存进库。洛谷接受用户名，会顺手
    # 换成 uid。
    #
    # 必须先抓取再查重：成员输入的是用户名，而库里存的是 uid，拿原始输入
    # 去比对会漏掉，等到插入时才被唯一约束拦住，那时只能回一个 500。
    try:
        profile = fetch_profile(payload.platform, identifier)
    except FetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 唯一约束是 (platform, identifier)：同一平台同一账号只能属于一个人。
    # 若旧记录未验证且验证码已过期，视为放弃认领，允许新成员接管。
    now = utcnow()
    existing = db.exec(
        select(Account).where(
            Account.platform == payload.platform,
            Account.identifier == profile.identifier,
        )
    ).first()
    if existing is not None:
        if existing.user_id == user.id:
            raise HTTPException(status_code=409, detail="你已经绑过这个账号了")
        expired_unverified = (
            existing.verified_at is None
            and existing.verify_expires_at is not None
            and existing.verify_expires_at <= now
        )
        if not expired_unverified:
            raise HTTPException(status_code=409, detail="这个账号已被其他成员绑定")
        delete_account_data(db, existing.id)
        db.delete(existing)
        db.flush()

    account = Account(
        user_id=user.id,
        platform=payload.platform,
        identifier=profile.identifier,
        verify_token=new_verify_token(),
        verify_expires_at=now + timedelta(hours=VERIFY_TTL_HOURS),
        created_at=now,
    )
    db.add(account)
    try:
        db.commit()
    except IntegrityError as exc:
        # 两个成员同时绑同一个账号时，上面的查重会双双通过，由唯一约束兜底。
        db.rollback()
        raise HTTPException(status_code=409, detail="这个账号已被其他成员绑定") from exc
    db.refresh(account)
    return account


@router.post("/{account_id}/verify", response_model=AccountVerifyResult)
def verify_account(
    account_id: int,
    user: User = Depends(require_campus_member),
    db: Session = Depends(get_session),
) -> AccountVerifyResult:
    """读平台签名，确认验证码在里面。

    带防抖：同一个账号在 ``verify_cooldown_seconds`` 内只能验一次。先通过
    SQLite 写锁串行化冷却判断并提交，释放锁之后再发起外部平台请求，既防止
    并发绕过冷却，又不把数据库锁拖到外部网络调用期间。
    """
    # 先用条件更新获取 SQLite 写锁再重读状态，串行化同一账号的并发验证。
    locked = db.exec(
        update(Account)
        .where(Account.id == account_id, Account.user_id == user.id)
        .values(identifier=Account.identifier)
    )
    if locked.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=404, detail="账号不存在")
    account = db.get(Account, account_id, populate_existing=True)
    if account is None or account.user_id != user.id:
        db.rollback()
        raise HTTPException(status_code=404, detail="账号不存在")

    if account.verified_at is not None:
        db.rollback()
        return AccountVerifyResult(ok=True, detail="这个账号已经验证过了", account=account)

    if not account.verify_token:
        db.rollback()
        raise HTTPException(status_code=400, detail="请先重新生成验证码")
    now = utcnow()
    if account.verify_expires_at and now >= account.verify_expires_at:
        db.rollback()
        raise HTTPException(status_code=400, detail="验证码已过期，请重新生成")

    settings = get_settings()
    if account.verify_attempted_at is not None:
        cooldown = account.verify_cooldown(
            base=settings.verify_cooldown_seconds,
            cap=settings.verify_cooldown_max_seconds,
        )
        elapsed = (now - account.verify_attempted_at).total_seconds()
        if elapsed < cooldown:
            db.rollback()
            wait = int(cooldown - elapsed) + 1
            raise HTTPException(
                status_code=429,
                detail=f"操作太快，请等 {wait} 秒后再试",
                # 标准的 429 响应头。前端的倒计时读它，就不用去解析中文提示。
                headers={"Retry-After": str(wait)},
            )

    verify_token = account.verify_token
    platform = account.platform
    identifier = account.identifier
    account.verify_attempted_at = now
    account.verify_attempts += 1
    db.add(account)
    db.commit()

    ok, detail = check_signature(platform, identifier, verify_token)
    if not ok:
        db.refresh(account)
        return AccountVerifyResult(ok=False, detail=detail, account=account)

    # 重新加锁落库验证结果，防止验证期间账号已被解绑。
    final = db.exec(
        update(Account)
        .where(Account.id == account_id, Account.user_id == user.id)
        .values(identifier=Account.identifier)
    )
    if final.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=404, detail="账号不存在")
    account = db.get(Account, account_id, populate_existing=True)
    if account is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="账号不存在")
    account.verified_at = utcnow()
    # 验证完就把码清掉，签名里那一行可以删了。
    account.verify_token = None
    account.verify_expires_at = None
    # 成功才清零，所以换个新验证码不会把冷却重新计时。
    account.verify_attempts = 0
    db.add(account)
    db.commit()
    db.refresh(account)

    return AccountVerifyResult(ok=True, detail="验证通过", account=account)


@router.post("/{account_id}/renew", response_model=AccountPublic)
def renew_token(
    account_id: int,
    user: User = Depends(require_campus_member),
    db: Session = Depends(get_session),
) -> Account:
    """换一个新的验证码。用于码过期或填错后重来。"""
    account = _own_account(db, user, account_id)
    if account.verified_at is not None:
        raise HTTPException(status_code=400, detail="这个账号已经验证过了")

    account.verify_token = new_verify_token()
    account.verify_expires_at = utcnow() + timedelta(hours=VERIFY_TTL_HOURS)
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_account(
    account_id: int,
    user: User = Depends(require_campus_member),
    db: Session = Depends(get_session),
) -> None:
    """解绑账号。

    连带删掉它的练习记录与快照。只删账号的话，daily_progress 里会留下一堆
    指向不存在账号的行：榜单查询会因为 join 不到而忽略它们，看上去没事，
    但表会随解绑次数一直变大，而且重新绑同一账号时旧数据会意外地复活。
    """
    account = _own_account(db, user, account_id)

    delete_account_data(db, account.id)
    db.delete(account)
    db.commit()


# --------------------------------------------------------------------------- #
# 给其他成员看的视角
# --------------------------------------------------------------------------- #


@router.get("/of/{user_id}", response_model=list[AccountPublic])
def accounts_of(
    user_id: int,
    _current: User = Depends(require_campus_member),
    db: Session = Depends(get_session),
) -> list[Account]:
    """看某个成员的账号。只返回已验证的，未验证的属于半成品状态。

    登录才能看，与成员目录一致：榜单页需要展示这些 handle。
    """
    statement = (
        select(Account)
        .where(Account.user_id == user_id, Account.verified_at.is_not(None))  # type: ignore[union-attr]
        .order_by(Account.platform)  # type: ignore[attr-defined]
    )
    return list(db.exec(statement).all())


__all__ = ["router", "UserPublic"]
