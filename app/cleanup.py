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

from .models import (
    Account,
    CampRegistration,
    DailyProgress,
    InviteUse,
    LoginTicket,
    NotificationRead,
    PasskeyCredential,
    PushDispatch,
    PushSubscription,
    RefreshQuota,
    SignupEntry,
    SolvedSnapshot,
    WebAuthnChallenge,
)


def delete_account_data(db: Session, account_id: int) -> int:
    """删掉某个账号的练习记录与快照。返回删掉的行数。"""
    removed = 0
    for row in db.exec(select(DailyProgress).where(DailyProgress.account_id == account_id)).all():
        db.delete(row)
        removed += 1
    for snapshot in db.exec(
        select(SolvedSnapshot).where(SolvedSnapshot.account_id == account_id)
    ).all():
        db.delete(snapshot)
        removed += 1
    return removed


def delete_user_accounts(db: Session, user_id: int) -> int:
    """删掉某个成员的所有账号及其数据。返回删掉的账号数。"""
    accounts = list(db.exec(select(Account).where(Account.user_id == user_id)).all())
    for account in accounts:
        delete_account_data(db, account.id)
        db.delete(account)
    return len(accounts)


def delete_user_references(db: Session, user_id: int) -> None:
    """删掉指向该成员的其余记录。

    ``invite_uses``、``refresh_quotas``、``camp_registrations`` 和
    ``notification_reads`` 都带外键指向 users，不先清掉的话删用户会被
    数据库拦下。
    """
    for model in (
        SignupEntry,
        PasskeyCredential,
        WebAuthnChallenge,
        LoginTicket,
        PushSubscription,
        PushDispatch,
    ):
        for row in db.exec(select(model).where(model.user_id == user_id)).all():
            db.delete(row)
    for row in db.exec(select(InviteUse).where(InviteUse.user_id == user_id)).all():
        db.delete(row)
    for row in db.exec(select(RefreshQuota).where(RefreshQuota.user_id == user_id)).all():
        db.delete(row)
    for row in db.exec(select(CampRegistration).where(CampRegistration.user_id == user_id)).all():
        db.delete(row)
    for row in db.exec(select(NotificationRead).where(NotificationRead.user_id == user_id)).all():
        db.delete(row)
