"""MI300X / dedicated GPU provider slot.

Same OpenAI-compatible wire protocol as Token Factory, but pointed at a
dedicated instance endpoint (vLLM/SGLang behind ``/v1``) once the lab
confirms the MI300X allocation. Nothing to configure yet — selection via
``AMD_PROVIDER=mi300x-gpu`` fails closed until the env vars are set:

* ``MI300X_API_BASE`` — e.g. ``http://<gpu-host>:8000/v1``
* ``MI300X_API_KEY`` — optional for open instances; a missing key is
  reported through ``missing_env()`` so status stays honest either way.
* ``MI300X_MODEL`` — served model name/version (or discovered via /v1/models).

The lab lead confirms availability later; until then this provider simply
reports ``configured=False`` and the interface is ready for it.
"""

from __future__ import annotations

from .base import OpenAICompatProvider


class GPUProvider(OpenAICompatProvider):
    name = "mi300x-gpu"
    backend_label = "amd"
    requires_key = False  # dedicated instances are often keyless on a private host
    base_default = ""
    env_base = "MI300X_API_BASE"
    env_key = "MI300X_API_KEY"
    env_model = "MI300X_MODEL"
    env_timeout = "MI300X_API_TIMEOUT"
