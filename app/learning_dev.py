"""Isolated local learning API. Never imported by app.main or deployment workflows."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from sqlmodel import Session, select

from .config import get_settings
from .db import get_engine
from .learning_models import LearningProblem, LearningVersion
from .main import create_app
from .routers.learning import ProblemContent, encoded, router


def assert_local_database():
    settings = get_settings()
    data_dir = os.environ.get("SGU_LEARN_DATA_DIR")
    if os.environ.get("SGU_LEARN_LOCAL") != "1" or not data_dir:
        raise RuntimeError(
            "Learning development requires SGU_LEARN_LOCAL=1 and an isolated data directory"
        )
    expected = "sqlite:///" + str(Path(data_dir).resolve() / "learning.db")
    if settings.database_url != expected:
        raise RuntimeError("Refusing to use a database outside the isolated learning directory")
    Path(data_dir).mkdir(parents=True, exist_ok=True)


def migrate_local_schema():
    # Only reachable after the local-directory guard; never run by the production app.
    with get_engine().begin() as connection:
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(learning_problems)")
        }
        if "creation_id" not in columns:
            connection.exec_driver_sql("ALTER TABLE learning_problems ADD COLUMN creation_id TEXT")
            connection.exec_driver_sql(
                "UPDATE learning_problems SET creation_id = lower(hex(randomblob(16)))"
            )
        if "creation_hash" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE learning_problems ADD COLUMN creation_hash TEXT NOT NULL DEFAULT ''"
            )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_learning_problems_creation_id "
            "ON learning_problems (creation_id)"
        )


def seed_examples(db):
    if db.exec(select(LearningProblem)).first():
        return
    examples = [
        (
            "A + B",
            "输入输出",
            "输入两个整数 a 和 b，输出它们的和。范围：-10⁹ ≤ a,b ≤ 10⁹。",
            [("20 22\n", "42\n"), ("-3 7", "4"), ("0 0", "0")],
        ),
        (
            "绝对值",
            "分支",
            "输入一个整数 n，输出其绝对值。范围：-10⁹ ≤ n ≤ 10⁹。",
            [("-12", "12"), ("0", "0"), ("25", "25")],
        ),
        (
            "三个数的最大值",
            "分支",
            "输入三个整数，输出最大值。",
            [("1 9 3", "9"), ("-9 -2 -7", "-2"), ("5 5 5", "5")],
        ),
        (
            "从 1 加到 n",
            "循环",
            "输入整数 n，输出 1 到 n 的和。范围：1 ≤ n ≤ 100000。注意整数范围。",
            [("5", "15"), ("1", "1"), ("100000", "5000050000")],
        ),
        (
            "数位和",
            "循环",
            "输入非负整数 n，输出十进制各数位之和。范围：0 ≤ n ≤ 10⁹。",
            [("12345", "15"), ("0", "0"), ("1000000000", "1")],
        ),
        (
            "偶数个数",
            "数组",
            "输入 n，再输入 n 个整数，输出其中偶数的个数。范围：1 ≤ n ≤ 1000。",
            [("5\n1 2 3 4 6", "3"), ("1\n-2", "1"), ("3\n1 3 5", "0")],
        ),
        (
            "逆序输出",
            "数组",
            "输入 n，再输入 n 个整数，按相反顺序输出。范围：1 ≤ n ≤ 1000。",
            [("4\n1 2 3 4", "4 3 2 1"), ("1\n8", "8"), ("3\n-1 0 1", "1 0 -1")],
        ),
        (
            "最大公约数",
            "函数",
            "输入两个正整数 a、b，输出它们的最大公约数。范围：1 ≤ a,b ≤ 10⁹。",
            [("12 18", "6"), ("17 13", "1"), ("10 10", "10")],
        ),
    ]
    meta = {
        "A + B": ("入门", ["读入", "算术"]),
        "绝对值": ("入门", ["分支"]),
        "三个数的最大值": ("入门", ["分支", "比较"]),
        "从 1 加到 n": ("基础", ["循环", "求和"]),
        "数位和": ("基础", ["循环", "数位"]),
        "偶数个数": ("基础", ["数组", "计数"]),
        "逆序输出": ("基础", ["数组", "输出"]),
        "最大公约数": ("进阶", ["函数", "数论", "递推"]),
    }
    for title, chapter, statement, cases in examples:
        difficulty, tags = meta[title]
        content = ProblemContent(
            title=title,
            chapter=chapter,
            difficulty=difficulty,
            tags=tags,
            statement=statement,
            environments=["c", "cpp", "python", "java8", "java21"],
            tests=[
                {"name": f"测试点 {i + 1}", "input": a, "output": b, "sample": i == 0}
                for i, (a, b) in enumerate(cases)
            ],
        )
        p = LearningProblem(draft_json=encoded(content), published_revision=1)
        db.add(p)
        db.flush()
        db.add(
            LearningVersion(id=str(uuid4()), problem_id=p.id, revision=1, content_json=p.draft_json)
        )
    db.commit()


def seed_preview_content(db):
    """给本地预览放一点集训/题单样例，方便验收管理页；只在这个隔离库里跑。"""
    from datetime import UTC, datetime, timedelta

    from .learning_helpers import upsert_learn_cache
    from .models import Camp, CampProblem, Platform, ProblemList, ProblemListItem, User

    if os.environ.get("SGU_LEARN_SEED_DEMO") != "1":
        return
    problems = db.exec(select(LearningProblem).order_by(LearningProblem.id)).all()
    if not problems:
        return
    for problem in problems:
        upsert_learn_cache(db, problem)
    db.commit()

    admin = db.exec(select(User).where(User.student_id == "00000000000")).first()
    if admin is None:
        return

    if not db.exec(select(ProblemList)).first():
        world = ProblemList(
            title="入门练习（本地样例）",
            description="本地预览用的示例题单。",
            created_by=admin.id or 0,
        )
        db.add(world)
        db.flush()
        for position, problem in enumerate(problems[:4]):
            db.add(
                ProblemListItem(
                    list_id=world.id or 0,
                    platform=Platform.LEARN,
                    key=str(problem.id),
                    position=position,
                )
            )
        db.commit()

    if not db.exec(select(Camp)).first():
        now = datetime.now(UTC).replace(microsecond=0)
        camp = Camp(
            name="示例集训（本地预览）",
            description="本地预览用的示例集训。",
            start_at=now - timedelta(hours=2),
            end_at=now + timedelta(days=3),
            created_by=admin.id or 0,
        )
        db.add(camp)
        db.flush()
        for position, problem in enumerate(problems[:3]):
            db.add(
                CampProblem(
                    camp_id=camp.id or 0,
                    platform=Platform.LEARN,
                    key=str(problem.id),
                    position=position,
                )
            )
        db.commit()


def create_learning_app():
    application = create_app()
    application.include_router(router, prefix="/api")
    original = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        assert_local_database()  # Runs before init_db or migrations.
        async with original(app):
            migrate_local_schema()
            with Session(get_engine()) as db:
                seed_examples(db)
                seed_preview_content(db)
            yield

    application.router.lifespan_context = lifespan
    return application


app = create_learning_app()
