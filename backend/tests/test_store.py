"""ReviewStore durability tests — SQLite persistence vs in-memory isolation.

Covers the "local SQLite" durability option required by
docs/planning/04_SCOPE_AND_ARCHITECTURE.md: a pending review queue must
survive a process restart, and the store must always disclose its mode.
Run from ``backend/``:  python -m pytest -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.store import CaseNotFound, ReviewStore


def _packet(case_id: str, created: str = "2026-10-17T00:00:00Z") -> dict:
    return {
        "case_id": case_id,
        "outcome": "REVIEW_REQUIRED",
        "manager_decision": "PENDING",
        "recommendation": "MANUAL_REVIEW",
        "created_at_utc": created,
        "policy_version": "synthetic-v0.1",
    }


def test_in_memory_store_is_isolated_and_disclosed():
    store = ReviewStore()
    with pytest.raises(CaseNotFound):
        store.get("case-none")
    assert store.durability == "in-memory (cleared on process restart)"
    assert store.unauthorized_side_effects == 0


def test_unknown_decision_target_fails_closed(tmp_path):
    store = ReviewStore(str(tmp_path / "store.sqlite"))
    with pytest.raises(CaseNotFound):
        store.apply_decision("case-missing", "APPROVED", None)
    assert store.unauthorized_side_effects == 0


def test_case_and_decision_survive_restart(tmp_path: Path):
    db = str(tmp_path / "store.sqlite")

    first = ReviewStore(db)
    first.create(
        _packet("case-persist", created="2026-10-17T01:00:00Z"),
        [{"ts": "2026-10-17T01:00:00Z", "event": "case_created", "detail": "csv_rows=1"}],
        {"policy": "synthetic-v0.1", "complaint": "late pickup — fries cold 🍟"},
    )
    first.apply_decision("case-persist", "APPROVED", "looks good")

    # Simulate a brand-new process over the same database file.
    second = ReviewStore(db)
    entry = second.get("case-persist")
    assert entry["packet"]["manager_decision"] == "APPROVED"
    assert entry["audit"][-1]["event"] == "manager_decision"
    assert "note=looks good" in entry["audit"][-1]["detail"]
    # Unicode round-trips intact (non-ASCII complaint text + emoji).
    assert entry["context"]["complaint"] == "late pickup — fries cold 🍟"

    rows = second.list()
    assert [r["case_id"] for r in rows] == ["case-persist"]
    assert rows[0]["manager_decision"] == "APPROVED"
    assert rows[0]["outcome"] == "REVIEW_REQUIRED"
    assert "survives restart" in second.durability


def test_list_sorts_newest_first(tmp_path: Path):
    store = ReviewStore(str(tmp_path / "store.sqlite"))
    store.create(_packet("case-old", created="2026-10-17T01:00:00Z"), [], {})
    store.create(_packet("case-new", created="2026-10-17T02:00:00Z"), [], {})
    assert [r["case_id"] for r in store.list()] == ["case-new", "case-old"]


def test_memory_path_is_treated_as_in_memory():
    store = ReviewStore(":memory:")
    assert store.durability == "in-memory (cleared on process restart)"