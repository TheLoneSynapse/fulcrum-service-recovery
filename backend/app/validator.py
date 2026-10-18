"""Deterministic validator — the safeguard layer.

Independently of any model output this module:

* verifies cited order ids against CSV rows (unsupported-claim detection),
* detects complaint-vs-CSV conflicts,
* enforces the policy's ``never_automate`` / ``must_escalate_if`` rules,
* treats prompt-injection text as data (it can only cause escalation),
* routes each case to SUPPORTED_DRAFT / REVIEW_REQUIRED.

No side effects exist here: nothing is refunded, sent, or edited. The only
outcomes are routing decisions, evidence references and missing facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .intake import IntakeCase
from .model_adapter import detect_injection

_COMPENSATION = re.compile(
    r"\b(refund|reimburse|chargeback|coupon|voucher|money\s+back|compensation|"
    r"free\s+(?:meal|food|item)|discount)\b",
    re.IGNORECASE,
)
_SAFETY = re.compile(
    r"\b(sue|lawsuit|lawyer|legal\s+action|poisoned|food\s+poisoning|allergic|"
    r"hospital|injured|burned|choked)\b",
    re.IGNORECASE,
)
_NEVER_RECEIVED = re.compile(
    r"\b(never\s+(?:got|received|arrived|picked\s+up)|didn'?t\s+(?:get|receive|arrive)|"
    r"not\s+received|still\s+waiting)\b",
    re.IGNORECASE,
)
_LATE = re.compile(r"\b(late|delay(?:ed)?|waited|took\s+forever)\b", re.IGNORECASE)
_AMOUNT = re.compile(r"\$(\d+(?:\.\d{1,2})?)")
_RECEIVED_STATUSES = {"delivered", "picked_up", "picked up", "completed", "collected"}


@dataclass
class CheckResult:
    route: str  # SUPPORTED_DRAFT | REVIEW_REQUIRED
    reasons: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    missing_facts: list[str] = field(default_factory=list)


def _parse_hhmm(value: str) -> int | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def validate(intake: IntakeCase, rows: list[dict], policy: dict) -> CheckResult:
    complaint = intake.complaint_text
    result = CheckResult(route="SUPPORTED_DRAFT")
    never_automate = {str(x).lower() for x in policy.get("never_automate", [])}
    caps = policy.get("compensation_caps", {}) or {}

    # 0) Cite the input excerpt so every packet references its source text.
    excerpt = " ".join(complaint.split())[:120]
    result.evidence_refs.append(
        f"complaint:excerpt \"{excerpt}{'…' if len(complaint.strip()) > 120 else ''}\""
    )

    # 1) Prompt injection: treated strictly as data; can only escalate.
    injections = detect_injection(complaint)
    if injections:
        result.route = "REVIEW_REQUIRED"
        result.reasons.append("prompt_injection_treated_as_data")
        result.evidence_refs.append(
            "complaint: prompt_injection pattern(s) detected; "
            "treated as data, not followed"
        )

    # 2) Compensation requests are never automated (policy never_automate + caps).
    comp_match = _COMPENSATION.search(complaint)
    if comp_match:
        result.route = "REVIEW_REQUIRED"
        result.reasons.append("compensation_request_never_automated")
        result.evidence_refs.append(
            f"complaint: compensation keyword '{comp_match.group(1).lower()}' "
            f"policy:{policy.get('version')}:never_automate"
        )
        amount = _AMOUNT.search(complaint)
        cap_key = (
            "refund_usd"
            if comp_match.group(1).lower() in {"refund", "reimburse", "chargeback"}
            else "coupon_usd"
        )
        cap = caps.get(cap_key)
        if amount and cap is not None and float(amount.group(1)) > float(cap):
            result.reasons.append("compensation_over_policy_limit")
            result.evidence_refs.append(
                f"policy:{policy.get('version')}:compensation_caps.{cap_key}={cap} "
                f"but complaint requests ${amount.group(1)}"
            )

    # 3) Safety / threat keywords must escalate.
    safety = _SAFETY.search(complaint)
    if safety:
        result.route = "REVIEW_REQUIRED"
        result.reasons.append("threat_or_safety_issue")
        result.evidence_refs.append(f"complaint: safety keyword '{safety.group(1).lower()}'")

    # 4) Evidence checks: cited ids vs CSV.
    cited = intake.cited_order_ids
    if not cited:
        result.route = "REVIEW_REQUIRED"
        result.reasons.append("order_id_missing_but_asserted")
        result.missing_facts.append("no order id found in complaint text")
        result.evidence_refs.append("complaint: no order id cited; cannot verify a purchase")
    elif not rows:
        result.route = "REVIEW_REQUIRED"
        result.reasons.append("order_evidence_unavailable")
        for order_id in cited:
            result.missing_facts.append(f"order {order_id} not verifiable — no order CSV provided")
        result.evidence_refs.append("complaint: order id cited but no CSV rows available")
    else:
        index: dict[str, list[dict]] = {}
        for row in rows:
            index.setdefault(row["order_id"], []).append(row)
        for order_id in cited:
            match_rows = index.get(order_id, [])
            if not match_rows:
                result.route = "REVIEW_REQUIRED"
                result.reasons.append("unsupported_order_claim")
                result.missing_facts.append(
                    f"order {order_id} cited in complaint but absent from order CSV"
                )
                result.evidence_refs.append(
                    f"complaint:cites {order_id} | csv:no matching row (unsupported claim)"
                )
                continue
            order_conflict = False
            if len(match_rows) > 1:
                row_nums = ",".join(str(r["row_number"]) for r in match_rows)
                result.route = "REVIEW_REQUIRED"
                result.reasons.append("conflicting_order_data")
                result.evidence_refs.append(
                    f"csv:rows {row_nums} duplicate order={order_id} (conflict)"
                )
                order_conflict = True
            for row in match_rows:
                ref = (
                    f"csv:row{row['row_number']} order={row['order_id']} "
                    f"status={row.get('status', 'n/a')}"
                )
                status = (row.get("status") or "").lower()
                if _NEVER_RECEIVED.search(complaint) and status in _RECEIVED_STATUSES:
                    result.route = "REVIEW_REQUIRED"
                    result.reasons.append("conflicting_order_data")
                    result.missing_facts.append(
                        f"complaint says order {order_id} never arrived but CSV status is '{status}'"
                    )
                    result.evidence_refs.append(f"{ref} | complaint:claims never received (conflict)")
                    continue
                promised = _parse_hhmm(row.get("promised_time") or "")
                actual = _parse_hhmm(row.get("actual_time") or "")
                if (
                    _LATE.search(complaint)
                    and promised is not None
                    and actual is not None
                    and actual <= promised
                ):
                    result.route = "REVIEW_REQUIRED"
                    result.reasons.append("conflicting_order_data")
                    result.missing_facts.append(
                        f"complaint reports a delay for order {order_id} but CSV shows "
                        f"{row.get('actual_time')} vs promised {row.get('promised_time')}"
                    )
                    result.evidence_refs.append(f"{ref} | csv:timing contradicts delay claim (conflict)")
                    continue
                if not order_conflict:
                    result.evidence_refs.append(f"{ref} verified")

    # De-duplicate while preserving order.
    result.reasons = list(dict.fromkeys(result.reasons))
    result.evidence_refs = list(dict.fromkeys(result.evidence_refs))
    result.missing_facts = list(dict.fromkeys(result.missing_facts))
    return result
