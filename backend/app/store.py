"""ReviewStore — SQLite-backed review store with an explicit durability disclosure.

When constructed with a database path (the server does this by default, see
``app.main``), cases, audit events and routing context are persisted to a local
SQLite file, so a pending review queue survives process restarts — the scope
doc's "local SQLite" option. ``ReviewStore()`` without a path keeps the legacy
in-memory behaviour used by unit tests and the eval runner for hermetic runs.
``GET /api/status`` always discloses which mode is active via ``durability``.

The only mutation after creation is the manager decision, which appends an
audit event. There is no code path here that sends, refunds, or edits orders.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .intake import utc_now


class CaseNotFound(KeyError):
    pass


class ReviewStore:
    def __init__(self, db_path: str | None = None) -> None:
        self._lock = threading.Lock()
        # Instrumented counter for unauthorized side effects (target: ZERO).
        self.unauthorized_side_effects = 0
        if db_path == ":memory:":
            db_path = None
        self._db_path = db_path
        if db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Single shared connection; every access happens under self._lock, so
        # check_same_thread=False is safe for FastAPI's threadpool workers.
        self._conn = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cases ("
            "case_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self._conn.commit()

    @property
    def durability(self) -> str:
        if self._db_path:
            return f"sqlite ({Path(self._db_path).name}) — survives restart"
        return "in-memory (cleared on process restart)"

    @staticmethod
    def _dump(packet: dict, audit_events: list[dict], context: dict) -> str:
        return json.dumps(
            {"packet": packet, "audit": audit_events, "context": context},
            ensure_ascii=False,
        )

    @staticmethod
    def _load(payload: str) -> dict:
        entry = json.loads(payload)
        return {
            "packet": dict(entry["packet"]),
            "audit": list(entry["audit"]),
            "context": dict(entry["context"]),
        }

    def create(self, packet: dict, audit_events: list[dict], context: dict) -> dict:
        case_id = packet["case_id"]
        payload = self._dump(packet, audit_events, context)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cases (case_id, payload) VALUES (?, ?)",
                (case_id, payload),
            )
            self._conn.commit()
        return self.get(case_id)

    def get(self, case_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()
        if row is None:
            raise CaseNotFound(case_id)
        return self._load(row[0])

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT payload FROM cases").fetchall()
        items = [json.loads(payload)["packet"] for payload, in rows]
        items.sort(key=lambda p: p.get("created_at_utc", ""), reverse=True)
        return [
            {
                "case_id": p["case_id"],
                "outcome": p["outcome"],
                "manager_decision": p["manager_decision"],
                "recommendation": p.get("recommendation"),
                "created_at_utc": p.get("created_at_utc"),
                "policy_version": p["policy_version"],
            }
            for p in items
        ]

    def apply_decision(self, case_id: str, decision: str, note: str | None) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()
            if row is None:
                raise CaseNotFound(case_id)
            entry = json.loads(row[0])
            entry["packet"]["manager_decision"] = decision
            entry["audit"].append(
                {
                    "ts": utc_now(),
                    "event": "manager_decision",
                    "detail": f"decision={decision}" + (f" note={note}" if note else ""),
                }
            )
            self._conn.execute(
                "UPDATE cases SET payload = ? WHERE case_id = ?",
                (json.dumps(entry, ensure_ascii=False), case_id),
            )
            self._conn.commit()
        return self.get(case_id)