"""给已有的库补上新增的列。

``SQLModel.metadata.create_all`` 只会建缺的表，**不会给已有的表加列**。
新版本给 accounts 加了 rating、display_name、各种时间戳，给 users 之类的
表可能也会陆续加字段，所以需要一个显式的迁移步骤。

做法是拿模型定义的列和库里的实际列对比，缺什么就 ``ALTER TABLE ADD COLUMN``。
SQLite 支持加列，而且加列是 O(1) 的元数据操作，不重写表。

用法：

    python -m app.migrate          # 补列
    python -m app.migrate --check  # 只看有没有缺的，不改

只处理加列。删列、改类型、加约束这些 SQLite 支持得不好，真需要时应该
另写迁移并配合数据搬运，不该由这个脚本悄悄做。

除了加列，这里还修**历史数据格式**。加列是结构问题，格式是内容问题，
但两者都是「升级一次就完事」的事，分两个脚本反而容易漏跑一个。
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import inspect
from sqlmodel import SQLModel

from .db import get_engine, init_db

logger = logging.getLogger("sgu_acw.migrate")


def _default_clause(column) -> str:
    """把模型上的默认值渲染成 ``DEFAULT ...`` 子句，没有则返回空串。

    只处理标量默认值（数字、字符串、布尔）。带 ``default_factory`` 的字段
    （比如各种 ``created_at``）不加默认值，因为它们的值依赖运行时，SQL 层
    没法表达；这些列本来就允许为空。
    """
    default = column.default
    if default is None or default.is_callable:
        return ""

    value = default.arg
    if isinstance(value, bool):
        return f" DEFAULT {1 if value else 0}"
    if isinstance(value, int | float):
        return f" DEFAULT {value}"
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f" DEFAULT '{escaped}'"
    return ""


def missing_columns() -> dict[str, list[tuple[str, str]]]:
    """返回 ``{表名: [(列名, 类型), ...]}``，只含库里缺的列。"""
    from . import models  # noqa: F401  导入即注册表定义

    engine = get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    result: dict[str, list[tuple[str, str]]] = {}
    for table_name, table in SQLModel.metadata.tables.items():
        if table_name not in existing_tables:
            # 新表交给 create_all 建，这里不管。
            continue

        have = {col["name"] for col in inspector.get_columns(table_name)}
        missing: list[tuple[str, str]] = []
        for column in table.columns:
            if column.name in have:
                continue
            # 用 SQLAlchemy 的方言编译器把类型渲染成 SQLite 能懂的写法。
            col_type = column.type.compile(dialect=engine.dialect)

            # 一定要带 DEFAULT。SQLite 的 ADD COLUMN 不给默认值时，已有的行
            # 会填 NULL；而模型里这些字段是「非空整数」，代码读出来是 None。
            # 多数情况下 None 恰好是 falsy 所以看不出问题，但那是运气——
            # 例如 consecutive_failures 参与乘法时会直接报错。
            default = _default_clause(column)
            if table_name == "honor_recipients" and column.name == "user_id":
                default += ' REFERENCES "users" ("id")'
            missing.append((column.name, f"{col_type}{default}"))
        if missing:
            result[table_name] = missing
    return result


def _fix_nowcoder_keys(*, check_only: bool = False) -> int:
    """给牛客的历史题号补上来源前缀。

    早期的题单功能让管理员直接填数字（``233234``），但过题记录里存的是带
    来源前缀的 key（``nowcoder:contest:233234``）。两边对不上，于是：

    * 完成度永远算成 0 —— 比对时一个是裸数字一个有前缀，永远不相等；
    * 题名抓不到 —— 抓取按 ``来源:题号`` 解析，裸数字走不进任何分支。

    数字在牛客上是竞赛站的题目 id（主站的题是 uuid，不是数字），所以统一
    补成 ``nowcoder:contest:{数字}``。

    两张表都要修：``problem_list_items``（题单里有什么）和
    ``problem_cache``（抓回来的题目元信息）。只修一张的话，另一张仍然
    对不上。
    """
    from sqlalchemy import text

    fixed = 0
    tables = ("problem_list_items", "problem_cache")
    with get_engine().begin() as connection:
        for table in tables:
            rows = connection.execute(
                text(
                    f"SELECT id, key FROM {table} "  # noqa: S608  表名是常量
                    "WHERE platform = 'nowcoder' "
                    "AND key GLOB '[0-9]*' "
                    "AND key NOT LIKE '%:%'"
                )
            ).fetchall()
            if not rows:
                continue
            if check_only:
                logger.warning("%s 有 %d 条牛客题号缺前缀", table, len(rows))
                fixed += len(rows)
                continue

            for row_id, key in rows:
                connection.execute(
                    text(f"UPDATE {table} SET key = :key WHERE id = :id"),  # noqa: S608
                    {"key": f"nowcoder:contest:{key}", "id": row_id},
                )
            logger.info("%s 修了 %d 条牛客题号", table, len(rows))
            fixed += len(rows)

    return fixed


def _rename_columns(*, check_only: bool = False) -> int:
    """把改过名字的列迁过去。

    ``missing_columns`` 只处理加列，不会发现「模型里叫 A、库里叫 B」的情况——
    那种情况下它会以为 A 不存在，试图 ALTER TABLE ADD COLUMN。如果 B 上
    有 NOT NULL 约束就会失败，没有的话更糟：加出一个空列，旧数据全丢。

    SQLite 3.25+ 支持 RENAME COLUMN，是元数据操作，不重写表。生产用的是
    3.45，可以放心用。

    **只在旧名存在、新名不存在时改**。两个都在说明改过了，什么都不做；
    只有新名则说明是新建的库，也不用管。
    """
    from sqlalchemy import inspect, text

    # (表, 旧列名, 新列名)
    RENAMES: list[tuple[str, str, str]] = [
        # 题解里的 C++ 代码从 17 提到 20（ranges、结构化绑定），
        # 字段名一直没跟着改，容易误导。
        ("editorials", "code_cpp17", "code_cpp20"),
    ]

    inspector = inspect(get_engine())
    changed = 0
    with get_engine().begin() as connection:
        for table, old, new in RENAMES:
            if not inspector.has_table(table):
                continue
            columns = {col["name"] for col in inspector.get_columns(table)}
            if old not in columns or new in columns:
                continue
            if check_only:
                logger.warning("%s.%s 需要改名为 %s", table, old, new)
                changed += 1
                continue
            connection.execute(
                text(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')
            )
            logger.info("改名：%s.%s -> %s", table, old, new)
            changed += 1
    return changed


def _normalize_problem_keys(*, check_only: bool = False) -> int:
    """把洛谷/CF/AtCoder 题号统一到库里的规范大小写。

    ``normalize_key`` 已经会转洛谷/CF 大写、AtCoder 小写——但那是运行时
    行为，库里已有的非规范写法不会自动跟着改。管理员曾经输过小写洛谷
    题号（``p1001``）进过集训，跟过题记录里的大写 ``P1001`` 对不上。

    要动的表：``problem_list_items``（题单）、``problem_cache``（元信息
    缓存）、``camp_problems``（集训）。``daily_problems`` 是 CF 题号，
    本来就是大写，不动。
    """
    from sqlalchemy import text

    from .problems import normalize_key

    fixed = 0
    with get_engine().begin() as connection:
        for table in ("problem_list_items", "problem_cache", "camp_problems"):
            rows = connection.execute(
                text(f"SELECT id, platform, key FROM {table}")  # noqa: S608  表名是常量
            ).all()
            for row in rows:
                rid, platform, key = row
                normalized = normalize_key(platform, key)
                if normalized == key:
                    continue
                if check_only:
                    logger.warning(
                        "%s.%s 需要规范化：%s → %s", table, rid, key, normalized
                    )
                    fixed += 1
                    continue
                connection.execute(
                    text(f'UPDATE "{table}" SET key = :new WHERE id = :id'),  # noqa: S608
                    {"new": normalized, "id": rid},
                )
                logger.info("%s.%s %s → %s", table, rid, key, normalized)
                fixed += 1
    return fixed


def _widen_daily_solve_constraint(*, check_only: bool = False) -> int:
    """把「一人一天一条」放宽成「一人一天每道题一条」。

    原来唯一约束是 ``(user_id, day)``，一个人一天只留得下第一道命中的题。
    要统计「每道题谁最早通过」就必须每题各记一条，约束改成
    ``(user_id, day, platform, key)``。

    SQLite 不能直接改约束，只能重建表：建新表 → 拷数据 → 删旧表 → 改名。
    这个过程在事务里做，中途失败会整体回滚。

    **历史数据保留**：老记录只知道「那天通过了某道题」，不知道具体哪几道，
    补不回来。但它们仍然有效（至少说明那天签到了），所以原样搬过去。

    只处理加列的老路走不通：``missing_columns`` 比对的是列名，约束不在它
    的视野里，这个改动它发现不了。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(get_engine())
    if not inspector.has_table("daily_problem_solves"):
        return 0

    # 看现有约束。已经改过就跳过。
    with get_engine().connect() as connection:
        row = connection.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='daily_problem_solves'"
            )
        ).fetchone()
    if row is None:
        return 0
    ddl = row[0] or ""
    if "uq_daily_solve_user_day_problem" in ddl:
        return 0
    if "uq_daily_solve_user_day" not in ddl:
        # 约束名对不上，说明表结构不是预期的样子。不动它，免得把数据搞坏。
        logger.warning("daily_problem_solves 的约束与预期不符，跳过")
        return 0

    if check_only:
        logger.warning("daily_problem_solves 需要放宽唯一约束")
        return 1

    # 建新表 → 拷数据 → 删旧表 → 改名。整段一个事务。
    with get_engine().begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE daily_problem_solves_new (
                id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                day VARCHAR(10) NOT NULL,
                platform VARCHAR(16) NOT NULL,
                "key" VARCHAR(128) NOT NULL,
                account_id INTEGER,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT uq_daily_solve_user_day_problem
                    UNIQUE (user_id, day, platform, "key"),
                FOREIGN KEY(user_id) REFERENCES users (id),
                FOREIGN KEY(account_id) REFERENCES accounts (id)
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO daily_problem_solves_new
                (id, user_id, day, platform, "key", account_id, created_at)
            SELECT id, user_id, day, platform, "key", account_id, created_at
            FROM daily_problem_solves
            """
        )
        connection.exec_driver_sql("DROP TABLE daily_problem_solves")
        connection.exec_driver_sql(
            "ALTER TABLE daily_problem_solves_new RENAME TO daily_problem_solves"
        )
        # 索引跟着旧表一起没了，重建。
        connection.exec_driver_sql(
            "CREATE INDEX ix_daily_problem_solves_day "
            "ON daily_problem_solves (day)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_daily_problem_solves_user_id "
            "ON daily_problem_solves (user_id)"
        )

    logger.info("daily_problem_solves 的唯一约束已放宽")
    return 1


def _migrate_honor_users(*, check_only: bool = False) -> int:
    """Move only explicitly linked legacy identities; never resolve a name to a user."""
    import json

    from sqlmodel import Session, select

    from .honor_models import HonorPerson, HonorRecipient
    from .models import User

    # Check mode may run before the additive columns exist.
    if any(t in missing_columns() for t in ("users", "honor_recipients")):
        return 0
    changed = 0
    with Session(get_engine()) as db:
        if not check_only:
            from sqlalchemy import update

            db.execute(update(User).where(User.id == -1).values(honor_revision=User.honor_revision))
        for person in db.exec(select(HonorPerson)).all():
            if not person.user_id or person.merged_into_id:
                continue
            user = db.get(User, person.user_id)
            if user is None:
                continue
            if user.honor_revision == 1:
                changed += 1
                if not check_only:
                    names = json.loads(person.aliases_json)
                    if person.name != user.display_name:
                        names.append(person.name)
                    user.former_names_json = json.dumps(
                        list(dict.fromkeys(names)), ensure_ascii=False
                    )
                    user.honor_study_start = person.study_start
                    user.honor_study_end = person.study_end
                    user.honor_revision += 1
                    db.add(user)
            for recipient in db.exec(
                select(HonorRecipient).where(
                    HonorRecipient.person_id == person.id, HonorRecipient.user_id.is_(None)
                )
            ).all():
                changed += 1
                if not check_only:
                    recipient.user_id = user.id
                    recipient.person_id = None
                    db.add(recipient)
        if not check_only:
            db.commit()
    return changed


def run_migration(*, check_only: bool = False) -> int:
    """补列。返回补了几列。"""
    init_db()  # 先把缺的表建出来
    # 改名要在补列之前跑：改完之后模型和库对得上，missing_columns 才不会
    # 把「名字不同」误判成「缺列」。
    renamed = _rename_columns(check_only=check_only)

    # 改约束要重建表，放在补列之前：重建后的表结构是新的，missing_columns
    # 不会再多事。
    widened = _widen_daily_solve_constraint(check_only=check_only)

    fixed = _fix_nowcoder_keys(check_only=check_only)
    normalized = _normalize_problem_keys(check_only=check_only)

    pending = missing_columns()
    if not pending:
        logger.info("没有缺的列")
        return renamed + widened + fixed + normalized + _migrate_honor_users(check_only=check_only)

    total = normalized
    for table_name, columns in pending.items():
        for name, col_type in columns:
            if check_only:
                logger.warning("缺列 %s.%s (%s)", table_name, name, col_type)
                total += 1
                continue

            # 用原生 SQL 加列。SQLite 的 ALTER TABLE ADD COLUMN 不支持加
            # 非空且无默认值的列，所以新列都必须是可空的或者有默认值——
            # 这一点由模型定义保证（新增字段都给了默认值）。
            statement = f'ALTER TABLE "{table_name}" ADD COLUMN "{name}" {col_type}'
            logger.info("加列：%s", statement)
            with get_engine().begin() as connection:
                connection.exec_driver_sql(statement)
            total += 1

    return renamed + widened + total + fixed + _migrate_honor_users(check_only=check_only)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    check_only = "--check" in sys.argv
    count = run_migration(check_only=check_only)
    if check_only:
        print(f"有 {count} 处待处理" if count else "结构和数据都是最新的")
    else:
        print(f"处理了 {count} 处" if count else "结构和数据已经是最新的")


if __name__ == "__main__":
    main()
