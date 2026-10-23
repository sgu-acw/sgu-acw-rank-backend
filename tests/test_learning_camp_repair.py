"""历史封榜修复必须先预览，保留外部成绩与封榜时间，并防止重复应用。"""

# ruff: noqa: F811 -- pytest fixtures are injected by parameter name
import json
import os
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import Session

from app.db import get_engine
from app.learning_camp_repair import (
    LearningRepairError,
    apply_learning_repair,
    learning_repair_plan,
)
from app.models import Camp, CampFinalization
from tests.test_learning import local_client  # noqa: F401
from tests.test_learning_audit import add_run, make_learning_camp


def legacy_snapshot(client):
    camp_id, uid, cutoff = make_learning_camp(client, (1, 2, 3), external=True)
    rows = [
        {
            "rank": 1,
            "user_id": 999,
            "student_id": "99999999999",
            "solved_count": 2,
            "per_problem": {"codeforces:1A": True, "learn:2": True},
        },
        {
            "rank": 2,
            "user_id": uid,
            "student_id": "00000000000",
            "solved_count": 1,
            "per_problem": {"codeforces:1A": True, "learn:2": False},
        },
    ]
    with Session(get_engine()) as db:
        add_run(db, uid, 1, cutoff - timedelta(days=1))
        add_run(db, uid, 2, cutoff - timedelta(days=1))  # 已封存的 learn:2 不重算。
        add_run(db, uid, 3, cutoff + timedelta(seconds=1))
        db.add(
            CampFinalization(
                camp_id=camp_id,
                finalized_at=cutoff + timedelta(seconds=2),
                participants_json=json.dumps(rows),
                problems_json=json.dumps(["codeforces:1A", "learn:2"]),
                standings_json=json.dumps(rows),
            )
        )
        db.commit()
    return camp_id, uid


def test_legacy_preview_is_read_only_and_repair_is_conditional(local_client):
    c = local_client
    camp_id, uid = legacy_snapshot(c)
    with Session(get_engine()) as db:
        before = db.get(CampFinalization, camp_id).standings_json
        camp = db.get(Camp, camp_id)
        plan = learning_repair_plan(db, camp)
        assert plan.summary["missing_problem_keys"] == ["learn:1", "learn:3"]
        assert plan.summary["score_changed_rows"] == 1
        assert plan.summary["rank_changed_rows"] == 1
        assert db.get(CampFinalization, camp_id).standings_json == before
    endpoint = f"/api/admin/camps/{camp_id}/learning-score-audit"
    audit = c.get(endpoint)
    assert audit.status_code == 200 and audit.json() == plan.summary
    with Session(get_engine()) as db:
        camp = db.get(Camp, camp_id)
        with pytest.raises(LearningRepairError, match="快照已变化"):
            apply_learning_repair(db, camp, expected_hash="invalid")
        assert db.get(CampFinalization, camp_id).standings_json == before
        finalized = db.get(CampFinalization, camp_id).finalized_at
        apply_learning_repair(db, camp, expected_hash=plan.summary["snapshot_hash"])
        db.expire_all()
        result = db.get(CampFinalization, camp_id)
        assert result.finalized_at == finalized
        rows = json.loads(result.standings_json)
        repaired = next(row for row in rows if row["user_id"] == uid)
        assert repaired["solved_count"] == 2 and repaired["rank"] == 1
        assert repaired["per_problem"] == {
            "codeforces:1A": True,
            "learn:2": False,
            "learn:1": True,
            "learn:3": False,
        }
        assert next(row for row in rows if row["user_id"] == 999)["solved_count"] == 2
        with pytest.raises(LearningRepairError, match="快照已变化"):
            apply_learning_repair(db, camp, expected_hash=plan.summary["snapshot_hash"])
        assert learning_repair_plan(db, camp).summary["eligible"] is False
    c.cookies.clear()
    assert c.get(endpoint).status_code == 401


def test_repair_cli_defaults_to_preview_and_saves_private_backup(local_client, tmp_path):
    camp_id, _ = legacy_snapshot(local_client)
    command = [
        sys.executable,
        "-m",
        "app.learning_camp_repair",
        "--database-url",
        os.environ["SGU_ACW_DATABASE_URL"],
        "--camp-id",
        str(camp_id),
    ]
    root = Path(__file__).resolve().parents[1]
    preview = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True)
    plan = json.loads(preview.stdout)
    assert plan["applied"] is False and plan["eligible"] is True
    backup = tmp_path / "snapshot-backup.json"
    applied = subprocess.run(
        command + ["--apply", "--expected-hash", plan["snapshot_hash"], "--backup", str(backup)],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(applied.stdout)["applied"] is True
    saved = json.loads(backup.read_text())
    assert json.loads(saved["problems_json"]) == ["codeforces:1A", "learn:2"]
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with Session(get_engine()) as db:
        result = db.get(CampFinalization, camp_id)
        assert result.finalized_at.isoformat() == saved["finalized_at"]
        assert result.standings_json != saved["standings_json"]
