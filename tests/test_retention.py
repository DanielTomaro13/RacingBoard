"""Retention tests — stdlib only, no ML stack, no network.

    python3 tests/test_retention.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SPORTSDATA_MCP_SRC", "/dev/null")  # settings import guard

from moneyflow.db import DB           # noqa: E402  (creates the real schema)
from moneyflow import retention       # noqa: E402

DAY = 86400.0


def make_db(path: str, old_races: int, new_races: int, outcome_frac: float) -> None:
    """Real schema via DB, then hand-set created_at so ages are deterministic."""
    DB(path).close()
    conn = sqlite3.connect(path)
    now = time.time()
    for i in range(old_races + new_races):
        old = i < old_races
        key = f"{'OLD' if old else 'NEW'}:{i}"
        created = now - (200 * DAY if old else 1 * DAY)
        conn.execute("INSERT INTO races (race_key, date, created_at) VALUES (?,?,?)",
                     (key, "2026-01-01", created))
        for n in range(1, 9):
            for bucket in (60, 30, 5):
                conn.execute(
                    "INSERT INTO snapshots (race_key, number, offset_min, ts) VALUES (?,?,?,?)",
                    (key, n, bucket, created))
            if old and i < int(old_races * outcome_frac):
                conn.execute(
                    "INSERT INTO outcomes (race_key, number, firmed, won, placed) "
                    "VALUES (?,?,?,?,?)", (key, n, n % 2, int(n == 1), int(n <= 3)))
    conn.commit()
    conn.close()


def counts(path: str) -> dict:
    conn = sqlite3.connect(path)
    out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
           for t in ("races", "snapshots", "outcomes")}
    out["log"] = conn.execute("SELECT status FROM retention_log ORDER BY ts").fetchall()
    conn.close()
    return out


def run_case(name: str, fn) -> None:
    with tempfile.TemporaryDirectory() as td:
        fn(os.path.join(td, "t.db"))
    print(f"  ok {name}")


def case_empty(p):
    make_db(p, old_races=0, new_races=4, outcome_frac=0)
    assert retention.run(180, db_path=p) == 0
    c = counts(p)
    assert c["snapshots"] == 4 * 8 * 3 and c["log"][-1][0] == "EMPTY"


def case_held_low_coverage(p):
    make_db(p, old_races=10, new_races=2, outcome_frac=0.3)   # 30% < 60% gate
    assert retention.run(180, db_path=p) == 2
    c = counts(p)
    assert c["snapshots"] == 12 * 8 * 3, "HELD must delete nothing"
    assert c["log"][-1][0] == "HELD"


def case_held_review_cmd(p):
    make_db(p, old_races=10, new_races=0, outcome_frac=1.0)
    os.environ["MF_RETENTION_REVIEW_CMD"] = "exit 3"
    try:
        assert retention.run(180, db_path=p) == 2
    finally:
        del os.environ["MF_RETENTION_REVIEW_CMD"]
    c = counts(p)
    assert c["snapshots"] == 10 * 8 * 3 and c["log"][-1][0] == "HELD"


def case_dry_run(p):
    make_db(p, old_races=6, new_races=2, outcome_frac=1.0)
    assert retention.run(180, dry_run=True, db_path=p) == 0
    c = counts(p)
    assert c["snapshots"] == 8 * 8 * 3 and c["log"][-1][0] == "DRY_RUN"


def case_prune(p):
    make_db(p, old_races=6, new_races=2, outcome_frac=1.0)
    assert retention.run(180, db_path=p) == 0
    c = counts(p)
    assert c["snapshots"] == 2 * 8 * 3, "only NEW races' snapshots remain"
    assert c["outcomes"] == 6 * 8, "labels are forever"
    assert c["races"] == 8, "race identity is forever"
    assert c["log"][-1][0] == "PRUNED"
    # idempotent: a second run finds nothing old with snapshots... (races still
    # old, snapshots gone → delete 0, still PRUNED with deleted=0? races>0 so
    # gate runs; coverage still 1.0; deleted 0) — must not error.
    assert retention.run(180, db_path=p) == 0


def case_review_cmd_ok(p):
    make_db(p, old_races=4, new_races=0, outcome_frac=1.0)
    os.environ["MF_RETENTION_REVIEW_CMD"] = "echo model retrained; exit 0"
    try:
        assert retention.run(180, db_path=p) == 0
    finally:
        del os.environ["MF_RETENTION_REVIEW_CMD"]
    assert counts(p)["log"][-1][0] == "PRUNED"


if __name__ == "__main__":
    for f in (case_empty, case_held_low_coverage, case_held_review_cmd,
              case_dry_run, case_prune, case_review_cmd_ok):
        run_case(f.__name__, f)
    print("all retention tests passed")
