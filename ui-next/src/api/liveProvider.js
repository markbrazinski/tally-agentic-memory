// Live provider — talks to the real Tally backend. Same-origin by default (the
// FastAPI app serves this SPA and the /api routes), override with VITE_API_BASE.
//
// It never invents data: a failed or incomplete response surfaces as an error /
// unavailable, never a substituted mock (the fail-closed rule from the handoff).

const BASE = import.meta.env.VITE_API_BASE || "";

// Authentication is the Cognito session cookie (`tally_session`), set httpOnly +
// Secure + SameSite=Strict by POST /api/login and validated server-side on every
// request. `credentials: "same-origin"` is what carries it; the browser never
// sees or holds a token, so there is NOTHING to embed in this bundle.
//
// A build-time bearer token used to be baked in here (VITE_DEMO_TOKEN). That is
// deliberately gone: anything shipped to the browser is public, so a token in the
// bundle is a published credential. Do not reintroduce one.
function authHeaders(extra) {
  return { ...(extra || {}) };
}

// A 401 means the session is missing or expired. Bounce to the login screen
// rather than surfacing a raw error — an expired judge session should read as
// "sign in again", not as a broken product.
function handleUnauthorized(res) {
  if (res.status === 401 && typeof window !== "undefined") {
    window.location.href = "/login";
  }
  return res;
}

async function getJson(path) {
  const res = handleUnauthorized(
    await fetch(BASE + path, {
      headers: authHeaders({ Accept: "application/json" }),
      credentials: "same-origin",
    }),
  );
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
      // links.source is the real retained-PDF endpoint (exact S3 version).
      return { invoice: body.invoice || body, links: body.links || null, etag };
    },

    async getReconstruction(invoiceId) {
      // 404 before reconstruction exists is normal (not an error state).
      const res = await fetch(BASE + `/api/invoices/${invoiceId}/reconstruction`, {
        headers: authHeaders({ Accept: "application/json" }),
        credentials: "same-origin",
      });
      if (res.status === 404) return null;
      if (!res.ok) throw new Error(`GET reconstruction -> ${res.status}`);
      return await res.json();
    },

    async getDecision(invoiceId) {
      const res = await fetch(BASE + `/api/invoices/${invoiceId}/decision`, {
        headers: authHeaders({ Accept: "application/json" }),
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
    // Judge-facing "Restore demo": returns INV-1048 to READY_FOR_REVIEW so the
    // scenario can be re-run. Server-side it is the same restore the CLI uses,
    // and it touches only the hero — the other two queue outcomes are stable.
    async restoreDemo() {
      const res = handleUnauthorized(
        await fetch(BASE + "/api/demo/restore", {
          method: "POST",
          headers: authHeaders({ Accept: "application/json" }),
          credentials: "same-origin",
        }),
      );
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(
          (detail && (detail.code || detail.error)) || `restore -> ${res.status}`,
        );
      }
      return res.json();
    },

    async approve(invoiceId, recommendationId, approvalEtag, idempotencyKey) {
      const res = await fetch(
        BASE + `/api/invoices/${invoiceId}/recommendations/${recommendationId}/approve`,
        {
          method: "POST",
          headers: authHeaders({
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey,
            "If-Match": approvalEtag,
          }),
          credentials: "same-origin",
        },
      );
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(`approve -> ${res.status} ${JSON.stringify(detail)}`);
      }
      return await res.json();
    },

    // Draft the adjustment request from the sealed decision (idempotent per seal).
    async draft(invoiceId) {
      const res = await fetch(
        BASE + `/api/invoices/${invoiceId}/correspondence/draft`,
        {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          credentials: "same-origin",
        },
      );
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(`draft -> ${res.status} ${JSON.stringify(detail)}`);
      }
      return await res.json();
    },

    // Second authorization: run fresh send gates, then controlled send (idempotent).
    async approveSend(invoiceId, idempotencyKey) {
      const res = await fetch(
        BASE + `/api/invoices/${invoiceId}/correspondence/send`,
        {
          method: "POST",
          headers: authHeaders({
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey,
          }),
          credentials: "same-origin",
        },
      );
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(`send -> ${res.status} ${JSON.stringify(detail)}`);
      }
      return await res.json();
    },

    // Latest draft + send projection (or null) — backs the sent status + message id.
    async getCorrespondence(invoiceId) {
      const res = await fetch(BASE + `/api/invoices/${invoiceId}/correspondence`, {
        headers: authHeaders({ Accept: "application/json" }),
        credentials: "same-origin",
      });
      if (res.status === 404) return null;
      if (!res.ok) throw new Error(`GET correspondence -> ${res.status}`);
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
    // Why NEEDS EVIDENCE is unresolved, shown as the queue-row outcome detail.
    unresolvedReason: inv.unresolved_reason ?? rec.unresolved_reason ?? null,
    receivedAt: inv.received_at || null,
  };
}
