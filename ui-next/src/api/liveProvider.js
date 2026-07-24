// Live provider — talks to the real Tally backend. Same-origin by default (the
// FastAPI app serves this SPA and the /api routes), override with VITE_API_BASE.
//
// It never invents data: a failed or incomplete response surfaces as an error /
// unavailable, never a substituted mock (the fail-closed rule from the handoff).

const BASE = import.meta.env.VITE_API_BASE || "";

async function getJson(path) {
  const res = await fetch(BASE + path, {
    headers: { Accept: "application/json" },
    credentials: "same-origin",
  });
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return { body: await res.json(), etag: res.headers.get("ETag") };
}

export function createLiveProvider() {
  return {
    kind: "live",

    async listInvoices() {
      const { body } = await getJson("/api/invoices");
      return (body.invoices || []).map(normalizeQueueRow);
    },

    async getInvoice(invoiceId) {
      const { body, etag } = await getJson(`/api/invoices/${invoiceId}`);
      return { invoice: body.invoice || body, etag };
    },

    async getReconstruction(invoiceId) {
      // 404 before reconstruction exists is normal (not an error state).
      const res = await fetch(BASE + `/api/invoices/${invoiceId}/reconstruction`, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (res.status === 404) return null;
      if (!res.ok) throw new Error(`GET reconstruction -> ${res.status}`);
      return await res.json();
    },

    async getDecision(invoiceId) {
      const res = await fetch(BASE + `/api/invoices/${invoiceId}/decision`, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (res.status === 404) return null;
      if (!res.ok) throw new Error(`GET decision -> ${res.status}`);
      return await res.json();
    },

    // Subscribe to the aggregate SSE stream. onEvent gets each parsed public
    // event. Returns an unsubscribe function. Reconnect is native to
    // EventSource; Last-Event-ID resumes the cursor.
    subscribe(onEvent, onError) {
      const es = new EventSource(BASE + "/api/stream", { withCredentials: true });
      es.onmessage = (m) => {
        try { onEvent(JSON.parse(m.data)); } catch { /* heartbeat / non-JSON */ }
      };
      // Named events (event: <type>) also arrive; forward them.
      const forward = (e) => {
        try { onEvent(JSON.parse(e.data)); } catch { /* ignore */ }
      };
      [
        "invoice.received", "reconstruction.memory_retrieval_started",
        "reconstruction.completed", "evidence.rule_search_started",
        "evidence.rule_verified", "decision.recommendation_ready",
        "decision.sealed", "correspondence.sent", "correspondence.send_blocked",
      ].forEach((t) => es.addEventListener(t, forward));
      es.onerror = (e) => { if (onError) onError(e); };
      return () => es.close();
    },

    // First human authorization: approve the frozen recommendation + seal.
    async approve(invoiceId, recommendationId, approvalEtag, idempotencyKey) {
      const res = await fetch(
        BASE + `/api/invoices/${invoiceId}/recommendations/${recommendationId}/approve`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey,
            "If-Match": approvalEtag,
          },
          credentials: "same-origin",
        },
      );
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(`approve -> ${res.status} ${JSON.stringify(detail)}`);
      }
      return await res.json();
    },
  };
}

function claimVal(claims, field) {
  const c = claims && claims[field];
  return c ? c.value : null;
}
function claimMinor(claims, field) {
  const c = claims && claims[field];
  return c ? c.amount_minor : null;
}

// The queue row is built from the invoice projection's own fields + its claims
// dict (container/total live inside claims). Kept resilient to either the
// intake projection shape or a future dedicated queue projection.
function normalizeQueueRow(inv) {
  const claims = inv.claims || {};
  const status = inv.aggregate_status || inv.status || "RECEIVED";
  const container =
    inv.container_ref || claimVal(claims, "container_number") || null;
  const amountMinor =
    inv.claimed_amount_minor ?? claimMinor(claims, "total") ?? null;
  const rec = inv.recommendation || {};
  return {
    invoiceId: inv.invoice_id || inv.id,
    name: inv.display_name || `${inv.invoice_no || inv.invoice_id}.pdf`,
    container,
    amountMinor,
    disputedMinor: inv.disputed_amount_minor ?? rec.disputed_amount_minor ?? null,
    aggregateStatus: status,
    recommendationType: inv.recommendation_type || rec.recommendation_type || null,
    receivedAt: inv.received_at || null,
  };
}
