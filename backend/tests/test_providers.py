"""Provider interface tests — async client, fail-closed selection, provenance.

Covers the provider seam added for Deliverable #1 (Token Factory) and the
MI300X slot:

* selection: auto -> configured provider or explicit stub; unknown value fails closed
* explicit-but-unconfigured provider -> error result, no heuristic substitution
* async OpenAI-compatible client: success / 401 / unparseable content
* model discovery via GET /v1/models when AMD_MODEL is unset
* secrets never appear in status output or Deliverable #1 evidence
* Deliverable #1: blocked (exit 2, exact requirements) and verified (exit 0) paths

Run from ``backend/``:  python -m pytest -q
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from app import deliverable1
from app.model_adapter import ModelAdapter
from app.pipeline import Pipeline
from app.policy import PolicyEngine
from app.providers import (
    GPUProvider,
    InvalidProvider,
    StubProvider,
    TokenFactoryProvider,
    select_provider,
)
from app.store import ReviewStore

ROOT = Path(__file__).resolve().parents[2]

CSV_OK = "order_id,promised_time,actual_time,items,status,amount_usd\nA100,18:30,18:55,1x burger,completed,18.50\n"

PROVIDER_ENV = (
    "AMD_PROVIDER",
    "AMD_API_BASE",
    "AMD_API_KEY",
    "AMD_MODEL",
    "MI300X_API_BASE",
    "MI300X_API_KEY",
    "MI300X_MODEL",
)


def clear_env(monkeypatch) -> None:
    for var in PROVIDER_ENV:
        monkeypatch.delenv(var, raising=False)


def make_pipeline(adapter: ModelAdapter) -> Pipeline:
    return Pipeline(PolicyEngine(ROOT / "contracts"), adapter, ReviewStore())


# ------------------------------------------------------------ selection
def test_auto_selection_falls_back_to_stub(monkeypatch):
    clear_env(monkeypatch)
    provider = select_provider()
    assert isinstance(provider, StubProvider)
    assert provider.auto is True


def test_auto_selection_prefers_configured_token_factory(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("AMD_API_KEY", "unit-test-key-12345")
    provider = select_provider()
    assert isinstance(provider, TokenFactoryProvider)
    assert provider.configured is True


def test_explicit_stub_selection(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("AMD_PROVIDER", "stub")
    provider = select_provider()
    assert isinstance(provider, StubProvider)
    assert provider.auto is False


def test_unknown_provider_fails_closed(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("AMD_PROVIDER", "skynet")
    adapter = ModelAdapter()
    assert isinstance(adapter.provider, InvalidProvider)
    result = adapter.run("Order A100 was late.", ["A100"], True)
    assert result.attempted is False
    assert result.data is None
    assert result.error and "unknown AMD_PROVIDER" in result.error


def test_explicit_unconfigured_provider_never_silently_substitutes(monkeypatch):
    """AMD_PROVIDER=token-factory without a key -> fail closed, no heuristic draft."""
    clear_env(monkeypatch)
    monkeypatch.setenv("AMD_PROVIDER", "token-factory")
    adapter = ModelAdapter()
    result = adapter.run("Order A100 was late.", ["A100"], csv_present=True)
    assert result.attempted is False
    assert result.verified is False
    assert result.data is None  # no heuristic substitution
    assert result.error and "not configured" in result.error

    packet = make_pipeline(adapter).run(
        "Order A100 was late.", "synthetic-v0.1", order_csv=CSV_OK
    )["packet"]
    assert packet["outcome"] == "INFERENCE_FAILED"
    assert packet["draft_response"] is None
    assert packet["amd_inference"] == {
        "attempted": False,
        "verified": False,
        "model": None,
        "request_ref": None,
    }


def test_explicit_stub_still_runs_the_labelled_heuristic(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("AMD_PROVIDER", "stub")
    result = ModelAdapter().run("Order A100 was late.", ["A100"], True)
    assert result.backend == "local-heuristic"
    assert result.provider == "stub"
    assert result.attempted is False and result.verified is False and result.model is None
    assert result.data and result.data["draft_response"]


def test_mi300x_slot_is_ready_but_unconfigured(monkeypatch):
    clear_env(monkeypatch)
    provider = GPUProvider()
    assert provider.configured is False
    assert provider.missing_env() == ["MI300X_API_BASE"]
    monkeypatch.setenv("MI300X_API_BASE", "http://gpu-host.invalid:8000/v1")
    assert GPUProvider().configured is True
    monkeypatch.setenv("AMD_PROVIDER", "mi300x-gpu")
    assert isinstance(select_provider(), GPUProvider)


# ------------------------------------------------------------ async client
def _ok_handler(request: httpx.Request) -> httpx.Response:
    authed = request.headers.get("authorization") == "Bearer unit-test-key-12345"
    if request.url.path.endswith("/models"):
        if not authed:
            # Deliverable #1's unauthenticated reachability probe sees 401.
            return httpx.Response(401, json={"detail": "Login required"})
        return httpx.Response(
            200, json={"data": [{"id": "qwen3.8-27b"}, {"id": "deepseek-v4-flash"}]}
        )
    assert authed, "chat completion must carry the bearer key"
    body = json.loads(request.content)
    assert body["model"] == "qwen3.8-27b"  # discovered from /models (AMD_MODEL unset)
    content = json.dumps(
        {
            "issue_type": "late_fulfillment",
            "order_ids": ["A100"],
            "missing_facts": [],
            "draft_response": "We are sorry about the delay; a manager will follow up.",
        }
    )
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-unit-1",
            "model": "qwen3.8-27b",
            "choices": [{"message": {"role": "assistant", "content": content}}],
        },
    )


def make_adapter(handler) -> ModelAdapter:
    transport = httpx.MockTransport(handler)
    provider = TokenFactoryProvider(
        base="https://tf.test/radeon/api/v1",
        api_key="unit-test-key-12345",
        transport=transport,
    )
    return ModelAdapter(provider=provider)


def test_async_client_success_with_model_discovery():
    result = make_adapter(_ok_handler).run("My pickup was late. Order ID A100.", ["A100"], False)
    assert result.attempted is True
    assert result.verified is True
    assert result.model == "qwen3.8-27b"
    assert result.request_ref == "chatcmpl-unit-1"
    assert result.backend == "amd"
    assert result.provider == "token-factory"
    assert result.data and result.data["issue_type"] == "late_fulfillment"


def test_async_client_401_is_honest_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Login required"})

    provider = TokenFactoryProvider(
        base="https://tf.test/v1",
        api_key="unit-test-key-12345",
        model="qwen3.8-27b",
        transport=httpx.MockTransport(handler),
    )
    result = ModelAdapter(provider).run("Order A100 was late.", ["A100"], True)
    assert result.attempted is True  # HTTP was issued
    assert result.verified is False
    assert result.data is None
    assert result.error and "401" in result.error


def test_async_client_unparseable_content_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.8-27b"}]})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-unit-2",
                "model": "qwen3.8-27b",
                "choices": [{"message": {"content": "Sure, I will refund that!"}}],
            },
        )

    provider = TokenFactoryProvider(
        base="https://tf.test/v1",
        api_key="unit-test-key-12345",
        transport=httpx.MockTransport(handler),
    )
    result = ModelAdapter(provider).run("Order A100 was late.", ["A100"], True)
    assert result.attempted is True
    assert result.verified is False
    assert result.error == "provider returned an unparseable response"


def test_arun_works_without_external_loop():
    adapter = make_adapter(_ok_handler)
    result = asyncio.run(adapter.arun("Order A100 was late.", ["A100"], True))
    assert result.verified is True


# ------------------------------------------------------------ secrecy
def test_status_never_contains_the_api_key(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("AMD_API_KEY", "unit-test-key-12345")
    adapter = ModelAdapter()
    text = json.dumps(adapter.status()) + json.dumps(adapter.provider_slots())
    assert "unit-test-key-12345" not in text
    assert adapter.status()["missing_env"] == []


# ------------------------------------------------------------ Deliverable #1
def test_deliverable1_blocked_reports_exact_requirements(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    # Reachability probe still runs — use the real endpoint only if reachable;
    # offline-safe: inject a transport that answers 401 like the live service.
    provider = TokenFactoryProvider(
        explicit=True,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"detail": "Login required"})
        ),
    )
    out = tmp_path / "evidence.json"
    code, evidence = asyncio.run(deliverable1.run(provider=provider, out_path=out))
    assert code == 2
    assert evidence["verification"]["status"] == "blocked_missing_config"
    needs = evidence["verification"]["needs_from_organizer"]
    assert any("AMD_API_KEY" in item for item in needs)
    assert evidence["probe"]["auth_required"] is True
    assert evidence["probe"]["reachable"] is True
    written = out.read_text("utf-8")
    assert "unit-test" not in written  # no key material (none set) — and structure intact
    assert json.loads(written)["deliverable"] == "1"


def test_deliverable1_verified_run_writes_sanitized_evidence(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    provider = TokenFactoryProvider(
        explicit=True,
        api_key="unit-test-key-12345",
        transport=httpx.MockTransport(_ok_handler),
    )
    out = tmp_path / "evidence.json"
    code, evidence = asyncio.run(deliverable1.run(provider=provider, out_path=out))
    assert code == 0
    assert evidence["verification"]["verified"] is True
    assert evidence["models"]["selected"]["id"] == "qwen3.8-27b"
    assert evidence["chat"]["response"]["http_status"] == 200
    assert evidence["chat"]["response"]["model"] == "qwen3.8-27b"
    assert evidence["chat"]["started_at_utc"].endswith("Z")
    assert evidence["chat"]["finished_at_utc"].endswith("Z")

    written = out.read_text("utf-8")
    assert "unit-test-key-12345" not in written  # key never reaches disk
    assert "Bearer [REDACTED]" in written
    parsed = json.loads(written)
    assert parsed["chat"]["request"]["messages"][0]["role"] == "user"
    assert parsed["provider"] == "token-factory"
    assert parsed["endpoint"].startswith("https://")


def test_deliverable1_failed_chat_maps_auth_to_blocked(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "qwen3.8-27b",
                            "version": "2025-10-01",
                            "owned_by": "amd",
                        }
                    ]
                },
            )
        return httpx.Response(401, json={"detail": "Login required"})

    provider = TokenFactoryProvider(
        explicit=True,
        api_key="unit-test-key-12345",
        transport=httpx.MockTransport(handler),
    )
    out = tmp_path / "evidence.json"
    code, evidence = asyncio.run(deliverable1.run(provider=provider, out_path=out))
    assert code == 2
    assert evidence["verification"]["status"] == "chat_call_failed"
    assert evidence["verification"]["http_status"] == 401
    assert evidence["models"]["selected"]["version"] == "2025-10-01"
    assert "unit-test-key-12345" not in out.read_text("utf-8")


# ------------------------------------------------------------ transient retries
def test_429_is_retried_with_backoff_then_succeeds():
    """A transient 429 (shared capacity) is retried; the call then succeeds."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.8-27b"}]})
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(
                429,
                json={"detail": {"error": {"message": "at its concurrency limit"}}},
                headers={"Retry-After": "0"},
            )
        content = json.dumps(
            {
                "issue_type": "late_fulfillment",
                "order_ids": ["A100"],
                "missing_facts": [],
                "draft_response": "We are sorry about the delay; a manager will follow up.",
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-retry-1",
                "model": "qwen3.8-27b",
                "choices": [{"message": {"content": content}}],
            },
        )

    provider = TokenFactoryProvider(
        base="https://tf.test/v1",
        api_key="unit-test-key-12345",
        transport=httpx.MockTransport(handler),
    )
    result = ModelAdapter(provider).run("Order A100 was late.", ["A100"], True)
    assert calls["n"] == 3  # two 429s + one success
    assert result.verified is True
    assert result.request_ref == "chatcmpl-retry-1"


def test_persistent_429_fails_after_bounded_attempts():
    """Retries are bounded: persistent 429 fails honestly, no infinite loop."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.8-27b"}]})
        calls["n"] += 1
        return httpx.Response(
            429,
            json={"detail": {"error": {"message": "at its concurrency limit"}}},
            headers={"Retry-After": "0"},
        )

    provider = TokenFactoryProvider(
        base="https://tf.test/v1",
        api_key="unit-test-key-12345",
        transport=httpx.MockTransport(handler),
    )
    result = ModelAdapter(provider).run("Order A100 was late.", ["A100"], True)
    assert calls["n"] == provider.max_attempts  # bounded
    assert result.attempted is True
    assert result.verified is False
    assert result.data is None  # no heuristic substitution
    assert result.error and "429" in result.error


def test_401_is_never_retried():
    """Auth failures are terminal — a bad key must not burn retries."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"detail": "Login required"})

    provider = TokenFactoryProvider(
        base="https://tf.test/v1",
        api_key="unit-test-key-12345",
        model="qwen3.8-27b",
        transport=httpx.MockTransport(handler),
    )
    result = ModelAdapter(provider).run("Order A100 was late.", ["A100"], True)
    assert calls["n"] == 1  # single attempt, no retry on 401
    assert result.verified is False


def test_retry_delay_honours_retry_after_header():
    provider = TokenFactoryProvider(base="https://tf.test/v1", api_key="unit-test-key-12345")
    resp = httpx.Response(429, headers={"Retry-After": "7"})
    assert provider._retry_delay(resp, attempt=1) == 7.0
    no_header = httpx.Response(429)
    assert provider._retry_delay(no_header, attempt=1) == 1.0   # 2^0
    assert provider._retry_delay(no_header, attempt=2) == 2.0   # 2^1
    capped = httpx.Response(429, headers={"Retry-After": "9999"})
    assert provider._retry_delay(capped, attempt=1) == provider.retry_backoff_cap_s
