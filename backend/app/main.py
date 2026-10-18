"""Fulcrum Service Recovery — FastAPI service.

Serves the JSON API under /api/* and the static frontend at /.
Ground rules enforced here: no secrets in responses, fail-closed policy
loading, bounded CSV errors, and honest inference provenance in every packet.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles

from .exporter import PacketExporter  # noqa: F401  (re-exported for tests)
from .intake import MalformedCSV
from .model_adapter import ModelAdapter
from .pipeline import Pipeline
from .policy import PolicyEngine, UnknownPolicy
from .schemas import BatchIntakeRequest, DecisionRequest, IntakeRequest
from .store import CaseNotFound, ReviewStore

ROOT = Path(__file__).resolve().parents[2]  # .../fulcrum-service-recovery-main
CONTRACTS_DIR = ROOT / "contracts"
FRONTEND_DIR = ROOT / "frontend"

app = FastAPI(
    title="Fulcrum Service Recovery",
    version="0.1.0",
    # No public API schema or interactive docs: the endpoints are exercised by
    # the bundled frontend only, and /docs would advertise the full route
    # surface of the (deliberately unauthenticated) demo API to anyone.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Durable by default: cases persist to a local SQLite file so a pending review
# queue survives restarts. Override with FULCRUM_STORE_PATH ("" or ":memory:"
# selects the in-memory store; tests set that via tests/conftest.py).
STORE_PATH = os.environ.get("FULCRUM_STORE_PATH", "").strip() or str(
    ROOT / "data" / "fulcrum_cases.sqlite"
)
store = ReviewStore(STORE_PATH)
pipeline = Pipeline(PolicyEngine(CONTRACTS_DIR), ModelAdapter(), store)

# ------------------------------------------------------------ abuse limits
# The demo API is unauthenticated by design (single-tenant judge demo; no
# cookies, no sessions). To keep one visitor from draining the shared AMD
# quota or flooding the queue, case creation is rate-limited per client IP.
# The value is read per call so tests can tune it; windows are bounded.
_RATE_WINDOW_S = 60.0
_rate_lock = threading.Lock()
_rate_windows: dict[str, deque[float]] = {}


def _creation_limit() -> int:
    try:
        return max(1, int(os.environ.get("FULCRUM_RATE_LIMIT", "60")))
    except ValueError:
        return 60


def _client_key(request: Request) -> str:
    """Nearest-hop client: last X-Forwarded-For entry behind Render's proxy,
    direct peer otherwise (local runs / tests)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.client.host if request.client else "unknown"


def _enforce_creation_limit(request: Request) -> None:
    now = time.monotonic()
    limit = _creation_limit()
    key = _client_key(request)
    with _rate_lock:
        if len(_rate_windows) > 4096:  # bound memory across varied client keys
            _rate_windows.clear()
        window = _rate_windows.setdefault(key, deque())
        while window and now - window[0] > _RATE_WINDOW_S:
            window.popleft()
        if len(window) >= limit:
            retry_after = max(1, int(_RATE_WINDOW_S - (now - window[0])) + 1)
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limited",
                    "message": "too many cases created from this address — try again shortly",
                },
                headers={"Retry-After": str(retry_after)},
            )
        window.append(now)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/status")
def status() -> dict:
    """Backend + inference provenance for the UI banner. Never exposes secrets."""
    return {
        "service": "fulcrum-service-recovery",
        "inference": pipeline.model.status(),
        "provider_slots": pipeline.model.provider_slots(),
        "store_durability": store.durability,
        "unauthorized_side_effects": store.unauthorized_side_effects,
        "synthetic_data_only": True,
    }


@app.get("/api/policies")
def policies() -> list[dict]:
    return pipeline.policies.versions()


@app.post("/api/cases")
def create_case(request: IntakeRequest, http_request: Request) -> dict:
    # Quota guard runs BEFORE the pipeline so a limited client cannot burn
    # AMD inference calls on rejected requests.
    _enforce_creation_limit(http_request)
    try:
        return pipeline.run(
            complaint_text=request.complaint_text,
            policy_version=request.policy_version,
            source=request.source,
            order_csv=request.order_csv,
        )
    except MalformedCSV as exc:
        raise HTTPException(status_code=422, detail={"error": "malformed_csv", "message": str(exc)})
    except UnknownPolicy:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_policy", "message": f"policy '{request.policy_version}' not found"},
        )


@app.post("/api/cases/batch")
def create_cases_batch(request: BatchIntakeRequest, http_request: Request) -> dict:
    """Run many complaints through the pipeline in one call.

    Shared CSV/policy means identical inputs for every case, so a bad policy
    or malformed CSV fails the whole batch closed (422) instead of producing
    a misleading partial run. Unexpected per-case failures are reported per
    index without aborting the rest.
    """
    _enforce_creation_limit(http_request)  # counts as one window slot per call
    cases: list[dict] = []
    errors: list[dict] = []
    for index, complaint in enumerate(request.complaints):
        try:
            result = pipeline.run(
                complaint_text=complaint,
                policy_version=request.policy_version,
                source=request.source,
                order_csv=request.order_csv,
            )
            cases.append({"index": index, **result})
        except MalformedCSV as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "malformed_csv", "message": str(exc)},
            )
        except UnknownPolicy:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "unknown_policy",
                    "message": f"policy '{request.policy_version}' not found",
                },
            )
        except Exception as exc:  # one bad case must not abort the batch
            errors.append(
                {"index": index, "error": type(exc).__name__, "message": str(exc)[:200]}
            )
    return {
        "cases": cases,
        "errors": errors,
        "requested": len(request.complaints),
        "created": len(cases),
    }


@app.get("/api/cases")
def list_cases() -> list[dict]:
    return store.list()


@app.get("/api/cases/{case_id}")
def get_case(case_id: str) -> dict:
    try:
        entry = store.get(case_id)
    except CaseNotFound:
        raise HTTPException(status_code=404, detail={"error": "case_not_found"})
    return {"packet": entry["packet"], "audit": entry["audit"]}


@app.post("/api/cases/{case_id}/decision")
def decide_case(case_id: str, request: DecisionRequest) -> dict:
    try:
        entry = store.apply_decision(case_id, request.decision, request.note)
    except CaseNotFound:
        raise HTTPException(status_code=404, detail={"error": "case_not_found"})
    return {"packet": entry["packet"], "audit": entry["audit"]}


@app.get("/api/cases/{case_id}/export")
def export_case(
    case_id: str,
    format: str = Query(default="json", pattern="^(json|md)$"),
) -> Response:
    try:
        body, media_type, filename = pipeline.export(case_id, format)
    except CaseNotFound:
        raise HTTPException(status_code=404, detail={"error": "case_not_found"})
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/cases/{case_id}/export")
def export_case_post(
    case_id: str,
    format: str = Query(default="json", pattern="^(json|md)$"),
) -> Response:
    return export_case(case_id, format)


# Static frontend must be mounted LAST so /api routes take precedence.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
