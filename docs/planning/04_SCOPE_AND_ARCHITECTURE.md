# PRODUCT SCOPE — FULCRUM SERVICE RECOVERY
## One job
A restaurant manager pastes a customer complaint, optionally uploads an anonymized order CSV, selects a versioned restaurant response policy, and obtains (1) evidence-grounded response draft or (2) explicit manager-review request plus a downloadable review packet.

## MVP (strict)
1. Browser form: complaint, optional order CSV, policy version selection.
2. Structured AMD inference: extract issue, order identifiers, missing facts, and candidate response. Use a REAL model call and display the model identifier/backend evidence.
3. Deterministic safeguards: check cited order IDs against fixture data; compensation never auto-executes; enforce policy limits and escalate conflicts/unknowns.
4. Manager packet: draft, cited input excerpts/row identifiers, policy version, unknowns, decision recommendation, review status, and export JSON/MD.
5. Local approve/reject/request-info recorded as a manager action in app state. No outbound sends, refunds, POS changes, contacts, or live customer data.
6. Audit of 20 synthetic labeled cases; transparent metrics.

## Pipeline
`Text/CSV/Policy → IntakeCase(normalize) → AMD inference(extract + draft) → deterministic validator → ManagerReviewPacket → human decision (local only) → export receipt`

## Technical seams
- `IntakeAdapter`: text/CSV now, phone intake later.
- `ModelAdapter`: AMD-compatible API with model call metadata; no silent fallback to mock on judged happy path.
- `PolicyEngine`: deterministic rules and source reference checks; versioned fixture policy.
- `ReviewStore`: local SQLite or in-memory durable for demo (choose one and disclose durability limits).
- `PacketExporter`: sanitized JSON and optionally Markdown.
- `Web UI`: one input screen and one packet/review screen.

## Non-goals
No real telecom integration, no FrictionMap scheduling, no refunds, no sending emails/SMS, no CRM/POS integration, no autonomous customer outreach, no forecasting, no multi-agent system, no production identity/PII pipeline.

## Success conditions
All demo cases yield grounded draft OR explicit escalation; zero unauthorized actions; explicit unknowns; judge can see actual AMD inference; clean startup and public demo URL; reproducible evaluations.
