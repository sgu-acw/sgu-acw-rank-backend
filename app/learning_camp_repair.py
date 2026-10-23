"""显式修复早期封榜遗漏的题库列。

常规结算不会调用此模块。先生成只读预览，批准后由维护脚本带快照哈希执行。
已存在的题目成绩和封榜时间保持原值，仅补遗漏的题库题，再按原规则排名。
"""

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from sqlalchemy import update
from sqlmodel import Session, select

from .learning_helpers import learn_done_by_user, learning_enabled
from .models import Camp, CampFinalization, CampProblem, Platform


class LearningRepairError(RuntimeError):
    pass


@dataclass
class LearningRepairPlan:
    summary: dict
    original: dict
    problems_json: str
    standings_json: str


def learning_repair_plan(db: Session, camp: Camp) -> LearningRepairPlan:
    final = db.get(CampFinalization, camp.id, populate_existing=True)
    if final is None or final.finalized_at is None:
        raise LearningRepairError("集训尚未封榜，无需历史修复")
    original = {
        "camp_id": final.camp_id,
        "started_at": final.started_at.isoformat(),
        "finalized_at": final.finalized_at.isoformat(),
        "participants_json": final.participants_json,
        "problems_json": final.problems_json,
        "standings_json": final.standings_json,
    }
    digest = sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
    existing_keys = json.loads(final.problems_json)
    learning_keys = [
        f"{Platform.LEARN}:{p.key}"
        for p in db.exec(
            select(CampProblem)
            .where(CampProblem.camp_id == camp.id, CampProblem.platform == Platform.LEARN)
            .order_by(CampProblem.position, CampProblem.id)
        ).all()
    ]
    missing = [key for key in learning_keys if key not in existing_keys]
    old_rows = json.loads(final.standings_json)
    proposed = json.loads(final.standings_json)
    enabled = learning_enabled()
    if missing and enabled:
        done = learn_done_by_user(db, [row["user_id"] for row in proposed], through=camp.end_at)
        for row in proposed:
            passed = done.get(row["user_id"], set())
            additions = {key: key.split(":", 1)[1] in passed for key in missing}
            # 保留原有各列和过题数；不重建外部平台历史快照。
            row["per_problem"].update(additions)
            row["solved_count"] += sum(additions.values())
        proposed.sort(key=lambda r: (-r["solved_count"], r["student_id"], r["user_id"]))
        rank, previous = 0, None
        for index, row in enumerate(proposed):
            if previous is None or row["solved_count"] < previous:
                rank = index + 1
            row["rank"] = rank
            previous = row["solved_count"]
    old_by_id = {row["user_id"]: row for row in old_rows}
    changes = []
    for row in proposed:
        before = old_by_id[row["user_id"]]
        delta = row["solved_count"] - before["solved_count"]
        if delta or row["rank"] != before["rank"]:
            changes.append(
                {
                    "user_id": row["user_id"],
                    "solved_before": before["solved_count"],
                    "solved_after": row["solved_count"],
                    "rank_before": before["rank"],
                    "rank_after": row["rank"],
                    "added_ac_keys": [key for key in missing if row["per_problem"].get(key)],
                }
            )
    summary = {
        "camp_id": camp.id,
        "finalized_at": final.finalized_at.isoformat(),
        "snapshot_hash": digest,
        "eligible": bool(missing) and enabled,
        "participants": len(old_rows),
        "missing_problem_keys": missing,
        "score_changed_rows": sum(r["solved_before"] != r["solved_after"] for r in changes),
        "rank_changed_rows": sum(r["rank_before"] != r["rank_after"] for r in changes),
        "changes": changes,
    }
    return LearningRepairPlan(
        summary,
        original,
        json.dumps([*existing_keys, *missing], ensure_ascii=False),
        json.dumps(proposed, ensure_ascii=False),
    )


def apply_learning_repair(db: Session, camp: Camp, *, expected_hash: str) -> dict:
    plan = learning_repair_plan(db, camp)
    if plan.summary["snapshot_hash"] != expected_hash:
        raise LearningRepairError("封榜快照已变化，请重新生成修复预览")
    if not plan.summary["eligible"]:
        raise LearningRepairError("当前快照没有可修复的题库列，或题库功能已关闭")
    original = plan.original
    written = db.exec(
        update(CampFinalization)
        .where(
            CampFinalization.camp_id == camp.id,
            CampFinalization.finalized_at == datetime.fromisoformat(original["finalized_at"]),
            CampFinalization.started_at == datetime.fromisoformat(original["started_at"]),
            CampFinalization.problems_json == original["problems_json"],
            CampFinalization.standings_json == original["standings_json"],
            CampFinalization.participants_json == original["participants_json"],
        )
        .values(problems_json=plan.problems_json, standings_json=plan.standings_json)
    )
    if written.rowcount != 1:
        raise LearningRepairError("封榜快照已变化，未写入修复结果")
    db.commit()
    return plan.summary


def main():
    """命令行默认以 SQLite 只读模式预览；--apply 要求已核对的哈希和备份路径。"""
    import argparse
    import os
    from pathlib import Path

    from sqlalchemy.engine import URL, make_url
    from sqlmodel import create_engine

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--camp-id", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-hash")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.apply and (not args.expected_hash or not args.backup):
        parser.error("--apply 必须同时提供 --expected-hash 与 --backup")
    db_url = make_url(args.database_url)
    if db_url.get_backend_name() != "sqlite" or not db_url.database:
        parser.error("仅支持现有 SQLite 文件")
    db_path = Path(db_url.database).resolve()
    if not db_path.is_file():
        parser.error("数据库文件不存在")
    if not args.apply:
        db_url = URL.create(
            "sqlite", database=f"file:{db_path}", query={"mode": "ro", "uri": "true"}
        )
    engine = create_engine(db_url)
    with Session(engine) as db:
        camp = db.get(Camp, args.camp_id)
        if camp is None:
            raise SystemExit("集训不存在")
        try:
            plan = learning_repair_plan(db, camp)
            if args.apply:
                if not plan.summary["eligible"]:
                    raise LearningRepairError("没有需要补齐的题库列，未写入")
                if args.expected_hash != plan.summary["snapshot_hash"]:
                    raise LearningRepairError("预览哈希已变化，未写入")
                args.backup.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(args.backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as backup:
                    json.dump(plan.original, backup, ensure_ascii=False, indent=2)
                    backup.flush()
                    os.fsync(backup.fileno())
                apply_learning_repair(db, camp, expected_hash=args.expected_hash)
            print(json.dumps({"applied": args.apply, **plan.summary}, ensure_ascii=False, indent=2))
        except LearningRepairError as e:
            raise SystemExit(str(e)) from e


if __name__ == "__main__":
    main()
