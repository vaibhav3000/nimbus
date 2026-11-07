/* Nimbus requester app (FR-21). Vanilla JS against the documented API only:
   POST /requests, POST /requests/{id}/answer, POST /escalations,
   GET /tasks, GET /tasks/{id}, GET /tasks/{id}/timeline.
   State lives server-side, so a refresh just re-renders from the API. */

const $ = (id) => document.getElementById(id);
let selectedTaskId = null;

/* Off unless the server says NIMBUS_DEMO_CONTROLS=1.  Gates the OUTPUT_READY
   stand-in below, which reports a delivery no robot performed. */
let demoControls = false;
let c3StructuredForms = false;

async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  return { status: res.status, data: await res.json() };
}

function msgId() {
  return "app-" + Math.random().toString(36).slice(2, 12);
}

function chip(status) {
  return status ? `<span class="chip ${esc(status)}">${esc(status)}</span>` : "";
}

function shortText(t) {
  return t.length > 44 ? t.slice(0, 44) + "…" : t;
}

/* Escape before any innerHTML interpolation: the timeline carries edge-supplied
   text (escalation payloads), which is this app's trust boundary. */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function refreshTasks() {
  const { data } = await api("GET", "/tasks");
  $("task-count").textContent = `(${data.tasks.length})`;
  const list = $("task-list");
  list.innerHTML = "";
  for (const t of data.tasks) {
    const li = document.createElement("li");
    li.innerHTML = `<strong>${esc(t.task_id)}</strong>${chip(t.status)}<br>
      <span class="muted">${esc(shortText(t.request_text))}</span>`;
    if (t.task_id === selectedTaskId) li.className = "active";
    li.onclick = () => selectTask(t.task_id);
    list.appendChild(li);
  }
}

async function selectTask(taskId) {
  selectedTaskId = taskId;
  location.hash = taskId;
  await Promise.all([renderDetail(), refreshTasks()]);
}

async function renderDetail() {
  const box = $("detail");
  if (!selectedTaskId) {
    box.innerHTML = `<p class="muted">Select a task on the left. Nothing is lost on refresh: state lives in the API.</p>`;
    return;
  }
  const { status, data: task } = await api("GET", `/tasks/${selectedTaskId}`);
  if (status === 404) { box.innerHTML = `<p class="muted">Task not found.</p>`; return; }

  let html = `<h3>${esc(task.task_id)} ${chip(task.status)}</h3>
    <p class="muted">“${esc(task.request_text)}”</p>`;

  if (task.notice) {
    html += `<div class="notice">${esc(task.notice)}</div>`;
  }
  if (task.reason && (task.status === "queued" || task.status === "rejected")) {
    html += `<p class="muted"><strong>${task.status === "queued" ? "Waiting" : "Not possible"}:</strong> ${esc(task.reason)}</p>`;
  }
  if (task.question) {
    html += `<div class="qbox"><strong>Question:</strong> ${esc(task.question.question)}
      <br><span class="muted">Readings: ${esc(task.question.possible_readings.join(" · "))}</span>
      <p><input type="text" id="answer" placeholder="Type your answer…">
      <button id="answer-btn">Answer</button></p></div>`;
  }
  if (task.lease) {
    html += `<h4>Lease v${esc(task.lease.version)}</h4>
      <pre class="lease">${esc(JSON.stringify(task.lease, null, 2))}</pre>`;
  }
  if (task.result_ref) {
    html += `<div class="result"><strong>Result:</strong> ${esc(task.result_ref)}</div>`;
  }
  if (task.status === "leased" && demoControls) {
    html += `<p><button class="secondary" id="finish-btn">Demo: edge sends OUTPUT_READY</button></p>`;
  }

  const { data: tl } = await api("GET", `/tasks/${selectedTaskId}/timeline`);
  html += `<h4>Decision timeline (FR-20)</h4><ol class="timeline">` +
    (tl.timeline || []).map(r =>
      `<li><strong>${esc(r.who)}</strong>: ${esc((r.output && (r.output.event || r.output.decision)) || "?")}
       <span class="muted">${esc(r.timestamp)}</span></li>`).join("") +
    `</ol>`;
  box.innerHTML = html;

  const answerBtn = $("answer-btn");
  if (answerBtn) answerBtn.onclick = submitAnswer;
  const finishBtn = $("finish-btn");
  if (finishBtn) finishBtn.onclick = simulateOutputReady;
}

async function submitAnswer() {
  const text = $("answer").value.trim();
  if (!text) return;
  await api("POST", `/requests/${selectedTaskId}/answer`, { text, message_id: msgId() });
  await renderDetail();
  await refreshTasks();
}

/* Learn which optional controls this deployment allows, before the first render. */
async function loadUiConfig() {
  try {
    const { data } = await api("GET", "/ui-config");
    demoControls = Boolean(data && data.demo_controls);
    c3StructuredForms = Boolean(data && data.c3_structured_forms);
    if (c3StructuredForms) configureC3Form(data.c3_form_options || {});
  } catch (e) {
    demoControls = false;   // unreachable or old server: stay off, never fabricate
  }
}

function fillSelect(id, rows, blankLabel = null) {
  const el = $(id);
  el.innerHTML = "";
  if (blankLabel !== null) el.appendChild(new Option(blankLabel, ""));
  for (const row of rows || []) el.appendChild(new Option(row.label, row.id));
}

function configureC3Form(options) {
  $("free-text-composer").hidden = true;
  $("c3-form").hidden = false;
  fillSelect("c3-item", options.items);
  fillSelect("c3-destination", options.destinations);
  fillSelect("c3-source", options.sources, "Use normal stock location");
  fillSelect("c3-priority", options.priorities);
  const syncDeadline = () => {
    const row = (options.priorities || []).find((p) => p.id === $("c3-priority").value);
    if (row) $("c3-deadline").value = row.deadline_minutes;
  };
  $("c3-priority").onchange = syncDeadline;
  syncDeadline();
}

async function simulateOutputReady() {
  // In the real system the edge sends this; here the app offers it as a
  // one-click stand-in so the full result path is demoable. It reports under
  // the lease version currently in force (a re-plan moves it past 1).
  //
  // Hidden unless NIMBUS_DEMO_CONTROLS=1: it manufactures a delivery no robot
  // performed, so leaving it on makes an FR-21 demo look complete when edge
  // execution never happened.  Re-checked here, not just at render time.
  if (!demoControls) return;
  const { data: current } = await api("GET", `/tasks/${selectedTaskId}`);
  await api("POST", "/escalations", {
    type: "OUTPUT_READY",
    message_id: msgId(),
    lease_version: current.lease ? current.lease.version : 1,
    task_id: selectedTaskId,
    payload: { result_ref: `local://outputs/${selectedTaskId}/delivery-confirmed` },
  });
  await renderDetail();
  await refreshTasks();
}

$("send").onclick = async () => {
  const text = $("request-text").value.trim();
  if (!text) return;
  const { data } = await api("POST", "/requests", { text, message_id: msgId() });
  $("request-text").value = "";
  await refreshTasks();
  await selectTask(data.task_id || selectedTaskId);
};

$("c3-form").onsubmit = async (event) => {
  event.preventDefault();
  const form = {
    item: $("c3-item").value,
    destination: $("c3-destination").value,
    source: $("c3-source").value || null,
    priority: $("c3-priority").value,
    deadline_minutes: Number($("c3-deadline").value),
  };
  const { status, data } = await api("POST", "/requests", { form, message_id: msgId() });
  if (status >= 400) { alert(data.error || "Unable to create task"); return; }
  await refreshTasks();
  await selectTask(data.task_id || selectedTaskId);
};

window.onhashchange = () => {
  if (location.hash.slice(1) !== selectedTaskId) selectTask(location.hash.slice(1));
};

/* Config first: a render that ran before it would briefly show a gated control. */
loadUiConfig().then(refreshTasks).then(() => {
  const fromHash = location.hash.slice(1);
  if (fromHash) selectTask(fromHash);
});
