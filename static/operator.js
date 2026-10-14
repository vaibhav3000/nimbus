/* Nimbus operator console (FR-22 minimal view). Vanilla JS over the
   documented API only. Pause/cancel are RECORDED INTENTS: nothing reaches a
   robot in this slice. */

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
}

async function selectTask(taskId) {
  selectedTaskId = taskId;
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

refreshTasks();
