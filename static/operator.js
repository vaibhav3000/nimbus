/* Nimbus operator console (FR-22 view). Vanilla JS over the
   documented API only. Pause and cancel record the operator intent and
   publish a stop lease to robots via the cloud orchestrator (FR-22). */

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Scoped session storage token helper (P4 A3).
// Security trade-off: sessionStorage scopes token lifetime strictly to the active browser
// tab session. Tokens are never persisted across browser restarts or written to disk,
// preventing token leakage while allowing refresh without re-prompting.
function getOperatorToken() {
  try {
    return sessionStorage.getItem("nimbus_operator_token") || window.__NIMBUS_OPERATOR_TOKEN || "";
  } catch (e) {
    return window.__NIMBUS_OPERATOR_TOKEN || "";
  }
}

function setOperatorToken(tok) {
  const clean = String(tok || "").trim();
  window.__NIMBUS_OPERATOR_TOKEN = clean;
  try {
    if (clean) sessionStorage.setItem("nimbus_operator_token", clean);
    else sessionStorage.removeItem("nimbus_operator_token");
  } catch (e) {}
}

function clearOperatorToken() {
  setOperatorToken("");
  showAuthError("Logged out. Please enter a valid operator token.");
}

function showAuthError(msg) {
  let bar = $("operator-auth-banner");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "operator-auth-banner";
    bar.className = "notice";
    bar.style.borderColor = "var(--status-urgent)";
    bar.style.backgroundColor = "rgba(196, 85, 74, 0.15)";
    bar.style.margin = "12px 24px";
    bar.style.padding = "10px 14px";
    bar.style.border = "1px solid #c4554a";
    bar.style.borderRadius = "4px";
    const header = document.querySelector("header");
    if (header && header.parentNode) {
      header.parentNode.insertBefore(bar, header.nextSibling);
    } else {
      document.body.prepend(bar);
    }
  }
  bar.innerHTML = `<strong>Operator Authentication Required (401/403):</strong> ${esc(msg)}
    <div style="margin-top: 8px;">
      <input type="password" id="operator-token-input" placeholder="Enter Operator Bearer token…" style="width: 260px; padding: 4px 8px; font-size: 13px;">
      <button id="save-operator-token-btn" style="padding: 4px 10px; font-size: 13px;">Set Token</button>
      <button id="clear-operator-token-btn" class="secondary" style="padding: 4px 10px; font-size: 13px;">Clear</button>
    </div>`;
  const saveBtn = $("save-operator-token-btn");
  if (saveBtn) {
    saveBtn.onclick = () => {
      const val = ($("operator-token-input") || {}).value;
      if (val) {
        setOperatorToken(val);
        bar.remove();
        if (typeof renderAll === "function") renderAll().catch(() => {});
      }
    };
  }
  const clearBtn = $("clear-operator-token-btn");
  if (clearBtn) {
    clearBtn.onclick = () => clearOperatorToken();
  }
}

function hideAuthError() {
  const bar = $("operator-auth-banner");
  if (bar) bar.remove();
}

// Ingest token from URL parameter if present, then sanitize URL
try {
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.has("token")) {
    setOperatorToken(urlParams.get("token"));
    window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);
  }
} catch (e) {}

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  const tok = getOperatorToken();
  if (tok) {
    opts.headers["Authorization"] = `Bearer ${tok}`;
  }
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  const status = res.status;
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    data = {};
  }
  if (status === 401 || status === 403) {
    const detail = (data && (data.detail || data.error)) || `HTTP ${status} Unauthorized`;
    showAuthError(detail);
  } else if (status >= 200 && status < 300) {
    hideAuthError();
  }
  return { status, data };
}

const msgId = () => "op-" + Math.random().toString(36).slice(2, 12);
let selectedTaskId = null;


let demoTaskId = null;
const demoLoggedStages = new Set();

function formatTime(d = new Date()) {
  return d.toTimeString().split(" ")[0];
}

function appendDemoStream(stageKey, badgeClass, badgeLabel, detailText) {
  const stream = $("demo-progress-stream");
  if (!stream) return;
  if (stream.querySelector(".muted")) {
    stream.innerHTML = "";
  }
  const streamKey = `${demoTaskId}:${stageKey}`;
  if (demoLoggedStages.has(streamKey)) return;
  demoLoggedStages.add(streamKey);

  const row = document.createElement("div");
  row.className = "stream-entry";
  row.innerHTML = `<span class="stream-time">[${formatTime()}]</span>
    <span class="stream-badge ${badgeClass}">${esc(badgeLabel)}</span>
    <span class="stream-text">${esc(detailText)}</span>`;
  stream.appendChild(row);
  stream.scrollTop = stream.scrollHeight;
}

function setDemoProgressBar(stageName, details = {}) {
  const fill = $("demo-progress-fill");
  const s1 = $("step-received");
  const s2 = $("step-clarifying");
  const s3 = $("step-leased");
  const s4 = $("step-in-progress");
  const s5 = $("step-final");
  const finalLabel = $("step-final-label");
  if (!s1 || !s2 || !s3 || !s4 || !s5) return;

  [s1, s2, s3, s4, s5].forEach((s) => {
    s.className = "progress-step";
    const node = s.querySelector(".step-node");
    if (node) node.textContent = s.dataset.num;
  });
  if (finalLabel) finalLabel.textContent = "Completed / Blocked";

  if (stageName === "received") {
    if (fill) fill.style.width = "10%";
    s1.classList.add("active");
  } else if (stageName === "clarifying") {
    if (fill) fill.style.width = "30%";
    s1.classList.add("completed");
    s1.querySelector(".step-node").textContent = "✓";
    s2.classList.add("active");
  } else if (stageName === "leased") {
    if (fill) fill.style.width = "55%";
    s1.classList.add("completed");
    s1.querySelector(".step-node").textContent = "✓";
    if (details.clarified) {
      s2.classList.add("completed");
      s2.querySelector(".step-node").textContent = "✓";
    } else {
      s2.classList.add("skipped");
    }
    s3.classList.add("completed");
    s3.querySelector(".step-node").textContent = "✓";
    s4.classList.add("active");
  } else if (stageName === "in_progress") {
    if (fill) fill.style.width = "75%";
    s1.classList.add("completed");
    s1.querySelector(".step-node").textContent = "✓";
    if (details.clarified) {
      s2.classList.add("completed");
      s2.querySelector(".step-node").textContent = "✓";
    } else {
      s2.classList.add("skipped");
    }
    s3.classList.add("completed");
    s3.querySelector(".step-node").textContent = "✓";
    s4.classList.add("active");
  } else if (stageName === "completed") {
    if (fill) fill.style.width = "100%";
    s1.classList.add("completed");
    s1.querySelector(".step-node").textContent = "✓";
    if (details.clarified) {
      s2.classList.add("completed");
      s2.querySelector(".step-node").textContent = "✓";
    } else {
      s2.classList.add("skipped");
    }
    s3.classList.add("completed");
    s3.querySelector(".step-node").textContent = "✓";
    s4.classList.add("completed");
    s4.querySelector(".step-node").textContent = "✓";
    s5.classList.add("completed");
    s5.querySelector(".step-node").textContent = "✓";
    if (finalLabel) finalLabel.textContent = "Completed";
  } else if (["blocked", "cancelled", "rejected", "failed"].includes(stageName)) {
    if (fill) fill.style.width = "100%";
    s1.classList.add("completed");
    s1.querySelector(".step-node").textContent = "✓";
    s5.classList.add("blocked");
    s5.querySelector(".step-node").textContent = "✗";
    if (finalLabel) finalLabel.textContent = stageName === "blocked" ? "Blocked" : stageName.toUpperCase();
  }
}

function syncDemoTask(task) {
  if (!task) return;
  const status = task.status;
  const badge = $("demo-task-badge");
  if (badge) badge.textContent = `Task: ${task.task_id} [${status}]`;

  const clarifyBox = $("demo-clarify-box");
  const isClarified = demoLoggedStages.has(`${task.task_id}:clarified`);

  if (status === "received") {
    setDemoProgressBar("received");
    appendDemoStream("received", "received", "Request received", `Task ${task.task_id} logged in queue`);
  } else if (status === "needs_clarification") {
    setDemoProgressBar("clarifying");
    if (clarifyBox) {
      clarifyBox.style.display = "flex";
      $("demo-clarify-prompt").textContent = task.question || "Clarification needed:";
    }
    appendDemoStream("clarifying", "clarifying", "Clarifying", task.question || "Awaiting requester answer");
  } else {
    if (clarifyBox) clarifyBox.style.display = "none";
  }

  if (status === "leased") {
    const l = task.lease || {};
    setDemoProgressBar("leased", { clarified: isClarified });
    appendDemoStream("leased", "leased", "Leased", `Lease v${l.version || 1} issued to ${l.cluster_id || "fleet"} (priority: ${l.priority || "routine"})`);
    setDemoProgressBar("in_progress", { clarified: isClarified });
    appendDemoStream("in_progress", "in-progress", "In progress", "Dispatched to robot fleet, route executing");
  } else if (status === "in_progress" || status === "active") {
    setDemoProgressBar("in_progress", { clarified: isClarified });
    appendDemoStream("in_progress", "in-progress", "In progress", "Dispatched to robot fleet, route executing");
  } else if (status === "completed") {
    setDemoProgressBar("completed", { clarified: isClarified });
    appendDemoStream("completed", "completed", "Completed", "Delivery verified, task completed successfully");
  } else if (["blocked", "cancelled", "rejected", "failed"].includes(status)) {
    setDemoProgressBar(status);
    appendDemoStream(status, "blocked", status.toUpperCase(), task.reason || `Task transitioned to ${status}`);
  }
}

function setupDemoControls() {
  const input = $("demo-request-input");
  const submitBtn = $("demo-submit-btn");
  const clarifyBtn = $("demo-clarify-btn");
  const clarifyInput = $("demo-clarify-input");

  if (!submitBtn || !input) return;

  const doSubmit = async () => {
    const text = input.value.trim();
    if (!text) return;
    submitBtn.disabled = true;
    input.disabled = true;

    const stream = $("demo-progress-stream");
    if (stream) stream.innerHTML = "";
    demoTaskId = "submitting...";
    demoLoggedStages.clear();

    const badge = $("demo-task-badge");
    if (badge) badge.textContent = "Submitting request...";

    setDemoProgressBar("received");
    appendDemoStream("received", "received", "Request received", `"${text}"`);

    try {
      const { status, data } = await api("POST", "/requests", { text, message_id: msgId() });
      if (status === 201 && data && data.task_id) {
        demoTaskId = data.task_id;
        input.value = "";
        if (badge) badge.textContent = `Task: ${demoTaskId} [${data.status}]`;
        await refreshTasks();
        selectTask(demoTaskId);
      } else {
        alert(`Request failed: ${data.error || status}`);
        if (badge) badge.textContent = "Request failed";
        setDemoProgressBar("idle");
      }
    } catch (err) {
      console.error("Submit request error:", err);
      alert("Failed to submit request");
      if (badge) badge.textContent = "Error submitting";
      setDemoProgressBar("idle");
    } finally {
      submitBtn.disabled = false;
      input.disabled = false;
      input.focus();
    }
  };

  submitBtn.onclick = doSubmit;
  input.onkeydown = (e) => {
    if (e.key === "Enter") doSubmit();
  };

  if (clarifyBtn && clarifyInput) {
    const doClarify = async () => {
      const answer = clarifyInput.value.trim();
      if (!answer || !demoTaskId) return;
      clarifyBtn.disabled = true;
      try {
        const { status, data } = await api("POST", `/requests/${demoTaskId}/answer`, { text: answer, message_id: msgId() });
        if (status === 201) {
          appendDemoStream("clarified", "clarifying", "Clarifying", `Answered: "${answer}"`);
          clarifyInput.value = "";
          const clarifyBox = $("demo-clarify-box");
          if (clarifyBox) clarifyBox.style.display = "none";
          await refreshTasks();
        } else {
          alert(`Clarification failed: ${data.error || status}`);
        }
      } catch (err) {
        console.error("Clarification error:", err);
        alert("Failed to submit clarification");
      } finally {
        clarifyBtn.disabled = false;
      }
    };
    clarifyBtn.onclick = doClarify;
    clarifyInput.onkeydown = (e) => {
      if (e.key === "Enter") doClarify();
    };
  }
}

let _refreshTasksInProgress = false;
async function refreshTasks() {
  if (_refreshTasksInProgress) return;
  _refreshTasksInProgress = true;
  try {
    return await _doRefreshTasks();
  } finally {
    _refreshTasksInProgress = false;
  }
}
async function _doRefreshTasks() {
  refreshStock();
  const { data } = await api("GET", "/tasks");
  const rows = $("task-rows");
  rows.innerHTML = "";
  for (const t of data.tasks) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(t.task_id)}</td>
      <td><span class="chip ${esc(t.status)}">${esc(t.status)}</span></td>
      <td>${esc(t.lease ? t.lease.priority : "-")}</td>
      <td>${esc(t.lease ? "v" + t.lease.version : "-")}</td>
      <td>${esc(t.lease ? t.lease.cluster_id : "-")}</td>`;
    if (t.task_id === selectedTaskId) tr.className = "active";
    tr.onclick = () => selectTask(t.task_id);
    rows.appendChild(tr);
  }
  if (window.NimbusSpatial && window.NimbusSpatial.onTasksUpdate) {
    window.NimbusSpatial.onTasksUpdate(data.tasks);
  }
  if (demoTaskId && data.tasks) {
    const active = data.tasks.find((t) => t.task_id === demoTaskId);
    if (active) syncDemoTask(active);
  }
}

function displayItemName(item) {
  if (item === "infusion_pump_paediatric") return "pediatric_infusion_pump";
  return item;
}

async function refreshStock() {
  const { data } = await api("GET", "/fleet");
  const clusters = data.fleet;
  const rawItems = [...new Set(clusters.flatMap((c) => Object.keys(c.stock)))];
  const itemsMap = new Map();
  for (const item of rawItems) {
    const display = displayItemName(item);
    if (!itemsMap.has(display)) {
      itemsMap.set(display, item);
    }
  }
  const displayItems = [...itemsMap.keys()].sort();
  $("stock-head").innerHTML = `<tr><th>item</th>${clusters.map((c) =>
    `<th>${esc(c.cluster_id)} <span class="muted">${esc(c.health)}</span></th>`).join("")}</tr>`;
  $("stock-rows").innerHTML = displayItems.map((display) => {
    const raw = itemsMap.get(display);
    return `<tr><td>${esc(display)}</td>${clusters.map((c) => `<td>${esc(c.stock[raw] ?? c.stock[display] ?? 0)}</td>`).join("")}</tr>`;
  }).join("");
}

async function selectTask(taskId) {
  selectedTaskId = taskId;
  demoTaskId = taskId;
  if (window.NimbusSpatial && window.NimbusSpatial.onTaskSelect) {
    window.NimbusSpatial.onTaskSelect(taskId);
  }
  await Promise.all([renderDetail(), refreshTasks()]);
}

async function renderDetail() {
  const box = $("detail");
  if (!selectedTaskId) {
    box.innerHTML = `<p class="muted">Click a row to load its decision timeline.</p>`;
    return;
  }
  const { status, data: task } = await api("GET", `/tasks/${selectedTaskId}`);
  if (status === 404) { box.innerHTML = `<p class="muted">Task not found.</p>`; return; }

  const { data: tl } = await api("GET", `/tasks/${selectedTaskId}/timeline`);
  box.innerHTML = `
    <h3>${esc(task.task_id)} <span class="chip ${esc(task.status)}">${esc(task.status)}</span></h3>
    <p class="muted">${esc(task.request_text)}</p>
    <p>
      <button id="pause-btn">Pause</button>
      <button id="cancel-btn">Cancel</button>
      <span class="muted">intents are recorded, not delivered</span>
    </p>
    <h4>Decision timeline</h4>
    <ol class="timeline">${tl.timeline.map((r) =>
      `<li><strong>${esc(r.who)}</strong>: ${esc(r.output.event || r.output.decision || "?")}
       <span class="muted">${esc(r.timestamp)}</span></li>`).join("")}</ol>`;

  $("pause-btn").onclick = () => control("pause");
  $("cancel-btn").onclick = () => control("cancel");
}

async function control(action) {
  const cancelBtn = $("cancel-btn");
  const pauseBtn = $("pause-btn");
  if (action === "cancel" && cancelBtn) {
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Cancelling... (Awaiting Edge Ack)";
  } else if (action === "pause" && pauseBtn) {
    pauseBtn.disabled = true;
    pauseBtn.textContent = "Pausing... (Awaiting Edge Ack)";
  }
  const { status, data } = await api(
    "POST", `/tasks/${selectedTaskId}/control`, { action, message_id: msgId() }
  );
  if (status !== 201) {
    alert(`Intent not recorded: ${data.error || status}`);
  }
  await renderDetail();
  await refreshTasks();
}

async function refreshFleet() {
  try {
    const { status, data } = await api("GET", "/fleet");
    if (status === 200 && window.NimbusSpatial && window.NimbusSpatial.updateFleet) {
      window.NimbusSpatial.updateFleet(data.fleet);
    }
  } catch (e) {
    // fleet query optional/graceful fallback
  }
}

async function refreshFault() {
  try {
    const { status, data } = await api("GET", "/fault");
    if (status === 200 && window.NimbusSpatial && window.NimbusSpatial.updateFault) {
      window.NimbusSpatial.updateFault(data);
    }
  } catch (e) {
    // fault query optional/graceful fallback
  }
}

async function updateLlmStatus() {
  const badge = $("llm-status-badge");
  if (!badge) return;

  try {
    const { status, data } = await api("GET", "/llm-status");
    if (status === 200 && data && data.status) {
      if (data.status === "live") {
        badge.textContent = "LLM: Bedrock (live)";
        badge.className = "llm-badge llm-live";
      } else if (data.status === "fallback") {
        badge.textContent = "LLM: rule fallback";
        badge.className = "llm-badge llm-fallback";
      } else {
        badge.textContent = "LLM: unknown";
        badge.className = "llm-badge llm-unknown";
      }
      return;
    }
  } catch (e) {
    // fallback to timeline inspection
  }

  try {
    const { status: tStatus, data: tData } = await api("GET", "/tasks");
    if (tStatus !== 200 || !tData.tasks || tData.tasks.length === 0) {
      badge.textContent = "LLM: unknown";
      badge.className = "llm-badge llm-unknown";
      return;
    }
    const tasks = tData.tasks;
    let latestLlm = null;
    for (let t = tasks.length - 1; t >= 0 && !latestLlm; t--) {
      const taskId = tasks[t].task_id;
      const { status: tlStatus, data: tlData } = await api("GET", `/tasks/${taskId}/timeline`);
      if (tlStatus !== 200 || !tlData.timeline) continue;
      for (let i = tlData.timeline.length - 1; i >= 0; i--) {
        const row = tlData.timeline[i];
        if (row.output && row.output.llm) {
          latestLlm = row.output.llm;
          break;
        }
      }
    }
    if (!latestLlm) {
      badge.textContent = "LLM: unknown";
      badge.className = "llm-badge llm-unknown";
    } else if (latestLlm.tokens_estimated === false && latestLlm.model) {
      badge.textContent = "LLM: Bedrock (live)";
      badge.className = "llm-badge llm-live";
    } else {
      badge.textContent = "LLM: rule fallback";
      badge.className = "llm-badge llm-fallback";
    }
  } catch (e) {
    badge.textContent = "LLM: unknown";
    badge.className = "llm-badge llm-unknown";
  }
}

function setupStockControls() {
  const setBtn = $("set-stock-btn");
  const resetBtn = $("reset-stock-btn");
  if (setBtn) {
    setBtn.onclick = async () => {
      const cluster = $("stock-cluster-select")?.value || "east-wing";
      const item = $("stock-item-select")?.value || "pediatric_infusion_pump";
      const qtyVal = $("stock-qty-input")?.value;
      const qty = parseInt(qtyVal, 10);
      if (isNaN(qty) || qty < 0) return;
      setBtn.disabled = true;
      try {
        await api("POST", "/fleet/stock", {
          cluster_id: cluster,
          stock: { [item]: qty }
        });
        await refreshStock();
        await refreshTasks();
      } catch (err) {
        console.error("Failed to set stock:", err);
      } finally {
        setBtn.disabled = false;
      }
    };
  }
  if (resetBtn) {
    resetBtn.onclick = async () => {
      const cluster = $("stock-cluster-select")?.value || "east-wing";
      const item = $("stock-item-select")?.value || "pediatric_infusion_pump";
      resetBtn.disabled = true;
      try {
        await api("POST", "/inventory/reset", {
          cluster_id: cluster,
          item: item
        });
        const qtyInput = $("stock-qty-input");
        if (qtyInput) qtyInput.value = "0";
        await refreshStock();
        await refreshTasks();
      } catch (err) {
        console.error("Failed to reset stock:", err);
      } finally {
        resetBtn.disabled = false;
      }
    };
  }
}

function setupMemoryControls() {
  const btn = $("reset-memory-btn");
  if (!btn) return;
  btn.onclick = async () => {
    btn.disabled = true;
    const prevText = btn.textContent;
    btn.textContent = "Resetting...";
    try {
      const { status, data } = await api("POST", "/reset-memory", {});
      if (status === 200 || status === 201) {
        btn.textContent = "Memory reset!";
        await refreshTasks();
        await refreshFleet();
        if (selectedTaskId) await renderDetail();
        setTimeout(() => {
          btn.textContent = prevText;
          btn.disabled = false;
        }, 1500);
      } else {
        alert(`Failed to reset memory: ${data.error || status}`);
        btn.textContent = prevText;
        btn.disabled = false;
      }
    } catch (err) {
      console.error("Failed to reset memory:", err);
      alert("Failed to reset request memory");
      btn.textContent = prevText;
      btn.disabled = false;
    }
  };
}

refreshTasks();
refreshFleet();
refreshFault();
updateLlmStatus();
setupStockControls();
setupMemoryControls();
setupDemoControls();
setInterval(() => {
  refreshTasks();
  refreshFleet();
  refreshFault();
}, 2000);
setInterval(updateLlmStatus, 5000);
