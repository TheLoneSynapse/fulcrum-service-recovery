# 20 labeled synthetic cases

`cases.json` holds the 20 labeled cases (5 ordinary · 3 wrong/missing order IDs ·
3 conflicts · 3 compensation · 2 ambiguity/no evidence · 2 prompt injection ·
2 operational) per `docs/planning/06_VERIFICATION_AND_EVALS.md`.

`eval_report.json` is the measured report, regenerated with:

```bash
cd backend && python -m app.evals
```

Synthetic only — no real customer data.
