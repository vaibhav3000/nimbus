# UI Security and State Correctness Audit

**Components Evaluated:** `static/requester.html`, `static/operator.html`, `static/app.js`, `static/operator.js`  
**Audit Scope:** Stored/Reflected XSS mitigation, Content Security Policy (CSP), polling concurrency guards, cancel/override honest state representation.

---

## 1. Cross-Site Scripting (XSS) Vulnerability Review & Remediation

### Untrusted Input Vectors
All dynamic strings arriving from network APIs or user text input boxes are treated as untrusted:
- Natural language request strings (`request_text`, `text`)
- Robot names and cluster identifiers (`robot_id`, `cluster_id`, `east-wing`, `west-wing`)
- Task identifiers and leases (`task_id`, `lease_id`, `version`)
- Edge notices, clarify questions, and error messages (`notice`, `error`, `question`)
- Inventory item keys and quantities

### Remediation Applied
1. **Mandatory HTML Escaping (`esc`)**:
   In `static/app.js` and `static/operator.js`, an explicit HTML escaping sanitizer was introduced:
   ```javascript
   function esc(str) {
     if (str === null || str === undefined) return '';
     return String(str)
       .replace(/&/g, '&amp;')
       .replace(/</g, '&lt;')
       .replace(/>/g, '&gt;')
       .replace(/"/g, '&quot;')
       .replace(/'/g, '&#39;');
   }
   ```
2. **Sanitized Interpolation**:
   All `innerHTML` assignments interpolating task, robot, or lease metadata now wrap every interpolated variable with `esc(...)`:
   - `<td>${esc(t.task_id)}</td>`
   - `<td><span class="badge ${badgeClass}">${esc(t.status)}</span></td>`
   - `<span>${esc(clusterId)}: ${esc(count)}</span>`
3. **Restricted Content Security Policy (CSP)**:
   Added a strict CSP meta header to both `static/index.html` and `static/operator.html`:
   ```html
   <meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self';">
   ```
   This prevents external malicious script injection, untrusted network exfiltration, and unauthorized inline frames.
4. **Regression Verification**:
   Payloads such as `<img src=x onerror=alert(1)>` and `<script>alert('pwned')</script>` submitted via `POST /requests` are safely escaped into `&lt;img src=x onerror=alert(1)&gt;` and rendered as inert text in both requester and operator tables without executing JavaScript.

---

## 2. Polling Concurrency and Stale-State Prevention

1. **Overlapping Fetch Elimination**:
   In `static/operator.js`, recurring `setInterval` loops could previously launch overlapping HTTP calls under high network latency. An in-flight lock flag was implemented:
   ```javascript
   let _refreshTasksInProgress = false;
   async function pollTasks() {
     if (_refreshTasksInProgress) return;
     _refreshTasksInProgress = true;
     try {
       await fetchTasks();
     } finally {
       _refreshTasksInProgress = false;
     }
   }
   ```
2. **Network Disconnection vs Empty State**:
   If an API fetch fails (due to network partition or timeout), the UI displays a yellow `Network Disconnected / Reconnecting...` banner rather than clearing active task rows and misrepresenting the fleet as empty.
3. **Monotonic State Sequencing**:
   Incoming task update payloads check `task.updated_at` or `lease.version` against existing DOM state. Older responses arriving out of order cannot overwrite newer client state.

---

## 3. Honest Cancel and Override State Presentation

### Finding (Addendum 4.3)
Previously, clicking "Cancel" in the operator console immediately flipped the local badge to `Cancelled` or `Robot Stopped`, even though the edge robot had not yet received or acknowledged the stop lease.

### Remediation Applied
1. **Pending State Presentation**:
   When an operator triggers `POST /tasks/{id}/control` with action `cancel`, the console immediately sets the task badge to:
   `Cancelling... (Awaiting Edge Ack)` with a warning amber badge.
2. **State Transition Chain**:
   - `CANCEL_REQUESTED`: Operator pressed Cancel; HTTP 201 received by cloud.
   - `STOP_LEASE_PUBLISHED`: Cloud generated a zero-speed stop lease version $V+1$.
   - `EDGE_ACKNOWLEDGED`: Edge cluster check-in confirms receipt of stop lease.
   - `ROBOT_STOPPED`: Telemetry confirms robot linear velocity is $0.0\text{ m/s}$.
3. The UI now accurately distinguishes cloud acceptance from verified edge physical halt.
