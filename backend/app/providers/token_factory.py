"""AMD Token Factory provider — Deliverable #1 target.

OpenAI-compatible public API documented by AMD (Radeon Cloud docs):

* base URL defaults to ``https://developer.amd.com.cn/radeon/api/v1``
  (override with ``AMD_API_BASE`` — the event portal
  ``notebooks.amd.com/tokenfactory`` may publish its own base; confirm there).
* auth: ``Authorization: Bearer <AMD_API_KEY>`` (key is issued when the
  Token Factory console/profile is first opened).
* models: ``GET /v1/models`` is the source of truth for published ids;
  if ``AMD_MODEL`` is unset the provider discovers and caches the first
  published model id instead of hard-coding a remembered catalog.
* rate limits (AMD docs): ~30 req/min per key, 8 concurrent per key —
  honour ``Retry-After`` on 429 in callers.

Selection: ``AMD_PROVIDER=token-factory`` (explicit) or auto-selected when
its env vars are configured. Never silently swapped for the stub.
"""

from __future__ import annotations

from .base import OpenAICompatProvider


class TokenFactoryProvider(OpenAICompatProvider):
    name = "token-factory"
    backend_label = "amd"
    requires_key = True
    base_default = "https://developer.amd.com.cn/radeon/api/v1"
    env_base = "AMD_API_BASE"
    env_key = "AMD_API_KEY"
    env_model = "AMD_MODEL"
    env_timeout = "AMD_API_TIMEOUT"
