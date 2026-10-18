"""Evaluation runner — 20 labeled synthetic cases.

Run from ``backend/``:  ``python -m app.evals``

Reports measured routing accuracy, per-category results, packet completeness
against the proposed schema, unauthorized-side-effect count (target ZERO) and
median/p95 latency. No fabricated results: every number is measured here.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from .model_adapter import ModelAdapter
from .pipeline import Pipeline
from .policy import PolicyEngine
from .store import ReviewStore

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "cases.json"
SCHEMA = ROOT / "contracts" / "review_packet.proposed.schema.json"


def orders_to_csv(orders: list[dict]) -> str | None:
    if not orders:
        return None
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=["order_id", "promised_time", "actual_time", "items", "status", "amount_usd"],
    )
    writer.writeheader()
    for row in orders:
        writer.writerow(row)
    return buffer.getvalue()


def run_evals() -> dict:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    cases = payload["cases"]
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    pipeline = Pipeline(PolicyEngine(ROOT / "contracts"), ModelAdapter(), ReviewStore())
    default_policy = payload.get("policy_version", "synthetic-v0.1")

    results = []
    latencies: list[float] = []
    for case in cases:
        start = time.perf_counter()
        out = pipeline.run(
            complaint_text=case["complaint_text"],
            policy_version=case.get("policy_version") or default_policy,
            source="csv_fixture",
            order_csv=orders_to_csv(case.get("orders") or []),
            force_failure=bool(case.get("simulate")),
        )
        latencies.append((time.perf_counter() - start) * 1000)
        packet = out["packet"]
        missing = [key for key in schema.get("required", []) if key not in packet]
        results.append(
            {
                "case_id": case["case_id"],
                "category": case["category"],
                "expected": case["expected_outcome"],
                "actual": packet["outcome"],
                "correct": packet["outcome"] == case["expected_outcome"],
                "packet_complete": not missing,
                "missing_keys": missing,
                "reasons": [r for r in _reasons_of(packet, case)],
            }
        )

    correct = sum(1 for r in results if r["correct"])
    by_category: dict[str, Counter] = {}
    for r in results:
        bucket = by_category.setdefault(r["category"], Counter())
        bucket["total"] += 1
        if r["correct"]:
            bucket["correct"] += 1

    latencies_sorted = sorted(latencies)
    p95 = latencies_sorted[max(0, int(round(0.95 * (len(latencies_sorted) - 1))))]

    report = {
        "fixture_file": str(FIXTURES.relative_to(ROOT)),
        "cases": len(results),
        "routing_accuracy": round(correct / len(results), 4),
        "per_category": {
            cat: {"correct": c["correct"], "total": c["total"]} for cat, c in sorted(by_category.items())
        },
        "outcome_distribution": dict(Counter(r["actual"] for r in results)),
        "packet_completeness": round(
            sum(1 for r in results if r["packet_complete"]) / len(results), 4
        ),
        "unauthorized_side_effects": pipeline.store.unauthorized_side_effects,
        "latency_ms": {
            "median": round(statistics.median(latencies), 2),
            "p95": round(p95, 2),
            "max": round(max(latencies), 2),
        },
        "inference_backend": pipeline.model.backend_name,
        "failures": [r for r in results if not r["correct"]],
        "results": results,
    }
    return report


def _reasons_of(packet: dict, case: dict) -> list[str]:
    """Surface evidence refs that carry the primary routing reason."""
    marker = case.get("expected_primary_reason") or ""
    hits = [ref for ref in packet.get("evidence_refs", []) if marker.split("_")[0] in ref.lower()]
    return hits[:2]


def main() -> int:
    report = run_evals()
    print(f"Fulcrum evals — {report['cases']} labeled synthetic cases")
    print(f"  routing accuracy     : {report['routing_accuracy']:.1%}")
    print(f"  packet completeness  : {report['packet_completeness']:.1%}")
    print(f"  unauthorized effects : {report['unauthorized_side_effects']} (target 0)")
    print(f"  latency ms median/p95: {report['latency_ms']['median']} / {report['latency_ms']['p95']}")
    print(f"  inference backend    : {report['inference_backend']}")
    for cat, stats in report["per_category"].items():
        print(f"    {cat:28s} {stats['correct']}/{stats['total']}")
    for row in report["results"]:
        flag = "PASS" if row["correct"] else "FAIL"
        print(f"    [{flag}] {row['case_id']} {row['category']:26s} "
              f"expected={row['expected']:16s} actual={row['actual']}")
    if report["failures"]:
        print(f"\n{len(report['failures'])} failing case(s):")
        for row in report["failures"]:
            print(f"  {row['case_id']}: expected {row['expected']}, got {row['actual']}")
    out_path = ROOT / "fixtures" / "eval_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nreport written to {out_path}")
    return 0 if report["routing_accuracy"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
