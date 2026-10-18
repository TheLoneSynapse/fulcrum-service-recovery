"""Edge-case tests: CSV parsing boundaries, request validation, export fallbacks.

Complements the smoke/provider/store suites with the inputs that usually
break things: header-only CSV, ragged rows, BOMs, empty/oversized complaints,
invalid enums, unknown-case decisions, and export formatting when fields are
absent. Every expectation below follows the implementation contract
(``intake.parse_order_csv``, ``schemas`` request models, ``exporter``).

Run from ``backend/``:  python -m pytest -q
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.exporter import PacketExporter
from app.intake import MalformedCSV, normalize, parse_order_csv
from app.main import app

client = TestClient(app)


# ------------------------------------------------------------- CSV boundaries
def test_csv_header_only_is_rejected():
    """A header with no data rows is unusable — explicit bounded error."""
    with pytest.raises(MalformedCSV, match="no data rows"):
        parse_order_csv("order_id,promised_time,actual_time\n")


def test_csv_empty_order_id_row_is_rejected():
    with pytest.raises(MalformedCSV, match="empty order_id"):
        parse_order_csv("order_id,status\n,completed\n")


def test_csv_ragged_row_is_rejected():
    """Too many fields vs the header must fail, not silently truncate."""
    with pytest.raises(MalformedCSV, match="more fields"):
        parse_order_csv("order_id,promised_time\nA100,18:30,extra\n")


def test_csv_missing_required_column_is_rejected():
    with pytest.raises(MalformedCSV, match="order_id"):
        parse_order_csv("transaction_id,amount\nT1,9.99\n")


def test_csv_bom_and_leading_blank_lines_are_stripped():
    raw = "﻿\n\norder_id,status\nA100,completed\n"
    rows = parse_order_csv(raw)
    assert [r["order_id"] for r in rows] == ["A100"]


def test_csv_order_ids_uppercased_and_row_numbers_count_data_rows():
    rows = parse_order_csv("order_id,status\na100,completed\nB200,lATE\n")
    assert [r["order_id"] for r in rows] == ["A100", "B200"]
    # row_number is a 1-based DATA row index (header excluded) so evidence
    # refs like "csv:row1" point at the first real row.
    assert [r["row_number"] for r in rows] == [1, 2]
    # only order_id is uppercased; other fields pass through stripped
    assert rows[1]["status"] == "lATE"


def test_csv_none_or_blank_input_is_empty_not_an_error():
    assert parse_order_csv(None) == []
    assert parse_order_csv("   \n  ") == []


# --------------------------------------------------------- request validation
def test_empty_complaint_is_rejected():
    res = client.post(
        "/api/cases",
        json={"complaint_text": "", "policy_version": "synthetic-v0.1"},
    )
    assert res.status_code == 422


def test_oversized_complaint_is_rejected():
    res = client.post(
        "/api/cases",
        json={"complaint_text": "x" * 8001, "policy_version": "synthetic-v0.1"},
    )
    assert res.status_code == 422


def test_unknown_source_kind_is_rejected():
    res = client.post(
        "/api/cases",
        json={"complaint_text": "Order A100 was late.", "source": "email"},
    )
    assert res.status_code == 422


def test_decision_rejects_unknown_value():
    res = client.post(
        "/api/cases/case-whatever/decision",
        json={"decision": "DELETED"},
    )
    assert res.status_code == 422


def test_decision_note_is_bounded():
    res = client.post(
        "/api/cases/case-whatever/decision",
        json={"decision": "APPROVED", "note": "n" * 1001},
    )
    assert res.status_code == 422


def test_decision_on_unknown_case_is_404():
    res = client.post(
        "/api/cases/case-does-not-exist/decision",
        json={"decision": "APPROVED"},
    )
    assert res.status_code == 404


# --------------------------------------------------------------- intake build
def test_normalize_strips_text_and_assigns_case_identity():
    case = normalize("  My pickup was late. Order ID A100.  ", "synthetic-v0.1", "web_form")
    assert case.case_id.startswith("case-")
    assert case.complaint_text == "My pickup was late. Order ID A100."
    assert case.cited_order_ids == ["A100"]
    assert case.order_id == "A100"
    assert case.policy_version == "synthetic-v0.1"
    assert case.created_at_utc.endswith("Z")


# ------------------------------------------------------------ export fallbacks
def test_markdown_export_without_draft_or_audit_falls_back_to_none():
    packet = {
        "case_id": "case-test1234567",
        "outcome": "REVIEW_REQUIRED",
        "manager_decision": "PENDING",
        "policy_version": "synthetic-v0.1",
        "evidence_refs": [],
        "missing_facts": [],
        "amd_inference": {"attempted": False, "verified": False},
    }
    md = PacketExporter.to_markdown(packet, [])
    assert "_(no draft — review required)_" in md
    assert "- none" in md
    assert "case-test1234567" in md
    # disclosure footer is always present
    assert "Nothing was refunded, sent, or changed" in md


def test_json_export_preserves_unicode_and_round_trips():
    packet = {"case_id": "case-emoji000001", "draft_response": "Sorry about the 🍕 cold pizza!"}
    text = PacketExporter.to_json(packet)
    assert "🍕" in text  # ensure_ascii=False: readable, not \uXXXX-escaped
    assert json.loads(text)["draft_response"] == "Sorry about the 🍕 cold pizza!"
