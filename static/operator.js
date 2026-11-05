/* Nimbus operator console (FR-22 view). Vanilla JS over the
   documented API only. Pause and cancel record the operator intent and
   publish a stop lease to robots via the cloud orchestrator (FR-22). */

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  return { status: res.status, data: await res.json() };
}

const msgId = () => "op-" + Math.random().toString(36).slice(2, 12);
let selectedTaskId = null;

async function refreshTasks() {
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

refreshTasks();
refreshFleet();
refreshFault();
updateLlmStatus();
setupStockControls();
setInterval(() => {
  refreshTasks();
  refreshFleet();
  refreshFault();
}, 4000);
setInterval(updateLlmStatus, 5000);
