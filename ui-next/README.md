# Tally — Reconstruction Workbench (React / Vite)

A faithful React port of the Tally design component (`Tally.dc.html`).
Same brand, IA, six workflow states, evidence drawer, and animations — now a
real, importable React app you can wire to a backend.

## Run it

```bash
cd tally-react
npm install
npm run dev        # http://localhost:5173
npm run build      # production bundle in dist/
```

Requires Node 18+.

## What's here

```
tally-react/
  index.html            Vite entry
  package.json          react 18 + vite + @vitejs/plugin-react
  vite.config.js
  src/
    main.jsx            mounts <Workbench scene="live" reducedMotion internal />
    Workbench.jsx       the whole app (one class component)
    index.css           resets, @keyframes, responsive rules (the only non-inline styles)
```

There is also a **verification harness** at the repo root:
`../Tally-React-Preview.html` — loads this exact `src/Workbench.jsx` via CDN
React + Babel so it runs with no build step. It is only for previewing; ship
the Vite project, not the harness.

## Props (mirror the design's reviewer tweaks)

`<Workbench scene reducedMotion internal />`

- `scene`: `"live"` (default, plays the arrival→reconstruct→recommend sequence)
  · `"recommendation"` · `"sendGate"` · `"sendBlocked"` · `"sent"`
  · `"insufficient"` · `"completed"` — jump straight to a state.
- `reducedMotion`: boolean — disables timers' delays and CSS animation.
- `internal`: boolean — reveals ANNOTATIONS / REPLAY / HANDOFF (keep **false**
  for demo/submission).

## How the port was done (and why it's exact)

- **Logic is copied verbatim.** The design's logic class was already a
  React-class-shaped object (state / setState / lifecycle / plain data
  builders). It moved over unchanged: `renderVals()`, `buildWorkbench()`,
  `buildDrawer()`, `buildHandoff()`, the state machine (`rank`/`at`), and every
  handler.
- **View is JSX with a `css()` shim.** Instead of hand-translating hundreds of
  CSS declarations to React style objects (error-prone), `css("...")` parses the
  design's exact inline-style strings into style objects at runtime, and
  `S(str, extra)` merges dynamic overrides. So the markup is a 1:1 copy of the
  design's template with `{{x}}` → `{x}`, `<sc-for>` → `.map`, `<sc-if>` →
  `{cond && ...}`. Verified pixel-identical.
- **Animations are identical** — same `@keyframes` and classNames in
  `index.css` as the design's `<helmet>` block (card fade-rise, pulse,
  grid-width transition, reduced-motion guard).

## Parity checklist (verified)

- Queue with live INV-1048 arrival, filters, status pills.
- Six workbench states: intake → reconstructing → ready-for-review →
  ready-to-send → sent, plus the INV-1050 valid-invoice record.
- 7-day ledger, sourced timeline, provenance legend, pipeline stepper.
- Evidence drawer (Source / Used by / Verification) replacing the action rail,
  day source-chain, restore-on-close.
- Two authorizations (Approve $700 dispute → Approve & Send), send gate with
  Fallback NONE, sent acknowledgment + limitation copy.
- Completed-context chip strip at send phase.

## Wiring to a real backend (next steps)

The port keeps the exact seams the design documented. Replace these in
`Workbench.jsx`:

1. **`openInvoice()` timers → event stream.** The `this.later(...)` chain that
   advances `wb` is the stand-in for a per-invoice SSE/WebSocket subscription.
   Replace with a subscription that sets `wb` from server `sequence` events;
   reconcile on reconnect.
2. **Queue arrival.** `applyScene()`'s `arrived` timer stands in for the
   `invoice.received` queue-stream insert.
3. **`buildDrawer()` / `buildWorkbench()` data.** Currently hard-coded
   representative values (invoice claims, timeline, tariff, day adjudication).
   Back these with: CockroachDB Managed MCP (timeline/memory), Distributed
   Vector Indexing (tariff candidate), deterministic engine (validation +
   amounts), versioned S3 (exact source versions).
4. **`approveSend()` gate.** The 5 `gateChecks` should become real
   preconditions; keep the fail-closed behavior (`sendBlocked` scene) — never
   show substitute memory.
5. **`buildHandoff()`** is documentation only; drop it from production or keep
   behind `internal`.

Everything marked `BACKEND REQUIRED` / `BLOCKED BY CURRENT TRUTH` in the design
and in `../Tally-Context-and-Handoff.md` still applies unchanged.

## HTML vs React — tradeoffs (why this port exists)

| | `Tally.dc.html` (design component) | `tally-react/` (this) |
|---|---|---|
| Runs in browser | yes (needs the design runtime) | yes (Vite / any bundler) |
| Importable component | no | `import Workbench from "./Workbench.jsx"` |
| Wire to real APIs | awkward | native — replace timers with data hooks |
| npm / tests / CI | no | yes |
| Smoothness / animation | identical | identical (same keyframes) |
| Fastest to hand to a hackathon team | — | this one |

Use the DC as the clickable design spec; build on `tally-react/` for the
shipping app.
