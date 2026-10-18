"""Provider interface — one async contract for every inference backend.

Two real AMD backends implement it today:

* ``TokenFactoryProvider`` — the AMD Token Factory public OpenAI-compatible
  API (Deliverable #1 target).
* ``GPUProvider`` — dedicated GPU endpoint slot for the MI300X instance once
  the lab confirms it is available (same OpenAI-compatible wire protocol,
  different endpoint + env vars).

* ``StubProvider`` is the explicit stub mode: a deterministic local heuristic
  that never claims a model call (``attempted=False, verified=False,
  model=None``).

Selection is environment-driven (``AMD_PROVIDER``) and **never silent**: an
explicitly selected provider that is not configured fails closed with a clear
error instead of falling back to the stub, and unknown selections fail closed
too (ground rule: no silent mock fallback on the judged path).
"""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


@dataclass
class ProviderResponse:
    """Outcome of one chat-completion call. Never carries secrets."""

    ok: bool
    status_code: Optional[int] = None
    content: Optional[str] = None
    model: Optional[str] = None  # model name/version as served
    request_ref: Optional[str] = None
    error: Optional[str] = None
    latency_ms: Optional[float] = None
    attempts: int = 1


@dataclass
class ModelsResult:
    """Outcome of GET /v1/models."""

    ok: bool
    status_code: Optional[int] = None
    models: list[dict] = field(default_factory=list)
    error: Optional[str] = None


class ProviderError(RuntimeError):
    pass


class UnknownProvider(ProviderError):
    pass


def run_sync(coro: Any) -> Any:
    """Run a provider coroutine from synchronous pipeline code.

    Works both outside a loop (``asyncio.run``) and from inside a running
    loop (dedicated thread with its own loop), so sync FastAPI endpoints,
    the eval runner and async callers can all use the same client.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _bounded(text: Optional[str], limit: int = 160) -> str:
    if not text:
        return ""
    return " ".join(text.split())[:limit]


class Provider(ABC):
    """Async inference provider contract."""

    name: str = "provider"
    backend_label: str = "amd"  # what ModelResult.backend reports
    is_stub: bool = False
    requires_key: bool = True
    base_default: str = ""
    env_base: str = ""
    env_key: str = ""
    env_model: str = ""
    env_timeout: str = ""

    def __init__(
        self,
        base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        explicit: bool = False,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base = (
            (base if base is not None else os.environ.get(self.env_base) or "")
            or self.base_default
        ).rstrip("/")
        self.api_key = (
            api_key if api_key is not None else os.environ.get(self.env_key) or ""
        )
        self.model = (
            model if model is not None else os.environ.get(self.env_model) or ""
        )
        try:
            self.timeout = float(
                timeout
                if timeout is not None
                else (os.environ.get(self.env_timeout) or 30)
            )
        except ValueError:
            self.timeout = 30.0
        self.explicit = explicit
        self.selection_error: Optional[str] = None
        self._transport = transport
        self._resolved_model: Optional[str] = None

    # ------------------------------------------------------------- state
    @property
    def configured(self) -> bool:
        return bool(self.base) and (not self.requires_key or bool(self.api_key))

    def missing_env(self) -> list[str]:
        missing: list[str] = []
        if not self.base:
            missing.append(self.env_base)
        if self.requires_key and not self.api_key:
            missing.append(self.env_key)
        return missing

    @property
    def resolved_model(self) -> Optional[str]:
        return self.model or self._resolved_model

    # ------------------------------------------------------------- transport
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def list_models(self) -> ModelsResult:
        """GET {base}/models — source of truth for published model ids."""
        if not self.base:
            return ModelsResult(ok=False, error=f"no endpoint configured ({self.env_base})")
        try:
            async with self._client() as client:
                resp = await client.get(f"{self.base}/models", headers=self._headers())
        except httpx.TimeoutException:
            return ModelsResult(ok=False, error=f"model listing timed out after {self.timeout:g}s")
        except httpx.HTTPError as exc:
            return ModelsResult(
                ok=False, error=f"{type(exc).__name__}: model listing connection failed"
            )
        if resp.status_code >= 400:
            return ModelsResult(
                ok=False,
                status_code=resp.status_code,
                error=f"HTTP {resp.status_code} {_bounded(resp.text)}".strip(),
            )
        try:
            body = resp.json()
            raw = body.get("data") if isinstance(body, dict) else body
            models = [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []
        except ValueError:
            return ModelsResult(
                ok=False, status_code=resp.status_code, error="provider returned non-JSON model list"
            )
        if not models:
            return ModelsResult(
                ok=False, status_code=resp.status_code, error="provider returned an empty model list"
            )
        return ModelsResult(ok=True, status_code=resp.status_code, models=models)

    async def probe(self) -> ModelsResult:
        """Unauthenticated reachability probe (used by Deliverable #1 evidence)."""
        original_key, self.api_key = self.api_key, ""
        try:
            return await self.list_models()
        finally:
            self.api_key = original_key

    async def complete(self, payload: dict) -> ProviderResponse:
        """POST {base}/chat/completions with an OpenAI-style payload.

        Transient capacity signals (HTTP 429, 5xx) are retried with bounded
        exponential backoff, honouring the ``Retry-After`` header when the
        service sends one (AMD docs: shared capacity, honour Retry-After on
        429). Auth and validation errors (401/403/4xx others) are never
        retried, and the attempt count is reported on the response.
        """
        if not self.base:
            return ProviderResponse(ok=False, error=f"no endpoint configured ({self.env_base})")
        url = f"{self.base}/chat/completions"
        headers = self._headers()
        attempt = 0
        resp = None
        while True:
            attempt += 1
            started = time.perf_counter()
            try:
                async with self._client() as client:
                    resp = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException:
                # Timeout already consumed the full wait budget — not retried.
                return ProviderResponse(
                    ok=False,
                    error=f"provider timeout after {self.timeout:g}s",
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    attempts=attempt,
                )
            except httpx.HTTPError as exc:
                # Transport-level failure: the request WAS issued, no fake claim either way.
                return ProviderResponse(
                    ok=False,
                    error=f"{type(exc).__name__}: provider call failed",
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    attempts=attempt,
                )
            latency = round((time.perf_counter() - started) * 1000, 2)
            if resp.status_code >= 400:
                transient = resp.status_code == 429 or resp.status_code >= 500
                if transient and attempt < self.max_attempts:
                    await asyncio.sleep(self._retry_delay(resp, attempt))
                    continue
                detail = _bounded(resp.text)
                return ProviderResponse(
                    ok=False,
                    status_code=resp.status_code,
                    error=f"HTTP {resp.status_code}" + (f" {detail}" if detail else ""),
                    latency_ms=latency,
                    attempts=attempt,
                )
            break
        try:
            body: Any = resp.json()
        except ValueError:
            return ProviderResponse(
                ok=False, status_code=resp.status_code, error="provider returned non-JSON body",
                latency_ms=latency,
            )
        if not isinstance(body, dict):
            return ProviderResponse(
                ok=False, status_code=resp.status_code, error="provider returned unexpected body shape",
                latency_ms=latency,
            )
        choices = body.get("choices")
        content: Optional[str] = None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            if isinstance(message, dict):
                content = message.get("content")
        if content is None:
            return ProviderResponse(
                ok=False, status_code=resp.status_code, error="provider returned no completion content",
                latency_ms=latency,
            )
        return ProviderResponse(
            ok=True,
            status_code=resp.status_code,
            content=content,
            model=body.get("model") or payload.get("model"),
            request_ref=body.get("id") or resp.headers.get("x-request-id"),
            latency_ms=latency,
            attempts=attempt,
        )

    async def discover_model(self) -> Optional[str]:
        """Resolve a model id from GET /v1/models (cached per instance)."""
        if self.resolved_model:
            return self.resolved_model
        listing = await self.list_models()
        if not listing.ok:
            raise ProviderError(listing.error or "model discovery failed")
        for entry in listing.models:
            model_id = entry.get("id")
            if isinstance(model_id, str) and model_id:
                self._resolved_model = model_id
                return model_id
        raise ProviderError("model discovery returned no usable model id")

    # ------------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "provider": self.name,
            "configured": self.configured,
            "explicit": self.explicit,
            "endpoint_configured": bool(self.base),
            "model": self.resolved_model or None,
            "missing": self.missing_env(),
            "selection_error": self.selection_error,
        }

    @abstractmethod
    async def run_completion(self, payload: dict) -> ProviderResponse:
        """Provider-specific completion entry point (keeps stub off the HTTP path)."""


class OpenAICompatProvider(Provider):
    """Shared implementation for OpenAI-compatible HTTP providers."""

    # Bounded retry for transient capacity signals only (429/5xx).
    max_attempts: int = 3
    retry_backoff_s: float = 1.0
    retry_backoff_cap_s: float = 30.0

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return min(max(float(raw), 0.0), self.retry_backoff_cap_s)
            except ValueError:
                pass  # HTTP-date form: fall back to exponential backoff
        return min(self.retry_backoff_s * (2 ** (attempt - 1)), self.retry_backoff_cap_s)

    async def run_completion(self, payload: dict) -> ProviderResponse:
        return await self.complete(payload)


class InvalidProvider(Provider):
    """Unknown AMD_PROVIDER value — fail closed, never fall back silently."""

    requires_key = False
    backend_label = "none"
    env_base = env_key = env_model = env_timeout = ""

    def __init__(self, choice: str, explicit: bool = True) -> None:
        super().__init__(explicit=explicit)
        self.name = choice or "invalid"
        self.base = ""
        self.api_key = ""
        self.model = ""
        self.selection_error = (
            f"unknown AMD_PROVIDER '{choice}' — valid values: "
            "auto | token-factory | mi300x-gpu | stub"
        )

    @property
    def configured(self) -> bool:
        return False

    def missing_env(self) -> list[str]:
        return []

    async def run_completion(self, payload: dict) -> ProviderResponse:
        return ProviderResponse(ok=False, error=self.selection_error)


_VALID_CHOICES = {"token-factory", "tokenfactory", "tf", "mi300x-gpu", "mi300x", "gpu", "stub", "local-heuristic", "heuristic"}


def select_provider() -> Provider:
    """Resolve the active provider from AMD_PROVIDER (default: auto)."""
    from .gpu import GPUProvider
    from .stub import StubProvider
    from .token_factory import TokenFactoryProvider

    raw = (os.environ.get("AMD_PROVIDER") or "").strip()
    choice = raw.lower().replace("_", "-")
    if choice in ("", "auto", "automatic"):
        for cls in (TokenFactoryProvider, GPUProvider):
            provider = cls()
            if provider.configured:
                return provider
        return StubProvider(auto=True)
    if choice in ("token-factory", "tokenfactory", "tf"):
        return TokenFactoryProvider(explicit=True)
    if choice in ("mi300x-gpu", "mi300x", "gpu"):
        return GPUProvider(explicit=True)
    if choice in ("stub", "local-heuristic", "heuristic"):
        return StubProvider(explicit=True)
    return InvalidProvider(raw, explicit=True)


def all_provider_statuses() -> dict:
    """Safe, secret-free status for every provider slot (for /api/status)."""
    from .gpu import GPUProvider
    from .stub import StubProvider
    from .token_factory import TokenFactoryProvider

    return {
        "token-factory": TokenFactoryProvider().status(),
        "mi300x-gpu": GPUProvider().status(),
        "stub": StubProvider().status(),
    }


__all__ = [
    "Provider",
    "OpenAICompatProvider",
    "InvalidProvider",
    "ProviderResponse",
    "ModelsResult",
    "ProviderError",
    "UnknownProvider",
    "run_sync",
    "select_provider",
    "all_provider_statuses",
]
