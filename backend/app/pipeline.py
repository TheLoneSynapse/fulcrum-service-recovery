"""Pipeline — orchestrates intake → inference → validation → packet.

Flow (scope doc):
  Text/CSV/Policy -> IntakeCase -> AMD inference (extract + draft)
    -> deterministic validator -> ManagerReviewPacket -> human decision -> export

Every packet carries ``amd_inference`` provenance. If a configured provider
call fails, the outcome is INFERENCE_FAILED and no draft is produced — never
a fake inference claim.
"""

from __future__ import annotations

from .exporter import PacketExporter
from .intake import IntakeCase, MalformedCSV, normalize, parse_order_csv, utc_now
from .model_adapter import ModelAdapter
from .policy import PolicyEngine, UnknownPolicy
from .store import ReviewStore
from .validator import validate

RECOMMENDATION = {
    "SUPPORTED_DRAFT": "APPROVE_DRAFT",
    "REVIEW_REQUIRED": "MANUAL_REVIEW",
    "INFERENCE_FAILED": "FIX_INFERENCE",
}


class Pipeline:
    def __init__(self, policy_engine: PolicyEngine, model: ModelAdapter, store: ReviewStore):
        self.policies = policy_engine
        self.model = model
        self.store = store

    def run(
        self,
        complaint_text: str,
        policy_version: str,
        source: str = "web_form",
        order_csv: str | None = None,
        force_failure: bool = False,
    ) -> dict:
        """Run the full pipeline and persist the case. Returns {packet, audit}."""
        intake = normalize(complaint_text, policy_version, source)
        rows = parse_order_csv(order_csv)  # raises MalformedCSV (bounded)
        policy = self.policies.load(policy_version)  # raises UnknownPolicy (fail closed)

        model_result = self.model.run(
            intake.complaint_text,
            intake.cited_order_ids,
            csv_present=bool(rows),
            force_failure=force_failure,
        )
        checks = validate(intake, rows, policy)

        audit = [
            {
                "ts": intake.created_at_utc,
                "event": "case_created",
                "detail": (
                    f"source={source} policy={policy_version} "
                    f"csv_rows={len(rows)} backend={model_result.backend} "
                    f"provider={model_result.provider}"
                ),
            },
            {
                "ts": utc_now(),
                "event": "inference",
                "detail": (
                    f"backend={model_result.backend} provider={model_result.provider} "
                    f"attempted={model_result.attempted} "
                    f"verified={model_result.verified}"
                    + (f" error={model_result.error}" if model_result.error else "")
                ),
            },
            {
                "ts": utc_now(),
                "event": "validation",
                "detail": (
                    f"route={'REVIEW_REQUIRED' if checks.route != 'SUPPORTED_DRAFT' else 'SUPPORTED_DRAFT'} "
                    f"reasons={','.join(checks.reasons) or 'none'}"
                ),
            },
        ]

        evidence_refs = list(checks.evidence_refs)
        missing_facts = list(checks.missing_facts)
        reasons = list(checks.reasons)

        if model_result.error:
            outcome = "INFERENCE_FAILED"
            draft = None
            missing_facts.append(f"inference unavailable: {model_result.error}")
            evidence_refs.append("inference: provider failure — no draft generated (no fake claim)")
        elif checks.route == "REVIEW_REQUIRED":
            outcome = "REVIEW_REQUIRED"
            draft = None
            missing_facts.append("routing reasons: " + ", ".join(reasons))
        else:
            outcome = "SUPPORTED_DRAFT"
            draft = (model_result.data or {}).get("draft_response")
            if not draft:
                outcome = "REVIEW_REQUIRED"
                draft = None
                reasons.append("model_returned_no_draft")
                missing_facts.append("model returned no draft_response")
            for fact in (model_result.data or {}).get("missing_facts", []) or []:
                if fact not in missing_facts:
                    missing_facts.append(fact)

        packet = {
            "case_id": intake.case_id,
            "outcome": outcome,
            "draft_response": draft,
            "evidence_refs": evidence_refs,
            "missing_facts": missing_facts,
            "policy_version": policy_version,
            "recommendation": RECOMMENDATION[outcome],
            "created_at_utc": intake.created_at_utc,
            "manager_decision": "PENDING",
            "amd_inference": model_result.provenance,
        }
        context = {
            "complaint_text": intake.complaint_text,
            "order_csv": order_csv,
            "policy_version": policy_version,
            "source": source,
            "backend": model_result.backend,
            "routing_reasons": reasons,
        }
        entry = self.store.create(packet, audit, context)
        return {"packet": entry["packet"], "audit": entry["audit"]}

    def export(self, case_id: str, fmt: str) -> tuple[str, str, str]:
        """Returns (body, media_type, filename) for json/md export."""
        entry = self.store.get(case_id)
        packet, audit = entry["packet"], entry["audit"]
        if fmt == "md":
            return (
                PacketExporter.to_markdown(packet, audit),
                "text/markdown; charset=utf-8",
                f"{case_id}.md",
            )
        return (
            PacketExporter.to_json(packet),
            "application/json; charset=utf-8",
            f"{case_id}.json",
        )


__all__ = ["Pipeline", "MalformedCSV", "UnknownPolicy", "IntakeCase"]
