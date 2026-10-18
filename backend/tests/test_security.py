"""Security hardening tests: disabled public docs, hardening headers, and the
per-client creation rate limit that bounds AMD-quota use on the unauthenticated
demo API.

Run from ``backend/``:  python -m pytest -q
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main
from app.main import app

client = TestClient(app)

CSV_OK = "order_id,promised_time,actual_time,items,status,amount_usd\nA100,18:30,18:55,1x burger,completed,18.50\n"


def _create() -> "object":
    return client.post(
        "/api/cases",
        json={
            "complaint_text": "My pickup was late. Order ID A100.",
            "order_csv": CSV_OK,
            "policy_version": "synthetic-v0.1",
        },
    )


# ------------------------------------------------------------ surface hiding
def test_interactive_docs_and_schema_are_disabled():
    """/docs, /redoc and /openapi.json must not advertise the API surface."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_security_headers_present_on_api_and_static():
    for path in ("/health", "/", "/api/status"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert res.headers.get("X-Content-Type-Options") == "nosniff", path
        assert res.headers.get("X-Frame-Options") == "DENY", path
        assert res.headers.get("Referrer-Policy") == "same-origin", path


# ------------------------------------------------------------ creation quota
def test_creation_rate_limit_trips_and_advertises_retry_after(monkeypatch):
    """After FULCRUM_RATE_LIMIT creations in a window, the next is a 429 with
    Retry-After — the guard runs before the pipeline, so no AMD call is spent."""
    main._rate_windows.clear()
    monkeypatch.setenv("FULCRUM_RATE_LIMIT", "2")
    try:
        assert _create().status_code == 200
        assert _create().status_code == 200
        blocked = _create()
        assert blocked.status_code == 429
        detail = blocked.json()["detail"]
        assert detail["error"] == "rate_limited"
        assert int(blocked.headers["Retry-After"]) >= 1
        # batch endpoint shares the same quota
        batch = client.post(
            "/api/cases/batch",
            json={"complaints": ["Order A100 was late."], "policy_version": "synthetic-v0.1"},
        )
        assert batch.status_code == 429
    finally:
        main._rate_windows.clear()


def test_invalid_rate_limit_env_falls_back_to_sane_default(monkeypatch):
    monkeypatch.setenv("FULCRUM_RATE_LIMIT", "not-a-number")
    assert main._creation_limit() == 60
    monkeypatch.setenv("FULCRUM_RATE_LIMIT", "0")
    assert main._creation_limit() >= 1
