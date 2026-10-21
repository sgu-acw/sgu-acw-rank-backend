"""把本地预览库里的已发布题目导入生产库。

只搬 learning_problems / learning_versions，并按发布版本刷新 problem_cache，
让题单和集训能立刻显示题名。学习记录（learning_runs）不搬——那是本地
测试账号的自报成绩，和线上用户无关。
"""

import json
import sqlite3
import sys

source, target = sys.argv[1], sys.argv[2]
src = sqlite3.connect(source)
dst = sqlite3.connect(target)

problems = src.execute(
    "select id, creation_id, creation_hash, draft_json, draft_revision, published_revision, created_at"
    " from learning_problems where published_revision is not null"
).fetchall()
print(f"源库已发布题目 {len(problems)} 道")

imported = skipped = 0
for row in problems:
    pid, creation_id, creation_hash, draft_json, draft_rev, pub_rev, created = row
    exists = dst.execute(
        "select 1 from learning_problems where creation_id = ?", (creation_id,)
    ).fetchone()
    if exists:
        skipped += 1
        continue
    dst.execute(
        "insert into learning_problems (id, creation_id, creation_hash, draft_json,"
        " draft_revision, published_revision, created_at) values (?,?,?,?,?,?,?)",
        (pid, creation_id, creation_hash, draft_json, draft_rev, pub_rev, created),
    )
    versions = src.execute(
        "select id, revision, content_json, created_at from learning_versions where problem_id = ?",
        (pid,),
    ).fetchall()
    for vid, rev, content, vcreated in versions:
        dst.execute(
            "insert or ignore into learning_versions (id, problem_id, revision, content_json, created_at)"
            " values (?,?,?,?,?)",
            (vid, pid, rev, content, vcreated),
        )
    # 题单/集训从 problem_cache 取题名与标签。
    published = src.execute(
        "select content_json from learning_versions where problem_id = ? and revision = ?",
        (pid, pub_rev),
    ).fetchone()
    if published:
        content = json.loads(published[0])
        tags = [t for t in (content.get("chapter"), content.get("difficulty")) if t]
        tags += content.get("tags", [])
        dst.execute(
            "insert or replace into problem_cache (platform, key, name, url, rating, tags_json,"
            " missing_reason, fetched_at) values (?,?,?,?,?,?,?, datetime('now'))",
            ("learn", str(pid), content.get("title", ""), f"/learn/problem?id={pid}", None,
             json.dumps(tags, ensure_ascii=False), ""),
        )
    imported += 1

dst.commit()
print(f"导入 {imported} 道，跳过已存在 {skipped} 道")
print("生产库题目数：", dst.execute("select count(*) from learning_problems").fetchone()[0])
