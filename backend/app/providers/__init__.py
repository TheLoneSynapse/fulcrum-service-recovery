"""Inference providers for Fulcrum Service Recovery.

Public surface: ``select_provider()`` plus the provider classes. See
``base.py`` for the contract and the no-silent-fallback rules.
"""

from __future__ import annotations

from .base import (
    InvalidProvider,
    ModelsResult,
    OpenAICompatProvider,
    Provider,
    ProviderError,
    ProviderResponse,
    UnknownProvider,
    all_provider_statuses,
    run_sync,
    select_provider,
)
from .gpu import GPUProvider
from .stub import StubProvider
from .token_factory import TokenFactoryProvider

__all__ = [
    "Provider",
    "OpenAICompatProvider",
    "InvalidProvider",
    "TokenFactoryProvider",
    "GPUProvider",
    "StubProvider",
    "ProviderResponse",
    "ModelsResult",
    "ProviderError",
    "UnknownProvider",
    "run_sync",
    "select_provider",
    "all_provider_statuses",
]
