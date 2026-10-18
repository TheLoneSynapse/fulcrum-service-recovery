"""Explicit stub mode — the labelled local-heuristic backend.

Selected via ``AMD_PROVIDER=stub`` (or auto-selected when no real provider
is configured). Never performs HTTP, never claims a model call: every packet
built on this provider reports ``attempted=False, verified=False,
model=None``. The heuristic extraction itself lives in ``model_adapter``
(the deterministic rules are not "inference" and are never labelled as such).
"""

from __future__ import annotations

from .base import Provider, ProviderResponse


class StubProvider(Provider):
    name = "stub"
    backend_label = "local-heuristic"
    is_stub = True
    requires_key = False
    env_base = env_key = env_model = env_timeout = ""

    def __init__(self, auto: bool = False, explicit: bool = False) -> None:
        super().__init__(explicit=explicit or not auto)
        self.auto = auto  # auto = chosen because nothing else is configured

    @property
    def configured(self) -> bool:
        return False  # a stub is never a "configured provider"

    def missing_env(self) -> list[str]:
        return []

    async def run_completion(self, payload: dict) -> ProviderResponse:
        return ProviderResponse(
            ok=False,
            error="stub mode: no model call is attempted or claimed",
        )

    def status(self) -> dict:
        state = super().status()
        state["note"] = (
            "explicit stub (AMD_PROVIDER=stub) — deterministic local heuristic; "
            "packets report attempted=False, verified=False, model=None"
            if not self.auto
            else "no provider configured — running the labelled local-heuristic stub "
            "(attempts=False, verified=False, model=None)"
        )
        return state
