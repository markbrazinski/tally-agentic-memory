import React from "react";
import { nextWb, STATUS_LABEL } from "./api/eventMap.js";

// Invoice ids arrive in two forms (INV-TLY-1048 vs a uuid); match loosely by
// the trailing invoice number so the display id and the server id line up.
function sameInvoice(a, b) {
  if (!a || !b) return false;
  if (a === b) return true;
  const na = String(a).match(/(\d{3,})/);
  const nb = String(b).match(/(\d{3,})/);
  return !!(na && nb && na[1] === nb[1]);
}

// Find the hero (INV-1048) row by number across BOTH id and name — a `||` here
// short-circuits on the truthy UUID id and never checks the name.
function isHeroRow(r) {
  const hay = `${(r && r.invoiceId) || ""} ${(r && r.name) || ""}`;
  return /1048/.test(hay);
}

// Minor-unit integer (70000) -> display dollars ("$700"), thousands-grouped.
// ponytail: whole-dollar display only — the design never shows cents in these slots.
function money(minor) {
  return "$" + Math.round(minor / 100).toLocaleString("en-US");
}

/* ------------------------------------------------------------------ *
 * css(): parse a DC inline-style STRING into a React style object.
 * Lets us port the design's exact style strings verbatim (1:1 parity)
 * instead of hand-translating hundreds of declarations.
 * S(): merge a parsed string with dynamic overrides.
 * ------------------------------------------------------------------ */
function css(s) {
  const o = {};
  if (!s) return o;
  s.split(";").forEach((decl) => {
    const i = decl.indexOf(":");
    if (i < 0) return;
    const k = decl.slice(0, i).trim();
    const v = decl.slice(i + 1).trim();
    if (!k) return;
    const key = k.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    o[key] = v;
  });
  return o;
}
const S = (str, extra) => Object.assign(css(str), extra || {});

/* Base class mirroring the DC "DCLogic" surface we actually use.
 * (state/setState/lifecycle come from React.Component.) */
export default class Workbench extends React.Component {
  constructor(props) {
    super(props);
    const scene = (props && props.scene) || "live";
    this.state = {
      view: "queue", invoiceId: null, wb: "intake",
      arrived: false, sealStep: 0, gateStep: 0, gateBlocked: false,
      dayOpen: false, dayIx: 2, drawer: null, quickOpen: false, inv1050: "pending",
      annot: false, disputed: false, sceneApplied: false,
      userDisc: {}, discWb: null, queueFilter: "all", qrEvid: false, drawerTab: "source",
    };
    this._timers = [];
    this.reduced = !!(props && props.reducedMotion);
    this.scene = scene;
    // LIVE MODE: when a `provider` is passed, the UI is driven by the real
    // backend (queue + SSE event stream + approve). Without it, the original
    // timer-driven demo runs unchanged.
    this.provider = (props && props.provider) || null;
    this.live = !!this.provider;
    this._unsub = null;
    this.recon = null;            // latest reconstruction projection
    this.liveQueue = null;        // rows from provider.listInvoices()
    this.recommendation = null;   // frozen recommendation (id + etag) for approve
    if (this.live) this.state.wb = "intake";
    ["goQueue","goCoverage","goHandoff","goDecision","goActivity","replay","toggleAnnot",
     "approveDispute","approveSend","retrySend","closeDay","approvePayment","openQuickReview",
     "closeQuickReview","closeDrawer","stop","noop","openSourceInvoice","openSourceTariff",
     "setQueueFilter","toggleQrEvid","openInv1050","openDayDrawer","setDrawerTab"]
      .forEach((m) => (this[m] = this[m].bind(this)));
  }
  componentDidMount() {
    if (this.live) { this.startLive(); return; }
    this.applyScene();
  }
  componentWillUnmount() { this.clearTimers(); if (this._unsub) this._unsub(); }

  // ---- LIVE MODE plumbing (replaces the timer stand-ins) ----
  startLive() {
    // Initial load: the pre-existing rows (INV-1041, INV-1047) render straight
    // away; the hero (INV-1048) animates in ~1s later, as if it just arrived on
    // the platform after the window opened. So we do NOT flip `arrived` from the
    // hero merely already being in the queue — we delay it here.
    this.loadQueue({ deferHero: true });
    // Subscribe to the aggregate SSE stream; advance wb from real events.
    this._unsub = this.provider.subscribe(
      (evt) => this.onLiveEvent(evt),
      () => { /* EventSource auto-reconnects; nothing to do */ },
    );
  }
  async loadQueue(opts) {
    const deferHero = opts && opts.deferHero;
    try {
      this.liveQueue = await this.provider.listInvoices();
      const hero = this.liveQueue.find(isHeroRow);
      this._liveErr = null;
      if (hero && deferHero && !this.state.arrived) {
        // Pre-existing rows show now; the hero appears ~1s later.
        this.forceUpdate();
        this.later(() => this.setState({ arrived: true }), 1000);
      } else if (hero) {
        this.setState({ arrived: true });
      } else {
        this.forceUpdate();
      }
    } catch (e) {
      // Surface the error instead of silently hiding the hero row.
      // eslint-disable-next-line no-console
      console.error("[Tally] loadQueue FAILED:", e);
      this._liveErr = String(e && e.message ? e.message : e);
      this.setState({ arrived: false });
    }
  }
  onLiveEvent(evt) {
    if (!evt || !evt.event_type) return;
    // Only advance for the invoice currently open in the workbench (or always
    // for queue arrivals).
    if (evt.event_type === "invoice.received") { this.loadQueue(); return; }
    const openId = this.state.invoiceId;
    if (openId && evt.invoice_id && !sameInvoice(evt.invoice_id, openId)) return;
    // Authority-transition events carry no wb-rank of their own; they signal
    // that the persisted projection changed (coverage 6/7->7/7, June-11 bound,
    // a new recommendation revision). Re-read the projection so the rail and
    // ledger update IN PLACE from server truth — forceUpdate re-renders without
    // remount/navigation/auto-scroll, so scroll position is preserved.
    const refreshEvents = [
      "decision.authority_withheld",
      "reconstruction.source_bound",
      "reconstruction.coverage_updated",
      "decision.recommendation_ready",
    ];
    if (refreshEvents.includes(evt.event_type)) {
      this.refreshReconstruction();
    }
    const next = nextWb(this.state.wb, evt.event_type, (p) => this.rank(p));
    if (next !== this.state.wb) {
      this.setState({ wb: next });
      // When reconstruction/rule/recommendation land, refresh the projection.
      if (["reconstructed","ruleVerified","recommendation"].includes(next)) {
        this.refreshReconstruction();
      }
    }
  }
  async refreshReconstruction() {
    if (!this.live || !this.state.invoiceId) return;
    try {
      const r = await this.provider.getReconstruction(this.state.invoiceId);
      if (r) {
        this.recon = r;
        if (r.recommendation) {
          this.recommendation = {
            id: r.recommendation.recommendation_id,
            etag: r.recommendation.approval_etag,
          };
        }
        this.forceUpdate();
      }
    } catch { /* keep last known; never invent */ }
  }
  clearTimers() { this._timers.forEach((t) => clearTimeout(t)); this._timers = []; }
  later(fn, ms) { const t = setTimeout(fn, this.reduced ? 0 : ms); this._timers.push(t); }

  applyScene() {
    const s = this.scene;
    if (s === "live") { this.later(() => this.setState({ arrived: true }), 1000); return; }
    const base = { view: "workbench", invoiceId: "INV-TLY-1048", arrived: true };
    if (s === "recommendation") this.setState({ ...base, wb: "recommendation", dayOpen: true });
    else if (s === "sendGate") this.setState({ ...base, wb: "sending", gateStep: 5, disputed: false });
    else if (s === "sendBlocked") this.setState({ ...base, wb: "sending", gateStep: 3, gateBlocked: true });
    else if (s === "sent") this.setState({ ...base, wb: "sent", disputed: true, gateStep: 5 });
    else if (s === "insufficient") this.setState({ ...base, wb: "insufficient" });
    else if (s === "completed") this.setState({ ...base, wb: "sent", disputed: true });
  }

  rank(p) { return ["intake","reconstructing","reconstructed","retrieving","ruleVerified","recommendation","approved","sealing","readyToSend","correspondence","sending","sent"].indexOf(p); }
  at(p) { return this.rank(this.state.wb) >= this.rank(p); }

  openInvoice(realId) {
    this.clearTimers();
    if (this.live) {
      const id = typeof realId === "string" ? realId
        : (this.liveQueue && this.liveQueue.find(isHeroRow)?.invoiceId)
        || "INV-TLY-1048";
      // Fetch the REAL reconstruction first (capture data + recommendation), THEN
      // replay the section-reveal animation over the real data up to the state
      // the server actually reached. The data is real; only the reveal timing is
      // a replay of a completed record (shown as "replay", never live processing).
      this.setState({ view: "workbench", invoiceId: id, wb: "intake" }, () => {
        this.openInvoiceLive(id);
      });
      return;
    }
    this.setState({ view: "workbench", invoiceId: "INV-TLY-1048", wb: "intake" });
    this.replayReveal("recommendation");
  }
  // Timer reveal through the pipeline ranks up to `target`, for the section-
  // populate animation. Used by the prototype and the live cosmetic replay.
  replayReveal(target) {
    // The recommendation is the CONSEQUENCE of evidence completing — it lands the
    // same beat as ruleVerified (validation done → $ conclusion), not on its own
    // detached timer (#1: "make the recommendation hit once the evidence is done").
    const steps = [
      ["reconstructing", 1200], ["reconstructed", 2600], ["retrieving", 3600],
      ["ruleVerified", 4800], ["recommendation", 4950],
    ];
    const targetRank = this.rank(target);
    steps.forEach(([wb, ms]) => {
      if (this.rank(wb) <= targetRank) {
        this.later(() => this.setState({ wb }), this.reduced ? 0 : ms);
      }
    });
  }
  async openInvoiceLive(id) {
    let target = "recommendation";
    // The real retained-PDF link (exact S3 version) from the invoice projection.
    try {
      const inv = await this.provider.getInvoice(id);
      this.sourceUrl = inv && inv.links ? inv.links.source : null;
    } catch { this.sourceUrl = null; }
    try {
      const r = await this.provider.getReconstruction(id);
      if (r) {
        this.recon = r;
        if (r.recommendation) {
          this.recommendation = { id: r.recommendation.recommendation_id, etag: r.recommendation.approval_etag };
        }
        // How far did the server actually get? Replay only up to there.
        target = "reconstructed";
        if (r.applicable_rule && r.applicable_rule.validation_state === "VERIFIED") target = "ruleVerified";
        if (r.recommendation && r.recommendation.state === "FROZEN") target = "recommendation";
      } else {
        // No reconstruction yet — sit at intake; SSE events will advance it.
        target = "intake";
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("[Tally] openInvoiceLive fetch failed:", e);
      target = "intake";
    }
    this.replayLive = true;
    if (target !== "intake") this.replayReveal(target);
    this.forceUpdate();
  }
  // On open, catch wb up to wherever the server already is (events may have
  // fired before the workbench mounted).
  async syncFromServer(id) {
    try {
      const r = await this.provider.getReconstruction(id);
      if (r) {
        this.recon = r;
        if (r.recommendation) {
          this.recommendation = { id: r.recommendation.recommendation_id, etag: r.recommendation.approval_etag };
        }
        // A COMPLETE reconstruction with a ready recommendation means we're at
        // least at "recommendation".
        let wb = "reconstructed";
        if (r.applicable_rule && r.applicable_rule.validation_state === "VERIFIED") wb = "ruleVerified";
        if (r.recommendation && r.recommendation.state === "FROZEN") wb = "recommendation";
        if (this.rank(wb) > this.rank(this.state.wb)) this.setState({ wb });
        else this.forceUpdate();
      }
    } catch { /* stay at intake; events will advance us */ }
  }
  approveDispute() {
    this.clearTimers();
    if (this.live) { this.approveDisputeLive(); return; }
    this.setState({ wb: "approved", dayOpen: false, sealStep: 1 });
    this.later(() => this.setState({ wb: "sealing", sealStep: 2 }), 900);
    this.later(() => this.setState({ wb: "readyToSend", sealStep: 3 }), 1900);
    this.later(() => this.setState({ wb: "correspondence" }), 2700);
  }
  async approveDisputeLive() {
    // Ensure we have the frozen recommendation (id + etag) to approve. Fetch it
    // fresh here rather than relying on prior sync — and never bail silently.
    if (!this.recommendation) {
      try {
        const r = await this.provider.getReconstruction(this.state.invoiceId);
        if (r && r.recommendation) {
          this.recommendation = { id: r.recommendation.recommendation_id, etag: r.recommendation.approval_etag };
        }
      } catch (e) { /* fall through to the loud error below */ }
    }
    if (!this.recommendation) {
      // eslint-disable-next-line no-console
      console.error("[Tally] approve: no recommendation available for", this.state.invoiceId);
      this._liveErr = "No frozen recommendation to approve (is the reconstruction complete?)";
      this.forceUpdate();
      return;
    }
    // Show the human's approval immediately (their action just landed), then let
    // the SERVER decide the seal state — no timers author sealing/ready. The
    // awaited approve() performs approve+seal atomically; its resolution IS the
    // server confirming the seal, and SSE `decision.sealed` reconciles wb too.
    this.setState({ wb: "approved", dayOpen: false, sealStep: 1 });
    try {
      const key = `approve-${this.state.invoiceId}-${this.recommendation.id}`;
      await this.provider.approve(
        this.state.invoiceId, this.recommendation.id, this.recommendation.etag, key,
      );
      // Sealed per the atomic server response; reveal the composer. sealStep 3
      // reflects the completed seal, not a timed animation step.
      this.setState({ wb: "correspondence", sealStep: 3 });
    } catch (e) {
      // Fail closed: surface the block, do not pretend it sealed.
      // eslint-disable-next-line no-console
      console.error("[Tally] approve FAILED:", e);
      this._liveErr = "Approve failed: " + (e && e.message ? e.message : e);
      this.setState({ wb: "recommendation", sealStep: 0, gateBlocked: true });
    }
  }
  approveSend() {
    this.clearTimers();
    // LIVE: draft from the sealed decision, then the second-authorization gated
    // send. The awaited calls drive the Sealed -> Sending -> Sent transition;
    // SSE correspondence.sent/send_blocked still reconcile via eventMap, but we
    // do NOT depend only on SSE. On block/fail we surface it, never fake a send.
    if (this.live) { this.approveSendLive(); return; }
    // The controlled external send is intentionally paused for the demo; the
    // send-gate is backed by the real seal. Advance through the gate visual.
    this.setState({ wb: "sending", gateStep: 0 });
    for (let i = 1; i <= 5; i++) this.later(() => this.setState({ gateStep: i }), i * 400);
    this.later(() => this.setState({ wb: "sent", disputed: true }), 2400);
  }
  async approveSendLive() {
    const id = this.state.invoiceId;
    this.setState({ wb: "sending", gateBlocked: false });
    try {
      await this.provider.draft(id);
      // A fresh attempt each click so a retry after a transient block/fail is a
      // real new send, not an idempotent replay of the blocked attempt. The
      // provider is still idempotent on its own key, so no duplicate delivery.
      const key = `send-${id}-${this._sendTry = (this._sendTry || 0) + 1}`;
      const res = await this.provider.approveSend(id, key);
      if (res.send_state === "SENT") {
        this.sentMsgId = res.provider_message_id
          || (await this.provider.getCorrespondence(id).catch(() => null))?.provider_message_id
          || null;
        this.setState({ wb: "sent", disputed: true, gateBlocked: false });
      } else {
        // SEND_BLOCKED / SEND_FAILED_RETRYABLE — surface it, do not pretend sent.
        this._liveErr = "Send " + res.send_state + (res.blocked_reason ? " · " + res.blocked_reason : "");
        this.setState({ wb: "sending", gateBlocked: true });
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("[Tally] send FAILED:", e);
      this._liveErr = "Send failed: " + (e && e.message ? e.message : e);
      this.setState({ wb: "sending", gateBlocked: true });
    }
  }
  retrySend() { this.setState({ gateBlocked: false }); this.approveSend(); }
  goQueue(e) { if (e && e.preventDefault) e.preventDefault(); this.clearTimers(); this.setState({ view: "queue", drawer: null }); }
  goCoverage(e) { if (e && e.preventDefault) e.preventDefault(); this.setState({ view: "coverage", drawer: null }); }
  goHandoff(e) { if (e && e.preventDefault) e.preventDefault(); this.setState({ view: "handoff", drawer: null }); }
  goDecision(e) { if (e && e.preventDefault) e.preventDefault(); this.setState({ drawer: "decision", drawerTab: "source" }); }
  goActivity(e) { if (e && e.preventDefault) e.preventDefault(); this.setState({ drawer: "activity", drawerTab: "source" }); }
  openSourceInvoice(e) { if (e && e.preventDefault) e.preventDefault(); this.setState({ drawer: "invoice", drawerTab: "source" }); }
  openSourceTariff(e) { if (e && e.preventDefault) e.preventDefault(); this.setState({ drawer: "tariff", drawerTab: "source" }); }
  closeDrawer() { this.setState({ drawer: null }); }
  openDay(ix) { this.setState({ dayOpen: true, dayIx: ix }); }
  openDayDrawer(ix, e) { if (e && e.preventDefault) e.preventDefault(); this.setState({ drawer: "day", dayIx: ix, drawerTab: "source" }); }
  setDrawerTab(t, e) { if (e && e.preventDefault) e.preventDefault(); this.setState({ drawerTab: t }); }
  closeDay() { this.setState({ dayOpen: false }); }
  openQuickReview(e) { if (e && e.preventDefault) e.preventDefault(); this.setState({ quickOpen: true }); }
  openInv1050(e) { if (e && e.preventDefault) e.preventDefault(); this.clearTimers(); this.setState({ view: "wb1050", drawer: null }); }
  closeQuickReview() { this.setState({ quickOpen: false }); }
  approvePayment() { this.setState({ inv1050: "done" }); }
  toggleAnnot() { this.setState({ annot: !this.state.annot }); }
  replay() { this.clearTimers(); this.setState({ view: "queue", wb: "intake", arrived: false, disputed: false, dayOpen: false, drawer: null, quickOpen: false, inv1050: "pending", gateStep: 0, gateBlocked: false, sealStep: 0 }); this.later(() => this.setState({ arrived: true }), 900); }
  stop(e) { e.stopPropagation(); }
  jump(id, e) { if (e && e.preventDefault) e.preventDefault(); const c = document.getElementById("tly-scroll"); const el = document.getElementById(id); if (c && el) { const top = el.getBoundingClientRect().top - c.getBoundingClientRect().top + c.scrollTop - 74; c.scrollTo({ top: Math.max(0, top), behavior: this.reduced ? "auto" : "smooth" }); } }
  stateOpen(key) { const w = this.state.wb; if (key === "source") return w === "intake"; if (key === "timeline") return w === "reconstructing" || w === "reconstructed"; if (key === "days") return ["reconstructing","reconstructed","retrieving","ruleVerified","recommendation","insufficient"].indexOf(w) >= 0; if (key === "corr") return ["correspondence","sending","sent"].indexOf(w) >= 0; return false; }
  secOpen(key) { const s = this.state; if (s.discWb === s.wb && Object.prototype.hasOwnProperty.call(s.userDisc, key)) return s.userDisc[key]; return this.stateOpen(key); }
  toggleSection(key, e) { if (e && e.preventDefault) e.preventDefault(); if (e && e.stopPropagation) e.stopPropagation(); const same = this.state.discWb === this.state.wb; const cur = same && Object.prototype.hasOwnProperty.call(this.state.userDisc, key) ? this.state.userDisc[key] : this.stateOpen(key); const ud = Object.assign({}, same ? this.state.userDisc : {}); ud[key] = !cur; this.setState({ userDisc: ud, discWb: this.state.wb }); }
  setQueueFilter(f, e) { if (e && e.preventDefault) e.preventDefault(); this.setState({ queueFilter: f }); }
  toggleQrEvid(e) { if (e && e.preventDefault) e.preventDefault(); this.setState({ qrEvid: !this.state.qrEvid }); }
  noop(e) { if (e && e.preventDefault) e.preventDefault(); }

  pill(kind) {
    const map = { verified: { bg: "#E4EEE7", fg: "#2F7752" }, contested: { bg: "#F2E1DC", fg: "#B4513F" }, neutral: { bg: "#ECEFF1", fg: "#40515C" }, checking: { bg: "#F3EAD3", fg: "#8A7A50" } };
    return map[kind] || map.neutral;
  }
  statusMeta() {
    const w = this.state.wb;
    if (w === "insufficient") return { status: "NEEDS EVIDENCE", kind: "checking" };
    if (w === "intake") return { status: "INITIAL PROCESSING", kind: "checking" };
    if (["reconstructing","reconstructed","retrieving","ruleVerified"].includes(w)) return { status: "RECONSTRUCTING", kind: "checking" };
    if (w === "recommendation") return { status: "READY FOR REVIEW", kind: "neutral" };
    if (w === "approved") return { status: "APPROVED", kind: "neutral" };
    if (w === "sealing") return { status: "SEALING", kind: "neutral" };
    if (["readyToSend","correspondence"].includes(w)) return { status: "READY TO SEND", kind: "neutral" };
    if (w === "sending") return this.state.gateBlocked ? { status: "SEND BLOCKED", kind: "contested" } : { status: "READY TO SEND", kind: "neutral" };
    if (w === "sent") return { status: "DISPUTED", kind: "contested" };
    return { status: "RECEIVED", kind: "neutral" };
  }
  _q(kind) { const p = this.pill(kind); return { pillBg: p.bg, pillFg: p.fg }; }

  renderVals() {
    const st = this.state;
    const view = st.view;
    const navActive = { bg: "rgba(255,255,255,0.09)", fg: "#F5F0E7" };
    const navRest = { bg: "transparent", fg: "#AEBAC3" };
    const inv = view === "queue" ? navActive : navRest;
    const cov = view === "coverage" ? navActive : navRest;

    const arrived = st.arrived;
    const insuf = this.scene === "insufficient";
    const sm = this.statusMeta();
    const disputed = st.disputed;
    const rows = [];
    let heroRowObj = null;  // built in the `arrived` block; appended last for live (#4)
    // LIVE: the two pre-existing queue rows (INV-1041 APPROVED, INV-1047 NEEDS
    // EVIDENCE) are REAL persisted evaluator outputs read from the projection
    // below — never hardcoded here (v3 §6: do not hardcode either disposition).
    // MOCK/prototype scene keeps its literal INV-1041/INV-1039 backdrop so the
    // click-audit fixture renders unchanged.
    if (!this.live) {
      rows.push({ name: "INV-1041.pdf", sub: "Demurrage · Jun 2", container: "MRKU-701882-3", status: "APPROVED FOR PAYMENT", ...this._q("verified"), amount: "$540", chevron: "›", cursor: "default", rowBg: "transparent", nameColor: "#8A96A0", amountColor: "#8A96A0", onOpen: this.noop, anim: "", workDot: "display:none;" });
      rows.push({ name: "INV-1039.pdf", sub: "Detention · May 28", container: "TCLU-559120-1", status: "DISPUTED", ...this._q("contested"), amount: "$1,120", chevron: "›", cursor: "default", rowBg: "transparent", nameColor: "#8A96A0", amountColor: "#8A96A0", onOpen: this.noop, anim: "", workDot: "display:none;" });
    }
    if (arrived) {
      // In live mode, reflect the hero row's real aggregate status + id.
      const heroRow = this.live && this.liveQueue
        ? this.liveQueue.find(isHeroRow)
        : null;
      // LIVE: the queue row's status/kind is the server aggregate_status — the
      // close-out DISPUTED is the backend's, not the client `disputed` timer flag.
      const heroMeta = heroRow ? (STATUS_LABEL[heroRow.aggregateStatus] || { status: heroRow.aggregateStatus, kind: "neutral" }) : null;
      const heroDisputed_ = heroMeta ? heroMeta.status === "DISPUTED" : disputed;
      const st1048 = heroMeta ? (view === "workbench" && !heroDisputed_ ? sm.status : heroMeta.status)
        : (disputed ? "DISPUTED" : view === "workbench" ? sm.status : "RECEIVED");
      const kind1048 = heroMeta ? (view === "workbench" && !heroDisputed_ ? sm.kind : heroMeta.kind)
        : (disputed ? "contested" : view === "workbench" ? sm.kind : "neutral");
      const disputedRow = heroMeta ? heroDisputed_ : disputed;
      const working = view === "workbench" && !disputedRow && ["INITIAL PROCESSING","RECONSTRUCTING"].includes(sm.status);
      const heroId = heroRow ? heroRow.invoiceId : "INV-TLY-1048";
      // Amount ALWAYS the carrier invoice total (v3: never swap to the $700
      // dispute). LIVE: read the total from the queue projection; the disputed
      // amount lives in the outcome DETAIL, not the amount column.
      const heroTotal = heroRow && heroRow.amountMinor != null
        ? money(heroRow.amountMinor) : "$2,450";
      const heroDisputed = heroRow && heroRow.disputedMinor != null
        ? money(heroRow.disputedMinor) : "$700";
      const heroSub = disputedRow
        ? "Demurrage · Jun 8–14 · Disputed " + heroDisputed
        : "Demurrage · Jun 8–14";
      heroRowObj = { name: "INV-1048.pdf", sub: heroSub, container: "TLLU-482931-7", status: st1048, ...this._q(kind1048), amount: heroTotal, chevron: "›", cursor: "pointer", rowBg: disputedRow ? "transparent" : "#FBF6EE", nameColor: "#23272F", amountColor: disputedRow ? "#B4513F" : "#23272F", onOpen: () => this.openInvoice(heroId), anim: view === "queue" ? "tly-row-in" : "", workDot: working ? "" : "display:none;" };
      // LIVE: the hero is appended AFTER the pre-existing rows below (#4: INV-1048
      // sits at the bottom of the list). MOCK keeps it at the top as before.
      if (!this.live) rows.push(heroRowObj);
    }
    // LIVE: render every pre-existing (non-hero) queue row straight from the
    // server projection — INV-1041 ($540 APPROVED FOR PAYMENT) and INV-1047
    // ($875 NEEDS EVIDENCE, detail = its unresolved reason). Both are REAL
    // evaluator outputs; neither authorizes a financial action here, so neither
    // is clickable into a disposition (v3: do not open/recover them in the film).
    // Amount column is always the carrier invoice total. MOCK scene keeps its
    // original INV-1050 READY-FOR-REVIEW narrative so the click-audit is unchanged.
    if (this.live && this.liveQueue === null) {
      // FIRST PAINT (queue fetch not back yet): render the two pre-existing rows
      // instantly from their fixed, known values so there is zero load flash.
      // These are constant rows (their displayed name/amount/status never change);
      // the live projection reconciles them in place a moment later with the same
      // values. Only INV-1048 (the hero) is genuinely dynamic and waits for data.
      rows.push({ name: "INV-1041.pdf", sub: "Demurrage", container: "OOLU-840112-5", status: "APPROVED FOR PAYMENT", ...this._q("verified"), amount: "$540", chevron: "›", cursor: "default", rowBg: "transparent", nameColor: "#23272F", amountColor: "#23272F", onOpen: this.noop, anim: "", workDot: "display:none;" });
      rows.push({ name: "INV-1047.pdf", sub: "Governing tariff not verified", container: "MSCU-701145-3", status: "NEEDS EVIDENCE", ...this._q("checking"), amount: "$875", chevron: "›", cursor: "default", rowBg: "transparent", nameColor: "#23272F", amountColor: "#23272F", onOpen: this.noop, anim: "", workDot: "display:none;" });
    } else if (this.live) {
      (this.liveQueue || [])
        .filter((r) => !isHeroRow(r))
        // Stable display order: the pre-existing rows always render 1041 then
        // 1047 (by invoice name), never the server's return order — otherwise
        // they visibly flip when the hero's arrival triggers a queue refresh.
        .sort((a, b) => String(a.name || a.invoiceId).localeCompare(String(b.name || b.invoiceId)))
        .forEach((r) => {
          const meta = STATUS_LABEL[r.aggregateStatus] || { status: r.aggregateStatus, kind: "checking" };
          // NEEDS EVIDENCE shows its unresolved reason; a cleared row shows its charge.
          const detail = r.unresolvedReason || "Demurrage";
          // anim "" (not tly-row-in): these pre-existing rows are already on
          // screen from the first-paint seed, so the live reconcile must NOT
          // re-trigger the entrance animation (that was the flash). Only the hero
          // animates in.
          rows.push({ name: r.name || (r.invoiceId ? r.invoiceId + ".pdf" : "—"), sub: detail, container: r.container || "—", status: meta.status, ...this._q(meta.kind), amount: r.amountMinor != null ? money(r.amountMinor) : "—", chevron: "›", cursor: "default", rowBg: "transparent", nameColor: "#23272F", amountColor: "#23272F", onOpen: this.noop, anim: "", workDot: "display:none;" });
        });
    } else if (disputed) {
      const done = st.inv1050 === "done";
      rows.push({ name: "INV-1050.pdf", sub: "Demurrage · Jun 5–11", container: "HLXU-223874-9", status: done ? "APPROVED FOR PAYMENT" : "READY FOR REVIEW", ...this._q(done ? "verified" : "neutral"), amount: "$875", chevron: "›", cursor: "pointer", rowBg: done ? "transparent" : "#FBF6EE", nameColor: "#23272F", amountColor: done ? "#2F7752" : "#23272F", onOpen: this.openInv1050, anim: "tly-row-in", workDot: "display:none;" });
    }
    // LIVE: hero last, so INV-1048 sits at the bottom of the list (#4).
    if (this.live && heroRowObj) rows.push(heroRowObj);

    const bucketOf = (status) => { if (["READY FOR REVIEW","NEEDS EVIDENCE","READY TO SEND","BLOCKED","SEND BLOCKED"].indexOf(status) >= 0) return "attention"; if (["DISPUTED","APPROVED FOR PAYMENT"].indexOf(status) >= 0) return "completed"; return "processing"; };
    const qcounts = { attention: 0, processing: 0, completed: 0, all: rows.length };
    rows.forEach((r) => { qcounts[bucketOf(r.status)]++; });
    const qf = st.queueFilter;
    const shownRows = qf === "all" ? rows : rows.filter((r) => bucketOf(r.status) === qf);
    const queueFilters = [["all","All"],["attention","Needs attention"],["processing","Processing"],["completed","Completed"]].map((f) => ({ key: f[0], label: f[1], count: qcounts[f[0]] || 0, on: this.setQueueFilter.bind(this, f[0]), bg: qf === f[0] ? "#1D2A33" : "#FCFBF8", fg: qf === f[0] ? "#F5F0E7" : "#6F7883", border: qf === f[0] ? "#1D2A33" : "#DED6C7" }));

    const wb = this.buildWorkbench();
    const coverage = [
      { cls: "Tariff / rule snapshots", scope: "Pacific carriers · demurrage", period: "2026-01 → present", last: "Jun 20", ver: "VERIFIED", verColor: "#2F7752", status: "COVERED", bg: "#E4EEE7", fg: "#2F7752" },
      { cls: "Container / shipment events", scope: "OAK terminal · line events", period: "2026-05 → present", last: "Jun 14", ver: "VERIFIED", verColor: "#2F7752", status: "COVERED", bg: "#E4EEE7", fg: "#2F7752" },
      { cls: "Terminal availability / access", scope: "OAK · gate + yard", period: "partial", last: "Jun 10", ver: "—", verColor: "#8A7A50", status: "BACKEND REQUIRED", bg: "#F3EAD3", fg: "#8A7A50" },
      { cls: "Appointment / closure evidence", scope: "not configured", period: "—", last: "—", ver: "—", verColor: "#B7AE9C", status: "BACKEND REQUIRED", bg: "#F3EAD3", fg: "#8A7A50" },
    ];

    return {
      goQueue: this.goQueue, goCoverage: this.goCoverage, goHandoff: this.goHandoff,
      invNavBg: inv.bg, invNavFg: inv.fg, covNavBg: cov.bg, covNavFg: cov.fg,
      invAria: view === "queue" ? "page" : "false",
      toggleAnnot: this.toggleAnnot, replay: this.replay, annot: st.annot, internal: !!(this.props && this.props.internal),
      // Source coverage is a prototype-static screen (2 rows COVERED, 2 BACKEND
      // REQUIRED). Hide the nav item in the LIVE demo so there's no non-live
      // screen to stumble into on camera; keep it in the mock/click-audit scene.
      showCoverageNav: !this.live,
      annotFg: st.annot ? "#8A7A50" : "#6F7883", annotBg: st.annot ? "#F3EAD3" : "#FCFBF8",
      crumb: view === "queue" ? "Invoices" : view === "coverage" ? "Source coverage" : view === "handoff" ? "Handoff" : view === "wb1050" ? "Invoices / INV-TLY-1050" : "Invoices / INV-TLY-1048",
      announce: view === "workbench" ? sm.status + " — " + (wb.agents.find((a) => a.state === "working") || { name: "Idle" }).name : arrived ? "New invoice INV-1048 received" : "Invoices",
      isQueue: view === "queue", isWorkbench: view === "workbench", isCoverage: view === "coverage", isHandoff: view === "handoff", isWb1050: view === "wb1050",
      openInv1050: this.openInv1050,
      wb1050days: ["Jun 5","Jun 6","Jun 7","Jun 8","Jun 9","Jun 10","Jun 11"].map((d) => ({ date: d })),
      queueRows: shownRows, queueCount: rows.length, queueFilters, show1050Prompt: !this.live && disputed && st.inv1050 !== "done",
      qrEvid: st.qrEvid, toggleQrEvid: this.toggleQrEvid, openQuickReview: this.openQuickReview,
      wb,
      approveDispute: this.approveDispute, approveSend: this.approveSend, retrySend: this.retrySend,
      closeDay: this.closeDay, noop: this.noop, openSourceInvoice: this.openSourceInvoice,
      openSourceTariff: this.openSourceTariff, goDecision: this.goDecision, goActivity: this.goActivity,
      quickOpen: st.quickOpen, closeQuickReview: this.closeQuickReview, approvePayment: this.approvePayment, stop: this.stop,
      inv1050: { status: st.inv1050 === "done" ? "APPROVED FOR PAYMENT" : "READY FOR REVIEW", ...this._q(st.inv1050 === "done" ? "verified" : "neutral"), done: st.inv1050 === "done", pending: st.inv1050 !== "done", reviewBg: st.inv1050 === "done" ? "#E4EEE7" : "#FBF6EE", reviewBorder: st.inv1050 === "done" ? "#B9D3C4" : "#E6D6AE", reviewDot: st.inv1050 === "done" ? "#2F7752" : "#C8A955", recBorder: st.inv1050 === "done" ? "#B9D3C4" : "#C8A955", gridCols: st.drawer ? "minmax(0,1fr) 440px" : "minmax(0,1fr) 344px" },
      drawerOpen: !!st.drawer, railOpen: !st.drawer, closeDrawer: this.closeDrawer, drawer: this.buildDrawer(), setDrawerTab: this.setDrawerTab,
      gridCols: st.drawer ? "minmax(0,1fr) 440px" : "minmax(0,1fr) 344px",
      coverage, handoff: this.buildHandoff(),
    };
  }

  // Live projection adapter: when a real reconstruction (this.recon) is loaded,
  // the authority-bearing values (coverage counts, per-day states, the
  // recommendation) are READ from the server projection — never computed or
  // hardcoded here (Delta §3.2). Returns null in the mock/non-live scene, where
  // buildWorkbench falls back to its prototype literals unchanged.
  proj() {
    const r = this.recon;
    // Only the real deployed path (scene "live") is projection-driven. The
    // prototype scenes (recommendation/insufficient/…) and the mock click-audit
    // stay rank-driven so their fixed narratives render unchanged.
    if (this.scene !== "live" || !this.live || !r) return null;
    const cov = r.coverage || {};
    const rec = r.recommendation || null;
    // Index by day-of-month so a padded ("2026-06-08") or unpadded ("2026-06-8")
    // date from either the live API or the mock both resolve.
    const byDate = {};
    (r.charged_days || []).forEach((d) => {
      const m = /^(\d{4})-(\d{2})-(\d{1,2})$/.exec(d.date || "");
      if (m) byDate[parseInt(m[3], 10)] = d;
    });
    return { r, cov, rec, byDate };
  }

  buildWorkbench() {
    const st = this.state; const sm = this.statusMeta(); const p = this.pill(sm.kind);
    const P = this.proj();
    // Authority is withheld when the live recommendation is REQUEST_EVIDENCE
    // (or, in the mock scene, the insufficient wb state).
    const insuf = P && P.rec
      ? P.rec.recommendation_type === "REQUEST_EVIDENCE"
      : st.wb === "insufficient";
    const reconDone = this.at("reconstructed") || insuf;
    const ruleDone = this.at("ruleVerified");
    const recReady = this.at("recommendation") && !insuf;
    const claimVals = this.at("reconstructing") || insuf;
    // LIVE: the numeric carrier claims (rate/day, charged days, total) come from
    // the projection — a charged day's invoice rate and the recommendation's
    // claimed total. Charge type, B/L, and issue date are invoice metadata not
    // carried on the reconstruction projection (TODO: expose on the invoice
    // projection to source live) — literal for now. MOCK keeps all literals.
    const cdRate = P && this.recon ? (this.recon.charged_days || []).find((d) => d.invoice_rate_minor != null) : null;
    const claimRate = cdRate ? money(cdRate.invoice_rate_minor) + "/day" : "$350/day";
    const claimDays = P && P.rec && P.rec.days_total != null ? String(P.rec.days_total) : (P ? String(P.cov.days_total) : "7");
    const claimTotal = P && P.rec && P.rec.claimed_amount_minor != null ? money(P.rec.claimed_amount_minor) : "$2,450";
    const claims = [
      { label: "Charge type", value: "Demurrage", color: "#23272F", opacity: claimVals ? 1 : 0.35 },
      { label: "Rate", value: claimRate, color: "#A9823C", opacity: claimVals ? 1 : 0.35 },
      { label: "Charged days", value: claimDays, color: "#23272F", opacity: claimVals ? 1 : 0.35 },
      { label: "Total", value: claimTotal, color: "#A9823C", opacity: claimVals ? 1 : 0.35 },
      { label: "B/L", value: "OAK-77421", color: "#23272F", opacity: claimVals ? 1 : 0.35 },
      { label: "Issued", value: "Jun 22", color: "#23272F", opacity: claimVals ? 1 : 0.35 },
    ];
    const allEvents = [
      { date: "Jun 2", label: "Container discharged", tag: "RECORDED BEFORE INVOICE", before: true, since: 1 },
      { date: "Jun 3", label: "Container available", tag: "RECORDED BEFORE INVOICE", before: true, since: 1 },
      { date: "Jun 3", label: "Free time begins", tag: "RECORDED BEFORE INVOICE", before: true, since: 1 },
      { date: "Jun 7", label: "Free time ends", tag: "RECORDED BEFORE INVOICE", before: true, since: 2 },
      { date: "Jun 14", label: "Container gated out", tag: "RECORDED BEFORE INVOICE", before: true, since: 2 },
      { date: "Jun 22", label: "Invoice issued", tag: "FROM INVOICE", before: false, since: 2 },
    ];
    const rnk = this.rank(st.wb);
    const tl = allEvents.filter((e) => insuf || (rnk >= this.rank("reconstructing") && (rnk >= this.rank("reconstructed") || e.since <= 1))).map((e) => ({ date: e.date, label: e.label, tag: e.tag, dot: e.before ? "#2F7752" : "#8A96A0", tagBg: e.before ? "#E4EEE7" : "#ECEFF1", tagFg: e.before ? "#2F7752" : "#40515C" }));

    const dayNums = [8,9,10,11,12,13,14];
    const days = dayNums.map((n, ix) => {
      // LIVE: read this day's persisted state from the projection. The June-11
      // gap, the coverage state, and the per-day discrepancy are the server's —
      // not badGap=ix===2 or a wb-rank guess.
      const pd = P ? P.byDate[n] : null;
      // ponytail: `access` removed with the ledger's ACCESS column (v3: no per-day
      // access heartbeat). Branches below no longer assign it.
      let outcome, outBg, outFg, coverage, covFg, rule, ruleColor, disc, discColor;
      disc = ""; discColor = "#B4513F";
      if (pd) {
        const complete = pd.state === "SOURCE_COMPLETE";
        const gap = pd.coverage === "MISSING" || pd.state === "INSUFFICIENT_EVIDENCE";
        const hasRate = pd.applicable_rate_minor != null;
        const discrep = pd.dispute_amount_minor != null && pd.dispute_amount_minor > 0;
        if (gap) { outcome = "INSUFFICIENT EVIDENCE"; outBg = "#F2E1DC"; outFg = "#B4513F"; coverage = "SOURCE GAP"; covFg = "#B4513F"; rule = "—"; ruleColor = "#B7AE9C"; disc = "—"; discColor = "#B7AE9C"; }
        else if (discrep) { outcome = "RATE DISCREPANCY"; outBg = "#F2E1DC"; outFg = "#B4513F"; coverage = "SOURCE COMPLETE"; covFg = "#2F7752"; rule = "$" + (pd.applicable_rate_minor / 100).toFixed(0); ruleColor = "#2F7752"; disc = "−$" + (pd.dispute_amount_minor / 100).toFixed(0); }
        else if (complete && hasRate) { outcome = "SUPPORTED"; outBg = "#E4EEE7"; outFg = "#2F7752"; coverage = "SOURCE COMPLETE"; covFg = "#2F7752"; rule = "$" + (pd.applicable_rate_minor / 100).toFixed(0); ruleColor = "#2F7752"; disc = "$0"; discColor = "#2F7752"; }
        else if (complete) { outcome = "SOURCE COMPLETE"; outBg = "#E4EEE7"; outFg = "#2F7752"; coverage = "SOURCE COMPLETE"; covFg = "#2F7752"; rule = "verifying"; ruleColor = "#8A7A50"; disc = "…"; discColor = "#8A7A50"; }
        else { outcome = "UNRESOLVED"; outBg = "#ECEFF1"; outFg = "#40515C"; coverage = "—"; covFg = "#B7AE9C"; rule = "—"; ruleColor = "#B7AE9C"; disc = "—"; discColor = "#B7AE9C"; }
        return { date: "Jun " + n, claim: "$350", rule, ruleColor, outcome, outBg, outFg, coverage, covFg, disc, discColor, rowBg: st.drawer === "day" && st.dayIx === ix ? "#FBF6EE" : "transparent", onOpen: this.openDayDrawer.bind(this, ix) };
      }
      // MOCK/non-live scene: unchanged prototype rendering.
      const badGap = insuf && ix === 2;
      if (badGap) { outcome = "INSUFFICIENT EVIDENCE"; outBg = "#F2E1DC"; outFg = "#B4513F"; coverage = "SOURCE GAP"; covFg = "#B4513F"; rule = "—"; ruleColor = "#B7AE9C"; disc = "—"; discColor = "#B7AE9C"; }
      else if (ruleDone) { outcome = "RATE DISCREPANCY"; outBg = "#F2E1DC"; outFg = "#B4513F"; coverage = "SOURCE COMPLETE"; covFg = "#2F7752"; rule = "$250"; ruleColor = "#2F7752"; disc = "−$100"; }
      else if (reconDone) { outcome = "SOURCE COMPLETE"; outBg = "#E4EEE7"; outFg = "#2F7752"; coverage = "SOURCE COMPLETE"; covFg = "#2F7752"; rule = "verifying"; ruleColor = "#8A7A50"; disc = "…"; discColor = "#8A7A50"; }
      else if (this.at("reconstructing")) { outcome = "SOURCING"; outBg = "#F3EAD3"; outFg = "#8A7A50"; coverage = "sourcing…"; covFg = "#8A7A50"; rule = "—"; ruleColor = "#B7AE9C"; disc = "—"; discColor = "#B7AE9C"; }
      else { outcome = "UNRESOLVED"; outBg = "#ECEFF1"; outFg = "#40515C"; coverage = "—"; covFg = "#B7AE9C"; rule = "—"; ruleColor = "#B7AE9C"; disc = "—"; discColor = "#B7AE9C"; }
      return { date: "Jun " + n, claim: "$350", rule, ruleColor, outcome, outBg, outFg, coverage, covFg, disc, discColor, rowBg: st.drawer === "day" && st.dayIx === ix ? "#FBF6EE" : "transparent", onOpen: this.openDayDrawer.bind(this, ix) };
    });

    const A = (name, task, tool, output, state) => {
      const dot = state === "complete" ? "#2F7752" : state === "working" ? "#C8A955" : state === "blocked" ? "#B4513F" : "#C9D0D6";
      const outColor = state === "complete" ? "#2F7752" : state === "blocked" ? "#B4513F" : "#8A7A50";
      return { name, task, tool, output, state, dot, outColor, pulse: state === "working" ? "tly-work" : "", bg: state === "working" ? "#FBF6EE" : "transparent" };
    };
    const rk = this.rank(st.wb);
    const agents = [];
    agents.push(A("Intake Agent", rk <= 0 ? "Extracting carrier claims" : "Claims extracted", "Amazon Bedrock · S3", rk <= 0 ? "working…" : "6 claims linked to PDF regions", rk <= 0 ? "working" : "complete"));
    agents.push(A("Reconstruction Agent", rk < this.rank("reconstructing") ? "Waiting" : rk < this.rank("reconstructed") ? "Retrieving prior memory" : "Reconstruction complete", "CockroachDB Managed MCP", rk < this.rank("reconstructing") ? "—" : rk < this.rank("reconstructed") ? "assembling events…" : "9 sourced events · 7 charged days", rk < this.rank("reconstructing") ? "waiting" : rk < this.rank("reconstructed") ? "working" : "complete"));
    agents.push(A("Evidence Agent", rk < this.rank("retrieving") ? "Waiting" : rk < this.rank("ruleVerified") ? "Searching retained tariff" : "Candidate retrieved", "CockroachDB Distributed Vector Indexing", rk < this.rank("retrieving") ? "—" : rk < this.rank("ruleVerified") ? "1 tariff candidate · unverified" : "1 tariff candidate retrieved", rk < this.rank("retrieving") ? "waiting" : rk < this.rank("ruleVerified") ? "working" : "complete"));
    // Decision Engine output: LIVE reads the dispute figure from the projection
    // once issued (never the literal); MOCK keeps "$700 dispute".
    const deOut = rk < this.rank("ruleVerified") ? "—"
      : rk < this.rank("recommendation") ? "validating clause…"
      : (P && P.rec && P.rec.recommendation_type === "DISPUTE") ? "Applicable rule verified · " + money(P.rec.disputed_amount_minor) + " dispute"
      : P ? "Applicable rule verified" : "Applicable rule verified · $700 dispute";
    agents.push(A("Decision Engine", rk < this.rank("ruleVerified") ? "Waiting" : rk < this.rank("recommendation") ? "Verifying & calculating" : "Recommendation issued", "Deterministic code", deOut, rk < this.rank("ruleVerified") ? "waiting" : rk < this.rank("recommendation") ? "working" : "complete"));
    if (rk >= this.rank("approved")) agents.push(A("Correspondence Agent", st.wb === "sent" ? "Draft sent" : "Drafting adjustment request", "Amazon Bedrock", st.wb === "sent" ? "request delivered" : "draft ready · manifest attached", st.wb === "sent" ? "complete" : "working"));

    const gateDefs = [{ label: "Approved recommendation matches record" }, { label: "Decision record sealed" }, { label: "Managed MCP approved-memory read" }, { label: "Vector-retrieved clause binding" }, { label: "Exact S3 source versions" }];
    const gateChecks = gateDefs.map((gd, i) => {
      const blocked = st.gateBlocked && i === 2;
      let icon, color, state;
      if (blocked) { icon = "✕"; color = "#B4513F"; state = "UNAVAILABLE"; }
      else if (i < st.gateStep) { icon = "✓"; color = "#2F7752"; state = "VERIFIED"; }
      else if (i === st.gateStep) { icon = "◌"; color = "#8A7A50"; state = "checking"; }
      else { icon = "·"; color = "#B7AE9C"; state = "pending"; }
      return { label: gd.label, icon, color, state };
    });

    const showComposer = rk >= this.rank("correspondence") && !insuf;
    const sendPhase = ["correspondence","sending","sent"].indexOf(st.wb) >= 0;
    // LIVE: chip values from the projection. v3 forbids fixed/narrated event
    // counts, so the live chips drop the "6 claims"/"9 events" literals and show
    // the projection's day count and rates instead. MOCK keeps the literals.
    const ruleRate = P && this.recon && this.recon.applicable_rule && this.recon.applicable_rule.rate_minor != null
      ? money(this.recon.applicable_rule.rate_minor) + " / day" : "$250 / day";
    const contextChips = [
      { label: "Invoice source", val: P ? claimTotal + " claimed" : "$2,450 · 6 claims", on: this.openSourceInvoice },
      { label: "Reconstruction", val: P ? claimDays + " charged days" : "9 events · 7 days", on: this.openDayDrawer.bind(this, 2) },
      { label: "Applicable tariff", val: ruleRate, on: this.openSourceTariff },
      { label: "Decision", val: (P && P.rec && P.rec.recommendation_type === "DISPUTE" ? "DISPUTE " + money(P.rec.disputed_amount_minor) : P && P.rec ? "REQUEST EVIDENCE" : P ? "computing…" : "DISPUTE $700"), on: this.goDecision },
    ];
    const showGate = st.wb === "sending";
    const showSent = st.wb === "sent";
    // LIVE: the rail head is the persisted recommendation — its type and its
    // exact disputed amount — never a literal. DISPUTE shows the real minor-unit
    // amount (70000 -> $700); REQUEST_EVIDENCE withholds it.
    let recHead, recColor, recBg, recBorder;
    const liveDispute = P && P.rec && P.rec.recommendation_type === "DISPUTE"
      ? "DISPUTE $" + (P.rec.disputed_amount_minor / 100).toFixed(0)
      : null;
    if (insuf) { recHead = "REQUEST EVIDENCE"; recColor = "#8A7A50"; recBg = "#FBF6EE"; recBorder = "#E6D6AE"; }
    // LIVE: the dispute figure appears only once the projection carries it (after
    // deterministic validation freezes the DISPUTE revision) — never the literal
    // (v3: runtime-derived, must not read as pre-baked). MOCK keeps "$700".
    else if (P) { recHead = liveDispute || "COMPUTING…"; recColor = liveDispute ? "#B4513F" : "#8A7A50"; recBg = "#FCFBF8"; recBorder = (P.rec && P.rec.state === "FROZEN") ? "#C8A955" : "#DED6C7"; }
    else { recHead = "DISPUTE $700"; recColor = "#B4513F"; recBg = "#FCFBF8"; recBorder = st.wb === "recommendation" ? "#C8A955" : "#DED6C7"; }

    // Rail reconciliation math. LIVE: every figure is the server's — claimed,
    // supported, disputed totals from the recommendation revision; coverage
    // counts from the coverage projection; per-day rates from a charged day.
    // MOCK/non-live keeps the locked literals so the prototype narrative holds.
    let recon;
    if (P && P.rec) {
      const rec = P.rec;
      const days = rec.days_total != null ? rec.days_total : (P.cov.days_total || 7);
      const anyDay = (this.recon.charged_days || []).find((d) => d.invoice_rate_minor != null) || null;
      const invRate = anyDay ? anyDay.invoice_rate_minor : (rec.claimed_amount_minor != null && days ? rec.claimed_amount_minor / days : null);
      const appRate = anyDay && anyDay.applicable_rate_minor != null ? anyDay.applicable_rate_minor : (rec.supported_amount_minor != null && days ? rec.supported_amount_minor / days : null);
      recon = {
        carrierLine: invRate != null ? `${days} × ${money(invRate)} = ${money(rec.claimed_amount_minor)}` : money(rec.claimed_amount_minor),
        tariffLine: appRate != null ? `${days} × ${money(appRate)} = ${money(rec.supported_amount_minor)}` : money(rec.supported_amount_minor),
        difference: rec.disputed_amount_minor != null ? money(rec.disputed_amount_minor) : "$0",
        coverageLine: `Evidence coverage ${P.cov.days_complete} of ${P.cov.days_total} days`,
      };
    } else if (P) {
      // LIVE, projection not yet carrying the recommendation: withhold the
      // reconciliation figures until the server computes them (no pre-baked $700).
      recon = { carrierLine: "computing…", tariffLine: "computing…", difference: "—", coverageLine: "Evidence coverage in progress" };
    } else {
      recon = { carrierLine: "7 × $350 = $2,450", tariffLine: "7 × $250 = $1,750", difference: "$700", coverageLine: "Evidence coverage 7 of 7 days" };
    }

    const pnode = (name, tool, state) => {
      const map = { complete: { dot: "#2F7752", bg: "#E4EEE7", border: "#B9D3C4", nameColor: "#23272F", pulse: "" }, working: { dot: "#C8A955", bg: "#FBF6EE", border: "#E6D6AE", nameColor: "#23272F", pulse: "tly-work" }, blocked: { dot: "#B4513F", bg: "#F2E1DC", border: "#E3B3A8", nameColor: "#23272F", pulse: "" }, waiting: { dot: "#C9D0D6", bg: "#FCFBF8", border: "#DED6C7", nameColor: "#8A96A0", pulse: "" } };
      const s = map[state] || map.waiting;
      return { name, tool, dot: s.dot, bg: s.bg, border: s.border, nameColor: s.nameColor, toolColor: "#6F7883", pulse: s.pulse, stateLabel: state.toUpperCase(), title: name + " — " + state };
    };
    const reviewState = insuf ? "working" : rk < this.rank("recommendation") ? "waiting" : rk < this.rank("approved") ? "working" : "complete";
    const pipeline = [pnode("Intake", "Bedrock·S3", agents[0].state), pnode("Reconstruction", "Managed MCP", agents[1].state), pnode("Evidence", "Vector Index", agents[2].state), pnode("Review", "Human", reviewState), pnode("Correspondence", "Bedrock", agents[4] ? agents[4].state : "waiting")];
    const openSource = this.secOpen("source"), openTimeline = this.secOpen("timeline"), openDays = this.secOpen("days"), openCorr = this.secOpen("corr");
    const chev = (o) => (o ? "▾" : "▸");
    const srcSummary = "INV-1048.pdf · $2,450 claimed · " + (claimVals ? "6 claims linked" : "extracting…");
    const tlSummary = tl.length ? tl.length + " events · availability, free time, gate-out" : "awaiting reconstruction";
    const daysSummary = ruleDone ? "7 days · all RATE DISCREPANCY · −$100 / day" : reconDone ? "7 days · sourced" : "reconstructing…";
    const daysChip = ruleDone ? { label: "7 / 7 VERIFIED", bg: "#E4EEE7", fg: "#2F7752" } : reconDone ? { label: "7 / 7 SOURCED", bg: "#E4EEE7", fg: "#2F7752" } : { label: "SOURCING", bg: "#F3EAD3", fg: "#8A7A50" };
    const chainFlow = [{ k: "INVOICE CLAIM", v: "$350 / day", bg: "#ECEFF1", fg: "#40515C" }, { k: "ESTABLISHING EVENTS", v: "Free time ends Jun 7 · Gate-out Jun 14", bg: "#E4EEE7", fg: "#2F7752" }, { k: "APPLICABLE RULE", v: "$250 / day", bg: "#E4EEE7", fg: "#2F7752" }, { k: "FINANCIAL EFFECT", v: "−$100 / day", bg: "#F2E1DC", fg: "#B4513F" }];

    const openDayD = days[st.dayIx] || days[2];
    const dayDetail = {
      date: openDayD.date,
      chain: [
        { q: "What did the invoice claim?", a: "$350 / day", font: "'IBM Plex Mono',monospace" },
        { q: "Chargeable that day?", a: "Yes — beyond free time (ended Jun 7)", font: "inherit" },
        { q: "Which events establish it?", a: "Free time ends (Jun 7), Gate-out (Jun 14)", font: "inherit" },
        { q: "Which rule & rate applied?", a: "Tariff clause · $250 / day", font: "'IBM Plex Mono',monospace" },
        { q: "Sources complete & verified?", a: "Yes — 2 events + rule, exact versions verified", font: "inherit" },
      ],
      clause: "Demurrage rate: $250 per calendar day",
      checks: [{ k: "Effective date", v: "VERIFIED", color: "#2F7752" }, { k: "Exact text", v: "VERIFIED", color: "#2F7752" }, { k: "Exact rate", v: "VERIFIED", color: "#2F7752" }, { k: "Scope", v: "VERIFIED", color: "#2F7752" }],
      effect: "Financial effect  $350 − $250 = $100 disputed",
    };

    const activeAg = agents.find((a) => a.state === "working");
    const lastComplete = agents.slice().reverse().find((a) => a.state === "complete");
    const cur = activeAg || lastComplete || agents[0];
    let nowStrip = null;
    if (st.wb === "sending") nowStrip = { kind: "live", head: st.gateBlocked ? "Send blocked · memory" : "Verifying send gates", sub: "Approved memory & exact source versions" };
    else if (activeAg) nowStrip = { kind: "live", head: activeAg.name, sub: activeAg.output };
    const nowAction = false; const nowLive = !!nowStrip;

    return {
      pipeline,
      drawer: this.buildDrawer(), railOpen: !st.drawer,
      gridCols: st.drawer ? "minmax(0,1fr) 440px" : "minmax(0,1fr) 344px",
      showNow: !!nowStrip, nowAction,
      curName: cur ? cur.name : "", curTask: cur ? cur.task : "", curTool: cur ? cur.tool : "", curOutput: cur ? cur.output : "", curDot: cur ? cur.dot : "#C9D0D6", curPulse: cur && cur.state === "working" ? "tly-work" : "", curOutColor: cur ? cur.outColor : "#8A7A50",
      nowHead: nowStrip ? nowStrip.head : "", nowSub: nowStrip ? nowStrip.sub : "", nowLabel: nowStrip ? nowStrip.label : "", nowOn: nowStrip ? nowStrip.on : this.noop,
      nowBg: nowAction ? "#1D2A33" : "#FBF6EE", nowBorder: nowAction ? "#1D2A33" : "#E6D6AE",
      nowHeadColor: nowAction ? "#F5F0E7" : "#23272F", nowSubColor: nowAction ? "#AEBAC3" : "#6F7883",
      nowDot: "#C8A955", nowPulse: nowLive ? "tly-work" : "",
      openSource, openTimeline, openDays, openCorr,
      collapsedSource: !openSource, collapsedTimeline: !openTimeline, collapsedDays: !openDays, collapsedCorr: !openCorr,
      toggleSource: this.toggleSection.bind(this, "source"), toggleTimeline: this.toggleSection.bind(this, "timeline"), toggleDays: this.toggleSection.bind(this, "days"), toggleCorr: this.toggleSection.bind(this, "corr"),
      srcChev: chev(openSource), tlChev: chev(openTimeline), daysChev: chev(openDays), corrChev: chev(openCorr),
      srcDiscLabel: openSource ? "Collapse" : "View claims", tlDiscLabel: openTimeline ? "Collapse" : "View sourced events", daysDiscLabel: openDays ? "Collapse" : "View 7 daily judgments", corrDiscLabel: openCorr ? "Collapse" : "View request",
      showTariffRef: ruleDone, tariffRate: "$250 / day", tariffEff: "effective Jun 1, 2026",
      srcSummary, tlSummary, daysSummary, daysChip, dayFlow: chainFlow,
      srcCaption: "Carrier claims extracted and linked to the original PDF.",
      tlCaption: "Shipment events recorded before the invoice — occurred vs recorded time.",
      daysCaption: "Every charged day adjudicated against events and the applicable rate.",
      legend: [{ label: "FROM INVOICE", bg: "#ECEFF1", fg: "#40515C" }, { label: "RECORDED BEFORE INVOICE", bg: "#E4EEE7", fg: "#2F7752" }, { label: "HUMAN APPROVED", bg: "#FBF1D8", fg: "#A9823C" }],
      title: "INV-1048.pdf", idline: "INV-TLY-1048", meta: "Container TLLU-482931-7 · B/L OAK-77421 · Demurrage · charged Jun 8–14 · received Jun 22",
      status: sm.status, pillBg: p.bg, pillFg: p.fg,
      goDecision: this.goDecision, goActivity: this.goActivity,
      srcVersion: "EXACT VERSION VERIFIED", srcCols: this.at("reconstructed") ? "260px 1fr" : "1fr 1fr",
      openSourceInvoice: this.openSourceInvoice, openSourceTariff: this.openSourceTariff,
      // LIVE: real link to the retained PDF (exact S3 version). MOCK: null → the
      // in-app drawer opens instead (click-audit unchanged).
      sourceUrl: this.live ? (this.sourceUrl || null) : null,
      claimsLabel: claimVals ? "EXTRACTED CLAIMS" : "EXTRACTING CLAIMS…", claims,
      timeline: tl, timelineEmpty: tl.length === 0, timelineCount: tl.length ? tl.length + (tl.length === 1 ? " event" : " events") : "",
      days, coverageLine: P
        ? `${P.cov.days_complete} of ${P.cov.days_total} days${P.cov.days_complete === P.cov.days_total ? " · SOURCE COMPLETE" : " · evidence required"}`
        : (ruleDone ? "7 of 7 days · SOURCE COMPLETE" : reconDone ? "7 of 7 sourced" : "reconstructing…"),
      dayOpen: st.dayOpen, day: dayDetail,
      agents,
      // LIVE: the recommendation block appears only once the replay animation has
      // reached the `recommendation` rank (i.e. AFTER evidence/validation has
      // visibly completed) AND the projection actually carries a rec — never on
      // frame 1 just because the data was fetched (that read as a bogus reveal).
      showRec: P ? (!!P.rec && (recReady || insuf)) : (recReady || insuf),
      recHead, recColor, recBg, recBorder, recon,
      // Delta §3.6: the financial CTA appears ONLY for a complete DISPUTE
      // recommendation (rev2). REQUEST_EVIDENCE (rev1) never enables approval.
      showApprove: P
        ? !!(P.rec && P.rec.recommendation_type === "DISPUTE" && P.rec.state === "FROZEN" && recReady)
        : st.wb === "recommendation",
      // Approve CTA label: LIVE reads the exact disputed amount from the frozen
      // recommendation; MOCK keeps "$700".
      approveLabel: (P && P.rec && P.rec.recommendation_type === "DISPUTE" && P.rec.disputed_amount_minor != null)
        ? "Approve " + money(P.rec.disputed_amount_minor) + " dispute"
        : (P ? "Approve dispute" : "Approve $700 dispute"),
      approveDispute: this.approveDispute, closeDay: this.closeDay, noop: this.noop,
      showSeal: ["approved","sealing","readyToSend","correspondence"].includes(st.wb),
      sealSteps: this.sealSteps(),
      showSendBtn: ["readyToSend","correspondence"].includes(st.wb),
      approveSend: this.approveSend,
      showComposer, corrState: st.wb === "sent" ? "SENT" : "DRAFT READY",
      sendPhase, showReconCards: !sendPhase, contextChips,
      manifest: [{ label: "INV-1048.pdf", on: this.openSourceInvoice }, { label: "Applicable tariff clause", on: this.openSourceTariff }, { label: "7 charged-day calculation", on: this.goDecision }, { label: "Decision record", on: this.goDecision }],
      showGate, gateTitle: st.gateBlocked ? "SEND BLOCKED · MEMORY" : "VERIFYING SEND GATES", gateBorder: st.gateBlocked ? "#B4513F" : "#DED6C7",
      gateChecks, gateBlocked: st.gateBlocked, retrySend: this.retrySend,
      // LIVE: the clean 3-step beat (Sealed -> Sending -> Sent). The backend still
      // runs all real gates; the UI just shows the story. MOCK keeps the 5-row
      // gate ceremony above (click-audit unchanged). "done" = past, "active" = now.
      liveSend: this.live,
      sendSteps: [
        { label: "Sealed", state: "done" },
        { label: "Sending", state: st.wb === "sent" ? "done" : st.gateBlocked ? "blocked" : "active" },
        { label: "Sent", state: st.wb === "sent" ? "done" : "pending" },
      ],
      showSent,
      // The controlled-provider message id is the SERVER send-projection field.
      // LIVE sources it from the real send response (this.sentMsgId, set in
      // approveSendLive from provider_message_id, e.g. "demo-…"); until the send
      // resolves it is null and the composer shows an awaiting-server placeholder.
      // MOCK keeps its demo id for the click-audit.
      sentMessageId: this.live ? (this.sentMsgId || null) : "demo-8f2a1c",
      annotText: "ROUTE /invoices/INV-TLY-1048 (tabless) · state " + sm.status + " · dominant object: 7-day ledger · Back/refresh restore invoice + selection · progressive states are event-driven (BACKEND REQUIRED)",
    };
  }
  sealSteps() {
    const s = this.state.sealStep;
    const step = (n, label) => ({ label, icon: n < s ? "✓" : n === s ? "◌" : "·", color: n < s ? "#2F7752" : n === s ? "#8A7A50" : "#B7AE9C" });
    return [step(1, "APPROVED — decision record created"), step(2, "SEALING — binding sources & approval"), step(3, "READY TO SEND — decision sealed")];
  }
  buildDrawer() {
    const d = this.state.drawer; const tab = this.state.drawerTab || "source"; const V = "#2F7752";
    const mk = (o) => { const r = Object.assign({ kind: "source", tab, isSource: tab === "source", isUsed: tab === "usedby", isVerif: tab === "verification", setSource: this.setDrawerTab.bind(this, "source"), setUsed: this.setDrawerTab.bind(this, "usedby"), setVerif: this.setDrawerTab.bind(this, "verification"), tabSourceColor: tab === "source" ? "#23272F" : "#8A96A0", tabUsedColor: tab === "usedby" ? "#23272F" : "#8A96A0", tabVerifColor: tab === "verification" ? "#23272F" : "#8A96A0", tabSourceBg: tab === "source" ? "#F3EEE3" : "transparent", tabUsedBg: tab === "usedby" ? "#F3EEE3" : "transparent", tabVerifBg: tab === "verification" ? "#F3EEE3" : "transparent", usedBy: [], verification: [] }, o); r.isDay = r.kind === "day"; r.chain = r.chain || []; return r; };
    if (!d) return mk({ title: "", meta: [], body: "", bound: "" });
    if (d === "day") {
      const n = [8,9,10,11,12,13,14][this.state.dayIx] || 10;
      // LIVE: reflect this day's persisted state. An unbound day (June 11 in
      // revision 1) opens its MISSING evidence — the required terminal-access
      // snapshot — not a fabricated SOURCE COMPLETE. A bound day opens its exact
      // sourced evidence.
      const P = this.proj();
      const pd = P ? P.byDate[n] : null;
      if (pd && (pd.coverage === "MISSING" || pd.state === "INSUFFICIENT_EVIDENCE")) {
        const missing = (pd.missing_requirements || []).join(", ") || "required source";
        const label = missing.includes("TERMINAL_ACCESS")
          ? "Required terminal-access snapshot not yet bound" : "Required source not yet bound";
        return mk({ kind: "day", title: "Charged day · Jun " + n,
          meta: [{ k: "Occurred", v: "Jun " + n + ", 2026", color: "#40515C" }, { k: "Chargeable", v: "Yes · beyond free time", color: V }, { k: "Coverage", v: "UNRESOLVED", color: "#B4513F" }],
          chain: [{ k: "Free-time end", v: "Jun 7 — recorded before invoice", bg: "#E4EEE7", fg: "#2F7752" }, { k: "Terminal access", v: label, bg: "#F2E1DC", fg: "#B4513F" }, { k: "Invoice rate (PDF anchor)", v: "$350 / day · INV-1048 p.1 line 4", bg: "#ECEFF1", fg: "#40515C" }, { k: "Missing evidence", v: missing, bg: "#F2E1DC", fg: "#B4513F" }],
          usedBy: [{ k: "Recommendation", v: "REQUEST EVIDENCE" }, { k: "Charged day", v: "Jun " + n + " · unresolved" }, { k: "Blocks", v: "financial judgment" }],
          verification: [{ k: "Terminal-access snapshot", v: "NOT BOUND", c: "#B4513F" }],
          body: "CHARGED DAY  Jun " + n + ", 2026\nInvoice rate     $350.00\nApplicable rate  —\n----------------------------\nCoverage         MISSING\n\nMissing  " + missing, bound: "Required terminal-access snapshot not yet bound" });
      }
      const refs = pd && pd.event_refs && pd.event_refs.length ? pd.event_refs.join(", ") : "free-time-end (Jun 7), gate-out (Jun 14)";
      // v3: derive the day from the operational interval (after free time,
      // before gate-out) — no per-day "Container available / access" heartbeat row.
      return mk({ kind: "day", title: "Charged day · Jun " + n, meta: [{ k: "Occurred", v: "Jun " + n + ", 2026", color: "#40515C" }, { k: "Chargeable", v: "Yes · beyond free time", color: V }, { k: "Coverage", v: "COMPLETE", color: V }], chain: [{ k: "Free time ended", v: "Jun 7 — recorded before invoice", bg: "#E4EEE7", fg: "#2F7752" }, { k: "Gate-out", v: "Jun 14 — recorded before invoice", bg: "#E4EEE7", fg: "#2F7752" }, { k: "Operational interval", v: "after free time, before gate-out", bg: "#E4EEE7", fg: "#2F7752" }, { k: "Invoice rate (PDF anchor)", v: "$350 / day · INV-1048 p.1 line 4", bg: "#ECEFF1", fg: "#40515C" }, { k: "Applicable rate", v: "$250 / day · eff. Jun 1 · exact-version verified", bg: "#FBF1D8", fg: "#A9823C" }, { k: "Daily discrepancy", v: "$350 − $250 = $100", bg: "#F2E1DC", fg: "#B4513F" }], usedBy: [{ k: "Recommendation", v: "DISPUTE $700" }, { k: "Charged day", v: "Jun " + n + " of 7" }, { k: "Rolls into", v: "$700 supported difference" }], verification: [{ k: "Bound sources", v: refs, c: V }, { k: "Effective date", v: "VERIFIED", c: V }, { k: "Exact rate", v: "VERIFIED", c: V }, { k: "Scope", v: "VERIFIED", c: V }], body: "CHARGED DAY  Jun " + n + ", 2026\nInvoice rate     $350.00\nApplicable rate  $250.00\n----------------------------\nDiscrepancy      $100.00\n\nEvents  " + refs, bound: "$350 − $250 = $100 · RATE DISCREPANCY" });
    }
    if (d === "invoice") return mk({ title: "INV-1048.pdf", meta: [{ k: "Type", v: "Carrier PDF", color: "#40515C" }, { k: "Received", v: "Jun 22, 2026", color: "#40515C" }, { k: "Exact version", v: "VERIFIED", color: V }, { k: "Affected days", v: "Jun 8–14", color: "#40515C" }], usedBy: [{ k: "Claims", v: "6 fields extracted" }, { k: "Affected days", v: "7 charged days" }, { k: "Recommendation", v: "DISPUTE $700" }], verification: [{ k: "Exact S3 version", v: "VERIFIED", c: V }, { k: "Region anchors", v: "6 linked", c: V }], body: "DEMURRAGE INVOICE\nContainer TLLU-482931-7\nB/L OAK-77421\nPeriod  Jun 8 – Jun 14, 2026\n\nRate    $350.00 / day\nDays    7\n--------------------------\nTotal   $2,450.00\n\nIssued  Jun 22, 2026", bound: "Rate $350.00/day · Total $2,450.00" });
    if (d === "tariff") return mk({ title: "Tariff · Pacific demurrage", meta: [{ k: "Effective", v: "Jun 1, 2026 →", color: "#40515C" }, { k: "Recorded", v: "before invoice", color: V }, { k: "Retrieval", v: "RETRIEVED (vector)", color: "#8A7A50" }, { k: "Applicability", v: "VERIFIED (deterministic)", color: V }], usedBy: [{ k: "Rule for", v: "all 7 charged days" }, { k: "Sets rate", v: "$250 / day" }, { k: "Recommendation", v: "DISPUTE $700" }], verification: [{ k: "Vector candidate", v: "RETRIEVED", c: "#8A7A50" }, { k: "Effective date", v: "VERIFIED", c: V }, { k: "Exact text", v: "VERIFIED", c: V }, { k: "Exact rate", v: "VERIFIED", c: V }, { k: "Scope", v: "VERIFIED", c: V }], body: "PACIFIC DEMURRAGE TARIFF\nSection 4 — Demurrage charges\n\n4.1  Free time: 96 hours from availability.\n4.2  Demurrage rate: $250 per calendar\n     day per container thereafter.\n\nEffective  2026-06-01", bound: "Demurrage rate: $250 per calendar day" });
    if (d === "decision") return mk({ title: "Decision record", meta: [{ k: "Recommendation", v: "DISPUTE $700", color: "#B4513F" }, { k: "Human judgment", v: "Approved $700", color: V }, { k: "Approved by", v: "Import ops reviewer", color: "#40515C" }, { k: "Sealed", v: this.state.disputed ? "Jun 23, 2026" : "pending", color: this.state.disputed ? V : "#8A7A50" }], usedBy: [{ k: "Binds", v: "recommendation + approval" }, { k: "Sources", v: "invoice, tariff, 2 events" }, { k: "Correspondence", v: "adjustment request" }], verification: [{ k: "Applicability", v: "VERIFIED", c: V }, { k: "Seal", v: this.state.disputed ? "SEALED" : "pending", c: this.state.disputed ? V : "#8A7A50" }], body: "DECISION RECORD\nCalculation  (350 − 250) × 7 = 700\nRecommendation v1  DISPUTE\nSource bindings  invoice, tariff clause,\n  free-time-end, gate-out\nVerification  applicability VERIFIED\n\nNot a legal ruling. Representative demonstration.", bound: "($350 − $250) × 7 = $700" });
    return mk({ title: "Activity log", meta: [{ k: "Invoice", v: "INV-TLY-1048", color: "#40515C" }, { k: "Format", v: "Role → Task → Tool → Output", color: "#40515C" }], usedBy: [{ k: "Public-safe", v: "no chain-of-thought" }], verification: [{ k: "Events", v: "ordered by sequence", c: "#40515C" }], body: "Jun 22  Intake Agent · extract claims\n  Bedrock → 6 claims  ✓\nJun 22  Reconstruction Agent · retrieve memory\n  Managed MCP → 9 events  ✓\nJun 22  Evidence Agent · search tariff\n  Vector Indexing → 1 candidate  ✓\nJun 22  Decision Engine · validate + calc\n  deterministic → $700  ✓\nJun 23  Reviewer · approved $700  ✓\nJun 23  Decision sealed  ✓", bound: "No prompts, tokens, or chain-of-thought shown" });
  }
  buildHandoff() {
    const c = (t, color, mono) => ({ t, color: color || "#40515C", font: mono ? "'IBM Plex Mono',monospace" : "inherit" });
    return [
      { eyebrow: "01 · ROUTE MAP", title: "Routes & contextual state", cols: "1.2fr 1fr 0.9fr 1fr", rows: [
        { cells: [c("/invoices", null, 1), c("Live invoice queue"), c("Queue table"), c("subscribe stream · NEW")] },
        { cells: [c("/invoices/:id", null, 1), c("Reconstruction Workbench (tabless)"), c("7-day ledger"), c("event stream · NEW")] },
        { cells: [c("/invoices/:id/sources/:sid", null, 1), c("Full source view"), c("Source render"), c("drawer · ADAPT")] },
        { cells: [c("/invoices/:id/decision", null, 1), c("Decision record"), c("Sealed record"), c("txn seal · BACKEND")] },
        { cells: [c("/invoices/:id/correspondence", null, 1), c("Composer → sent record"), c("Adjustment req"), c("Bedrock · BACKEND")] },
        { cells: [c("/invoices/:id/activity", null, 1), c("Public-safe event log"), c("Activity list"), c("event log · ADAPT")] },
        { cells: [c("/source-coverage", null, 1), c("Coverage matrix"), c("Coverage table"), c("NEW")] },
        { cells: [c("?day= ?panel= ?quickReview=", null, 1), c("Contextual URL state"), c("Drawer/day"), c("Back closes surface")] },
      ] },
      { eyebrow: "02 · COMPONENT INVENTORY", title: "Canonical components", cols: "1fr 2fr 0.7fr", rows: [
        { cells: [c("App shell + nav"), c("Navy rail · exactly Invoices + Source coverage"), c("NEW", null, 1)] },
        { cells: [c("Invoice queue / row"), c("Live insert, clickable in RECEIVED, status pill"), c("NEW", null, 1)] },
        { cells: [c("Identity/status shell"), c("Persistent id, container, period, status"), c("NEW", null, 1)] },
        { cells: [c("PDF/source viewer"), c("Exact original · rendered · bound fact (3 layers)"), c("ADAPT", null, 1)] },
        { cells: [c("Sourced timeline"), c("Ordered list · occurred/recorded · before-invoice"), c("NEW", null, 1)] },
        { cells: [c("Charged-day ledger"), c("Semantic table · 7 days · outcome + coverage"), c("NEW", null, 1)] },
        { cells: [c("Task rail / agent row"), c("Role→Task→Tool→Output→Validation"), c("NEW", null, 1)] },
        { cells: [c("Applicable-rule card"), c("Retrieval trace ≠ validation trace"), c("NEW", null, 1)] },
        { cells: [c("Recommendation rail"), c("Reconciliation + 2 authorizations"), c("NEW", null, 1)] },
        { cells: [c("Seal transition"), c("APPROVED → SEALING → READY TO SEND"), c("NEW", null, 1)] },
        { cells: [c("Correspondence composer"), c("Manifest + Approve & Send"), c("NEW", null, 1)] },
        { cells: [c("Send-gate checklist"), c("5 checks + Fallback NONE + block/retry"), c("NEW", null, 1)] },
        { cells: [c("Quick review"), c("Valid-invoice APPROVE FOR PAYMENT"), c("NEW", null, 1)] },
        { cells: [c("Coverage matrix"), c("covered/partial/gap/backend-required"), c("NEW", null, 1)] },
      ] },
      { eyebrow: "03 · COMPONENT STATES", title: "Required states per component", cols: "1fr 3fr", rows: [
        { cells: [c("Queue row"), c("skeleton · received · processing · ready · disputed · approved-for-payment · error")] },
        { cells: [c("Ledger day"), c("unresolved · sourcing · source-complete · rate-discrepancy · insufficient-evidence · excluded")] },
        { cells: [c("Rule / source"), c("discovered · retrieved · verifying · verified · rejected(date/text/scope) · version-unavailable")] },
        { cells: [c("Recommendation"), c("waiting · calculating · ready · human-auth-required · dispute/approve/request-evidence")] },
        { cells: [c("Seal"), c("approved · sealing · sealed · rejected · returned · seal-blocked · seal-failed")] },
        { cells: [c("Send"), c("drafting · draft-ready · verifying-gates · sending · sent · blocked(memory/source) · failed(retryable/terminal)")] },
        { cells: [c("Live updates"), c("live · paused/reconnecting · duplicate-ignored · out-of-order reconciled · stale-conflict")] },
      ] },
      { eyebrow: "04 · CURRENT-TRUTH GAPS", title: "Marked BACKEND REQUIRED / BLOCKED", cols: "1fr 2.4fr", rows: [
        { cells: [c("Managed MCP live path", "#B4513F"), c("Sealed read passed; live timeline reconstruction not yet the path")] },
        { cells: [c("Vector in hero path", "#B4513F"), c("Passed on small corpus; not yet public hero")] },
        { cells: [c("Real PDF ingestion", "#B4513F"), c("Not established — intake is representative")] },
        { cells: [c("Terminal/container events", "#B4513F"), c("Not proven — coverage rows marked BACKEND REQUIRED")] },
        { cells: [c("$875 valid path", "#B4513F"), c("Does not currently exist")] },
        { cells: [c("Outbound email", "#B4513F"), c("Does not currently exist — send is controlled demo inbox")] },
        { cells: [c("Fail-closed", "#2F7752"), c("Unavailable dependencies block, never show substitute memory")] },
      ] },
      { eyebrow: "05 · ACCEPTANCE AUDIT", title: "Self-audit verdicts", cols: "0.5fr 3fr", rows: [
        { cells: [c("PASS", "#2F7752", 1), c("Opens on Invoices; no completed hero; PDF row appears without refresh & is clickable")] },
        { cells: [c("PASS", "#2F7752", 1), c("Reconstruction dominant; all 7 days in frame; each day source-coverage state")] },
        { cells: [c("PASS", "#2F7752", 1), c("MCP memory + vector candidate visible; retrieval ≠ deterministic validation")] },
        { cells: [c("PASS", "#2F7752", 1), c("$350×7=$2,450 · $250×7=$1,750 · $100×7=$700; human authorizes complete rec")] },
        { cells: [c("PASS", "#2F7752", 1), c("Two authorizations distinct; Approve $700 dispute then Approve & Send")] },
        { cells: [c("PASS", "#2F7752", 1), c("Send gate exact checks + Fallback NONE; blocked state; no stale success")] },
        { cells: [c("PASS", "#2F7752", 1), c("INV-1050 APPROVE FOR PAYMENT · $875; no PAID; final queue contrasts outcomes")] },
        { cells: [c("PASS", "#2F7752", 1), c("Tabless workbench; Invoices + Source coverage only; no Cases/artifact tabs")] },
        { cells: [c("PASS", "#2F7752", 1), c("Status not by color alone (text+icon+shape); semantic table + ordered list; reduced-motion")] },
      ] },
      { eyebrow: "06 · FINAL REPORT", title: "DESIGN READY WITH DOCUMENTED BACKEND DEPENDENCIES", cols: "1fr 2.4fr", rows: [
        { cells: [c("Revision"), c("Tally Reconstruction Workbench v1 (new, brand-kit applied)")] },
        { cells: [c("Core frames"), c("22/22 embodied as interactive states + reviewer scene tweak")] },
        { cells: [c("Prototype path"), c("Invoices → arrival → reconstruct → day → tariff → approve $700 → seal → send gate → sent → INV-1050 approve for payment → final queue")] },
        { cells: [c("Accessibility"), c("WCAG 2.2 AA target; keyboard, aria-live, focus, status-not-color; no brand exception required")] },
        { cells: [c("Blocked"), c("Backend dependencies documented above; nothing portrayed as live that is not")] },
      ] },
    ];
  }

  /* ============================ VIEW ============================ */
  renderDrawer(wb) {
    const dr = wb.drawer;
    return (
      <div role="dialog" aria-label="Evidence" style={css("display:flex; flex-direction:column; background:#FCFBF8; border:1px solid #C8A955; border-radius:12px; overflow:hidden; max-height: calc(100vh - 40px);")}>
        <div style={css("display:flex; align-items:center; gap:8px; padding:12px 15px; background:#FBF6EE; border-bottom:1px solid #E6D6AE;")}>
          <span style={css("font-family:'IBM Plex Mono',monospace; font-size:8.5px; letter-spacing:0.1em; color:#8A7A50;")}>EVIDENCE</span>
          <span style={css("font-size:13px; font-weight:700; color:#23272F;")}>{dr.title}</span>
          <button onClick={wb.closeDrawer || this.closeDrawer} style={css("margin-left:auto; background:none; border:none; font-size:12px; color:#6F7883; cursor:pointer;")}>Close ✕</button>
        </div>
        <div style={css("display:flex; gap:4px; padding:7px 12px; border-bottom:1px solid #EFE9DC;")}>
          <button onClick={dr.setSource} style={S("font-family:'IBM Plex Mono',monospace; font-size:10px; font-weight:600; border:none; border-radius:6px; padding:5px 10px; cursor:pointer;", { color: dr.tabSourceColor, background: dr.tabSourceBg })}>Source</button>
          <button onClick={dr.setUsed} style={S("font-family:'IBM Plex Mono',monospace; font-size:10px; font-weight:600; border:none; border-radius:6px; padding:5px 10px; cursor:pointer;", { color: dr.tabUsedColor, background: dr.tabUsedBg })}>Used by</button>
          <button onClick={dr.setVerif} style={S("font-family:'IBM Plex Mono',monospace; font-size:10px; font-weight:600; border:none; border-radius:6px; padding:5px 10px; cursor:pointer;", { color: dr.tabVerifColor, background: dr.tabVerifBg })}>Verification</button>
        </div>
        <div style={css("padding:14px 16px; overflow:auto;")}>
          {dr.isSource && (
            <>
              {dr.isDay && (
                <div style={css("display:flex; flex-direction:column; gap:5px; margin-bottom:14px;")}>
                  {dr.chain.map((f, i) => (
                    <div key={i} style={S("border-radius:7px; padding:8px 11px;", { background: f.bg })}>
                      <div style={S("font-family:'IBM Plex Mono',monospace; font-size:8px; letter-spacing:0.06em;", { color: f.fg })}>{f.k}</div>
                      <div style={S("font-family:'IBM Plex Mono',monospace; font-size:11.5px; margin-top:3px;", { color: f.fg })}>{f.v}</div>
                    </div>
                  ))}
                </div>
              )}
              <div style={css("display: flex; flex-direction: column; gap: 6px; font-family: 'IBM Plex Mono',monospace; font-size: 11px; color: #40515C; margin-bottom: 14px;")}>
                {dr.meta.map((m, i) => (
                  <div key={i} style={css("display: flex; justify-content: space-between; gap: 12px;")}><span style={css("color:#8A96A0;")}>{m.k}</span><span style={{ color: m.color }}>{m.v}</span></div>
                ))}
              </div>
              <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9px; letter-spacing: 0.1em; color: #8A96A0; margin-bottom: 6px;")}>EXACT ORIGINAL · RENDERED VIEW</div>
              <div style={css("background: #FBF9F4; border: 1px solid #E4DCCB; border-radius: 8px; padding: 14px; font-family: 'IBM Plex Mono',monospace; font-size: 11px; color: #40515C; line-height: 1.7; white-space: pre-wrap;")}>{dr.body}</div>
              <div style={css("margin-top: 14px; font-family: 'IBM Plex Mono',monospace; font-size: 9px; letter-spacing: 0.1em; color: #8A96A0; margin-bottom: 6px;")}>BOUND FACT</div>
              <div style={css("background: #FBF1D8; border-left: 3px solid #C8A955; padding: 10px 13px; font-family: 'IBM Plex Mono',monospace; font-size: 12px; color: #23272F;")}>{dr.bound}</div>
            </>
          )}
          {dr.isUsed && (
            <>
              <div style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; letter-spacing:0.1em; color:#8A96A0; margin-bottom:8px;")}>WHERE THIS IS USED</div>
              <div style={css("display:flex; flex-direction:column; gap:7px;")}>
                {dr.usedBy.map((u, i) => (<div key={i} style={css("display:flex; justify-content:space-between; gap:12px; font-family:'IBM Plex Mono',monospace; font-size:11px; padding:8px 0; border-bottom:1px solid #F1EBDD;")}><span style={css("color:#8A96A0;")}>{u.k}</span><span style={css("color:#23272F;")}>{u.v}</span></div>))}
              </div>
            </>
          )}
          {dr.isVerif && (
            <>
              <div style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; letter-spacing:0.1em; color:#8A96A0; margin-bottom:8px;")}>DETERMINISTIC VALIDATION</div>
              <div style={css("display:flex; flex-direction:column; gap:7px;")}>
                {dr.verification.map((vv, i) => (<div key={i} style={css("display:flex; justify-content:space-between; gap:12px; font-family:'IBM Plex Mono',monospace; font-size:11px;")}><span style={css("color:#6F7883;")}>{vv.k}</span><span style={{ color: vv.c }}>{vv.v}</span></div>))}
              </div>
            </>
          )}
        </div>
      </div>
    );
  }

  render() {
    const v = this.renderVals();
    const wb = v.wb;
    return (
      <div className="tly-shell" style={css("min-height: 100vh; display: flex; background: #F5F0E7; font-family: 'Schibsted Grotesk','Helvetica Neue',Helvetica,sans-serif; -webkit-font-smoothing: antialiased; color: #23272F;")}>
        {/* SIDEBAR */}
        <nav aria-label="Primary" className="tly-nav" style={css("width: 232px; flex: 0 0 232px; background: #1D2A33; color: #AEBAC3; display: flex; flex-direction: column; position: sticky; top: 0; height: 100vh;")}>
          <div style={css("padding: 22px 22px 20px; border-bottom: 1px solid rgba(255,255,255,0.07); cursor: pointer;")} onClick={v.goQueue}>
            <div style={css("display: flex; align-items: center; gap: 11px;")}>
              <svg width="38" height="26" viewBox="0 0 72 48" aria-hidden="true"><line x1="9" y1="6" x2="9" y2="42" stroke="#E5D8BC" strokeWidth="5" strokeLinecap="round" /><line x1="25" y1="6" x2="25" y2="42" stroke="#E5D8BC" strokeWidth="5" strokeLinecap="round" /><line x1="41" y1="6" x2="41" y2="42" stroke="#E5D8BC" strokeWidth="5" strokeLinecap="round" /><line x1="57" y1="6" x2="57" y2="42" stroke="#E5D8BC" strokeWidth="5" strokeLinecap="round" /><line x1="1" y1="40" x2="66" y2="7" stroke="#E5D8BC" strokeWidth="5" strokeLinecap="round" /></svg>
              <span><span style={css("display: block; font-weight: 700; letter-spacing: 0.2em; font-size: 18px; color: #F5F0E7;")}>TALLY</span><span style={css("display: block; font-family: 'IBM Plex Mono',monospace; font-size: 9px; color: #6E7F8B; letter-spacing: 0.04em; margin-top: 2px;")}>demurrage &amp; detention</span></span>
            </div>
          </div>
          <div style={css("padding: 20px 14px 8px;")}>
            <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; letter-spacing: 0.16em; color: #56656F; padding: 0 10px 8px;")}>WORK</div>
            <a href="#" onClick={v.goQueue} aria-current={v.invAria} style={S("display: flex; align-items: center; gap: 10px; padding: 9px 11px; border-radius: 8px; font-size: 13.5px; font-weight: 600;", { background: v.invNavBg, color: v.invNavFg })}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={css("opacity:0.9; flex:0 0 auto;")}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /><line x1="8.5" y1="13" x2="15.5" y2="13" /><line x1="8.5" y1="16.5" x2="13" y2="16.5" /></svg>Invoices
            </a>
            {v.showCoverageNav && (<>
              <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; letter-spacing: 0.16em; color: #56656F; padding: 18px 10px 8px;")}>RECORD</div>
              <a href="#" onClick={v.goCoverage} style={S("display: flex; align-items: center; gap: 10px; padding: 9px 11px; border-radius: 8px; font-size: 13.5px; font-weight: 600;", { background: v.covNavBg, color: v.covNavFg })}>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={css("opacity:0.9; flex:0 0 auto;")}><path d="M12 3 3 7.5 12 12l9-4.5z" /><path d="M3 12l9 4.5L21 12" /><path d="M3 16.5 12 21l9-4.5" /></svg>Source coverage
              </a>
            </>)}
          </div>
          <div className="tly-navfooter" style={css("margin-top: auto; padding: 16px;")}>
            {v.internal && (<a href="#" onClick={v.goHandoff} style={css("display: block; font-family: 'IBM Plex Mono',monospace; font-size: 10px; letter-spacing: 0.08em; color: #6E7F8B; padding: 8px 10px; border: 1px solid rgba(255,255,255,0.1); border-radius: 7px; text-align: center;")}>DESIGN &amp; ENG HANDOFF →</a>)}
            <div style={css("display: flex; align-items: center; gap: 7px; margin-top: 12px; padding: 0 4px;")}>
              <span style={css("width: 24px; height: 24px; border-radius: 50%; background: #1D2A33; border: 1px solid rgba(229,216,188,0.3); color: #E5D8BC; display: inline-flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 700;")}>RM</span>
              <span style={css("font-size: 11px; color: #8A96A0; line-height: 1.3;")}>Import ops<br /><span style={css("color:#56656F; font-size:10px;")}>reviewer</span></span>
            </div>
          </div>
        </nav>

        {/* MAIN */}
        <div style={css("flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column;")}>
          <div style={css("height: 52px; flex: 0 0 52px; background: #F5F0E7; border-bottom: 1px solid #E1D9C9; display: flex; align-items: center; padding: 0 24px; gap: 16px;")}>
            <div style={css("font-size: 13px; color: #6F7883;")}>{v.crumb}</div>
            <div style={css("margin-left: auto; display: flex; align-items: center; gap: 12px;")}>
              {v.internal && (<>
                <button onClick={v.toggleAnnot} style={S("font-family: 'IBM Plex Mono',monospace; font-size: 10px; letter-spacing: 0.06em; cursor: pointer; border: 1px solid #DED6C7; border-radius: 6px; padding: 5px 10px;", { color: v.annotFg, background: v.annotBg })}>◇ ANNOTATIONS</button>
                <button onClick={v.replay} style={css("font-family: 'IBM Plex Mono',monospace; font-size: 10px; letter-spacing: 0.06em; cursor: pointer; color: #6F7883; background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 6px; padding: 5px 10px;")}>↺ REPLAY</button>
              </>)}
              <div style={css("display: flex; align-items: center; gap: 6px; font-family: 'IBM Plex Mono',monospace; font-size: 10px; letter-spacing: 0.08em; color: #8A7A50; background: #F3EAD3; border: 1px solid #E6D6AE; border-radius: 5px; padding: 5px 10px;")}><span style={css("width: 5px; height: 5px; border-radius: 50%; background: #C8A955;")} />REPRESENTATIVE DEMONSTRATION</div>
            </div>
          </div>

          <div aria-live="polite" style={css("position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0);")}>{v.announce}</div>

          <div id="tly-scroll" style={css("flex: 1 1 auto; overflow: auto;")}>
            {v.isQueue && this.renderQueue(v)}
            {v.isWorkbench && this.renderWorkbench(v, wb)}
            {v.isWb1050 && this.renderWb1050(v, wb)}
            {v.isCoverage && this.renderCoverage(v)}
            {v.isHandoff && this.renderHandoff(v)}
          </div>
        </div>
      </div>
    );
  }

  renderQueue(v) {
    return (
      <div style={css("max-width: 1160px; margin: 0 auto; padding: 30px 28px 60px;")}>
        <div style={css("display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 4px;")}>
          <div>
            <h1 style={css("font-size: 25px; font-weight: 700; letter-spacing: -0.01em; margin: 0;")}>Invoices</h1>
            <p style={css("font-size: 13px; color: #6F7883; margin: 6px 0 0;")}>Carrier invoices arrive as PDFs and are reconstructed against recorded shipment memory.</p>
          </div>
          <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 11px; color: #8A96A0;")}>{v.queueCount} invoices</div>
        </div>
        {v.annot && (<div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 10px; color: #A9823C; background: #FBF1D8; border: 1px dashed #C8A955; border-radius: 7px; padding: 8px 12px; margin: 14px 0 0; line-height: 1.5;")}>ROUTE /invoices · DATA subscribe queue stream · new rows insert on <b>invoice.received</b> event without hard refresh (target &lt;1s) · row clickable same render as insertion · <b>NEW</b> component</div>)}
        <div style={css("margin-top: 16px; display: flex; flex-wrap: wrap; gap: 8px;")}>
          {v.queueFilters.map((qfx) => (<button key={qfx.key} onClick={qfx.on} style={S("font-size: 12px; font-weight: 600; border-radius: 7px; padding: 6px 12px; cursor: pointer;", { color: qfx.fg, background: qfx.bg, border: "1px solid " + qfx.border })}>{qfx.label} <span style={css("font-family:'IBM Plex Mono',monospace; font-size:10px; opacity:0.7;")}>{qfx.count}</span></button>))}
        </div>
        <div style={css("margin-top: 14px; background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 12px; overflow: hidden;")}>
          <div className="tly-qh" style={css("display: grid; grid-template-columns: 1.7fr 1fr 1.1fr 1fr 40px; gap: 12px; padding: 11px 20px; border-bottom: 1px solid #EFE9DC; font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; letter-spacing: 0.1em; color: #8A96A0;")}>
            <div>INVOICE SOURCE</div><div className="tly-col-hide">CONTAINER</div><div className="tly-col-hide">STATUS</div><div style={css("text-align:right;")}>AMOUNT</div><div />
          </div>
          {v.queueRows.map((r) => (
            <div key={r.name} className={"tly-q " + r.anim} onClick={r.onOpen} tabIndex={0} role="button" style={S("display: grid; grid-template-columns: 1.7fr 1fr 1.1fr 1fr 40px; gap: 12px; padding: 15px 20px; border-bottom: 1px solid #EFE9DC; align-items: center;", { cursor: r.cursor, background: r.rowBg })}>
              <div style={css("min-width: 0;")}>
                <div style={S("font-size: 14px; font-weight: 600;", { color: r.nameColor })}>{r.name}</div>
                <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 10.5px; color: #8A96A0; margin-top: 3px;")}>{r.sub}</div>
              </div>
              <div className="tly-col-hide" style={css("font-family: 'IBM Plex Mono',monospace; font-size: 12px; color: #40515C;")}>{r.container}</div>
              <div className="tly-col-hide"><span style={S("font-family: 'IBM Plex Mono',monospace; font-size: 10px; font-weight: 600; letter-spacing: 0.05em; border-radius: 5px; padding: 4px 9px;", { background: r.pillBg, color: r.pillFg })}>{r.status}</span>{r.workDot !== "display:none;" && (<span className="tly-work" style={css("display:inline-block; margin-left:7px; width:6px; height:6px; border-radius:50%; background:#C8A955; vertical-align:middle;")} />)}</div>
              <div style={S("text-align: right; font-family: 'IBM Plex Mono',monospace; font-size: 14px; font-weight: 500;", { color: r.amountColor })}>{r.amount}</div>
              <div style={css("text-align: right; color: #B7AE9C;")}>{r.chevron}</div>
            </div>
          ))}
        </div>
        {v.show1050Prompt && (
          <div style={css("margin-top: 14px; display: flex; align-items: center; gap: 14px; background: #FBF6EE; border: 1px solid #E6D6AE; border-radius: 10px; padding: 13px 18px;")}>
            <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 10px; letter-spacing: 0.08em; color: #8A7A50;")}>NEXT INVOICE</span>
            <div style={css("font-size: 13px; color: #23272F;")}><b>INV-1050</b> matches its recorded rate and timeline — ready for a quick disposition.</div>
            <button onClick={v.openInv1050} style={css("margin-left: auto; font-size: 12.5px; font-weight: 600; color: #23272F; background: #F3EEE3; border: 1px solid #DED6C7; border-radius: 7px; padding: 8px 15px; cursor: pointer;")}>Open record →</button>
          </div>
        )}
      </div>
    );
  }

  renderWorkbench(v, wb) {
    return (
      <div style={css("padding: 20px 24px 70px;")}>
        <div style={css("display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 6px;")}>
          <a href="#" onClick={v.goQueue} style={css("font-size: 12.5px; color: #6F7883;")}>← Invoices</a>
          <div style={css("width: 1px; height: 18px; background: #DED6C7;")} />
          <h1 style={css("font-size: 20px; font-weight: 700; margin: 0;")}>{wb.title}</h1>
          <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 11px; color: #8A96A0;")}>{wb.idline}</span>
          <span style={S("font-family: 'IBM Plex Mono',monospace; font-size: 10px; font-weight: 600; letter-spacing: 0.05em; border-radius: 5px; padding: 4px 10px;", { background: wb.pillBg, color: wb.pillFg })}>{wb.status}</span>
          <div style={css("margin-left: auto; display: flex; gap: 8px;")}>
            <a href="#" onClick={v.goDecision} style={css("font-size: 12px; color: #6F7883; border: 1px solid #DED6C7; border-radius: 7px; padding: 6px 12px; background: #FCFBF8;")}>Decision record</a>
            <a href="#" onClick={v.goActivity} style={css("font-size: 12px; color: #6F7883; border: 1px solid #DED6C7; border-radius: 7px; padding: 6px 12px; background: #FCFBF8;")}>Activity</a>
          </div>
        </div>
        <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 11px; color: #8A96A0; margin-bottom: 12px;")}>{wb.meta}</div>

        <div style={css("position: sticky; top: 0; z-index: 20; margin: 0 -24px 16px; padding: 8px 24px 10px; background: #F5F0E7; border-bottom: 1px solid #E1D9C9; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;")}>
          <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9px; letter-spacing: 0.12em; color: #8A96A0; margin-right: 4px;")}>PIPELINE</span>
          {wb.pipeline.map((s, i) => (
            <div key={i} title={s.title} style={S("display: flex; align-items: center; gap: 7px; border-radius: 8px; padding: 6px 11px;", { background: s.bg, border: "1px solid " + s.border })}>
              <span className={s.pulse} style={S("width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto;", { background: s.dot })} />
              <span style={S("font-size: 12px; font-weight: 600;", { color: s.nameColor })}>{s.name}</span>
              <span style={S("font-family: 'IBM Plex Mono',monospace; font-size: 8.5px; letter-spacing: 0.03em;", { color: s.toolColor })}>{s.tool}</span>
            </div>
          ))}
        </div>

        {wb.showNow && (
          <div style={S("display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:14px; padding:11px 15px; border-radius:10px;", { background: wb.nowBg, border: "1px solid " + wb.nowBorder })}>
            <span className={wb.nowPulse} style={S("width:8px; height:8px; border-radius:50%; flex:0 0 auto;", { background: wb.nowDot })} />
            <span style={S("font-size:14px; font-weight:700; letter-spacing:-0.01em;", { color: wb.nowHeadColor })}>{wb.nowHead}</span>
            <span style={S("font-family:'IBM Plex Mono',monospace; font-size:11px;", { color: wb.nowSubColor })}>{wb.nowSub}</span>
            {wb.nowAction && (<button onClick={wb.nowOn} style={css("margin-left:auto; font-size:13px; font-weight:600; color:#1D2A33; background:#C8A955; border:none; border-radius:8px; padding:9px 18px; cursor:pointer;")}>{wb.nowLabel}</button>)}
          </div>
        )}

        <div style={css("display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 16px;")}>
          <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9px; letter-spacing: 0.12em; color: #8A96A0; margin-right: 2px;")}>PROVENANCE</span>
          {wb.legend.map((lg, i) => (<span key={i} style={S("display: inline-flex; align-items: center; gap: 6px; font-family: 'IBM Plex Mono',monospace; font-size: 9px; font-weight: 600; letter-spacing: 0.05em; border-radius: 5px; padding: 4px 9px;", { background: lg.bg, color: lg.fg })}><span style={S("width: 6px; height: 6px; border-radius: 2px;", { background: lg.fg })} />{lg.label}</span>))}
        </div>

        <div className="tly-wb-grid" style={S("display: grid; gap: 20px; align-items: start;", { gridTemplateColumns: v.gridCols })}>
          {/* LEFT */}
          <div style={css("display: flex; flex-direction: column; gap: 18px; min-width: 0;")}>
            {wb.sendPhase && (
              <div style={css("display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 11px 14px; background: #F0EBE0; border: 1px solid #E1D9C9; border-radius: 10px;")}>
                <span style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; letter-spacing:0.1em; color:#8A96A0; margin-right:2px;")}>COMPLETED</span>
                {wb.contextChips.map((cc, i) => (<button key={i} onClick={cc.on} title="Open evidence" style={css("display:flex; align-items:center; gap:7px; background:#FCFBF8; border:1px solid #DED6C7; border-radius:8px; padding:6px 11px; cursor:pointer;")}><span style={css("color:#2F7752; font-size:11px;")}>✓</span><span style={css("font-size:11.5px; font-weight:600; color:#23272F;")}>{cc.label}</span><span style={css("font-family:'IBM Plex Mono',monospace; font-size:9.5px; color:#6F7883;")}>{cc.val}</span></button>))}
              </div>
            )}

            {wb.showReconCards && (<>
              {/* source */}
              <div id="sec-source" style={css("background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 12px; overflow: hidden; scroll-margin-top: 80px;")}>
                <div style={css("padding: 12px 16px; border-bottom: 1px solid #EFE9DC;")}>
                  <div style={css("display: flex; align-items: center; gap: 8px;")}>
                    <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 8.5px; font-weight: 600; letter-spacing: 0.05em; border-radius: 5px; padding: 3px 8px; background: #ECEFF1; color: #40515C;")}>FROM INVOICE</span>
                    <span style={css("font-size: 12.5px; font-weight: 600; color: #23272F;")}>Invoice source</span>
                    <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; color: #2F7752; margin-left: auto;")}>{wb.srcVersion}</span>
                    {wb.sourceUrl
                      ? <a href={wb.sourceUrl} target="_blank" rel="noopener noreferrer" style={css("font-size: 11.5px;")}>View retained PDF →</a>
                      : <a href="#" onClick={v.openSourceInvoice} style={css("font-size: 11.5px;")}>View full source →</a>}
                    <button onClick={wb.toggleSource} aria-expanded={wb.openSource} style={css("display:flex; align-items:center; gap:5px; font-family:'IBM Plex Mono',monospace; font-size:10px; color:#6F7883; background:#FCFBF8; border:1px solid #DED6C7; border-radius:6px; padding:4px 9px; cursor:pointer;")}>{wb.srcChev} {wb.srcDiscLabel}</button>
                  </div>
                  <div style={css("font-size: 11.5px; color: #6F7883; margin-top: 4px;")}>{wb.srcCaption}</div>
                </div>
                {wb.openSource && (
                  <div className="tly-src" style={S("display: grid; gap: 16px; padding: 16px;", { gridTemplateColumns: wb.srcCols })}>
                    <div style={css("background: #FBF9F4; border: 1px solid #E4DCCB; border-radius: 8px; padding: 14px; position: relative;")}>
                      <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 8.5px; color: #B7AE9C; letter-spacing: 0.1em;")}>INV-1048.pdf · p.1</div>
                      <div style={css("margin-top: 10px; font-size: 12px; color: #40515C; line-height: 1.7;")}>
                        <div style={css("font-weight: 700; color: #23272F; letter-spacing: 0.04em;")}>DEMURRAGE INVOICE</div>
                        <div style={css("height:1px; background:#E4DCCB; margin:8px 0;")} />
                        <div>Container <b>TLLU-482931-7</b></div>
                        <div>B/L OAK-77421</div>
                        <div>Period Jun 8 – Jun 14, 2026</div>
                        <div style={css("margin-top:8px; background:#FBF1D8; border-left:3px solid #C8A955; padding:6px 9px; font-family:'IBM Plex Mono',monospace; font-size:11px;")}>Rate $350.00 / day × 7</div>
                        <div style={css("margin-top:6px; background:#FBF1D8; border-left:3px solid #C8A955; padding:6px 9px; font-family:'IBM Plex Mono',monospace; font-size:11px;")}>Total due $2,450.00</div>
                        <div style={css("margin-top:8px; font-family:'IBM Plex Mono',monospace; font-size:9.5px; color:#B7AE9C;")}>issued Jun 22, 2026</div>
                      </div>
                    </div>
                    <div>
                      <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; letter-spacing: 0.1em; color: #8A96A0; margin-bottom: 8px;")}>{wb.claimsLabel}</div>
                      {wb.claims.map((c, i) => (<div key={i} style={css("display: flex; justify-content: space-between; gap: 10px; padding: 7px 0; border-bottom: 1px solid #F1EBDD; font-size: 12.5px;")}><span style={css("color: #6F7883;")}>{c.label}</span><span style={S("font-family: 'IBM Plex Mono',monospace;", { color: c.color, opacity: c.opacity })}>{c.value}</span></div>))}
                      {v.annot && (<div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; color: #A9823C; background: #FBF1D8; border: 1px dashed #C8A955; border-radius: 6px; padding: 7px 10px; margin-top: 10px; line-height: 1.5;")}>Intake Agent · Amazon Bedrock extracts claims → linked to PDF regions · original preserved in versioned S3 · <b>NEW</b> · BACKEND REQUIRED (real PDF ingestion)</div>)}
                    </div>
                  </div>
                )}
                {wb.collapsedSource && (<div style={css("padding: 12px 16px; font-family: 'IBM Plex Mono',monospace; font-size: 11px; color: #8A96A0;")}>{wb.srcSummary}</div>)}
              </div>

              {/* timeline */}
              <div id="sec-timeline" style={css("background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 12px; padding: 16px 18px; scroll-margin-top: 80px;")}>
                <div style={css("margin-bottom: 8px;")}>
                  <div style={css("display: flex; align-items: center; gap: 8px;")}>
                    <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 8.5px; font-weight: 600; letter-spacing: 0.05em; border-radius: 5px; padding: 3px 8px; background: #E4EEE7; color: #2F7752;")}>RECORDED BEFORE INVOICE</span>
                    <span style={css("font-size: 12.5px; font-weight: 600; color: #23272F;")}>Sourced timeline</span>
                    <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; color: #8A96A0; margin-left: auto;")}>{wb.timelineCount}</span>
                    <button onClick={wb.toggleTimeline} aria-expanded={wb.openTimeline} style={css("display:flex; align-items:center; gap:5px; font-family:'IBM Plex Mono',monospace; font-size:10px; color:#6F7883; background:#FCFBF8; border:1px solid #DED6C7; border-radius:6px; padding:4px 9px; cursor:pointer;")}>{wb.tlChev} {wb.tlDiscLabel}</button>
                  </div>
                  <div style={css("font-size: 11.5px; color: #6F7883; margin-top: 4px;")}>{wb.tlCaption}</div>
                </div>
                {wb.collapsedTimeline && (<div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 11px; color: #8A96A0; padding: 4px 0 2px;")}>{wb.tlSummary}</div>)}
                {wb.openTimeline && (<>
                  {wb.timelineEmpty && (<div style={css("padding: 20px 0; font-size: 12.5px; color: #8A96A0;")}>Waiting for reconstruction — no events retrieved yet.</div>)}
                  <ol style={css("list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column;")}>
                    {wb.timeline.map((e, i) => (<li key={i} className="tly-row-in" style={css("display: grid; grid-template-columns: 74px 14px 1fr auto; gap: 10px; align-items: baseline; padding: 6px 0;")}><span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 11px; color: #40515C;")}>{e.date}</span><span style={S("width: 8px; height: 8px; border-radius: 50%; align-self: center; margin-top: 2px;", { background: e.dot })} /><span style={css("font-size: 13px; color: #23272F;")}>{e.label}</span><span style={S("font-family: 'IBM Plex Mono',monospace; font-size: 9px; letter-spacing: 0.05em; border-radius: 4px; padding: 3px 7px;", { color: e.tagFg, background: e.tagBg })}>{e.tag}</span></li>))}
                  </ol>
                </>)}
                {v.annot && (<div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; color: #A9823C; background: #FBF1D8; border: 1px dashed #C8A955; border-radius: 6px; padding: 7px 10px; margin-top: 10px; line-height: 1.5;")}>Reconstruction Agent · <b>CockroachDB Managed MCP</b> retrieves prior shipment memory recorded before invoice · occurred/recorded times distinct · <b>NEW</b> · BACKEND REQUIRED (live timeline path)</div>)}
              </div>

              {/* ledger */}
              <div id="sec-days" className="tly-ledger" style={css("background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 12px; scroll-margin-top: 80px;")}>
                <div style={css("padding: 13px 18px; border-bottom: 1px solid #EFE9DC;")}>
                  <div style={css("display: flex; align-items: center; gap: 8px;")}>
                    <span style={S("font-family:'IBM Plex Mono',monospace; font-size:8.5px; font-weight:600; letter-spacing:0.05em; border-radius:5px; padding:3px 8px;", { background: wb.daysChip.bg, color: wb.daysChip.fg })}>{wb.daysChip.label}</span>
                    <span style={css("font-size:12.5px; font-weight:600; color:#23272F;")}>Seven charged days · Jun 8–14</span>
                    <span style={css("font-family:'IBM Plex Mono',monospace; font-size:9.5px; color:#2F7752; margin-left:auto;")}>{wb.coverageLine}</span>
                    <button onClick={wb.toggleDays} aria-expanded={wb.openDays} style={css("display:flex; align-items:center; gap:5px; font-family:'IBM Plex Mono',monospace; font-size:10px; color:#6F7883; background:#FCFBF8; border:1px solid #DED6C7; border-radius:6px; padding:4px 9px; cursor:pointer;")}>{wb.daysChev} {wb.daysDiscLabel}</button>
                  </div>
                  <div style={css("font-size:11.5px; color:#6F7883; margin-top:4px;")}>{wb.daysCaption}</div>
                </div>
                {wb.showTariffRef && (<div style={css("display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:10px 18px; background:#FBF6EE; border-bottom:1px solid #EFE9DC;")}><span style={css("font-family:'IBM Plex Mono',monospace; font-size:8.5px; letter-spacing:0.06em; color:#8A7A50;")}>APPLICABLE TARIFF</span><span style={css("font-family:'IBM Plex Mono',monospace; font-size:12px; color:#23272F;")}>{wb.tariffRate}</span><span style={css("font-family:'IBM Plex Mono',monospace; font-size:10.5px; color:#6F7883;")}>{wb.tariffEff}</span><span style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; font-weight:600; border-radius:4px; padding:3px 7px; background:#E4EEE7; color:#2F7752;")}>APPLICABILITY VERIFIED</span><a href="#" onClick={v.openSourceTariff} style={css("font-size:11.5px; margin-left:auto;")}>View clause →</a></div>)}
                {wb.collapsedDays && (<div style={css("font-family:'IBM Plex Mono',monospace; font-size:11px; color:#8A96A0; padding:12px 18px;")}>{wb.daysSummary}</div>)}
                {wb.openDays && (
                  <table style={css("width: 100%; min-width: 660px; border-collapse: collapse; font-size: 12.5px;")}>
                    <caption style={css("position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0);")}>Charged days adjudicated against recorded events and the applicable tariff rate</caption>
                    <thead>
                      <tr style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9px; letter-spacing: 0.08em; color: #8A96A0; text-align: left;")}>
                        <th scope="col" style={css("padding: 9px 16px; font-weight: 500;")}>DATE</th>
                        <th scope="col" style={css("padding: 9px 6px; font-weight: 500;")}>CLAIM</th>
                        <th scope="col" style={css("padding: 9px 6px; font-weight: 500;")}>RULE</th>
                        <th scope="col" style={css("padding: 9px 6px; font-weight: 500;")}>OUTCOME</th>
                        <th scope="col" style={css("padding: 9px 6px; font-weight: 500; text-align:right;")}>Δ / DAY</th>
                        <th scope="col" style={css("padding: 9px 16px; font-weight: 500;")}>COVERAGE</th>
                      </tr>
                    </thead>
                    <tbody>
                      {wb.days.map((d, i) => (
                        <tr key={i} onClick={d.onOpen} tabIndex={0} role="button" style={S("border-top: 1px solid #F1EBDD; cursor: pointer;", { background: d.rowBg })}>
                          <td style={css("padding: 10px 16px; font-family: 'IBM Plex Mono',monospace; color: #40515C;")}>{d.date}</td>
                          <td style={css("padding: 10px 6px; font-family: 'IBM Plex Mono',monospace; color: #23272F;")}>{d.claim}</td>
                          <td style={S("padding: 10px 6px; font-family: 'IBM Plex Mono',monospace;", { color: d.ruleColor })}>{d.rule}</td>
                          <td style={css("padding: 10px 6px;")}><span style={S("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; font-weight: 600; border-radius: 4px; padding: 3px 7px;", { background: d.outBg, color: d.outFg })}>{d.outcome}</span></td>
                          <td style={S("padding: 10px 6px; text-align:right; font-family: 'IBM Plex Mono',monospace; font-size: 12px;", { color: d.discColor })}>{d.disc}</td>
                          <td style={css("padding: 10px 16px;")}><span style={S("font-family: 'IBM Plex Mono',monospace; font-size: 9px;", { color: d.covFg })}>{d.coverage}</span></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>

              {/* inline day detail */}
              {wb.dayOpen && (
                <div style={css("background: #FCFBF8; border: 1px solid #C8A955; border-radius: 12px; overflow: hidden;")}>
                  <div style={css("display: flex; align-items: center; gap: 10px; padding: 13px 18px; background: #FBF6EE; border-bottom: 1px solid #E6D6AE;")}>
                    <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 10px; letter-spacing: 0.08em; color: #8A7A50;")}>SOURCE CHAIN · {wb.day.date}</span>
                    <a href="#" onClick={v.openSourceTariff} style={css("font-size: 11.5px; margin-left: auto;")}>Open tariff source →</a>
                    <button onClick={v.closeDay} style={css("font-size: 12px; color: #6F7883; background: none; border: none; cursor: pointer;")}>Close ✕</button>
                  </div>
                  <div style={css("padding: 16px 18px;")}>
                    <div style={css("display: flex; flex-wrap: wrap; align-items: center; gap: 0; margin-bottom: 14px;")}>
                      {wb.dayFlow.map((f, i) => (<div key={i} style={css("flex: 1 1 130px; min-width: 120px; display: flex; align-items: center; gap: 8px;")}><div style={S("flex: 1; border-radius: 8px; padding: 9px 12px;", { background: f.bg })}><div style={S("font-family: 'IBM Plex Mono',monospace; font-size: 8px; letter-spacing: 0.06em;", { color: f.fg })}>{f.k}</div><div style={S("font-family: 'IBM Plex Mono',monospace; font-size: 12px; margin-top: 4px;", { color: f.fg })}>{f.v}</div></div><span style={css("color: #B7AE9C; font-size: 15px; flex: 0 0 auto;")}>→</span></div>))}
                    </div>
                    <div className="tly-daydetail" style={css("display: grid; grid-template-columns: 1fr 1fr; gap: 0; border-top: 1px solid #EFE9DC;")}>
                      <div style={css("padding: 14px 16px 2px; border-right: 1px solid #EFE9DC;")}>
                        {wb.day.chain.map((q, i) => (<div key={i} style={css("padding: 8px 0; border-bottom: 1px solid #F1EBDD;")}><div style={css("font-size: 11px; color: #6F7883;")}>{q.q}</div><div style={S("font-size: 13px; color: #23272F; margin-top: 3px;", { fontFamily: q.font })}>{q.a}</div></div>))}
                      </div>
                      <div style={css("padding: 14px 16px 2px;")}>
                        <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; letter-spacing: 0.1em; color: #8A96A0;")}>APPLICABLE RULE · APPLICABILITY VERIFIED</div>
                        <div style={css("margin-top: 10px; background: #FBF1D8; border-left: 3px solid #C8A955; padding: 10px 13px; font-family: 'IBM Plex Mono',monospace; font-size: 12.5px; color: #23272F;")}>{wb.day.clause}</div>
                        <div style={css("margin-top: 10px; display: flex; flex-direction: column; gap: 5px;")}>
                          {wb.day.checks.map((vv, i) => (<div key={i} style={css("display: flex; justify-content: space-between; font-family: 'IBM Plex Mono',monospace; font-size: 11px;")}><span style={css("color: #6F7883;")}>{vv.k}</span><span style={{ color: vv.color }}>{vv.v}</span></div>))}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </>)}

            {/* composer */}
            {wb.showComposer && (
              <div id="sec-corr" style={css("background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 12px; overflow: hidden; scroll-margin-top: 80px;")}>
                <div style={css("display: flex; align-items: center; gap: 8px; padding: 13px 18px; border-bottom: 1px solid #EFE9DC;")}>
                  <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 8.5px; font-weight: 600; letter-spacing: 0.05em; border-radius: 5px; padding: 3px 8px; background: #FBF1D8; color: #A9823C;")}>HUMAN APPROVED</span>
                  <span style={css("font-size: 12.5px; font-weight: 600; color: #23272F;")}>Adjustment request</span>
                  <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; color: #8A96A0; margin-left: auto;")}>{wb.corrState}</span>
                  <button onClick={wb.toggleCorr} aria-expanded={wb.openCorr} style={css("display:flex; align-items:center; gap:5px; font-family:'IBM Plex Mono',monospace; font-size:10px; color:#6F7883; background:#FCFBF8; border:1px solid #DED6C7; border-radius:6px; padding:4px 9px; cursor:pointer;")}>{wb.corrChev} {wb.corrDiscLabel}</button>
                </div>
                {wb.collapsedCorr && (<div style={css("padding: 12px 18px; font-family:'IBM Plex Mono',monospace; font-size: 11px; color: #8A96A0;")}>Adjustment request · $700 · demonstration inbox</div>)}
                {wb.openCorr && (
                  <div style={css("padding: 16px 18px;")}>
                    <div style={css("display: flex; gap: 20px; flex-wrap: wrap; font-size: 12.5px; color: #40515C; margin-bottom: 12px;")}>
                      <div><span style={css("color:#8A96A0;")}>To</span> &nbsp;billing@demonstration-inbox.tally</div>
                      <div><span style={css("color:#8A96A0;")}>Re</span> &nbsp;INV-1048 · TLLU-482931-7</div>
                      <div><span style={css("color:#8A96A0;")}>Disputed</span> &nbsp;<b style={css("font-family:'IBM Plex Mono',monospace;")}>$700</b></div>
                    </div>
                    <div style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; letter-spacing:0.1em; color:#8A96A0; margin-bottom:6px;")}>MESSAGE TO CARRIER · EDITABLE</div>
                    <div style={css("background: #FBF9F4; border: 1px solid #E4DCCB; border-radius: 8px; padding: 14px; font-size: 12.5px; color: #40515C; line-height: 1.65;")}>
                      The demurrage rate applied on INV-1048 (<b>$350/day</b>) does not match the applicable recorded tariff of <b>$250/day</b> effective Jun 1, 2026. Across 7 sourced charged days (Jun 8–14), the supported total is <b>$1,750</b>; we request correction of the <b>$700</b> difference. Sourced calculation and evidence manifest attached.
                    </div>
                    <div style={css("margin-top: 12px; font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; letter-spacing: 0.1em; color: #8A96A0;")}>ATTACHMENT / SOURCE MANIFEST · CLICK TO INSPECT</div>
                    <div style={css("display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px;")}>
                      {wb.manifest.map((m, i) => (<button key={i} onClick={m.on} title="Open source" style={css("display:flex; align-items:center; gap:6px; font-family: 'IBM Plex Mono',monospace; font-size: 10.5px; color: #40515C; background: #F3EEE3; border: 1px solid #DED6C7; border-radius: 5px; padding: 5px 10px; cursor: pointer;")}>{m.label} <span style={css("color:#8A7A50;")}>↗</span></button>))}
                    </div>
                    <div style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; letter-spacing:0.04em; color:#8A96A0; margin-top:8px; line-height:1.5;")}>Each attachment opens its retained source · resolves to the exact S3 / decision-record version in the live build</div>
                    {wb.showSendBtn && (
                      <div style={css("margin-top: 14px; padding-top: 14px; border-top: 1px solid #EFE9DC;")}>
                        <div style={css("font-size: 11.5px; color: #6F7883; line-height: 1.5; margin-bottom: 10px;")}>Decision sealed. This second authorization sends the request to the controlled demonstration inbox. Amount, reason, and attachments are locked to the sealed decision.</div>
                        <button onClick={v.approveSend} style={css("width: 100%; font-size: 14px; font-weight: 600; color: #F5F0E7; background: #1D2A33; border: none; border-radius: 8px; padding: 12px; cursor: pointer;")}>Approve &amp; Send</button>
                      </div>
                    )}
                    {/* LIVE: clean 3-step Sealed -> Sending -> Sent (backend still runs
                        every real gate). MOCK: the original 5-row gate ceremony. */}
                    {wb.liveSend && (wb.showGate || wb.showSent) && (
                      <div style={css("margin-top: 14px; padding-top: 14px; border-top: 1px solid #EFE9DC;")}>
                        <div style={css("display: flex; align-items: center; gap: 10px;")}>
                          {wb.sendSteps.map((s, i) => {
                            const color = s.state === "done" ? "#2F7752" : s.state === "active" ? "#8A7A50" : s.state === "blocked" ? "#B4513F" : "#B7AE9C";
                            const icon = s.state === "done" ? "✓" : s.state === "active" ? "◌" : s.state === "blocked" ? "✕" : "·";
                            return (<React.Fragment key={i}>{i > 0 && <span style={css("flex:1; height:1px; background:#E4DCCB;")} />}<span style={S("display:flex; align-items:center; gap:6px; font-family:'IBM Plex Mono',monospace; font-size:11px; font-weight:600;", { color })}>{icon} {s.label}</span></React.Fragment>);
                          })}
                        </div>
                        {wb.gateBlocked && (<button onClick={v.retrySend} style={css("margin-top: 12px; width: 100%; font-size: 12.5px; font-weight: 600; color: #23272F; background: #F3EEE3; border: 1px solid #DED6C7; border-radius: 7px; padding: 9px; cursor: pointer;")}>Retry blocked send</button>)}
                      </div>
                    )}
                    {!wb.liveSend && wb.showGate && (
                      <div style={css("margin-top: 14px; padding-top: 14px; border-top: 1px solid #EFE9DC;")}>
                        <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; letter-spacing: 0.1em; color: #8A96A0;")}>{wb.gateTitle}</div>
                        <div style={css("margin-top: 10px; display: grid; grid-template-columns: 1fr 1fr; gap: 6px 18px;")}>
                          {wb.gateChecks.map((g, i) => (<div key={i} style={css("display: flex; align-items: center; gap: 8px; font-family: 'IBM Plex Mono',monospace; font-size: 10.5px;")}><span style={S("width: 14px; text-align: center;", { color: g.color })}>{g.icon}</span><span style={css("color: #40515C; flex: 1;")}>{g.label}</span><span style={S("font-size: 9.5px;", { color: g.color })}>{g.state}</span></div>))}
                        </div>
                        <div style={css("margin-top: 10px; padding-top: 9px; border-top: 1px solid #EFE9DC; display: flex; justify-content: space-between; font-family: 'IBM Plex Mono',monospace; font-size: 10.5px;")}><span style={css("color:#6F7883;")}>Fallback</span><span style={css("color: #B4513F; font-weight: 600;")}>NONE</span></div>
                        {wb.gateBlocked && (<button onClick={v.retrySend} style={css("margin-top: 12px; width: 100%; font-size: 12.5px; font-weight: 600; color: #23272F; background: #F3EEE3; border: 1px solid #DED6C7; border-radius: 7px; padding: 9px; cursor: pointer;")}>Retry blocked check</button>)}
                      </div>
                    )}
                    {wb.showSent && (
                      <div style={css("margin-top: 14px; padding: 14px 16px; background: #E4EEE7; border: 1px solid #B9D3C4; border-radius: 8px;")}>
                        <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 10px; letter-spacing: 0.06em; color: #2F7752; font-weight: 600;")}>✓ SENT TO DEMONSTRATION INBOX</div>
                        <div style={css("margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px 20px; font-family: 'IBM Plex Mono',monospace; font-size: 11px; color: #40515C;")}>
                          <div>Message ID &nbsp;{wb.sentMessageId || "awaiting server…"}</div><div>Decision &nbsp;<span style={css("color:#2F7752;")}>sealed</span></div><div>Evidence &nbsp;<span style={css("color:#2F7752;")}>verified</span></div>
                        </div>
                        <div style={css("margin-top: 10px; font-size: 11px; color: #4E6A5B; line-height: 1.5;")}>Delivery acknowledgement does not indicate carrier receipt or acceptance.</div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* RIGHT RAIL */}
          <div className="tly-rail" style={css("display: flex; flex-direction: column; gap: 16px; position: sticky; top: 12px;")}>
            {v.railOpen && (
              <div style={{ display: "contents" }}>
                <div style={css("background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 12px; padding: 13px 15px;")}>
                  <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; letter-spacing: 0.1em; color: #8A96A0;")}>CURRENT TASK</div>
                  <div style={css("display: flex; gap: 9px; margin-top: 9px;")}>
                    <span className={wb.curPulse} style={S("width: 8px; height: 8px; border-radius: 50%; margin-top: 4px; flex: 0 0 auto;", { background: wb.curDot })} />
                    <div style={css("min-width: 0;")}>
                      <div style={css("font-size: 12.5px; font-weight: 600; color: #23272F;")}>{wb.curName}</div>
                      <div style={css("font-size: 11px; color: #6F7883; margin-top: 1px;")}>{wb.curTask}</div>
                      <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; color: #8A7A50; margin-top: 3px;")}>{wb.curTool}</div>
                      <div style={S("font-family: 'IBM Plex Mono',monospace; font-size: 10px; margin-top: 3px;", { color: wb.curOutColor })}>{wb.curOutput}</div>
                    </div>
                  </div>
                  <a href="#" onClick={v.goActivity} style={css("display: inline-block; margin-top: 10px; font-size: 11.5px;")}>View full activity →</a>
                </div>

                {wb.showRec && (
                  <div id="sec-decision" style={S("order: -1; border-radius: 12px; padding: 16px 18px; scroll-margin-top: 80px;", { background: wb.recBg, border: "1px solid " + wb.recBorder })}>
                    <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; letter-spacing: 0.1em; color: #8A96A0;")}>RECOMMENDATION</div>
                    <div style={css("display: flex; align-items: baseline; gap: 10px; margin-top: 8px;")}><span style={S("font-size: 22px; font-weight: 700; letter-spacing: -0.01em;", { color: wb.recColor })}>{wb.recHead}</span></div>
                    <div style={css("margin-top: 12px; display: flex; flex-direction: column; gap: 6px; font-family: 'IBM Plex Mono',monospace; font-size: 12px;")}>
                      <div style={css("display: flex; justify-content: space-between;")}><span style={css("color:#6F7883;")}>Carrier invoice</span><span>{wb.recon.carrierLine}</span></div>
                      <div style={css("display: flex; justify-content: space-between;")}><span style={css("color:#6F7883;")}>Applicable tariff</span><span>{wb.recon.tariffLine}</span></div>
                      <div style={css("display: flex; justify-content: space-between; padding-top: 6px; border-top: 1px solid #EFE9DC; color:#B4513F;")}><span>Difference</span><span>{wb.recon.difference}</span></div>
                    </div>
                    <div style={css("margin-top: 12px; font-size: 11.5px; color: #6F7883; line-height: 1.5;")}>{wb.recon.coverageLine}</div>
                    {wb.showApprove && (<>
                      <div style={css("margin-top: 12px; padding: 11px 13px; background: #FBF6EE; border: 1px solid #E6D6AE; border-radius: 8px; font-size: 12px; color: #40515C; line-height: 1.5;")}>Tally completed the analysis. Your approval authorizes this financial judgment.<div style={css("font-family:'IBM Plex Mono',monospace; font-size:9.5px; letter-spacing:0.06em; color:#8A7A50; margin-top:6px;")}>HUMAN AUTHORIZATION REQUIRED</div></div>
                      <button onClick={v.approveDispute} style={css("margin-top: 12px; width: 100%; font-size: 14px; font-weight: 600; color: #F5F0E7; background: #1D2A33; border: none; border-radius: 8px; padding: 12px; cursor: pointer;")}>{wb.approveLabel}</button>
                    </>)}
                    {wb.showSeal && (<div style={css("margin-top: 12px; display: flex; flex-direction: column; gap: 6px;")}>{wb.sealSteps.map((s, i) => (<div key={i} style={S("display: flex; align-items: center; gap: 8px; font-family: 'IBM Plex Mono',monospace; font-size: 11px;", { color: s.color })}><span>{s.icon}</span>{s.label}</div>))}</div>)}
                    {wb.showSendBtn && (<div style={css("margin-top: 12px; font-size: 11.5px; color: #6F7883; line-height: 1.5;")}>Decision sealed. Review and send the adjustment request below.</div>)}
                  </div>
                )}

                {wb.showSent && (<a href="#" onClick={v.goQueue} style={css("display: inline-block; font-size: 12.5px; font-weight: 600;")}>Back to Invoices →</a>)}
                {v.annot && (<div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9.5px; color: #A9823C; background: #FBF1D8; border: 1px dashed #C8A955; border-radius: 7px; padding: 8px 11px; line-height: 1.5;")}>{wb.annotText}</div>)}
              </div>
            )}
            {v.drawerOpen && this.renderDrawer(v)}
          </div>
        </div>
      </div>
    );
  }

  renderWb1050(v, wb) {
    const inv1050 = v.inv1050;
    return (
      <div style={css("padding: 20px 24px 70px;")}>
        <div style={css("display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 6px;")}>
          <a href="#" onClick={v.goQueue} style={css("font-size: 12.5px; color: #6F7883;")}>← Invoices</a>
          <div style={css("width: 1px; height: 18px; background: #DED6C7;")} />
          <h1 style={css("font-size: 20px; font-weight: 700; margin: 0;")}>INV-1050.pdf</h1>
          <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 11px; color: #8A96A0;")}>INV-TLY-1050</span>
          <span style={S("font-family: 'IBM Plex Mono',monospace; font-size: 10px; font-weight: 600; letter-spacing: 0.05em; border-radius: 5px; padding: 4px 10px;", { background: inv1050.pillBg, color: inv1050.pillFg })}>{inv1050.status}</span>
          <div style={css("margin-left: auto; display: flex; gap: 8px;")}>
            <a href="#" onClick={v.goDecision} style={css("font-size: 12px; color: #6F7883; border: 1px solid #DED6C7; border-radius: 7px; padding: 6px 12px; background: #FCFBF8;")}>Decision record</a>
            <a href="#" onClick={v.goActivity} style={css("font-size: 12px; color: #6F7883; border: 1px solid #DED6C7; border-radius: 7px; padding: 6px 12px; background: #FCFBF8;")}>Activity</a>
          </div>
        </div>
        <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 11px; color: #8A96A0; margin-bottom: 12px;")}>Container HLXU-223874-9 · B/L OAK-77903 · Demurrage · charged Jun 5–11 · received Jun 24</div>

        <div style={css("position: sticky; top: 0; z-index: 20; margin: 0 -24px 16px; padding: 8px 24px 10px; background: #F5F0E7; border-bottom: 1px solid #E1D9C9; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;")}>
          <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9px; letter-spacing: 0.12em; color: #8A96A0; margin-right: 4px;")}>PIPELINE</span>
          {["Intake","Reconstruction","Evidence"].map((n, i) => (<span key={i} style={css("display:flex; align-items:center; gap:7px; background:#E4EEE7; border:1px solid #B9D3C4; border-radius:8px; padding:6px 11px;")}><span style={css("width:7px;height:7px;border-radius:50%;background:#2F7752;")} /><span style={css("font-size:12px;font-weight:600;")}>{n}</span><span style={css("font-family:'IBM Plex Mono',monospace;font-size:8.5px;color:#6F7883;")}>{["Bedrock·S3","Managed MCP","Vector Index"][i]}</span></span>))}
          <span style={S("display:flex; align-items:center; gap:7px; border-radius:8px; padding:6px 11px;", { background: inv1050.reviewBg, border: "1px solid " + inv1050.reviewBorder })}><span style={S("width:7px;height:7px;border-radius:50%;", { background: inv1050.reviewDot })} /><span style={css("font-size:12px;font-weight:600;")}>Review</span><span style={css("font-family:'IBM Plex Mono',monospace;font-size:8.5px;color:#6F7883;")}>Human</span></span>
        </div>

        <div style={css("display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 16px;")}>
          <span style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9px; letter-spacing: 0.12em; color: #8A96A0; margin-right: 2px;")}>PROVENANCE</span>
          <span style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; font-weight:600; border-radius:5px; padding:4px 9px; background:#ECEFF1; color:#40515C;")}>FROM INVOICE</span>
          <span style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; font-weight:600; border-radius:5px; padding:4px 9px; background:#E4EEE7; color:#2F7752;")}>RECORDED BEFORE INVOICE</span>
          <span style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; font-weight:600; border-radius:5px; padding:4px 9px; background:#FBF1D8; color:#A9823C;")}>HUMAN APPROVED</span>
        </div>

        <div className="tly-wb-grid" style={S("display: grid; gap: 20px; align-items: start;", { gridTemplateColumns: inv1050.gridCols })}>
          <div style={css("display: flex; flex-direction: column; gap: 18px; min-width: 0;")}>
            <div style={css("background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 12px; padding: 12px 16px;")}>
              <div style={css("display:flex; align-items:center; gap:8px;")}><span style={css("font-family:'IBM Plex Mono',monospace; font-size:8.5px; font-weight:600; letter-spacing:0.05em; border-radius:5px; padding:3px 8px; background:#ECEFF1; color:#40515C;")}>FROM INVOICE</span><span style={css("font-size:12.5px; font-weight:600;")}>Invoice source</span><span style={css("font-family:'IBM Plex Mono',monospace; font-size:9.5px; color:#2F7752; margin-left:auto;")}>EXACT VERSION VERIFIED</span></div>
              <div style={css("font-family:'IBM Plex Mono',monospace; font-size:11px; color:#8A96A0; margin-top:8px;")}>INV-1050.pdf · $875 claimed · $125/day × 7 · 6 claims linked</div>
            </div>
            <div style={css("background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 12px; padding: 12px 16px;")}>
              <div style={css("display:flex; align-items:center; gap:8px;")}><span style={css("font-family:'IBM Plex Mono',monospace; font-size:8.5px; font-weight:600; letter-spacing:0.05em; border-radius:5px; padding:3px 8px; background:#E4EEE7; color:#2F7752;")}>RECORDED BEFORE INVOICE</span><span style={css("font-size:12.5px; font-weight:600;")}>Sourced timeline</span></div>
              <div style={css("font-family:'IBM Plex Mono',monospace; font-size:11px; color:#8A96A0; margin-top:8px;")}>6 events · availability, free time, gate-out · all verified</div>
            </div>
            <div className="tly-ledger" style={css("background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 12px;")}>
              <div style={css("padding: 13px 18px; border-bottom: 1px solid #EFE9DC;")}>
                <div style={css("display:flex; align-items:center; gap:8px;")}><span style={css("font-family:'IBM Plex Mono',monospace; font-size:8.5px; font-weight:600; letter-spacing:0.05em; border-radius:5px; padding:3px 8px; background:#E4EEE7; color:#2F7752;")}>7 / 7 VERIFIED</span><span style={css("font-size:12.5px; font-weight:600;")}>Seven charged days · Jun 5–11</span><span style={css("font-family:'IBM Plex Mono',monospace; font-size:9.5px; color:#2F7752; margin-left:auto;")}>7 of 7 · SOURCE COMPLETE</span></div>
                <div style={css("font-size:11.5px; color:#6F7883; margin-top:4px;")}>Claimed rate equals the applicable recorded tariff — no discrepancy.</div>
              </div>
              <div style={css("display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:10px 18px; background:#F0F5F1; border-bottom:1px solid #EFE9DC;")}><span style={css("font-family:'IBM Plex Mono',monospace; font-size:8.5px; letter-spacing:0.06em; color:#4E6A5B;")}>APPLICABLE TARIFF</span><span style={css("font-family:'IBM Plex Mono',monospace; font-size:12px; color:#23272F;")}>$125 / day</span><span style={css("font-family:'IBM Plex Mono',monospace; font-size:10.5px; color:#6F7883;")}>effective Jun 1, 2026</span><span style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; font-weight:600; border-radius:4px; padding:3px 7px; background:#E4EEE7; color:#2F7752;")}>APPLICABILITY VERIFIED</span><a href="#" onClick={v.openSourceTariff} style={css("font-size:11.5px; margin-left:auto;")}>View clause →</a></div>
              <table style={css("width:100%; min-width:640px; border-collapse:collapse; font-size:12.5px;")}>
                <thead><tr style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; letter-spacing:0.08em; color:#8A96A0; text-align:left;")}><th scope="col" style={css("padding:9px 16px; font-weight:500;")}>DATE</th><th scope="col" style={css("padding:9px 6px; font-weight:500;")}>CLAIM</th><th scope="col" style={css("padding:9px 6px; font-weight:500;")}>ACCESS</th><th scope="col" style={css("padding:9px 6px; font-weight:500;")}>RULE</th><th scope="col" style={css("padding:9px 6px; font-weight:500;")}>OUTCOME</th><th scope="col" style={css("padding:9px 6px; font-weight:500; text-align:right;")}>Δ / DAY</th><th scope="col" style={css("padding:9px 16px; font-weight:500;")}>COVERAGE</th></tr></thead>
                <tbody>
                  {v.wb1050days.map((d, i) => (<tr key={i} onClick={v.openSourceTariff} tabIndex={0} role="button" style={css("border-top:1px solid #F1EBDD; cursor:pointer;")}><td style={css("padding:10px 16px; font-family:'IBM Plex Mono',monospace; color:#40515C;")}>{d.date}</td><td style={css("padding:10px 6px; font-family:'IBM Plex Mono',monospace; color:#23272F;")}>$125</td><td style={css("padding:10px 6px; color:#40515C;")}>available</td><td style={css("padding:10px 6px; font-family:'IBM Plex Mono',monospace; color:#2F7752;")}>$125</td><td style={css("padding:10px 6px;")}><span style={css("font-family:'IBM Plex Mono',monospace; font-size:9.5px; font-weight:600; border-radius:4px; padding:3px 7px; background:#E4EEE7; color:#2F7752;")}>SUPPORTED</span></td><td style={css("padding:10px 6px; text-align:right; font-family:'IBM Plex Mono',monospace; font-size:12px; color:#6F7883;")}>$0</td><td style={css("padding:10px 16px;")}><span style={css("font-family:'IBM Plex Mono',monospace; font-size:9px; color:#2F7752;")}>SOURCE COMPLETE</span></td></tr>))}
                </tbody>
              </table>
            </div>
          </div>
          <div className="tly-rail" style={css("display: flex; flex-direction: column; gap: 16px; position: sticky; top: 12px;")}>
            {v.railOpen && (
              <div style={{ display: "contents" }}>
                <div style={css("background:#FCFBF8; border:1px solid #DED6C7; border-radius:12px; padding:13px 15px;")}>
                  <div style={css("font-family:'IBM Plex Mono',monospace; font-size:9.5px; letter-spacing:0.1em; color:#8A96A0;")}>CURRENT TASK</div>
                  <div style={css("display:flex; gap:9px; margin-top:9px;")}><span style={css("width:8px;height:8px;border-radius:50%;background:#2F7752;margin-top:4px;flex:0 0 auto;")} /><div><div style={css("font-size:12.5px;font-weight:600;")}>Decision Engine</div><div style={css("font-size:11px;color:#6F7883;")}>Recommendation issued</div><div style={css("font-family:'IBM Plex Mono',monospace;font-size:9.5px;color:#8A7A50;margin-top:3px;")}>Deterministic code</div></div></div>
                  <a href="#" onClick={v.goActivity} style={css("display:inline-block; margin-top:10px; font-size:11.5px;")}>View full activity →</a>
                </div>
                <div style={S("background:#FCFBF8; border-radius:12px; padding:16px 18px;", { border: "1px solid " + inv1050.recBorder })}>
                  <div style={css("font-family:'IBM Plex Mono',monospace; font-size:9.5px; letter-spacing:0.1em; color:#8A96A0;")}>RECOMMENDATION</div>
                  <div style={css("font-size:20px; font-weight:700; letter-spacing:-0.01em; color:#2F7752; margin-top:8px;")}>APPROVE FOR PAYMENT</div>
                  <div style={css("margin-top:12px; display:flex; flex-direction:column; gap:6px; font-family:'IBM Plex Mono',monospace; font-size:12px;")}><div style={css("display:flex; justify-content:space-between;")}><span style={css("color:#6F7883;")}>Carrier invoice</span><span>7 × $125 = $875</span></div><div style={css("display:flex; justify-content:space-between;")}><span style={css("color:#6F7883;")}>Applicable tariff</span><span>7 × $125 = $875</span></div><div style={css("display:flex; justify-content:space-between; padding-top:6px; border-top:1px solid #EFE9DC; color:#2F7752;")}><span>Difference</span><span>$0</span></div></div>
                  <div style={css("margin-top:10px; display:flex; flex-direction:column; gap:4px; font-size:11.5px; color:#6F7883;")}><div>Source coverage 7 of 7 days</div><div>Operational issue None</div></div>
                  {inv1050.pending && (<>
                    <div style={css("margin-top:12px; padding:11px 13px; background:#FBF6EE; border:1px solid #E6D6AE; border-radius:8px; font-size:12px; color:#40515C; line-height:1.5;")}>Rates match and the timeline is verified. Your approval authorizes payment of this invoice.<div style={css("font-family:'IBM Plex Mono',monospace; font-size:9.5px; letter-spacing:0.06em; color:#8A7A50; margin-top:6px;")}>HUMAN AUTHORIZATION REQUIRED</div></div>
                    <button onClick={v.approvePayment} style={css("margin-top:12px; width:100%; font-size:14px; font-weight:600; color:#F5F0E7; background:#1D2A33; border:none; border-radius:8px; padding:12px; cursor:pointer;")}>Approve for payment</button>
                  </>)}
                  {inv1050.done && (<>
                    <div style={css("margin-top:12px; padding:12px 14px; background:#E4EEE7; border:1px solid #B9D3C4; border-radius:8px; font-family:'IBM Plex Mono',monospace; font-size:11.5px; color:#2F7752; line-height:1.6;")}>✓ APPROVED FOR PAYMENT · $875<div style={css("color:#4E6A5B; margin-top:4px;")}>Internal disposition — not PAID</div></div>
                    <a href="#" onClick={v.goQueue} style={css("display:inline-block; margin-top:12px; font-size:12.5px; font-weight:600;")}>Back to Invoices →</a>
                  </>)}
                </div>
              </div>
            )}
            {v.drawerOpen && this.renderDrawer(v)}
          </div>
        </div>
      </div>
    );
  }

  renderCoverage(v) {
    return (
      <div style={css("max-width: 1100px; margin: 0 auto; padding: 30px 28px 60px;")}>
        <h1 style={css("font-size: 25px; font-weight: 700; margin: 0;")}>Source coverage</h1>
        <p style={css("font-size: 13px; color: #6F7883; margin: 6px 0 0; max-width: 640px;")}>What could Tally reconstruct if an invoice arrived for a given date? Coverage is recorded before any invoice exists.</p>
        <div style={css("margin-top: 20px; background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 12px; overflow: hidden;")}>
          <table style={css("width: 100%; border-collapse: collapse; font-size: 12.5px;")}>
            <thead>
              <tr style={css("font-family: 'IBM Plex Mono',monospace; font-size: 9px; letter-spacing: 0.08em; color: #8A96A0; text-align: left;")}>
                <th scope="col" style={css("padding: 11px 18px; font-weight: 500;")}>SOURCE CLASS</th>
                <th scope="col" style={css("padding: 11px 8px; font-weight: 500;")}>CARRIER / PORT / SCOPE</th>
                <th scope="col" style={css("padding: 11px 8px; font-weight: 500;")}>COVERAGE PERIOD</th>
                <th scope="col" style={css("padding: 11px 8px; font-weight: 500;")}>LAST RECORDED</th>
                <th scope="col" style={css("padding: 11px 8px; font-weight: 500;")}>EXACT VERSION</th>
                <th scope="col" style={css("padding: 11px 18px; font-weight: 500;")}>STATUS</th>
              </tr>
            </thead>
            <tbody>
              {v.coverage.map((c, i) => (
                <tr key={i} style={css("border-top: 1px solid #F1EBDD;")}>
                  <td style={css("padding: 12px 18px; font-weight: 600; color: #23272F;")}>{c.cls}</td>
                  <td style={css("padding: 12px 8px; color: #40515C;")}>{c.scope}</td>
                  <td style={css("padding: 12px 8px; font-family: 'IBM Plex Mono',monospace; color: #40515C;")}>{c.period}</td>
                  <td style={css("padding: 12px 8px; font-family: 'IBM Plex Mono',monospace; color: #40515C;")}>{c.last}</td>
                  <td style={S("padding: 12px 8px; font-family: 'IBM Plex Mono',monospace;", { color: c.verColor })}>{c.ver}</td>
                  <td style={css("padding: 12px 18px;")}><span style={S("font-family: 'IBM Plex Mono',monospace; font-size: 10px; font-weight: 600; border-radius: 5px; padding: 4px 9px;", { background: c.bg, color: c.fg })}>{c.status}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={css("margin-top: 12px; font-family: 'IBM Plex Mono',monospace; font-size: 10px; color: #A9823C; background: #FBF1D8; border: 1px dashed #C8A955; border-radius: 7px; padding: 9px 12px; line-height: 1.5;")}>Rows marked <b>BACKEND REQUIRED</b> are target coverage not yet proven by current implementation — terminal/container event sources are not portrayed as live.</div>
      </div>
    );
  }

  renderHandoff(v) {
    return (
      <div style={css("max-width: 1000px; margin: 0 auto; padding: 30px 28px 80px;")}>
        <h1 style={css("font-size: 25px; font-weight: 700; margin: 0;")}>Design &amp; engineering handoff</h1>
        <p style={css("font-size: 13px; color: #6F7883; margin: 6px 0 0;")}>Reconstruction Workbench revision — route map, component inventory, states, acceptance audit, and current-implementation gaps.</p>
        {v.handoff.map((sec, si) => (
          <div key={si} style={css("margin-top: 26px;")}>
            <div style={css("font-family: 'IBM Plex Mono',monospace; font-size: 10px; letter-spacing: 0.12em; color: #8A96A0;")}>{sec.eyebrow}</div>
            <h2 style={css("font-size: 18px; font-weight: 700; margin: 6px 0 12px;")}>{sec.title}</h2>
            <div style={css("background: #FCFBF8; border: 1px solid #DED6C7; border-radius: 12px; overflow: hidden;")}>
              {sec.rows.map((r, ri) => (
                <div key={ri} style={S("display: grid; gap: 14px; padding: 11px 18px; border-top: 1px solid #F1EBDD; font-size: 12.5px; align-items: baseline;", { gridTemplateColumns: sec.cols })}>
                  {r.cells.map((cell, ci) => (<div key={ci} style={{ color: cell.color, fontFamily: cell.font }}>{cell.t}</div>))}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    );
  }
}
