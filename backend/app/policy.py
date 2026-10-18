"""PolicyEngine — deterministic, versioned policy loading. Fails closed."""

from __future__ import annotations

import json
from pathlib import Path


class UnknownPolicy(KeyError):
    """Raised when a requested policy version cannot be found."""


class PolicyEngine:
    """Loads versioned policy fixtures from the ``contracts/`` directory."""

    def __init__(self, contracts_dir: str | Path):
        self.contracts_dir = Path(contracts_dir)

    def _paths(self) -> list[Path]:
        return sorted(self.contracts_dir.glob("*.json"))

    def versions(self) -> list[dict]:
        """Summary of every loadable policy (safe to expose to the UI)."""
        out: list[dict] = []
        for path in self._paths():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or "version" not in data:
                continue
            out.append(
                {
                    "version": data["version"],
                    "notice": data.get("_notice"),
                    "source_file": path.name,
                    "allowed_draft_actions": data.get("allowed_draft_actions", []),
                    "never_automate": data.get("never_automate", []),
                    "must_escalate_if": data.get("must_escalate_if", []),
                    "compensation_caps": data.get("compensation_caps", {}),
                }
            )
        return out

    def load(self, version: str) -> dict:
        for path in self._paths():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("version") == version:
                return data
        # Fail closed: never fall back to a default policy silently.
        raise UnknownPolicy(version)
