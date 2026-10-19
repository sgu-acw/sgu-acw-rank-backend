"""题库题目接入集训/题单的辅助函数。

题库默认启用；``SGU_LEARN_ENABLED=0`` 可以整块关掉（回滚用）。关闭时
这些函数一律拒绝，主应用不会去碰 learning 表。
"""

import json
import os

from fastapi import HTTPException
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from .models import Platform, ProblemCache


def learning_enabled() -> bool:
    """题库功能是否启用。默认开启，SGU_LEARN_ENABLED=0 可关闭。"""
    return os.environ.get("SGU_LEARN_ENABLED", "1") != "0"


def learn_problem_or_400(db: Session, key: str):
    """按题单/集训里的 key（即题目 id）找到已发布的练习题。"""
    if not learning_enabled():
        raise HTTPException(status_code=400, detail="题库未启用")
    from .learning_models import LearningProblem

    problem = db.get(LearningProblem, int(key))
    if problem is None or problem.published_revision is None:
        raise HTTPException(status_code=400, detail="这道题不存在或未发布")
    return problem


def upsert_learn_cache(db: Session, problem) -> ProblemCache:
    """把练习题写进 ProblemCache，让题单/集训复用现成的题名与标签展示路径。

    草稿标题可能改，所以每次发布都刷新这条缓存；key 永远是题目 id。
    """
    from .learning_models import LearningVersion

    key = str(problem.id)
    row = db.exec(
        select(ProblemCache).where(ProblemCache.platform == Platform.LEARN, ProblemCache.key == key)
    ).first()
    if row is None:
        row = ProblemCache(platform=Platform.LEARN, key=key)
    version = db.exec(
        select(LearningVersion).where(
            LearningVersion.problem_id == problem.id,
            LearningVersion.revision == problem.published_revision,
        )
    ).first()
    if version is not None:
        content = json.loads(version.content_json)
        row.name = content.get("title", "")
        tags = [t for t in (content.get("chapter"), content.get("difficulty")) if t]
        tags += content.get("tags", [])
        row.tags_json = json.dumps(tags, ensure_ascii=False)
    row.url = f"/learn/problem?id={key}"
    row.missing_reason = ""
    db.add(row)
    db.flush()
    return row


def learn_done_keys(db: Session, user_id: int | None) -> set[str]:
    """用户在本地练习中 AC 过的题目 id 集合（自报成绩，仅做展示）。

    正式库没有 learning 表；即使存在脏数据也返回空集合而不是抛错。
    """
    if user_id is None or not learning_enabled():
        return set()
    from .learning_models import LearningRun, LearningVersion

    try:
        rows = db.exec(
            select(LearningVersion.problem_id)
            .join(LearningRun, LearningRun.version_id == LearningVersion.id)
            .where(LearningRun.user_id == user_id, LearningRun.verdict == "AC")
        ).all()
    except OperationalError:
        return set()
    return {str(pid) for pid in rows}
