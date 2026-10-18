"""PacketExporter — sanitized JSON and Markdown exports of a review packet.

Exports contain packet fields and audit events only: no secrets, no provider
keys, no hidden prompts, no environment values.
"""

from __future__ import annotations

import json


class PacketExporter:
    @staticmethod
    def to_json(packet: dict) -> str:
        return json.dumps(packet, indent=2, ensure_ascii=False)

    @staticmethod
    def to_markdown(packet: dict, audit: list[dict]) -> str:
        inference = packet.get("amd_inference", {})
        lines = [
            f"# Manager Review Packet — {packet['case_id']}",
            "",
            f"- **Outcome:** {packet['outcome']}",
            f"- **Recommendation:** {packet.get('recommendation', 'n/a')}",
            f"- **Manager decision:** {packet['manager_decision']}",
            f"- **Policy version:** {packet['policy_version']}",
            f"- **Created (UTC):** {packet.get('created_at_utc', 'n/a')}",
            "",
            "## AMD inference provenance",
            "",
            f"- attempted: `{inference.get('attempted')}`",
            f"- verified: `{inference.get('verified')}`",
            f"- model: `{inference.get('model')}`",
            f"- request ref: `{inference.get('request_ref')}`",
            "",
            "## Draft response",
            "",
            packet.get("draft_response") or "_(no draft — review required)_",
            "",
            "## Evidence references",
            "",
        ]
        lines += [f"- {ref}" for ref in packet.get("evidence_refs", [])] or ["- none"]
        lines += ["", "## Missing facts / unknowns", ""]
        lines += [f"- {fact}" for fact in packet.get("missing_facts", [])] or ["- none"]
        lines += ["", "## Audit trail", ""]
        lines += [f"- `{e['ts']}` **{e['event']}** — {e['detail']}" for e in audit] or ["- none"]
        lines += [
            "",
            "---",
            "_Synthetic demo data only. Nothing was refunded, sent, or changed._",
            "",
        ]
        return "\n".join(lines)
