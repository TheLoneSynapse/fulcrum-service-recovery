"""IntakeAdapter — normalize complaint text and anonymized order CSV input.

Synthetic data only. CSV parsing is bounded: malformed input raises
``MalformedCSV`` with a short, non-reflective message (no echoed content).
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Explicit forms first ("Order ID A100", "order #A100"), then loose token form.
_ORDER_EXPLICIT = re.compile(
    r"\border(?:\s+(?:id|number|#))?\s*[:#]?\s*([A-Za-z]{1,4}\d{2,6})\b", re.IGNORECASE
)
_ORDER_LOOSE = re.compile(r"\b([A-Za-z]{1,3}\d{3,6})\b")

REQUIRED_COLUMNS = ("order_id",)
OPTIONAL_COLUMNS = (
    "promised_time",
    "actual_time",
    "items",
    "status",
    "amount_usd",
    "notes",
)


class MalformedCSV(ValueError):
    """Bounded validation error for unusable CSV input."""


@dataclass
class IntakeCase:
    case_id: str
    source: str
    complaint_text: str
    policy_version: str
    created_at_utc: str
    order_id: str | None = None
    cited_order_ids: list[str] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def extract_order_ids(text: str) -> list[str]:
    """Deterministic order-id extraction, independent of any model output."""
    seen: list[str] = []
    for match in _ORDER_EXPLICIT.finditer(text):
        token = match.group(1).upper()
        if token not in seen:
            seen.append(token)
    if not seen:
        for match in _ORDER_LOOSE.finditer(text):
            token = match.group(1).upper()
            if token not in seen:
                seen.append(token)
    return seen


def normalize(request_complaint: str, policy_version: str, source: str) -> IntakeCase:
    text = request_complaint.strip()
    ids = extract_order_ids(text)
    return IntakeCase(
        case_id=f"case-{uuid.uuid4().hex[:12]}",
        source=source,
        complaint_text=text,
        policy_version=policy_version,
        created_at_utc=utc_now(),
        order_id=ids[0] if ids else None,
        cited_order_ids=ids,
    )


def parse_order_csv(raw: str | None) -> list[dict]:
    """Parse CSV text into row dicts with 1-based data-row numbers.

    Each row carries ``row_number`` so evidence references can cite it
    (e.g. ``csv:row2 order=A100 status=completed``).
    """
    if raw is None or not raw.strip():
        return []

    # Strip a UTF-8 BOM and any blank leading lines.
    cleaned = raw.lstrip("\ufeff").strip()
    if not cleaned:
        return []

    reader = csv.DictReader(io.StringIO(cleaned))
    if not reader.fieldnames:
        raise MalformedCSV("CSV has no header row")
    headers = [h.strip().lower() for h in reader.fieldnames if h is not None]
    if "order_id" not in headers:
        raise MalformedCSV("CSV missing required column 'order_id'")

    rows: list[dict] = []
    for index, raw_row in enumerate(reader, start=1):
        if None in raw_row:  # ragged row: more fields than the header
            raise MalformedCSV(f"row {index} has more fields than the header")
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
        order_id = row.get("order_id", "").upper()
        if not order_id:
            raise MalformedCSV(f"row {index} has an empty order_id")
        row["order_id"] = order_id
        row["row_number"] = index
        rows.append(row)
    if not rows:
        raise MalformedCSV("CSV contains a header but no data rows")
    return rows
