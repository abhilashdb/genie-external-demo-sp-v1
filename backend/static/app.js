// Genie Space SP Demo — frontend
// Single-page app: login view <-> chat view. No build step, no deps.

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  view: "login", // "login" | "chat"
  tab: "chat", // "chat" | "arch"
  user: null, // { username, dealership, role, sp_label }
  conversationId: null,
  messages: [], // { role: "user"|"assistant"|"system", text, sql?, rows?, columns?, error? }
  conversations: [], // [{ genie_conv_id, title, last_active_at, ... }]
  historyLoading: false,
  flowEvents: [], // newest first
  eventSource: null,
  reconnectAttempted: false,
  mermaidInitialized: false,
  mermaidRendered: false,
};

// Color classes per flow step (CSS has matching .step-<name> rules)
const STEP_COLORS = {
  login: "blue",
  sp_resolve: "purple",
  token_exchange: "amber",
  genie_call: "green",
  genie_rate_limit: "orange",
  genie_retry: "orange",
  genie_sql: "teal",
  sql_execute: "slate",
  rls_applied: "rose",
  response: "emerald",
  error: "red",
};

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);

function showView(view) {
  state.view = view;
  $("#view-login").hidden = view !== "login";
  $("#view-chat").hidden = view !== "chat";
  // Topbar is auth-gated; tab 1 label reflects auth state.
  const authed = view === "chat";
  $("#topbar").hidden = !authed;
  $("#tab-main-btn").textContent = authed ? "Chat" : "Sign in";
}

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderNewlines(text) {
  // Minimal markdown-ish: escape, then preserve blank-line paragraphs + newlines.
  const esc = escapeHtml(text || "");
  const paragraphs = esc.split(/\n{2,}/);
  return paragraphs.map((p) => `<p>${p.replace(/\n/g, "<br/>")}</p>`).join("");
}

function relativeTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Math.max(0, Date.now() - then);
  const s = Math.floor(diff / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function api(path, opts = {}) {
  const resp = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (resp.status === 401 && state.view !== "login") {
    // Session expired mid-session — force back to login.
    teardownChat();
    showView("login");
    showLoginError("Session expired. Please sign in again.");
    throw new Error("unauthorized");
  }
  return resp;
}

function showLoginError(msg) {
  const el = $("#login-error");
  el.textContent = msg;
  el.hidden = !msg;
}

// ---------------------------------------------------------------------------
// Login / logout
// ---------------------------------------------------------------------------
async function tryRestoreSession() {
  try {
    const resp = await fetch("/api/me", { credentials: "same-origin" });
    if (resp.ok) {
      const user = await resp.json();
      enterChat(user);
      return;
    }
  } catch (_) {}
  showView("login");
}

async function handleLogin(ev) {
  ev.preventDefault();
  showLoginError("");
  const username = $("#login-username").value.trim();
  const password = $("#login-password").value;
  const submitBtn = $("#login-submit");
  submitBtn.disabled = true;
  submitBtn.textContent = "Signing in...";
  try {
    const resp = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: "login failed" }));
      showLoginError(err.detail || "Login failed");
      return;
    }
    const user = await resp.json();
    enterChat(user);
  } catch (e) {
    if (e.message !== "unauthorized") showLoginError("Network error. Try again.");
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Sign in";
  }
}

async function handleLogout() {
  try {
    await api("/api/logout", { method: "POST" });
  } catch (_) {}
  teardownChat();
  showView("login");
  $("#login-username").value = "";
  $("#login-password").value = "";
  showLoginError("");
}

// ---------------------------------------------------------------------------
// Chat view lifecycle
// ---------------------------------------------------------------------------
function enterChat(user) {
  state.user = user;
  state.conversationId = null;
  state.messages = [];
  state.conversations = [];
  state.flowEvents = [];
  state.reconnectAttempted = false;
  renderTopbar();
  renderMessages();
  renderHistory();
  renderFlow();
  showView("chat");
  switchTab("main");
  openEventStream();
  refreshConversations();
  $("#chat-text").focus();
}

function teardownChat() {
  if (state.eventSource) {
    try {
      state.eventSource.close();
    } catch (_) {}
    state.eventSource = null;
  }
  state.user = null;
  state.conversationId = null;
  state.messages = [];
  state.conversations = [];
  state.flowEvents = [];
}

function renderTopbar() {
  const u = state.user;
  if (!u) return;
  $("#user-greeting").textContent = `Logged in as ${u.username} • ${u.dealership} • ${u.role}`;
  $("#sp-badge").textContent = `Connected via SP: ${u.sp_label}-sp`;
}

// ---------------------------------------------------------------------------
// History sidebar
// ---------------------------------------------------------------------------
async function refreshConversations() {
  try {
    const resp = await api("/api/conversations");
    if (!resp.ok) return;
    const data = await resp.json();
    state.conversations = data.conversations || [];
    renderHistory();
  } catch (_) {}
}

function renderHistory() {
  const container = $("#history-list");
  if (!container) return;
  if (state.conversations.length === 0) {
    container.innerHTML = `<div class="empty-state-sm">No past conversations yet.</div>`;
    return;
  }
  container.innerHTML = state.conversations
    .map((c) => {
      const active = c.genie_conv_id === state.conversationId ? " active" : "";
      const title = escapeHtml(c.title || "(untitled)");
      return `
        <button type="button" class="history-item${active}" data-conv-id="${escapeHtml(
          c.genie_conv_id,
        )}" title="${escapeHtml(c.last_active_at || "")}">
          <div class="history-title">${title}</div>
          <div class="history-meta">${escapeHtml(relativeTime(c.last_active_at))}</div>
        </button>`;
    })
    .join("");
  container.querySelectorAll(".history-item").forEach((el) => {
    el.addEventListener("click", () => handleOpenConversation(el.dataset.convId));
  });
}

async function handleOpenConversation(convId) {
  if (!convId || state.historyLoading) return;
  if (convId === state.conversationId) return;
  state.historyLoading = true;
  state.conversationId = convId;
  state.messages = [{ role: "system", text: "Loading conversation..." }];
  renderMessages();
  renderHistory();
  try {
    const resp = await api(`/api/conversations/${encodeURIComponent(convId)}/messages`);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }));
      state.messages = [{ role: "system", text: `Error: ${err.detail || "failed to load"}` }];
      renderMessages();
      return;
    }
    const data = await resp.json();
    state.messages = (data.messages || []).map((m) => ({
      role: m.role,
      text: m.text,
      sql: m.sql || null,
      rows: m.rows || null,
      columns: m.columns || null,
      resultExpired: !!m.result_expired,
    }));
    renderMessages();
  } catch (e) {
    if (e.message !== "unauthorized") {
      state.messages = [{ role: "system", text: "Network error loading conversation." }];
      renderMessages();
    }
  } finally {
    state.historyLoading = false;
  }
}

function handleNewChat() {
  state.conversationId = null;
  state.messages = [];
  renderMessages();
  renderHistory();
  $("#chat-text").focus();
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------
function pushMessage(msg) {
  state.messages.push(msg);
  renderMessages();
}

function renderMessages() {
  const container = $("#messages");
  if (state.messages.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <h3>Ask Genie about your dealership</h3>
        <p>Try: "What were my top selling vehicles last quarter?"</p>
      </div>`;
    return;
  }
  container.innerHTML = state.messages.map(renderBubble).join("");
  container.scrollTop = container.scrollHeight;
}

function renderBubble(msg) {
  if (msg.role === "system") {
    return `<div class="bubble bubble-system"><div class="bubble-body">${escapeHtml(msg.text)}</div></div>`;
  }
  if (msg.role === "user") {
    return `<div class="bubble bubble-user"><div class="bubble-body">${renderNewlines(msg.text)}</div></div>`;
  }
  // assistant
  if (msg.loading) {
    return `<div class="bubble bubble-assistant"><div class="bubble-body"><span class="loader"></span> Asking Genie...</div></div>`;
  }
  let body = "";
  if (msg.text) body += `<div class="answer-text">${renderNewlines(msg.text)}</div>`;
  if (msg.sql) {
    body += `
      <details class="sql-block">
        <summary>Show SQL</summary>
        <pre><code>${escapeHtml(msg.sql)}</code></pre>
      </details>`;
  }
  if (msg.rows && msg.columns && msg.rows.length > 0) {
    body += renderTable(msg.columns, msg.rows);
  } else if (msg.rows && msg.rows.length === 0) {
    body += `<div class="rows-empty">(query returned 0 rows)</div>`;
  } else if (msg.resultExpired) {
    body += `<div class="rows-expired">Results no longer available (expired in Genie). Re-run the SQL to refresh.</div>`;
  }
  return `<div class="bubble bubble-assistant"><div class="bubble-body">${body}</div></div>`;
}

function renderTable(columns, rows) {
  const head = columns
    .map((c) => `<th>${escapeHtml(c.name)}<span class="col-type">${escapeHtml(c.type || "")}</span></th>`)
    .join("");
  const shown = rows.slice(0, 50);
  const body = shown
    .map((row) => {
      const cells = columns
        .map((c) => {
          const v = row[c.name];
          const display = v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
          return `<td>${escapeHtml(display)}</td>`;
        })
        .join("");
      return `<tr>${cells}</tr>`;
    })
    .join("");
  const footer =
    rows.length > 50 ? `<div class="table-footnote">Showing 50 of ${rows.length} rows.</div>` : "";
  return `<div class="result-table-wrap"><table class="result-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>${footer}</div>`;
}

async function handleChatSubmit(ev) {
  ev.preventDefault();
  const input = $("#chat-text");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";

  pushMessage({ role: "user", text });
  const placeholder = { role: "assistant", loading: true };
  pushMessage(placeholder);

  try {
    const resp = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message: text, conversation_id: state.conversationId }),
    });
    // Remove placeholder regardless
    state.messages = state.messages.filter((m) => m !== placeholder);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }));
      pushMessage({ role: "system", text: `Error: ${err.detail || "request failed"}` });
      return;
    }
    const data = await resp.json();
    const isNewThread = !state.conversationId && !!data.conversation_id;
    if (data.conversation_id) state.conversationId = data.conversation_id;
    pushMessage({
      role: "assistant",
      text: data.answer_text,
      sql: data.sql || null,
      rows: data.rows || null,
      columns: data.columns || null,
    });
    // Update the sidebar — new thread inserts a row; follow-ups bump recency.
    refreshConversations();
    if (isNewThread) renderHistory();
  } catch (e) {
    state.messages = state.messages.filter((m) => m !== placeholder);
    if (e.message !== "unauthorized") {
      pushMessage({ role: "system", text: "Network error contacting backend." });
    }
    renderMessages();
  }
}

// ---------------------------------------------------------------------------
// Flow events (SSE)
// ---------------------------------------------------------------------------
function openEventStream() {
  if (state.eventSource) {
    try { state.eventSource.close(); } catch (_) {}
  }
  const es = new EventSource("/api/events/stream", { withCredentials: true });
  state.eventSource = es;

  es.addEventListener("flow", (e) => {
    try {
      const payload = JSON.parse(e.data);
      state.flowEvents.unshift(payload);
      renderFlow();
    } catch (err) {
      // swallow parse errors
    }
  });

  es.addEventListener("error", () => {
    // Try one reconnect after 2s, then give up (browser will also auto-retry EventSource).
    if (state.reconnectAttempted) return;
    state.reconnectAttempted = true;
    setTimeout(() => {
      if (state.view === "chat") openEventStream();
    }, 2000);
  });
}

function renderFlow() {
  const container = $("#flow-events");
  if (state.flowEvents.length === 0) {
    container.innerHTML = `<div class="empty-state-sm">No flow events yet. Send a message to see the backend trace.</div>`;
    return;
  }
  container.innerHTML = state.flowEvents.map(renderFlowCard).join("");
}

function renderFlowCard(ev) {
  const color = STEP_COLORS[ev.step] || "slate";
  const status = ev.status || "ok";
  const payloadJson = ev.payload ? JSON.stringify(ev.payload, null, 2) : "";
  const detail = ev.detail ? `<div class="flow-detail">${escapeHtml(ev.detail)}</div>` : "";
  const payloadBlock = payloadJson
    ? `<details class="flow-payload"><summary>payload</summary><pre>${escapeHtml(payloadJson)}</pre></details>`
    : "";
  return `
    <article class="flow-card step-${color} status-${status}">
      <div class="flow-card-head">
        <span class="step-pill step-pill-${color}">${escapeHtml(ev.step || "")}</span>
        <time class="flow-ts" data-ts="${escapeHtml(ev.ts || "")}">${escapeHtml(relativeTime(ev.ts))}</time>
      </div>
      <div class="flow-title">${escapeHtml(ev.title || "")}</div>
      ${detail}
      ${payloadBlock}
    </article>`;
}

function tickTimestamps() {
  document.querySelectorAll(".flow-ts").forEach((el) => {
    const ts = el.getAttribute("data-ts");
    el.textContent = relativeTime(ts);
  });
}

// ---------------------------------------------------------------------------
// Wire up
// ---------------------------------------------------------------------------
function attachEventListeners() {
  $("#login-form").addEventListener("submit", handleLogin);
  $("#logout-btn").addEventListener("click", handleLogout);
  $("#chat-form").addEventListener("submit", handleChatSubmit);
  $("#flow-clear").addEventListener("click", () => {
    state.flowEvents = [];
    renderFlow();
  });
  $("#sim-arm-btn").addEventListener("click", handleSimArm);
  $("#stress-btn").addEventListener("click", handleStressFire);
  $("#new-chat-btn").addEventListener("click", handleNewChat);
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
}

async function handleStressFire() {
  const count = parseInt($("#stress-count").value, 10) || 10;
  const btn = $("#stress-btn");
  const status = $("#stress-status");
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = `Firing ${count}...`;
  status.textContent = "";
  try {
    const resp = await api("/api/dev/stress-genie", {
      method: "POST",
      body: JSON.stringify({ count, question: "count sales" }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }));
      status.textContent = `failed: ${err.detail}`;
      return;
    }
    const data = await resp.json();
    status.textContent = `${data.ok} ok / ${data.failed} failed`;
  } catch (_) {
    status.textContent = "network error";
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

async function handleSimArm() {
  const count = parseInt($("#sim-count").value, 10) || 0;
  const btn = $("#sim-arm-btn");
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "Arming...";
  try {
    const resp = await api("/api/dev/simulate-rate-limit", {
      method: "POST",
      body: JSON.stringify({ count, status: 429 }),
    });
    if (!resp.ok) {
      $("#sim-status").textContent = "arm failed";
      return;
    }
    const data = await resp.json();
    $("#sim-status").textContent =
      data.armed > 0
        ? `armed: next ${data.armed} will 429`
        : "simulation off";
  } catch (_) {
    $("#sim-status").textContent = "arm failed";
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

// ---------------------------------------------------------------------------
// Tabs (Chat | Architecture)
// ---------------------------------------------------------------------------
function switchTab(name) {
  if (name !== "main" && name !== "arch") return;
  state.tab = name;
  document.querySelectorAll(".tab").forEach((b) => {
    const active = b.dataset.tab === name;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
  $("#tab-main").hidden = name !== "main";
  $("#tab-arch").hidden = name !== "arch";
  if (name === "arch") renderArchDiagrams();
}

async function renderArchDiagrams() {
  if (state.mermaidRendered) return;
  if (!window.mermaid) return; // script not yet loaded
  if (!state.mermaidInitialized) {
    window.mermaid.initialize({
      startOnLoad: false,
      theme: "default",
      securityLevel: "loose",
      flowchart: { htmlLabels: true, curve: "basis" },
      sequence: { mirrorActors: false, showSequenceNumbers: false },
    });
    state.mermaidInitialized = true;
  }
  try {
    await window.mermaid.run({ querySelector: "#tab-arch .mermaid" });
    state.mermaidRendered = true;
  } catch (e) {
    console.error("mermaid render failed", e);
    // If a diagram blew up, show the error in-place so it's not invisible.
    document.querySelectorAll("#tab-arch .mermaid").forEach((el) => {
      if (!el.querySelector("svg")) {
        el.innerHTML = `<div class="diagram-error">Diagram failed to render. Open browser console for details.</div>`;
      }
    });
  }
}

function init() {
  attachEventListeners();
  setInterval(tickTimestamps, 5000);
  tryRestoreSession();
}

init();
