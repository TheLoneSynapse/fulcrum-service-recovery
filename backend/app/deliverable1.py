"""Deliverable #1 — a real AMD Token Factory call with sanitized evidence.

Run from ``backend/``:

    python -m app.deliverable1            # probe + models + chat + write evidence
    python -m app.deliverable1 --stdout   # print evidence instead of writing

What it does, in order:

1. unauthenticated reachability probe of ``GET {base}/models`` (records the
   HTTP status so a 401 "Login required" is distinguished from an outage),
2. authenticated ``GET /v1/models`` → model name/version actually published,
3. one minimal ``POST /v1/chat/completions`` call,
4. writes a sanitized evidence record: endpoint, model name/version,
   UTC timestamps, latency, request-id, redacted request and result.
   The API key is NEVER written anywhere (the evidence text is scanned for
   the key value before saving; any hit aborts the write).

Exit codes:
    0  verified call (2xx + parseable completion)
    2  blocked — missing key/endpoint (prints exactly what is needed)
    1  attempted but failed (HTTP/transport/parse error)

Evidence lands in ``docs/evidence/deliverable1_token_factory.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .providers import TokenFactoryProvider

ROOT = Path(__file__).resolve().parents[2]  # .../fulcrum-service-recovery-main
EVIDENCE_PATH = ROOT / "docs" / "evidence" / "deliverable1_token_factory.json"

_PROBE_PROMPT = (
    "Reply with exactly one sentence confirming you are running on AMD "
    "infrastructure. Then stop."
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _redact(value: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            value = value.replace(secret, "[REDACTED]")
    return value


def _model_summary(entry: dict) -> dict:
    """Published model identity: id + any version-ish fields the API returns."""
    return {
        "id": entry.get("id"),
        "version": entry.get("version") or entry.get("model_version") or None,
        "owned_by": entry.get("owned_by") or None,
        "created": entry.get("created") or None,
    }


async def _probe(provider: TokenFactoryProvider) -> dict:
    result = await provider.probe()
    return {
        "path": "/models (unauthenticated)",
        "http_status": result.status_code,
        "reachable": result.status_code is not None and result.status_code < 500,
        "auth_required": result.status_code in (401, 403),
        "error": result.error,
    }


async def run(
    stdout_only: bool = False,
    provider: TokenFactoryProvider | None = None,
    out_path: Path | None = None,
) -> tuple[int, dict]:
    """Run Deliverable #1. ``provider``/``out_path`` exist for offline tests."""
    provider = provider or TokenFactoryProvider(explicit=True)
    out_path = out_path or EVIDENCE_PATH
    secrets = [provider.api_key]
    evidence: dict = {
        "deliverable": "1",
        "title": "AMD Token Factory — first verified call",
        "provider": provider.name,
        "endpoint": provider.base or None,
        "requested_at_utc": _utc_now(),
        "probe": None,
        "models": None,
        "chat": None,
        "verification": {"verified": False, "status": None, "reasons": []},
    }

    # ---------------------------------------------------------- 0) config
    missing = provider.missing_env()
    if missing:
        evidence["verification"] = {
            "verified": False,
            "status": "blocked_missing_config",
            "reasons": [f"missing environment variable(s): {', '.join(missing)}"],
            "needs_from_organizer": [
                "A Token Factory API key -> set as AMD_API_KEY (issued when the "
                "Token Factory console/profile is first opened; see "
                "notebooks.amd.com/tokenfactory and the Radeon Cloud auth docs).",
                "Confirmation that the OpenAI-compatible base URL matches the "
                f"event portal. Default in this repo: {provider.base_default} "
                "(override with AMD_API_BASE if the portal publishes another).",
                "Optionally AMD_MODEL naming the model/version to request; "
                "unset means this script picks the first id from GET /v1/models.",
            ],
        }
        evidence["probe"] = await _probe(provider)
        return _finish(evidence, 2, secrets, stdout_only, out_path)

    # ---------------------------------------------------------- 1) probe
    evidence["probe"] = await _probe(provider)

    # ---------------------------------------------------------- 2) models
    listing = await provider.list_models()
    if not listing.ok:
        evidence["verification"] = {
            "verified": False,
            "status": "models_listing_failed",
            "reasons": [listing.error or "GET /v1/models failed"],
            "http_status": listing.status_code,
        }
        code = 2 if listing.status_code in (401, 403) else 1
        return _finish(evidence, code, secrets, stdout_only, out_path)

    entries = listing.models
    chosen = None
    if provider.model:
        chosen = next(
            (m for m in entries if str(m.get("id")) == provider.model),
            {"id": provider.model},
        )
    else:
        chosen = entries[0]
        provider._resolved_model = str(chosen.get("id"))
    evidence["models"] = {
        "published_count": len(entries),
        "published_ids": [str(m.get("id")) for m in entries[:20]],
        "selected": _model_summary(chosen if isinstance(chosen, dict) else {"id": chosen}),
    }

    # ---------------------------------------------------------- 3) chat
    model_id = str(chosen.get("id") if isinstance(chosen, dict) else chosen)
    payload = {
        "model": model_id,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": _PROBE_PROMPT}],
    }
    started = _utc_now()
    response = await provider.run_completion(payload)
    finished = _utc_now()

    evidence["chat"] = {
        "request": {
            "url": f"{provider.base}/chat/completions",
            "model": model_id,
            "temperature": payload["temperature"],
            "messages": [
                {"role": m["role"], "content": m["content"]} for m in payload["messages"]
            ],
            "auth": "Authorization: Bearer [REDACTED]" if provider.api_key else "none",
        },
        "response": {
            "http_status": response.status_code,
            "model": response.model,
            "request_ref": response.request_ref,
            "latency_ms": response.latency_ms,
            "content": (response.content or "")[:500],
            "error": response.error,
        },
        "started_at_utc": started,
        "finished_at_utc": finished,
    }

    if not response.ok:
        evidence["verification"] = {
            "verified": False,
            "status": "chat_call_failed",
            "reasons": [response.error or "chat completion failed"],
            "http_status": response.status_code,
        }
        code = 2 if response.status_code in (401, 403) else 1
        return _finish(evidence, code, secrets, stdout_only, out_path)

    evidence["verification"] = {
        "verified": True,
        "status": "verified",
        "reasons": [
            f"GET /models -> HTTP {listing.status_code} ({len(entries)} published)",
            f"POST /chat/completions -> HTTP {response.status_code}, "
            f"model={response.model or model_id}, ref={response.request_ref or 'n/a'}",
        ],
    }
    return _finish(evidence, 0, secrets, stdout_only, out_path)


def _finish(
    evidence: dict,
    code: int,
    secrets: list[str],
    stdout_only: bool,
    out_path: Path,
) -> tuple[int, dict]:
    evidence["completed_at_utc"] = _utc_now()
    text = json.dumps(evidence, indent=2, ensure_ascii=False)
    # Hard guarantee: the key value never reaches disk or stdout.
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in text:
            text = _redact(text, [secret])
    if stdout_only:
        print(text)
        return code, evidence
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text + "\n", encoding="utf-8")
    status = evidence["verification"]["status"]
    print(f"deliverable1: status={status} exit={code}")
    print(f"evidence: {out_path}")
    if status == "blocked_missing_config":
        print("\nBLOCKED — exact requirements:")
        for item in evidence["verification"].get("needs_from_organizer", []):
            print(f"  - {item}")
    elif status == "verified":
        chat = evidence.get("chat", {})
        models = evidence.get("models", {})
        sel = (models or {}).get("selected", {})
        print(f"model: {sel.get('id')} (version={sel.get('version')})")
        print(f"served: {chat['response'].get('model')} ref={chat['response'].get('request_ref')}")
        print(f"time: {chat.get('started_at_utc')} -> {chat.get('finished_at_utc')} "
              f"({chat['response'].get('latency_ms')} ms)")
    return code, evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deliverable #1: verified Token Factory call")
    parser.add_argument("--stdout", action="store_true", help="print evidence instead of writing it")
    args = parser.parse_args(argv)
    code, _ = asyncio.run(run(stdout_only=args.stdout))
    return code


if __name__ == "__main__":
    sys.exit(main())
