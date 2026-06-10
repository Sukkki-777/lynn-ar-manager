let state = null;
let selectedInvoice = null;
let autoRun = true;
let poView = "ready";
let queueFilter = "pending";
let editingDraftId = null;

const fmtMoney = (value, currency = "USD") =>
  new Intl.NumberFormat("en-US", { style: "currency", currency }).format(Number(value || 0));

const byId = (id) => document.getElementById(id);

function isCashflowText(text = "") {
  const normalized = text.toLowerCase();
  const compact = normalized.replace(/[^a-z0-9]+/g, "");
  return (
    compact.includes("cashflow") ||
    normalized.includes("cash flow") ||
    normalized.includes("cash-flow") ||
    (compact.includes("cash") && /\d+\s*(day|days|week|weeks|month|months)/.test(normalized)) ||
    (compact.includes("cash") && normalized.includes("forecast"))
  );
}

function parseDate(value) {
  if (!value) return null;
  const parsed = new Date(`${value}T00:00:00`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function todayDate() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

function daysBetween(start, end) {
  return Math.round((end.getTime() - start.getTime()) / (1000 * 60 * 60 * 24));
}

function parseForecastWindow(command = "") {
  const match = command.toLowerCase().match(/(\d+)\s*(day|days|week|weeks|month|months)/);
  if (!match) return 30;
  const amount = Number(match[1]);
  const unit = match[2];
  if (unit.startsWith("week")) return amount * 7;
  if (unit.startsWith("month")) return amount * 30;
  return amount;
}

function collectionProbability(invoice) {
  if (invoice.status === "paid") return 0;
  const dueDate = parseDate(invoice.due_date);
  const overdueDays = dueDate ? Math.max(0, daysBetween(dueDate, todayDate())) : 0;
  const score = Number(invoice.risk_profile?.score || 0);
  if (score >= 75 || overdueDays >= 30 || invoice.status === "high_risk") return 0.25;
  if (invoice.status === "overdue" || overdueDays > 0) return 0.55;
  if (invoice.status === "due_soon") return 0.85;
  return 0.9;
}

function buildCashflowForecast(invoices = [], horizonDays = 30) {
  const start = todayDate();
  const end = new Date(start);
  end.setDate(end.getDate() + horizonDays);
  const future = [];
  const overdue = [];
  const eligible = [];

  invoices.forEach((invoice) => {
    if (invoice.status === "paid") return;
    const due = parseDate(invoice.due_date);
    if (!due) return;
    const amount = Number(invoice.amount || 0);
    const probability = collectionProbability(invoice);
    const record = {
      invoice: invoice.invoice_number || "",
      client: invoice.client_name || "",
      amount,
      currency: invoice.currency || "USD",
      due_date: invoice.due_date || "",
      status: invoice.status || "",
      probability,
      risk_adjusted_amount: Math.round(amount * probability * 100) / 100,
    };
    if (due < start) overdue.push(record);
    if (due >= start && due <= end) future.push(record);
    if (due <= end) eligible.push(record);
  });

  return {
    horizon_days: horizonDays,
    end_date: end.toISOString().slice(0, 10),
    contractual: Math.round(eligible.reduce((sum, item) => sum + item.amount, 0) * 100) / 100,
    risk_adjusted:
      Math.round(eligible.reduce((sum, item) => sum + item.risk_adjusted_amount, 0) * 100) / 100,
    at_risk: Math.round(overdue.reduce((sum, item) => sum + item.amount, 0) * 100) / 100,
    future,
    overdue,
  };
}

function localCashflowResult(command) {
  const forecast = buildCashflowForecast(receivableRecords(), parseForecastWindow(command));
  return {
    id: `local-${Date.now()}`,
    ts: new Date().toISOString(),
    command,
    intent: "cash_flow_forecast",
    message: `Cash flow forecast through ${forecast.end_date}.`,
    details: [],
    payload: forecast,
    dismissed_at: "",
  };
}

function parseTermsDays(value = "") {
  const match = String(value || "").match(/(\d+)/);
  return match ? Number(match[1]) : 30;
}

function addDays(date, days) {
  const copy = new Date(date);
  copy.setDate(copy.getDate() + days);
  return copy;
}

function poAsReceivable(po = {}) {
  const shipment = parseDate(po.shipment_date) || parseDate(po.po_date) || todayDate();
  const due = addDays(shipment, parseTermsDays(po.payment_terms));
  const dueText = due.toISOString().slice(0, 10);
  return {
    id: `pipeline:${po.id || po.po_number || ""}`,
    invoice_number: po.proposed_invoice_number || po.po_number || "",
    po_number: po.po_number || "",
    client_name: po.client_name || "Unknown",
    client_email: po.client_email || "",
    amount: Number(po.amount || 0),
    currency: po.currency || "USD",
    payment_terms: po.payment_terms || "Net 30",
    shipment_date: po.shipment_date || "",
    due_date: dueText,
    status: invoiceStatusFromDue(due),
    source: "po_pipeline",
  };
}

function invoiceStatusFromDue(dueDate) {
  const days = daysBetween(todayDate(), dueDate);
  if (days < 0) return Math.abs(days) >= 30 ? "high_risk" : "overdue";
  if (days <= 7) return "due_soon";
  return "open";
}

function receivableRecords() {
  const records = [];
  const seenPOs = new Set();
  (state.invoices || []).forEach((invoice) => {
    if (invoice.status === "paid") return;
    records.push(invoice);
    if (invoice.po_number) seenPOs.add(String(invoice.po_number).toLowerCase());
  });
  (state.po_pipeline || []).forEach((po) => {
    if (po.invoice_id || po.status === "invoiced") return;
    const poNumber = String(po.po_number || "").toLowerCase();
    if (poNumber && seenPOs.has(poNumber)) return;
    records.push(poAsReceivable(po));
    if (poNumber) seenPOs.add(poNumber);
  });
  return records;
}

function toast(message) {
  const el = byId("toast");
  el.textContent = message;
  el.classList.add("show");
  window.clearTimeout(window.toastTimer);
  window.toastTimer = window.setTimeout(() => el.classList.remove("show"), 4200);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const payload = await res.json();
  if (!res.ok || payload.error) {
    throw new Error(payload.error || `Request failed: ${res.status}`);
  }
  return payload;
}

async function refresh() {
  try {
    await api("/api/stripe/backfill-links", { method: "POST" });
  } catch (err) {
    console.warn("Stripe link backfill skipped", err);
  }
  try {
    await api("/api/stripe/sync-payments", { method: "POST" });
  } catch (err) {
    console.warn("Stripe sync skipped", err);
  }
  state = await api("/api/state");
  render();
}

function render() {
  renderConfig();
  renderScheduler();
  renderLynnBriefing();
  renderMetrics();
  renderCommandResults();
  renderActivity();
  renderDrafts();
  renderInvoices();
  if (selectedInvoice) {
    selectedInvoice = state.invoices.find((invoice) => invoice.id === selectedInvoice.id) || selectedInvoice;
    renderModal();
  }
}

function sourceLabel(source = "") {
  const normalized = String(source || "").toLowerCase();
  if (normalized.includes("stripe") || normalized.includes("payment")) return "Stripe confirmed payment";
  if (normalized.includes("daily") || normalized.includes("scheduled") || normalized.includes("manual run") || normalized.includes("demo run")) {
    return "Morning check-in";
  }
  if (normalized.includes("command") || normalized.includes("user")) return "Your request";
  if (normalized.includes("lynn reasoning")) return "Lynn";
  if (source === "Agent") return "Lynn";
  if (source === "Agent Reasoning") return "Lynn";
  return source || "Lynn";
}

function providerLabel(provider = "") {
  return {
    kimi: "Kimi-assisted",
    openai: "OpenAI-assisted",
    rules: "Rules-based",
    exa: "Exa-assisted",
  }[String(provider).toLowerCase()] || "";
}

function providerClass(provider = "") {
  const normalized = String(provider || "").toLowerCase();
  return normalized === "kimi" || normalized === "openai" ? "ai" : normalized === "rules" ? "rules" : "neutral";
}

function conciseText(text = "", maxChars = 190) {
  const cleaned = String(text || "").replace(/\s+/g, " ").trim();
  if (!cleaned) return "";
  const sentences = cleaned.split(/(?<=[.!?])\s+/).slice(0, 2).join(" ");
  const short = sentences || cleaned;
  return short.length > maxChars ? `${short.slice(0, maxChars - 1).trim()}…` : short;
}

function lynnVoice(text = "") {
  return String(text || "")
    .replace(/\bThe agent\b/g, "I")
    .replace(/\bthe agent\b/g, "I")
    .replace(/\bThe system\b/g, "I")
    .replace(/\bthe system\b/g, "I")
    .replace(/\bLynn\b/g, "I")
    .replace(/\bAgent\b/g, "I")
    .replace(/\bagent\b/g, "I")
    .replace(/\bhuman confirmation\b/gi, "your OK")
    .replace(/\bhuman approval\b/gi, "your OK")
    .replace(/\bConfirmation Queue\b/g, "Waiting for your OK")
    .replace(/\bCFO Approval Queue\b/g, "Waiting for your OK")
    .replace(/\bDaily scan\b/gi, "Morning check-in")
    .replace(/\bwebhook\b/gi, "Stripe confirmation");
}

function renderExpandableReasoning(id, reasoning, summary, provider, label = "Why I did this") {
  if (!reasoning && !summary) return "";
  const providerText = providerLabel(provider);
  const short = lynnVoice(summary || conciseText(reasoning));
  const hasMore = reasoning && reasoning.length > short.length + 12;
  return `
    <div class="activity-section reasoning-section compact-reasoning">
      <div class="section-title-row">
        <span>${escapeHtml(label)}${providerText ? ` · ${escapeHtml(providerText)}` : ""}</span>
        ${providerText ? `<em class="provider-badge ${escapeHtml(providerClass(provider))}">${escapeHtml(providerText)}</em>` : ""}
      </div>
      <p>${escapeHtml(short)}</p>
      ${
        hasMore
          ? `<details class="reasoning-details"><summary>Show more</summary><p>${escapeHtml(lynnVoice(reasoning))}</p></details>`
          : ""
      }
    </div>
  `;
}

function renderLynnBriefing() {
  if (!byId("lynnHeadline")) return;
  const briefing = state.lynnBriefing || {};
  const stats = briefing.stats || {};
  byId("lynnHeadline").textContent = briefing.headline || "Lynn is checking AR.";
  byId("lynnSummary").textContent = briefing.summary || "Briefing will appear after PO or invoice data is available.";
  byId("lynnNextAction").textContent = briefing.next_best_action || "No urgent approval is waiting.";
  byId("briefCritical").textContent = stats.critical_approvals || 0;
  byId("briefPending").textContent = stats.pending_approvals || 0;
  byId("briefForecast").textContent = fmtMoney(stats.forecast_30d || 0);
  const risk = briefing.top_risk;
  byId("lynnTopRisk").textContent = risk
    ? `Top risk: ${risk.invoice_number} · ${risk.client_name} · ${fmtMoney(risk.amount, risk.currency)} · ${risk.days_overdue}d overdue`
    : "No overdue risk selected.";
}

function renderScheduler() {
  const scheduler = state.scheduler || {
    enabled: true,
    time: "09:00",
    timezone: "Asia/Shanghai",
    mode: "Vercel Cron ready",
    next_run_at: "",
  };
  autoRun = scheduler.enabled !== false;
  const toggle = byId("autoRunToggle");
  toggle.classList.toggle("active", autoRun);
  toggle.setAttribute("aria-pressed", String(autoRun));
  byId("schedulerStatus").textContent = `${autoRun ? "Enabled" : "Paused"} · ${scheduler.time || "09:00"} ${
    scheduler.timezone || "Asia/Shanghai"
  }`;
  byId("schedulerMode").textContent = scheduler.mode || "Vercel Cron ready";
  byId("schedulerNext").textContent = scheduler.next_run_at
    ? new Date(scheduler.next_run_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : "Daily 09:00";
}

function renderConfig() {
  const llm = byId("openaiStatus");
  const stripe = byId("stripeStatus");
  const exa = byId("exaStatus");
  const providerName = state.config.llmProviderName || "LLM";
  const modelName = state.config.llmModel || "";
  llm.textContent = state.config.llmConfigured
    ? `${providerName} ready${modelName ? `: ${modelName}` : ""}`
    : "LLM key missing";
  stripe.textContent = state.config.stripeConfigured ? "Stripe ready" : "Stripe key missing";
  exa.textContent = state.config.exaConfigured ? "Exa ready" : "Exa key missing";
  llm.className = `status-pill ${state.config.llmConfigured ? "ready" : "missing"}`;
  stripe.className = `status-pill ${state.config.stripeConfigured ? "ready" : "missing"}`;
  exa.className = `status-pill ${state.config.exaConfigured ? "ready" : "missing"}`;
}

function renderMetrics() {
  if (!byId("metricOutstanding")) return;
  byId("metricOutstanding").textContent = fmtMoney(state.metrics.outstanding);
  byId("metricOverdue").textContent = fmtMoney(state.metrics.overdue);
  byId("metricDueWeek").textContent = fmtMoney(state.metrics.dueThisWeek);
  byId("metricPaid").textContent = fmtMoney(state.metrics.paidThisMonth);
  const activeInvoices = (state.invoices || []).filter((invoice) => invoice.status !== "paid");
  const paidInvoices = (state.invoices || []).filter((invoice) => invoice.status === "paid");
  const overdueInvoices = activeInvoices.filter((invoice) => invoice.status === "overdue" || invoice.status === "high_risk" || invoiceDaysOverdue(invoice) > 0);
  const dueWeekInvoices = activeInvoices.filter((invoice) => {
    const due = parseDate(invoice.due_date);
    if (!due) return false;
    const days = daysBetween(todayDate(), due);
    return days >= 0 && days <= 7;
  });
  if (byId("metricOutstandingSub")) byId("metricOutstandingSub").textContent = `${activeInvoices.length} invoices`;
  if (byId("metricOverdueSub")) byId("metricOverdueSub").textContent = `${new Set(overdueInvoices.map((invoice) => invoice.client_name)).size} clients`;
  if (byId("metricDueWeekSub")) byId("metricDueWeekSub").textContent = dueWeekInvoices[0]?.client_name || "None due";
  if (byId("metricPaidSub")) byId("metricPaidSub").textContent = `${paidInvoices.length} settled`;

  const forecast = state.cashflowForecast || buildCashflowForecast(state.invoices || [], 30);
  if (byId("cashflowWindow")) byId("cashflowWindow").textContent = `Next ${forecast.horizon_days || 30} days`;
  if (byId("cashflowContractual")) byId("cashflowContractual").textContent = fmtMoney(forecast.contractual);
  if (byId("cashflowRiskAdjusted")) byId("cashflowRiskAdjusted").textContent = fmtMoney(forecast.risk_adjusted);
  if (byId("cashflowAtRisk")) byId("cashflowAtRisk").textContent = fmtMoney(forecast.at_risk);
  renderCashflowBars();
  renderBuyerRiskList();
  renderAgingTable();
  renderStripePayments();
}

function invoiceDaysOverdue(invoice) {
  const due = parseDate(invoice?.due_date);
  if (!due) return 0;
  return Math.max(0, daysBetween(due, todayDate()));
}

function monthLabel(date) {
  return date.toLocaleString("en-US", { month: "short" });
}

function renderCashflowBars() {
  const container = byId("cashflowBars");
  if (!container) return;
  const start = todayDate();
  const months = [0, 1, 2].map((offset) => {
    const date = new Date(start.getFullYear(), start.getMonth() + offset, 1);
    return { key: `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`, label: monthLabel(date), total: 0 };
  });
  receivableRecords().forEach((invoice) => {
    if (invoice.status === "paid") return;
    const due = parseDate(invoice.due_date);
    if (!due) return;
    const key = `${due.getFullYear()}-${String(due.getMonth() + 1).padStart(2, "0")}`;
    const bucket = months.find((item) => item.key === key);
    if (bucket) bucket.total += Number(invoice.amount || 0);
  });
  const max = Math.max(...months.map((item) => item.total), 1);
  container.innerHTML = months
    .map(
      (item) => `
        <div class="cf-row">
          <span class="cf-lbl">${escapeHtml(item.label)}</span>
          <div class="cf-bg"><div class="cf-fill" style="width:${Math.max(3, Math.round((item.total / max) * 100))}%"></div></div>
          <span class="cf-val">${compactMoney(item.total)}</span>
        </div>
      `
    )
    .join("");
}

function renderBuyerRiskList() {
  const container = byId("buyerRiskList");
  if (!container) return;
  const grouped = new Map();
  receivableRecords().forEach((invoice) => {
    if (invoice.status === "paid") return;
    const client = invoice.client_name || "Unknown";
    const risk = invoice.risk_profile || {};
    const score = Number(risk.score || (invoice.status === "high_risk" ? 82 : invoiceDaysOverdue(invoice) > 0 ? 58 : 18));
    const current = grouped.get(client) || { client, score: 0, provider: "" };
    current.score = Math.max(current.score, score);
    if (risk.provider === "exa") current.provider = "Exa";
    grouped.set(client, current);
  });
  const rows = [...grouped.values()].sort((a, b) => b.score - a.score).slice(0, 5);
  if (!rows.length) {
    container.innerHTML = `<div class="empty">No active buyer risk.</div>`;
    return;
  }
  container.innerHTML = rows
    .map((item) => {
      const level = item.score >= 75 ? "Watch closely" : item.score >= 45 ? "Usually on time" : "Very reliable";
      const colorClass = item.score >= 75 ? "pill-clay" : item.score >= 45 ? "pill-sand" : "pill-sage";
      const fill = item.score >= 75 ? "#c89898" : item.score >= 45 ? "#c8b890" : "#98b898";
      return `
        <div class="risk-row">
          <span class="risk-name">${escapeHtml(item.client)}</span>
          <div class="risk-bar-bg"><div class="risk-fill" style="width:${Math.min(100, item.score)}%;background:${fill}"></div></div>
          ${item.provider ? `<span class="risk-source">${escapeHtml(item.provider)}</span>` : ""}
          <span class="pill ${colorClass}">${escapeHtml(level)}</span>
        </div>
      `;
    })
    .join("");
}

function renderAgingTable() {
  const container = byId("agingTable");
  if (!container) return;
  const buckets = {
    current: 0,
    late: 0,
    overdue: 0,
  };
  receivableRecords().forEach((invoice) => {
    if (invoice.status === "paid") return;
    const days = invoiceDaysOverdue(invoice);
    const amount = Number(invoice.amount || 0);
    if (days === 0) buckets.current += amount;
    else if (days <= 60) buckets.late += amount;
    else buckets.overdue += amount;
  });
  container.innerHTML = `
    <div class="aging-row"><span class="aging-lbl">On track (not overdue)</span><span class="aging-val">${fmtMoney(buckets.current)}</span></div>
    <div class="aging-row"><span class="aging-lbl">A little late (1-60d)</span><span class="aging-val amber">${fmtMoney(buckets.late)}</span></div>
    <div class="aging-row"><span class="aging-lbl">Needs attention (60d+)</span><span class="aging-val red">${fmtMoney(buckets.overdue)}</span></div>
  `;
}

function renderStripePayments() {
  const container = byId("stripePaymentList");
  if (!container) return;
  const paid = (state.invoices || [])
    .filter((invoice) => invoice.status === "paid")
    .sort((a, b) => String(b.paid_at || "").localeCompare(String(a.paid_at || "")));
  if (!paid.length) {
    container.innerHTML = `<div class="empty">No Stripe payments confirmed yet.</div>`;
    return;
  }
  container.innerHTML = paid
    .slice(0, 5)
    .map((invoice) => {
      const payment = invoice.payment_details || {};
      const amount = payment.amount ?? invoice.amount ?? 0;
      const currency = payment.currency || invoice.currency || "USD";
      return `
        <div class="stripe-payment-row">
          <div>
            <b>${escapeHtml(invoice.invoice_number || "Invoice")}</b>
            <span>${escapeHtml(invoice.client_name || "Customer")} · ${payment.paid_at ? escapeHtml(new Date(payment.paid_at).toLocaleString()) : "paid"}</span>
            <small>${escapeHtml(payment.reference || "Stripe confirmation")} · status changed from sent to paid</small>
          </div>
          <strong>${fmtMoney(amount, currency)}</strong>
        </div>
      `;
    })
    .join("");
}

function compactMoney(value) {
  const amount = Number(value || 0);
  if (Math.abs(amount) >= 1000) return `$${Math.round(amount / 1000)}K`;
  return fmtMoney(amount).replace(".00", "");
}

function activityBadge(item = {}) {
  const source = String(item.source || "").toLowerCase();
  const kind = String(item.kind || item.message || "").toLowerCase();
  const message = String(item.message || "").toLowerCase();
  if (source.includes("command") || source.includes("user") || kind.includes("command")) return { label: "Did as you asked", className: "badge-purple" };
  if (kind.includes("payment") || message.includes("paid") || source.includes("stripe")) return { label: "Payment confirmed", className: "badge-green" };
  if (kind.includes("risk alert") || message.includes("partial payment")) return { label: "Risk alert", className: "badge-red" };
  if (source.includes("po parser") || kind.includes("file reasoning")) return { label: "PO parsed", className: "badge-blue" };
  if (source.includes("morning check-in") || kind.includes("morning check-in")) return { label: "Morning check-in", className: "badge-blue" };
  if (kind.includes("risk") || kind.includes("escalation") || message.includes("escalat") || message.includes("high-risk")) return { label: "Firm message drafted", className: "badge-red" };
  if (kind.includes("reminder") || message.includes("reminder") || message.includes("due")) return { label: "Reminder drafted", className: "badge-amber" };
  return { label: "Invoice prepared", className: "badge-blue" };
}

function renderActivity() {
  const list = byId("activityList");
  if (!state.activity.length) {
    list.innerHTML = `<div class="empty">No Lynn activity yet.</div>`;
    return;
  }
  list.innerHTML = state.activity
    .slice(0, 8)
    .map(
      (item) => {
        const provider = item.provider || item.reasoning_provider || "";
        const reasoning = item.reasoning || "I recorded this update so you can see what changed in AR.";
        const badge = activityBadge(item);
        const shortReason = lynnVoice(item.reasoning_summary || conciseText(reasoning, 240));
        const fullReason = conciseText(reasoning, 240);
        return `
        <article class="log-item">
          <div class="why-box">
            <div class="why-label">Why I did this</div>
            <div class="why-text">${escapeHtml(shortReason)}</div>
            ${
              reasoning.length > fullReason.length + 12
                ? `<details class="reasoning-details"><summary>Show more</summary><p>${escapeHtml(lynnVoice(reasoning))}</p></details>`
                : ""
            }
          </div>
          <div class="did-row">
            <span class="badge ${escapeHtml(badge.className)}">${escapeHtml(badge.label)}</span>
            <span>${escapeHtml(item.outcome || item.message || "No action taken.")}</span>
          </div>
          <div class="log-foot">
            <span class="log-time">${new Date(item.ts).toLocaleString()}</span>
            <span class="log-src">${escapeHtml(sourceLabel(item.source))}</span>
          </div>
        </article>
      `;
      }
    )
    .join("");
}

function renderDrafts() {
  const priorityRank = { P0: 0, P1: 1, P2: 2 };
  const queueItems = (state.drafts || []).filter((draft) =>
    ["awaiting_confirmation", "approved", "sent", "held", "rejected", "skipped", "paused"].includes(draft.status)
  );
  const groups = {
    pending: queueItems.filter((draft) => draft.status === "awaiting_confirmation"),
    approved: queueItems.filter((draft) => ["approved", "sent"].includes(draft.status)),
    rejected: queueItems.filter((draft) => ["held", "rejected", "skipped", "paused"].includes(draft.status)),
    all: queueItems,
  };
  const visible = (groups[queueFilter] || groups.pending)
    .sort((a, b) => (priorityRank[a.priority] ?? 3) - (priorityRank[b.priority] ?? 3));
  updateQueueTabs(groups);
  if (byId("queuePendingBadge")) {
    const pending = groups.pending.length;
    byId("queuePendingBadge").textContent = `${pending} item${pending === 1 ? "" : "s"}`;
    byId("queuePendingBadge").className = `pill ${pending ? "pill-clay" : "pill-mist"}`;
  }
  const queue = byId("draftQueue");
  if (!visible.length) {
    const emptyText =
      queueFilter === "pending"
        ? "Nothing is waiting for your OK."
        : queueFilter === "approved"
          ? "No sent items yet."
          : "No held items.";
    queue.innerHTML = `<div class="empty">${emptyText}</div>`;
    return;
  }
  queue.innerHTML = visible
    .map(
      (draft) => {
        const approvalLabels = {
          create_invoice: "Scheduled invoice",
          policy_change: "You asked me to",
          pause_emails: "You asked me to",
          discount_offer: "You asked me to",
          writeoff_review: "You asked me to",
        };
        const badge = draftQueueBadge(draft);
        const status = queueStatus(draft.status);
        const origin = draft.origin_label || originLabel(draft);
        const description = draftDescription(draft, badge);
        const actions = draftActionLabels(draft, badge);
        const invoiceNumber = draftInvoiceNumber(draft);
        return `
        <article class="ok-item draft-card priority-${escapeHtml(String(draft.priority || "P2").toLowerCase())} ${escapeHtml(badge.edgeClass)} status-${escapeHtml(draft.status || "pending")}">
          <div class="queue-header">
            <span class="pill ${escapeHtml(badge.className)}">${escapeHtml(approvalLabels[draft.approval_type] || badge.label)}</span>
            <span class="queue-source">${escapeHtml(origin)} · ${new Date(draft.created_at || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
          </div>
          <div class="queue-title">${escapeHtml(draft.subject)}</div>
          ${
            invoiceNumber
              ? `<div class="queue-meta-line">Invoice ${escapeHtml(invoiceNumber)}${draftPOText(draft) ? ` · ${escapeHtml(draftPOText(draft))}` : ""}</div>`
              : ""
          }
          <div class="queue-sub">${escapeHtml(description)}${status.label !== "Waiting" ? ` ${escapeHtml(status.label)}.` : ""}</div>
          ${renderDraftAssets(draft, invoiceNumber)}
          ${draft.body ? renderDraftBodyPreview(draft) : ""}
          ${
            draft.status === "awaiting_confirmation"
              ? `<div class="draft-actions">
                  <button class="btn-ok btn-approve" data-draft-send="${draft.id}">✓ ${escapeHtml(actions.send)}</button>
                  <button class="btn-tweak btn-edit" data-draft-edit="${draft.id}">${escapeHtml(actions.edit)}</button>
                  <button class="btn-skip btn-reject" data-draft-skip="${draft.id}">${escapeHtml(actions.hold)}</button>
                </div>
                ${editingDraftId === draft.id ? renderDraftEditor(draft) : ""}`
              : `<p class="status-footnote">${escapeHtml(status.historyText)}${draft.sent_at ? ` · ${new Date(draft.sent_at).toLocaleString()}` : ""}</p>`
          }
        </article>
      `;
      }
    )
    .join("");
}

function draftInvoiceNumber(draft) {
  if (draft.invoice_number) return draft.invoice_number;
  if (draft.attachment_name) return String(draft.attachment_name).replace(/\.pdf$/i, "");
  const subjectMatch = String(draft.subject || "").match(/INV-\d{4}-\d{3,}/i);
  if (subjectMatch) return subjectMatch[0].toUpperCase();
  const bodyMatch = String(draft.body || "").match(/INV-\d{4}-\d{3,}/i);
  if (bodyMatch) return bodyMatch[0].toUpperCase();
  if (draft.po_id) {
    const po = (state.po_pipeline || []).find((item) => item.id === draft.po_id);
    if (po?.proposed_invoice_number) return po.proposed_invoice_number;
  }
  if (draft.invoice_id) {
    const invoice = (state.invoices || []).find((item) => item.id === draft.invoice_id);
    if (invoice?.invoice_number) return invoice.invoice_number;
  }
  return "";
}

function draftPOText(draft) {
  const po = (state.po_pipeline || []).find((item) => item.id === draft.po_id);
  return draft.po_number || po?.po_number || "";
}

function isCreateInvoiceDraft(draft) {
  return draft.approval_type === "create_invoice";
}

function renderDraftBodyPreview(draft) {
  if (isInternalActionDraft(draft)) {
    return `
      <div class="email-preview action-preview">
        <div class="email-preview-label">Action details</div>
        <p>${escapeHtml(draft.body || "")}</p>
      </div>
    `;
  }
  const paymentLink = draftPaymentLink(draft);
  const body = appendPaymentLinkIfMissing(draft.body || "", paymentLink);
  return `
    <div class="email-preview">
      <div class="email-preview-label">Email draft</div>
      <p>${escapeHtml(body)}</p>
    </div>
  `;
}

function renderDraftAssets(draft, invoiceNumber = "") {
  const attachmentName = draft.attachment_name || (invoiceNumber ? `${invoiceNumber}.pdf` : "");
  const paymentLink = draftPaymentLink(draft);
  if (!attachmentName && !paymentLink) return "";
  return `
    <div class="draft-assets">
      ${
        attachmentName
          ? draft.attachment_url
            ? `<a class="asset-chip" href="${escapeAttr(draft.attachment_url)}" target="_blank" rel="noreferrer">Attachment · ${escapeHtml(attachmentName)}</a>`
            : `<span class="asset-chip muted">Attachment · ${escapeHtml(attachmentName)} pending</span>`
          : ""
      }
      ${
        paymentLink
          ? paymentLink.startsWith("http")
            ? `<a class="asset-chip" href="${escapeAttr(paymentLink)}" target="_blank" rel="noreferrer">Stripe link</a>`
            : `<span class="asset-chip muted">${escapeHtml(paymentLink)}</span>`
          : ""
      }
    </div>
  `;
}

function draftPaymentLink(draft) {
  if (draft.payment_link) return draft.payment_link;
  if (draft.invoice_id) {
    const invoice = (state.invoices || []).find((item) => item.id === draft.invoice_id);
    if (invoice?.stripe_payment_link) return invoice.stripe_payment_link;
  }
  if (draft.body && /stripe payment link pending|payment link pending/i.test(draft.body)) return "Stripe payment link pending";
  return draft.approval_type === "create_invoice" || draft.purpose ? "Stripe payment link pending" : "";
}

function appendPaymentLinkIfMissing(body, paymentLink) {
  if (!paymentLink) return body;
  if (paymentLink.startsWith("http")) {
    const replaced = body.replace(/Stripe payment link pending|Payment link pending/gi, paymentLink);
    if (replaced !== body) return replaced;
  }
  if (/(pay|buy)\.stripe\.com|stripe payment link pending|payment link pending/i.test(body)) return body;
  return `${body.trim()}\n\nPayment link: ${paymentLink}`;
}

function renderDraftEditor(draft) {
  const bodyLabel = isInternalActionDraft(draft) ? "Action details" : "Email draft";
  return `
    <div class="draft-editor" data-draft-editor="${escapeAttr(draft.id)}">
      <label>
        Subject
        <input data-draft-subject="${escapeAttr(draft.id)}" value="${escapeAttr(draft.subject || "")}" />
      </label>
      <label>
        ${escapeHtml(bodyLabel)}
        <textarea data-draft-body="${escapeAttr(draft.id)}">${escapeHtml(draft.body || "")}</textarea>
      </label>
      <div class="draft-actions">
        <button class="btn-ok" data-draft-save="${escapeAttr(draft.id)}">Save changes</button>
        <button class="btn-tweak" data-draft-cancel="${escapeAttr(draft.id)}">Cancel</button>
      </div>
    </div>
  `;
}

function isInternalActionDraft(draft) {
  return ["policy_change", "pause_emails", "discount_offer", "writeoff_review"].includes(draft.approval_type);
}

function draftDescription(draft, badge) {
  const client = draft.client_name || "this customer";
  const email = draft.client_email || "their finance contact";
  const subject = String(draft.subject || "").toLowerCase();
  if (badge.edgeClass.includes("command")) {
    return `As you asked, I've prepared this for ${client}. I'll send it to ${email} once you OK it.`;
  }
  if (badge.edgeClass.includes("urgent") || subject.includes("overdue") || subject.includes("escalation")) {
    return `${client} still needs attention. I've drafted a firmer message and will wait for your OK before I send it.`;
  }
  if (subject.includes("reminder") || draft.purpose === "pre-due reminder") {
    return `${client} has a payment coming up. I've drafted a gentle reminder with the payment link included.`;
  }
  if (draft.approval_type === "create_invoice" || draft.purpose === "invoice email") {
    return `I've prepared the invoice package for ${client}: PDF, Stripe payment link, and the email draft.`;
  }
  return `I've prepared this action for ${client} and I'm waiting for your OK.`;
}

function draftActionLabels(draft, badge) {
  const subject = String(draft.subject || "").toLowerCase();
  const urgent = badge.edgeClass.includes("urgent") || subject.includes("overdue") || subject.includes("escalation");
  if (urgent) {
    return { send: "Send the firm message", edit: "Soften the tone", hold: "Hold for now" };
  }
  return { send: "Looks good, send it", edit: "Make changes", hold: "Hold for now" };
}

function draftQueueBadge(draft) {
  const origin = originLabel(draft).toLowerCase();
  const purpose = String(draft.purpose || draft.approval_type || draft.subject || "").toLowerCase();
  if (origin.includes("request") || draft.command) return { label: "You asked me to", className: "pill-mauve", edgeClass: "command cmd" };
  if (draft.status === "sent" || draft.status === "approved" || purpose.includes("paid")) return { label: "Payment confirmed", className: "pill-sage", edgeClass: "paid" };
  if (purpose.includes("partial payment")) return { label: "Payment mismatch", className: "pill-clay", edgeClass: "urgent partial" };
  if (purpose.includes("escalation") || purpose.includes("high-risk") || purpose.includes("overdue")) return { label: "Urgent follow-up", className: "pill-clay", edgeClass: "urgent esc" };
  if (purpose.includes("reminder") || purpose.includes("due")) return { label: "Reminder ready", className: "pill-sand", edgeClass: "reminder" };
  return { label: "Scheduled invoice", className: "pill-slate", edgeClass: "invoice" };
}

function updateQueueTabs(groups) {
  const ids = {
    pending: "queuePendingBtn",
    approved: "queueApprovedBtn",
    rejected: "queueRejectedBtn",
    all: "queueAllBtn",
  };
  Object.entries(ids).forEach(([key, id]) => {
    const button = byId(id);
    if (!button) return;
    button.classList.toggle("active", queueFilter === key);
    const count = button.querySelector("span");
    if (count) count.textContent = groups[key]?.length || 0;
  });
}

function queueStatus(status = "") {
  if (status === "awaiting_confirmation") return { label: "Waiting", className: "pending", historyText: "Waiting for your OK" };
  if (status === "sent") return { label: "Sent", className: "approved", historyText: "Looks good, sent" };
  if (status === "approved") return { label: "Sent", className: "approved", historyText: "Looks good, sent" };
  if (status === "held" || status === "rejected" || status === "skipped") return { label: "Held", className: "rejected", historyText: "Held for now" };
  if (status === "paused") return { label: "Paused", className: "paused", historyText: "Held for now" };
  return { label: status || "Recorded", className: "neutral", historyText: "Recorded" };
}

function originLabel(draft) {
  if (draft.command) return "your request";
  const source = String(draft.source || "").toLowerCase();
  if (source.includes("daily") || source.includes("scheduled") || source.includes("demo run") || source.includes("manual run")) return "Morning check-in";
  if (source.includes("po") || source.includes("parser") || source.includes("pipeline")) return "PO Intake";
  if (source.includes("stripe")) return "Stripe confirmed payment";
  if (source.includes("risk")) return "Risk Monitor";
  return draft.source || "Lynn";
}

function renderSentEmails() {
  if (!byId("sentCount") || !byId("sentEmailList")) return;
  const sent = state.drafts.filter((draft) => draft.status === "sent");
  byId("sentCount").textContent = sent.length;
  const list = byId("sentEmailList");
  if (!sent.length) {
    list.innerHTML = `<div class="empty">No sent emails yet.</div>`;
    return;
  }
  list.innerHTML = sent
    .map(
      (draft) => `
        <article class="draft-card sent-card">
          <h4>${escapeHtml(draft.subject)}</h4>
          <small>${escapeHtml(draft.client_name)} · sent ${new Date(draft.sent_at).toLocaleString()}</small>
          <p>${escapeHtml(draft.body)}</p>
          ${
            draft.attachment_url
              ? `<p><b>Attachment:</b> <a href="${escapeAttr(draft.attachment_url)}" target="_blank" rel="noreferrer">${escapeHtml(draft.attachment_name || "Invoice PDF")}</a></p>`
              : ""
          }
        </article>
      `
    )
    .join("");
}

function renderCommandResults() {
  const list = byId("commandResults");
  const results = (state.command_results || []).filter((result) => !result.dismissed_at).slice(0, 1);
  if (!results.length) {
    list.innerHTML = "";
    return;
  }
  list.innerHTML = results
    .map(
      (result) => `
        <article class="command-card">
          <div class="command-card-head">
            <h4>${escapeHtml(result.command)}</h4>
            <button class="mini-close" data-command-close="${result.id}" aria-label="Close command result">×</button>
          </div>
          ${renderCommandBody(result)}
          <time>${new Date(result.ts).toLocaleString()}</time>
        </article>
      `
    )
    .join("");
}

function renderCommandBody(result) {
  if (isCashflowText(result.command) && result.intent !== "cash_flow_forecast") {
    return `
      <p>This local backend is still running the old command logic. Restart the AR Agent server, then cashflow questions will use the forecast format.</p>
    `;
  }
  if (result.intent === "cash_flow_forecast" && result.payload) {
    const payload = result.payload;
    return `
      ${renderProviderReasoning(result)}
      <div class="forecast-summary">
        <div>
          <span>Contractual</span>
          <strong>${fmtMoney(payload.contractual)}</strong>
        </div>
        <div>
          <span>Risk-adjusted</span>
          <strong>${fmtMoney(payload.risk_adjusted)}</strong>
        </div>
        <div>
          <span>At risk</span>
          <strong>${fmtMoney(payload.at_risk)}</strong>
        </div>
      </div>
      <div class="forecast-window">Next ${escapeHtml(payload.horizon_days)} days · through ${escapeHtml(payload.end_date)}</div>
      ${renderForecastList("Upcoming", payload.future || [], "forecast-upcoming")}
      ${renderForecastList("At-risk overdue", payload.overdue || [], "forecast-risk")}
    `;
  }
  if (result.intent === "agent_action" && result.payload?.drafts?.length) {
    const pendingCount = result.payload.drafts.filter((draft) => draft.status === "awaiting_confirmation").length;
    return `
      <p class="lynn-direct-answer">${escapeHtml(lynnVoice(result.message))}</p>
      <p class="status-footnote">I've moved ${escapeHtml(pendingCount)} item${pendingCount === 1 ? "" : "s"} to Waiting for your OK. I’ll wait before sending anything.</p>
    `;
  }
  if (result.intent === "invoice_due_query") {
    return `
      <p class="lynn-direct-answer">${escapeHtml(lynnVoice(result.message))}</p>
      ${result.payload?.items?.length ? renderCommandItems(result.payload.items) : ""}
    `;
  }
  if (["agent_action", "invoice_query"].includes(result.intent) && result.payload?.items) {
    return `
      <p>${escapeHtml(lynnVoice(result.message))}</p>
      ${renderProviderReasoning(result)}
      ${renderCommandItems(result.payload.items)}
      ${result.details?.length ? `<p>${result.details.map(escapeHtml).join("<br/>")}</p>` : ""}
    `;
  }
  if (result.intent === "payment_status" && result.payload) {
    return renderPaymentStatus(result);
  }
  if (result.intent === "client_risk_query" && result.payload?.items) {
    return `
      <p>${escapeHtml(lynnVoice(result.message))}</p>
      <div class="command-list">
        ${result.payload.items
          .map(
            (item) => `
              <div class="command-row-card">
                <div>
                  <b>${escapeHtml(item.client)}</b>
                  <span>${escapeHtml(item.invoices)} active invoices · ${fmtMoney(item.overdue)} overdue</span>
                </div>
                <strong>${escapeHtml(item.max_score)}/100</strong>
              </div>
            `
          )
          .join("")}
      </div>
    `;
  }
  if (result.intent === "agent_judgment" && result.payload) {
    return `
      <p><b>${escapeHtml(result.payload.client || "My suggestion")}</b>: ${escapeHtml(lynnVoice(result.message))}</p>
      <div class="activity-section reasoning-section command-reasoning">
        <span>Why I did this</span>
        <p>${escapeHtml(lynnVoice(result.payload.reasoning || result.details?.[0] || ""))}</p>
        ${
          result.payload.openai_error
            ? `<small class="provider-note">${escapeHtml(result.payload.openai_error)}</small>`
            : ""
        }
      </div>
      <div class="forecast-summary">
        <div><span>Active invoices</span><strong>${escapeHtml(result.payload.active_invoices || 0)}</strong></div>
        <div><span>Overdue total</span><strong>${fmtMoney(result.payload.overdue_total || 0)}</strong></div>
        <div><span>Max overdue</span><strong>${escapeHtml(result.payload.max_days_overdue || 0)}d</strong></div>
      </div>
    `;
  }
  if (result.intent === "agent_setting") {
    return `
      <p>${escapeHtml(lynnVoice(result.message))}</p>
      ${renderProviderReasoning(result)}
      ${result.details?.length ? `<p>${result.details.map(escapeHtml).join("<br/>")}</p>` : ""}
    `;
  }
  return `
    <p>${escapeHtml(lynnVoice(result.message))}</p>
    ${renderProviderReasoning(result)}
    ${result.details?.length ? `<p>${result.details.map(escapeHtml).join("<br/>")}</p>` : ""}
  `;
}

function renderPaymentStatus(result) {
  const payload = result.payload || {};
  const scope = payload.scope || "summary";
  const payments =
    scope === "today"
      ? payload.stripe_payments_today || []
      : scope === "month"
        ? payload.stripe_payments_this_month || []
        : payload.stripe_payments || [];
  return `
    <p class="lynn-direct-answer">${escapeHtml(lynnVoice(result.message))}</p>
    ${
      payments.length
        ? `<div class="payment-list">
            ${payments
              .slice(0, 6)
              .map(
                (payment) => `
                  <div class="payment-row">
                    <div>
                      <b>${escapeHtml(payment.client || "Customer")}</b>
                      <span>${escapeHtml(payment.invoice || "Invoice")} · ${escapeHtml(payment.provider || "Stripe")} ${escapeHtml(payment.method || "payment")}</span>
                    </div>
                    <strong>${fmtMoney(payment.amount || 0, payment.currency || "USD")}</strong>
                  </div>
                `
              )
              .join("")}
          </div>`
        : ""
    }
    ${
      scope === "summary"
        ? `<div class="forecast-summary">
            <div><span>Outstanding</span><strong>${fmtMoney(payload.outstanding || 0)}</strong></div>
            <div><span>Overdue</span><strong>${fmtMoney(payload.overdue || 0)}</strong></div>
            <div><span>Due this week</span><strong>${fmtMoney(payload.due_this_week || 0)}</strong></div>
          </div>`
        : ""
    }
    ${result.details?.length ? `<p class="status-footnote">${result.details.map((item) => escapeHtml(lynnVoice(item))).join("<br/>")}</p>` : ""}
  `;
}

function renderProviderReasoning(result) {
  const payload = result.payload || {};
  if (!payload.reasoning && !payload.openai_error && !payload.provider) return "";
  return `
    <div class="activity-section reasoning-section command-reasoning">
      <span>Why I did this</span>
      ${payload.reasoning ? `<p>${escapeHtml(lynnVoice(payload.reasoning))}</p>` : ""}
    </div>
  `;
}

function renderCommandItems(items) {
  if (!items.length) return `<div class="empty compact-empty">No matching invoices.</div>`;
  return `
    <div class="command-list">
      ${items
        .slice(0, 8)
        .map(
          (item) => `
            <div class="command-row-card">
              <div>
                <b>${escapeHtml(item.invoice || item.client || "Item")}</b>
                <span>${escapeHtml(item.client || item.status || "")} ${item.due_date ? `· due ${escapeHtml(item.due_date)}` : ""}</span>
              </div>
              <div>
                ${item.amount !== undefined ? `<strong>${fmtMoney(item.amount, item.currency || "USD")}</strong>` : ""}
                ${item.risk ? `<span>${escapeHtml(item.risk)}</span>` : item.status ? `<span>${escapeHtml(item.status)}</span>` : ""}
              </div>
            </div>
          `
        )
        .join("")}
    </div>
  `;
}

function renderCommandDrafts(drafts) {
  return `
    <div class="command-draft-list">
      ${drafts
        .slice(0, 6)
        .map((draft) => {
          return `
            <article class="command-draft-card">
              <div class="draft-topline">
                <span class="priority-pill ${escapeHtml(String(draft.priority || "P2").toLowerCase())}">
                  ${escapeHtml(draft.priority || "P2")} · ${escapeHtml(draft.priority_label || "Routine")}
                </span>
                <span class="draft-owner">${escapeHtml(draft.status === "awaiting_confirmation" ? "Waiting for your OK" : draft.status || "created")}</span>
              </div>
              <h5>${escapeHtml(draft.subject || draft.invoice || "Email draft")}</h5>
              <p>${escapeHtml(draft.client || "Customer")} · ${escapeHtml(draft.invoice || "")} · ${fmtMoney(draft.amount || 0, draft.currency || "USD")}</p>
            </article>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderForecastList(title, items, className) {
  if (!items.length) return "";
  return `
    <div class="forecast-list ${className}">
      <h5>${escapeHtml(title)}</h5>
      ${items
        .slice(0, 5)
        .map(
          (item) => `
            <div class="forecast-row">
              <div>
                <b>${escapeHtml(item.invoice)}</b>
                <span>${escapeHtml(item.client)} · ${escapeHtml(item.due_date)}</span>
              </div>
              <div>
                <strong>${fmtMoney(item.amount, item.currency || "USD")}</strong>
                <span>${Math.round(Number(item.probability || 0) * 100)}% expected</span>
              </div>
            </div>
          `
        )
        .join("")}
    </div>
  `;
}

function renderInvoices() {
  const poPipeline = state.po_pipeline || [];
  const invoiceablePOs = poPipeline.filter((po) => po.status === "ready_to_invoice" && !po.invoice_id);
  const waitingPipelinePOs = poPipeline.filter((po) => ["upcoming", "held", "needs_review", "blocked"].includes(po.status) && !po.invoice_id);
  const visiblePOs = poView === "pipeline" ? waitingPipelinePOs : invoiceablePOs;
  if (byId("poListTitle")) {
    byId("poListTitle").textContent = poView === "pipeline" ? "PO pipeline" : "Ready to act on";
  }
  if (byId("invoiceCount")) {
    const count = visiblePOs.length;
    const noun = poView === "pipeline" ? "PO" : "order";
    byId("invoiceCount").textContent = `(${count} ${noun}${count === 1 ? "" : "s"})`;
  }
  document.querySelectorAll("[data-po-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.poView === poView);
  });
  const list = byId("invoiceList");
  if (visiblePOs.length) {
    list.innerHTML = visiblePOs
      .map((po) => {
        const badge = poBadge(po);
        return `
          <div class="po-row-card ${escapeHtml(po.status || "")}">
            <div class="po-top">
              <span class="po-client">${escapeHtml(po.client_name || "Unknown")}</span>
              <span class="po-amt">${fmtMoney(po.amount, po.currency || "USD")}</span>
            </div>
            <div class="po-meta">
              <span>${escapeHtml(poPipelineMeta(po))}</span>
              <span class="badge ${escapeHtml(badge.className)}">${escapeHtml(badge.label)}</span>
            </div>
          </div>
        `;
      })
      .join("");
    return;
  }

  if (!visiblePOs.length) {
    list.innerHTML = `<div class="empty">${
      poView === "pipeline" ? "No orders are waiting for later." : "No orders are ready to invoice."
    }</div>`;
    return;
  }
}

function poBadge(po) {
  if (po.status === "ready_to_invoice") return { label: "Invoice ready", className: "badge-blue" };
  if (po.status === "needs_review") return { label: "Needs info", className: "badge-amber" };
  if (po.status === "held") return { label: "Held", className: "badge-amber" };
  if (po.status === "upcoming") return { label: "Upcoming", className: "badge-gray" };
  if (po.status === "blocked") return { label: "Needs info", className: "badge-red" };
  return { label: labelStatus(po.status || "upcoming"), className: "badge-gray" };
}

function poPipelineMeta(po) {
  const parts = [po.po_number || "PO"];
  if (po.proposed_invoice_number) parts.push(po.proposed_invoice_number);
  parts.push(po.payment_terms || "Terms missing");
  if (po.shipment_date) parts.push(`shipped ${po.shipment_date}`);
  if (poView === "pipeline") {
    if (po.status === "held") parts.push("held until next check-in");
    else if (po.status === "upcoming") parts.push("upcoming");
    else parts.push("not ready");
  }
  return parts.join(" · ");
}

function invoiceBadge(invoice) {
  if (invoice.status === "paid") return { label: "Paid", className: "badge-green" };
  if (invoice.status === "high_risk" || invoiceDaysOverdue(invoice) >= 30) return { label: "Needs follow-up", className: "badge-red" };
  if (invoice.status === "overdue" || invoiceDaysOverdue(invoice) > 0) return { label: "Needs follow-up", className: "badge-amber" };
  if (invoice.status === "due_soon") return { label: "Due soon", className: "badge-amber" };
  return { label: "Invoice ready", className: "badge-blue" };
}

function openInvoice(invoiceId) {
  selectedInvoice = state.invoices.find((invoice) => invoice.id === invoiceId);
  if (!selectedInvoice) return;
  renderModal();
  byId("invoiceModal").showModal();
}

function renderModal() {
  if (!selectedInvoice) return;
  byId("modalInvoiceNumber").innerHTML = `
    <span class="brand-mark small"><span class="bike-wheel left"></span><span class="bike-frame"></span><span class="bike-wheel right"></span></span>
    <span>HelloBike · ${escapeHtml(selectedInvoice.invoice_number)}</span>
  `;
  byId("modalClient").textContent = `${selectedInvoice.client_name} · ${selectedInvoice.client_email || "no email"}`;
  const paymentLink = selectedInvoice.stripe_payment_link
    ? `<a href="${escapeAttr(selectedInvoice.stripe_payment_link)}" target="_blank" rel="noreferrer">${escapeHtml(selectedInvoice.stripe_payment_link)}</a>`
    : "<strong>Not created yet</strong>";
  const pdfLink = selectedInvoice.pdf_path
    ? `<a href="${escapeAttr(selectedInvoice.pdf_path)}" target="_blank" rel="noreferrer">Open PDF</a>`
    : "<strong>Not generated yet</strong>";
  const risk = selectedInvoice.risk_profile;
  const riskSources =
    risk?.sources?.length
      ? `<div class="field internal-field"><span>Risk Sources</span>${risk.sources
          .slice(0, 3)
          .map((source) =>
            source.url
              ? `<a href="${escapeAttr(source.url)}" target="_blank" rel="noreferrer">${escapeHtml(source.title || source.url)}</a>`
              : `<strong>${escapeHtml(source.title || "Source")}</strong>`
          )
          .join("<br/>")}</div>`
      : "";
  const riskBlock = risk
    ? `${field("Risk Score", `${risk.score}/100 · ${risk.level}`, "internal")}
       ${field("Risk Notes", risk.summary || "No notes", "internal")}
       ${field("Recommended Action", risk.recommended_action || "Standard follow-up", "internal")}
       ${field("Suggested Tone", risk.recommended_tone || "Standard", "internal")}
       ${risk.tone_options?.length ? field("Tone Options", risk.tone_options.join(" / "), "internal") : ""}
       ${risk.signals?.length ? field("Signals", risk.signals.join(" · "), "internal") : ""}
       ${riskSources}`
    : `${field("Risk Score", "Not checked yet", "internal")}`;
  const payment =
    selectedInvoice.payment_details ||
    (selectedInvoice.status === "paid"
      ? {
          provider: "Stripe",
          amount: selectedInvoice.amount,
          currency: selectedInvoice.currency,
          method: "card",
          paid_at: selectedInvoice.paid_at,
          reference: `stripe_record_${selectedInvoice.invoice_number}`,
          settlement_status: "succeeded",
        }
      : null);
  const paymentBlock =
    selectedInvoice.status === "paid" && payment
      ? `<section class="modal-section stripe-visible">
          <div class="section-label">Stripe payment record</div>
          ${field("Paid Amount", fmtMoney(payment.amount || selectedInvoice.amount, payment.currency || selectedInvoice.currency), "stripe")}
          ${field("Currency Settled", (payment.currency || selectedInvoice.currency || "USD").toUpperCase(), "stripe")}
          ${field("Payment Method", payment.method || "card", "stripe")}
          ${field("Paid At", payment.paid_at ? new Date(payment.paid_at).toLocaleString() : "Not available", "stripe")}
          ${field("Stripe Reference", payment.reference || "simulated_payment", "stripe")}
        </section>`
      : "";
  byId("modalBody").innerHTML = `
    <section id="invoiceDetailsSection" class="modal-section customer-visible">
      <div class="section-label">Included in invoice PDF</div>
      ${field("Seller", "HelloBike Manufacturing Co.")}
      ${field("Amount", fmtMoney(selectedInvoice.amount, selectedInvoice.currency))}
      ${field("Status", labelStatus(selectedInvoice.status))}
      ${field("Email Status", selectedInvoice.last_email_sent_at ? `Sent ${new Date(selectedInvoice.last_email_sent_at).toLocaleString()}` : "Not sent yet")}
      ${field("PO Number", selectedInvoice.po_number)}
      ${field("Payment Terms", selectedInvoice.payment_terms)}
      ${field("Invoice Date", selectedInvoice.invoice_date)}
      ${field("Due Date", selectedInvoice.due_date)}
      <div class="field"><span>Stripe Payment Link</span>${paymentLink}</div>
      <div class="field"><span>Invoice PDF</span>${pdfLink}</div>
      ${field("Notes", selectedInvoice.notes || "None")}
    </section>
    ${paymentBlock}
    <section id="agentReasoningSection" class="modal-section internal-only">
      <div class="section-label">Internal AI risk intelligence · not sent to customer</div>
      ${riskBlock}
    </section>
  `;
}

function field(label, value, kind = "customer") {
  const klass = kind === "internal" ? "internal-field" : kind === "stripe" ? "stripe-field" : "";
  return `<div class="field ${klass}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function labelStatus(status) {
  return {
    open: "Open",
    due_soon: "Due Soon",
    overdue: "Overdue",
    high_risk: "High Risk",
    paid: "Paid",
    ready_to_invoice: "Ready to Invoice",
    upcoming: "Upcoming",
    needs_review: "Needs Review",
    blocked: "Blocked",
    invoiced: "Invoiced",
    partial_paid: "Partial Paid",
  }[status] || status;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

async function withBusy(button, label, fn) {
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try {
    await fn();
  } catch (err) {
    toast(err.message);
  } finally {
    button.disabled = false;
    button.textContent = previous;
    if (state) render();
  }
}

byId("poFile").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    state = await api("/api/upload", { method: "POST", body });
    render();
    const summary = state.uploadSummary;
    if (summary?.duplicate_count) {
      toast(`${summary.added_count} new POs added. ${summary.duplicate_count} duplicates skipped.`);
    } else if (summary) {
      toast(`${summary.added_count} new POs added.`);
    } else {
      toast("PO tracker parsed.");
    }
  } catch (err) {
    toast(err.message);
  } finally {
    event.target.value = "";
  }
});

byId("commandBtn").addEventListener("click", (event) =>
  withBusy(event.currentTarget, "Asking", async () => {
    const command = byId("commandInput").value.trim();
    if (!command) {
      toast("Type a command first.");
      return;
    }
    const localCashflow = isCashflowText(command);
    try {
      const payload = await api("/api/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command }),
      });
      state = payload;
      if (payload.commandResult?.intent === "agent_action" && payload.commandResult?.payload?.drafts?.length) {
        queueFilter = "pending";
      }
      if (localCashflow && payload.commandResult?.intent !== "cash_flow_forecast") {
        state.command_results = [
          localCashflowResult(command),
          ...(state.command_results || []).filter((result) => result.command !== command),
        ];
      }
      render();
    } catch (err) {
      if (localCashflow) {
        state.command_results = [localCashflowResult(command), ...(state.command_results || [])];
        render();
        return;
      }
      state.command_results = [
        {
          id: `local-error-${Date.now()}`,
          command,
          intent: "safe_status_check",
          message: "Command could not complete. Please retry after checking the local server.",
          details: [err.message],
          payload: {},
          ts: new Date().toISOString(),
        },
        ...(state.command_results || []),
      ];
      render();
    }
  })
);

byId("autoRunToggle").addEventListener("click", async (event) => {
  autoRun = !autoRun;
  event.currentTarget.classList.toggle("active", autoRun);
  event.currentTarget.setAttribute("aria-pressed", String(autoRun));
  if (!state.scheduler) state.scheduler = {};
  state.scheduler.enabled = autoRun;
  renderScheduler();
  try {
    state = await api("/api/scheduler", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: autoRun, time: state.scheduler.time || "09:00" }),
    });
    render();
  } catch (err) {
    toast(autoRun ? "Daily auto-run enabled locally." : "Daily auto-run paused locally.");
  }
});

byId("invoiceList").addEventListener("click", (event) => {
  const row = event.target.closest("[data-invoice-id]");
  if (row) openInvoice(row.dataset.invoiceId);
});

document.querySelectorAll("[data-po-view]").forEach((button) => {
  button.addEventListener("click", () => {
    poView = button.dataset.poView || "ready";
    renderInvoices();
  });
});

document.querySelectorAll("[data-queue-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    queueFilter = button.dataset.queueFilter || "pending";
    editingDraftId = null;
    renderDrafts();
  });
});

byId("draftQueue").addEventListener("click", async (event) => {
  const cancel = event.target.closest("[data-draft-cancel]");
  const save = event.target.closest("[data-draft-save]");
  const send = event.target.closest("[data-draft-send]");
  const skip = event.target.closest("[data-draft-skip]");
  const edit = event.target.closest("[data-draft-edit]");
  if (edit) {
    editingDraftId = edit.dataset.draftEdit;
    renderDrafts();
    return;
  }
  if (cancel) {
    editingDraftId = null;
    renderDrafts();
    return;
  }
  if (save) {
    const draftId = save.dataset.draftSave;
    const subject = byId("draftQueue").querySelector(`[data-draft-subject="${CSS.escape(draftId)}"]`)?.value || "";
    const body = byId("draftQueue").querySelector(`[data-draft-body="${CSS.escape(draftId)}"]`)?.value || "";
    try {
      state = await api(`/api/drafts/${draftId}/edit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subject, body }),
      });
      editingDraftId = null;
      render();
      toast("Changes saved.");
    } catch (err) {
      toast(err.message);
    }
    return;
  }
  const draftId = send?.dataset.draftSend || skip?.dataset.draftSkip;
  if (!draftId) return;
  try {
    const payload = await api(`/api/drafts/${draftId}/${send ? "send" : "skip"}`, { method: "POST" });
    state = payload;
    editingDraftId = null;
    render();
    toast(payload.warning || (send ? "Looks good, sent." : "Held for now."));
  } catch (err) {
    toast(err.message);
  }
});

byId("commandResults").addEventListener("click", async (event) => {
  const send = event.target.closest("[data-draft-send]");
  const skip = event.target.closest("[data-draft-skip]");
  const draftId = send?.dataset.draftSend || skip?.dataset.draftSkip;
  if (draftId) {
    try {
      const payload = await api(`/api/drafts/${draftId}/${send ? "send" : "skip"}`, { method: "POST" });
      state = payload;
      render();
      toast(payload.warning || (send ? "Looks good, sent." : "Held for now."));
    } catch (err) {
      toast(err.message);
    }
    return;
  }
  const close = event.target.closest("[data-command-close]");
  if (!close) return;
  const commandId = close.dataset.commandClose;
  const previousResults = [...(state.command_results || [])];
  try {
    state.command_results = previousResults.filter((result) => result.id !== commandId);
    renderCommandResults();
    if (commandId.startsWith("local-")) return;
    await api(`/api/commands/${commandId}/dismiss`, { method: "POST" });
  } catch (err) {
    toast(err.message);
  }
});

byId("closeModal").addEventListener("click", () => byId("invoiceModal").close());

function scrollModalTo(sectionId) {
  const body = byId("modalBody");
  const section = byId(sectionId);
  if (!body || !section) return;
  body.scrollTo({ top: section.offsetTop - body.offsetTop - 8, behavior: "smooth" });
  section.classList.add("section-focus");
  window.setTimeout(() => section.classList.remove("section-focus"), 1200);
}

byId("viewDetailsBtn").addEventListener("click", () => scrollModalTo("invoiceDetailsSection"));

byId("viewReasoningBtn").addEventListener("click", () => scrollModalTo("agentReasoningSection"));

byId("createPaymentLinkBtn").addEventListener("click", (event) =>
  withBusy(event.currentTarget, "Creating", async () => {
    state = await api(`/api/invoices/${selectedInvoice.id}/payment-link`, { method: "POST" });
    selectedInvoice = state.invoices.find((invoice) => invoice.id === selectedInvoice.id) || selectedInvoice;
    render();
    renderModal();
    toast("Stripe payment link created.");
  })
);

byId("simulatePaidBtn").addEventListener("click", (event) =>
  withBusy(event.currentTarget, "Updating", async () => {
    state = await api(`/api/invoices/${selectedInvoice.id}/simulate-paid`, { method: "POST" });
    render();
    toast("Stripe confirmed payment.");
  })
);

refresh().catch((err) => toast(err.message));
