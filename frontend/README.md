# Frontend — one box in, decisions out

Static HTML/CSS/JS, no build step. Served by the backend at `/`.

## Screens

1. **Intake (one box)** — paste a complaint and hit ⚡ Analyze. Paste several
   separated by a **blank line** and the button becomes "Analyze N complaints",
   which runs `POST /api/cases/batch` and lands you on the review queue.
   Live meta line shows what will happen: complaint count, active policy,
   whether a CSV is attached. Evidence (CSV) and policy live behind an
   optional **⚙️ Advanced** disclosure — defaults apply automatically so a
   first-time user never has to open it.
   A **pending banner** appears when cases await a decision: "▶ Resume review"
   jumps straight into the oldest pending packet.

2. **Review queue** — summary banner (ready-to-approve vs. needs-judgment vs.
   decided), a bulk **"✔ Approve all N evidence-backed drafts"** button that
   records an audit event per case, and **"▶ Review pending cases"** to walk
   them one by one. Rows open any packet.

3. **Packet / decision** — the only human work. A smart CTA mirrors the
   packet's `recommendation` (approve draft ⇄ flag for follow-up) but never
   acts without a click; three explicit buttons remain below it.
   Keyboard: `A` approve · `R` reject · `I` request info · `N` next pending ·
   `B` back · `Esc` cancels the 2-second auto-advance to the next pending case.
   After each decision a toast counts down the advance, so a queue of 20 can be
   cleared without touching the mouse.

Nothing here can send, refund, or contact anyone — decisions only append
audit events (surfaced as `unauthorized_side_effects: 0` in `/api/status`).

## Structure

- `index.html` — three screen sections + toast container
- `app.js` — state machine (`intake → queue → packet`), keyboard handler,
  batch/decision API calls
- `styles.css` — design tokens at `:root`, queue/bulk/CTA/toast styles at the bottom
