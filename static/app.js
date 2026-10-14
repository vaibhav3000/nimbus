/* Nimbus requester app (FR-21). Vanilla JS against the documented API only:
   POST /requests, POST /requests/{id}/answer, POST /escalations,
   GET /tasks, GET /tasks/{id}, GET /tasks/{id}/timeline.
   State lives server-side, so a refresh just re-renders from the API. */

const $ = (id) => document.getElementById(id);
let selectedTaskId = null;

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
  return status ? `<span class="chip ${status}">${status}</span>` : "";
}

function shortText(t) {
  return t.length > 44 ? t.slice(0, 44) + "…" : t;
}

/* Escape before any innerHTML interpolation: the timeline carries edge-supplied
   text (escalation payloads), which is this app's trust boundary. */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
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
    li.innerHTML = `<strong>${t.task_id}</strong>${chip(t.status)}<br>
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

  let html = `<h3>${task.task_id} ${chip(task.status)}</h3>
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
    html += `<h4>Lease v${task.lease.version}</h4>
      <pre class="lease">${JSON.stringify(task.lease, null, 2)}</pre>`;
  }
  if (task.result_ref) {
    html += `<div class="result"><strong>Result:</strong> ${esc(task.result_ref)}</div>`;
  }
  if (task.status === "leased") {
    html += `<p><button class="secondary" id="finish-btn">Demo: edge sends OUTPUT_READY</button></p>`;
  }

  const { data: tl } = await api("GET", `/tasks/${selectedTaskId}/timeline`);
  html += `<h4>Decision timeline (FR-20)</h4><ol class="timeline">` +
    tl.timeline.map(r =>
      `<li><strong>${r.who}</strong>: ${esc(r.output.event || r.output.decision || "?")}
       <span class="muted">${r.timestamp}</span></li>`).join("") +
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

async function simulateOutputReady() {
  // In the real system the edge sends this; here the app offers it as a
  // one-click stand-in so the full result path is demoable. It reports under
  // the lease version currently in force (a re-plan moves it past 1).
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

window.onhashchange = () => {
  if (location.hash.slice(1) !== selectedTaskId) selectTask(location.hash.slice(1));
};

refreshTasks().then(() => {
  const fromHash = location.hash.slice(1);
  if (fromHash) selectTask(fromHash);
});
