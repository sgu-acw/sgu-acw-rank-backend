"""删除账号时的连带清理。

解绑一个平台账号时，除了账号本身，还有两类数据指向它：

* ``daily_progress``：每天过了几题。留着的话榜单会因 join 不到而忽略它们，
  看上去没事，但表会随解绑次数一直变大；而且重新绑同一账号时旧数据会意外
  复活，看起来像是「刚绑上就有几百题」。
* ``solved_snapshots``：已过题目的快照，用于洛谷的差分。

删除成员时同理，只是范围扩到他的所有账号。

放在这里而不是散在两处，是因为这两个清理路径必须保持一致——只改一处
就会留下孤儿数据，而且很难被发现。
"""

from __future__ import annotations

from sqlmodel import Session, select

from .honor_models import HonorPerson, HonorRecipient
from .learning_models import LearningRun
from .models import (
    Account,
    Camp,
    CampRegistration,
    DailyProblemSolve,
    DailyProgress,
    Invite,
    InviteUse,
    LoginTicket,
    NotificationRead,
    OAuthAccessToken,
    OAuthAuthCode,
    OAuthClient,
    PasskeyCredential,
    Post,
    ProblemList,
    PushDispatch,
    PushSubscription,
    RefreshQuota,
    ReviewRequest,
    Role,
    Signup,
    SignupEntry,
    SolvedSnapshot,
    User,
    WebAuthnChallenge,
    WorkshopStatusOverride,
)


def delete_account_data(db: Session, account_id: int) -> int:
    """删掉某个账号的练习记录、快照与每日一题过题引用。返回删掉的行数。"""
    removed = 0
    for row in db.exec(select(DailyProgress).where(DailyProgress.account_id == account_id)).all():
        db.delete(row)
        removed += 1
    for snapshot in db.exec(
        select(SolvedSnapshot).where(SolvedSnapshot.account_id == account_id)
    ).all():
        db.delete(snapshot)
        removed += 1
    for solve in db.exec(
        select(DailyProblemSolve).where(DailyProblemSolve.account_id == account_id)
    ).all():
        solve.account_id = None
        db.add(solve)
    db.flush()
    return removed


def delete_user_accounts(db: Session, user_id: int) -> int:
    """删掉某个成员的所有账号及其数据。返回删掉的账号数。"""
    accounts = list(db.exec(select(Account).where(Account.user_id == user_id)).all())
    for account in accounts:
        delete_account_data(db, account.id)
        db.delete(account)
    db.flush()
    return len(accounts)


def _fallback_owner_id(db: Session, excluded_user_id: int) -> int | None:
    root = db.exec(
        select(User.id).where(User.role == Role.ROOT, User.id != excluded_user_id)
    ).first()
    if root is not None:
        return root
    other_admin = db.exec(
        select(User.id).where(User.role == Role.ADMIN, User.id != excluded_user_id)
    ).first()
    return other_admin


def delete_user_references(
    db: Session, user_id: int, *, reassign_to_user_id: int | None = None
) -> None:
    """清理或转移指向该成员的全部外键记录，避免外键约束拦截删除。"""
    # Keep the original award and source name; remove its user association.
    for recipient in db.exec(select(HonorRecipient).where(HonorRecipient.user_id == user_id)).all():
        recipient.user_id = None
        db.add(recipient)
    for person in db.exec(select(HonorPerson).where(HonorPerson.user_id == user_id)).all():
        person.user_id = None
        person.revision += 1
        db.add(person)
    for invite in db.exec(select(Invite).where(Invite.created_by == user_id)).all():
        invite.created_by = None
        db.add(invite)
    for plist in db.exec(select(ProblemList).where(ProblemList.created_by == user_id)).all():
        plist.created_by = None
        db.add(plist)
    for client in db.exec(select(OAuthClient).where(OAuthClient.created_by == user_id)).all():
        client.created_by = None
        db.add(client)
    for review in db.exec(select(ReviewRequest).where(ReviewRequest.user_id == user_id)).all():
        review.user_id = None
        db.add(review)
    for review in db.exec(select(ReviewRequest).where(ReviewRequest.reviewed_by == user_id)).all():
        review.reviewed_by = None
        db.add(review)

    # 非空创建者/作者外键（文章、集训、报名、状态覆盖）转移给操作管理员或 root，保留历史内容。
    target_owner = (
        reassign_to_user_id
        if reassign_to_user_id is not None and reassign_to_user_id != user_id
        else _fallback_owner_id(db, user_id)
    )
    if target_owner is not None:
        for post in db.exec(select(Post).where(Post.author_id == user_id)).all():
            post.author_id = target_owner
            db.add(post)
        for camp in db.exec(select(Camp).where(Camp.created_by == user_id)).all():
            camp.created_by = target_owner
            db.add(camp)
        for signup in db.exec(select(Signup).where(Signup.created_by == user_id)).all():
            signup.created_by = target_owner
            db.add(signup)
        for override in db.exec(
            select(WorkshopStatusOverride).where(WorkshopStatusOverride.updated_by == user_id)
        ).all():
            override.updated_by = target_owner
            db.add(override)

    db.flush()
    for model in (
        DailyProblemSolve,
        LearningRun,
        SignupEntry,
        PasskeyCredential,
        WebAuthnChallenge,
        LoginTicket,
        PushSubscription,
        PushDispatch,
        InviteUse,
        RefreshQuota,
        CampRegistration,
        NotificationRead,
        OAuthAuthCode,
        OAuthAccessToken,
    ):
        for row in db.exec(select(model).where(model.user_id == user_id)).all():
            db.delete(row)
    db.flush()
