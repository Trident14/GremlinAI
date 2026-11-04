/* ============================================================================
   GremlinAI — Apple-Inspired Interactive Frontend Engine
   ============================================================================ */
'use strict';

let currentRunId = null;
let ws = null;
let currentDraftPlan = null;  // Holds { story, criteria, steps }
let totalSteps = 0;
let completedSteps = 0;
let frameCount = 0;
let lastFrameTs = Date.now();
let isTheaterMode = false;
let currentFitMode = 'fit';

const stepCards = {};       // step_id -> DOM element
const criteriaMap = {};     // criterion_id -> DOM element

// DOM References
const frameImg = document.getElementById('live-frame');
const actionToast = document.getElementById('action-toast');
const actionLabel = document.getElementById('action-label');
const bboxGroup = document.getElementById('bbox-group');
const bboxRect = document.getElementById('bbox-rect');
const bboxRectBg = document.getElementById('bbox-rect-bg');
const bboxLabelGroup = document.getElementById('bbox-label-group');
const bboxLabelBg = document.getElementById('bbox-label-bg');
const bboxLabelText = document.getElementById('bbox-label-text');
const agentLog = document.getElementById('agent-log');
const omnibarUrl = document.getElementById('omnibar-url');
const omnibarTitle = document.getElementById('omnibar-title');
const liveIndicator = document.getElementById('live-indicator');
const liveText = document.getElementById('live-text');
const planningOverlay = document.getElementById('planning-overlay');
const planningSubtext = document.getElementById('planning-subtext');
const configPanel = document.getElementById('config-panel');
const planReviewCard = document.getElementById('plan-review-card');
const topbarSummary = document.getElementById('topbar-summary');
const summaryUrl = document.getElementById('summary-url');
const summaryStepInfo = document.getElementById('summary-step-info');
const browserSection = document.getElementById('browser-section');
const theaterBtn = document.getElementById('theater-btn');
const fitBtn = document.getElementById('fit-btn');
const viewportCanvas = document.getElementById('viewport-canvas');
const progressFill = document.getElementById('progress-fill');
const progressText = document.getElementById('progress-text');
const progressPercent = document.getElementById('progress-percent');
const logCountBadge = document.getElementById('log-count-badge');
const autoscrollCb = document.getElementById('autoscroll-cb');
const lightboxModal = document.getElementById('lightbox-modal');
const lightboxImg = document.getElementById('lightbox-img');
const lightboxTitle = document.getElementById('lightbox-title');
const errorBanner = document.getElementById('error-banner');
const alertTitle = document.getElementById('alert-title');
const alertDesc = document.getElementById('alert-desc');

let bboxHideTimer = null;
let logCounter = 0;

// ── Presets & Helper Setters ──────────────────────────────────────────────────
function setUrl(url) {
  document.getElementById('url-input').value = url;
}

function loadDemoStory() {
  setUrl('http://localhost:5001');
  document.getElementById('story-input').value =
    "As a shopper, I can log in, add items to my cart, and check out successfully.\n" +
    "AC1: Valid login takes me to the product catalog.\n" +
    "AC2: The cart total equals the sum of added items.\n" +
    "AC3: Invalid email during checkout displays a validation error.\n" +
    "AC4: Successful checkout displays order confirmation.";
  document.getElementById('auth-username').value = "demo@test.com";
  document.getElementById('auth-password').value = "pass123";
  document.getElementById('auth-prelogin').checked = false;
  document.getElementById('auth-disclosure').open = true;
  logEntry('info', 'Loaded demo e-commerce store preset (:5001)');
}

function toggleConfigPanel(forceState) {
  const isCollapsed = configPanel.classList.contains('collapsed');
  const shouldCollapse = forceState !== undefined ? !forceState : !isCollapsed;
  configPanel.classList.toggle('collapsed', shouldCollapse);
  const button = document.getElementById('config-collapse-btn');
  if (button) {
    button.setAttribute('aria-expanded', String(!shouldCollapse));
    button.setAttribute('aria-label', shouldCollapse ? 'Open test configuration' : 'Collapse test configuration');
    button.setAttribute('title', shouldCollapse ? 'Open test configuration' : 'Collapse test configuration');
    button.innerHTML = shouldCollapse
      ? '<span class="collapse-icon">⌄</span>'
      : '<span class="collapse-icon">⌃</span>';
  }
}

function dismissAlert() {
  errorBanner.style.display = 'none';
}

function showAlert(title, message) {
  alertTitle.textContent = title;
  alertDesc.textContent = message;
  errorBanner.style.display = 'flex';
}

// ── Stage 1 & 2: Interactive Plan Draft & Review Workflow ─────────────────────
async function requestPlanDraft() {
  dismissAlert();
  const url = document.getElementById('url-input').value.trim();
  const story = document.getElementById('story-input').value.trim();
  if (!url || !story) {
    alert('Please enter a target application URL and user story.');
    return;
  }

  const planBtn = document.getElementById('plan-btn');
  planBtn.disabled = true;
  planBtn.innerHTML = '<span class="spinner"></span> Drafting Plan...';
  setStatus('running', 'Drafting Plan…');

  const auth = buildAuthPayload();
  const payload = { url, story };
  if (auth) payload.auth = auth.config;

  try {
    const res = await fetch('/api/plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.text();
      throw new Error(err);
    }

    currentDraftPlan = await res.json();
    renderPlanReviewModal(currentDraftPlan);
    logEntry('info', `📋 Draft plan generated with ${currentDraftPlan.steps.length} steps.`);
    setStatus('idle', 'Review Plan');
  } catch (err) {
    logEntry('error', 'Plan generation failed: ' + err.message);
    showAlert('Planning Error', 'Failed to generate plan. LLM may be unresponsive. ' + err.message);
    setStatus('error', 'Plan Failed');
  } finally {
    planBtn.disabled = false;
    planBtn.innerHTML = '<span>📋 Draft Test Plan</span>';
  }
}

function renderPlanReviewModal(plan) {
  // Render criteria summary pills
  const pillsContainer = document.getElementById('review-criteria-pills');
  pillsContainer.innerHTML = '';
  (plan.criteria || []).forEach(c => {
    const pill = document.createElement('div');
    pill.className = 'criteria-pill-tag';
    pill.innerHTML = `<span class="criteria-id-bold">${esc(c.id)}</span> <span>${esc(c.text)}</span>`;
    pillsContainer.appendChild(pill);
  });

  // Render editable steps
  const stepsContainer = document.getElementById('editable-steps-list');
  stepsContainer.innerHTML = '';
  (plan.steps || []).forEach((s, idx) => {
    appendEditableStepRow(stepsContainer, s, idx);
  });

  // Collapse config panel and show review modal
  toggleConfigPanel(false);
  planReviewCard.style.display = 'flex';
  planReviewCard.scrollIntoView({ behavior: 'smooth' });
}

function appendEditableStepRow(container, step, index) {
  const row = document.createElement('div');
  row.className = 'editable-step-row';
  row.dataset.stepId = step.id || `S${index + 1}`;
  row.dataset.criterionId = step.criterion_id || '';

  row.innerHTML = `
    <span class="step-num-badge">${esc(step.id || 'S' + (index + 1))}</span>
    <div class="step-edit-inputs">
      <input type="text" class="apple-input step-instr" value="${esc(step.instruction)}" placeholder="Action Instruction (e.g. Click Login button)">
      <input type="text" class="apple-input step-exp" value="${esc(step.expected)}" placeholder="Expected Outcome (e.g. Products page is visible)">
    </div>
    <button type="button" class="step-delete-btn" onclick="this.closest('.editable-step-row').remove()" title="Delete step">&times;</button>
  `;
  container.appendChild(row);
}

function addNewCustomStep() {
  const container = document.getElementById('editable-steps-list');
  const count = container.children.length + 1;
  const newStep = {
    id: `S${count}`,
    criterion_id: null,
    instruction: '',
    expected: ''
  };
  appendEditableStepRow(container, newStep, count - 1);
  const inputs = container.querySelectorAll('.step-instr');
  if (inputs.length) inputs[inputs.length - 1].focus();
}

function cancelPlanReview() {
  planReviewCard.style.display = 'none';
  toggleConfigPanel(true);
}

function collectConfirmedPlan() {
  const container = document.getElementById('editable-steps-list');
  const rows = container.querySelectorAll('.editable-step-row');
  const confirmedSteps = [];

  rows.forEach((row, i) => {
    const instr = row.querySelector('.step-instr').value.trim();
    const exp = row.querySelector('.step-exp').value.trim();
    if (instr) {
      confirmedSteps.push({
        id: `S${i + 1}`,
        criterion_id: row.dataset.criterionId || null,
        instruction: instr,
        expected: exp || 'Action succeeds',
      });
    }
  });

  if (!confirmedSteps.length) {
    alert('Please keep at least one test step in your plan.');
    return null;
  }

  return {
    story: currentDraftPlan ? currentDraftPlan.story : document.getElementById('story-input').value.trim(),
    criteria: currentDraftPlan ? currentDraftPlan.criteria : [],
    steps: confirmedSteps,
  };
}

async function executeConfirmedPlan() {
  const confirmedPlan = collectConfirmedPlan();
  if (!confirmedPlan) return;

  planReviewCard.style.display = 'none';
  await startRun(confirmedPlan);
}

async function startDirectRun() {
  await startRun(null);
}

// ── Test Execution Lifecycle ──────────────────────────────────────────────────
async function startRun(preApprovedPlan) {
  dismissAlert();
  const url = document.getElementById('url-input').value.trim();
  const story = document.getElementById('story-input').value.trim();
  if (!url || !story) {
    alert('Please enter a target URL and user story.');
    return;
  }

  // Reset UI State
  document.getElementById('steps-list').innerHTML = '';
  document.getElementById('criteria-list').innerHTML = '';
  document.getElementById('report-section').style.display = 'none';
  document.getElementById('agent-log').innerHTML = '';
  Object.keys(stepCards).forEach(k => delete stepCards[k]);
  Object.keys(criteriaMap).forEach(k => delete criteriaMap[k]);
  totalSteps = 0;
  completedSteps = 0;
  logCounter = 0;
  updateProgress(0, 0);

  // Address bar & Status
  omnibarUrl.textContent = url;
  omnibarTitle.textContent = 'Navigating...';
  setStatus('running', preApprovedPlan ? 'Running Approved Plan…' : 'Planning…');
  
  document.getElementById('plan-btn').style.display = 'none';
  document.getElementById('direct-run-btn').style.display = 'none';
  document.getElementById('stop-btn').style.display = '';

  // Show Planning overlay
  planningOverlay.style.display = 'flex';
  planningSubtext.textContent = preApprovedPlan
    ? 'Starting browser session and loading target site...'
    : 'Formulating test plan...';

  // Collapse config panel to give full focus to live browser
  toggleConfigPanel(false);

  // Update topbar summary
  summaryUrl.textContent = url;
  summaryStepInfo.textContent = 'Initializing test run...';
  topbarSummary.style.display = 'flex';

  const auth = buildAuthPayload();
  const body = { url, story };
  if (auth) {
    body.auth = auth.config;
    body.password = auth.password;
  }
  if (preApprovedPlan) {
    body.plan = preApprovedPlan;
  }

  let res;
  try {
    res = await fetch('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await res.text());
  } catch (e) {
    setStatus('error', 'Failed to start');
    logEntry('error', 'Execution error: ' + e.message);
    showAlert('Execution Stalled', 'Could not start test run: ' + e.message);
    planningOverlay.style.display = 'none';
    resetRunButtons();
    return;
  }

  const data = await res.json();
  currentRunId = data.run_id;
  logEntry('info', `▶ Run started [${currentRunId}]. Streaming live view...`);
  connectWS(currentRunId);
}

function buildAuthPayload() {
  const username = document.getElementById('auth-username').value.trim();
  const password = document.getElementById('auth-password').value;
  if (!username && !password) return null;
  return {
    config: {
      mode: 'form',
      login_url: document.getElementById('auth-login-url').value.trim() || null,
      username,
      extra_fields: {},
      success_hint: document.getElementById('auth-hint').value.trim() || null,
      pre_login: document.getElementById('auth-prelogin').checked,
    },
    password,
  };
}

async function stopRun() {
  if (!currentRunId) return;
  try {
    await fetch(`/api/runs/${currentRunId}/stop`, { method: 'POST' });
    logEntry('warn', '⏹ Stop requested by user');
    setStatus('idle', 'Stopping…');
  } catch (e) {
    logEntry('error', 'Stop error: ' + e.message);
  }
}

function resetRunButtons() {
  document.getElementById('plan-btn').style.display = '';
  document.getElementById('direct-run-btn').style.display = '';
  document.getElementById('stop-btn').style.display = 'none';
  planningOverlay.style.display = 'none';
  liveIndicator.classList.remove('active');
  liveText.textContent = 'STANDBY';
  topbarSummary.style.display = 'none';
}

function updateProgress(done, total) {
  completedSteps = done;
  totalSteps = total;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  progressFill.style.width = pct + '%';
  progressText.textContent = `${done} of ${total} steps completed`;
  progressPercent.textContent = pct + '%';
  if (total > 0) {
    summaryStepInfo.textContent = `Step ${done} of ${total} (${pct}%)`;
  }
}

// ── WebSocket Streaming ───────────────────────────────────────────────────────
function connectWS(runId) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/runs/${runId}`);

  ws.onopen = () => {
    liveIndicator.classList.add('active');
    liveText.textContent = 'LIVE';
  };

  ws.onmessage = ({ data }) => {
    let event;
    try { event = JSON.parse(data); } catch { return; }
    handleEvent(event);
  };

  ws.onclose = () => {
    logEntry('info', 'WebSocket stream closed');
    liveIndicator.classList.remove('active');
    liveText.textContent = 'OFFLINE';
  };

  ws.onerror = () => {
    logEntry('error', 'WebSocket connection error');
  };
}

function handleEvent(event) {
  switch (event.type) {
    case 'plan_created':    onPlanCreated(event.data); break;
    case 'step_started':    onStepStarted(event.data); break;
    case 'frame':           onFrame(event.data); break;
    case 'action_executed': onActionExecuted(event.data); break;
    case 'verdict':         onVerdict(event.data); break;
    case 'bug_logged':      onBugLogged(event.data); break;
    case 'step_finished':   onStepFinished(event.data); break;
    case 'metrics':         onMetrics(event.data); break;
    case 'run_finished':    onRunFinished(event.data); break;
    case 'run_error':       onRunError(event.data); break;
  }
}

// ── Event Handlers ────────────────────────────────────────────────────────────
function onPlanCreated(data) {
  planningOverlay.style.display = 'none';
  const steps = data.steps || [];
  const criteria = data.criteria || [];

  document.getElementById('plan-status').textContent = `${steps.length} Steps`;
  updateProgress(0, steps.length);

  // Criteria
  const cList = document.getElementById('criteria-list');
  cList.innerHTML = '';
  criteria.forEach(c => {
    const item = document.createElement('div');
    item.className = 'crit-item';
    item.id = `crit-${c.id}`;
    item.innerHTML = `<span class="crit-tag">${esc(c.id)}</span> <span>${esc(c.text)}</span>`;
    cList.appendChild(item);
    criteriaMap[c.id] = item;
  });

  // Steps
  const sList = document.getElementById('steps-list');
  sList.innerHTML = '';
  steps.forEach(s => {
    const card = document.createElement('div');
    card.className = 'step-card';
    card.id = `step-${s.id}`;
    card.innerHTML = `
      <div class="step-head">
        <div class="step-left">
          <span class="step-num">${esc(s.id)}</span>
          <span class="step-desc">${esc(s.instruction)}</span>
        </div>
        <span class="step-status-icon">⏳</span>
      </div>
      <div class="step-info-block">
        <div><strong>Expected:</strong> ${esc(s.expected)}</div>
      </div>
    `;
    sList.appendChild(card);
    stepCards[s.id] = card;
  });

  logEntry('info', `📋 Active plan: ${steps.length} steps, ${criteria.length} criteria`);
}

function onStepStarted(data) {
  planningOverlay.style.display = 'none';
  const card = stepCards[data.id];
  if (card) {
    card.className = 'step-card running';
    const icon = card.querySelector('.step-status-icon');
    if (icon) icon.innerHTML = '<span class="spinner"></span>';
  }
  setStatus('running', `Running ${data.id}…`);
  logEntry('info', `▶ [${data.id}] ${data.instruction}`);
}

function onFrame(data) {
  if (!data || !data.jpeg_b64) return;
  frameImg.src = 'data:image/jpeg;base64,' + data.jpeg_b64;

  frameCount++;
  const now = Date.now();
  if (now - lastFrameTs > 1000) {
    liveIndicator.classList.add('active');
    liveText.textContent = `LIVE (${frameCount} FPS)`;
    frameCount = 0;
    lastFrameTs = now;
  }

  if (data.url && data.url !== 'about:blank') {
    omnibarUrl.textContent = data.url;
    document.getElementById('omnibar-lock').textContent = data.url.startsWith('https://') ? '🔒' : '🌐';
  }
  if (data.title) {
    omnibarTitle.textContent = data.title;
  }
}

function triggerFrameRefresh() {
  logEntry('info', 'Refreshing browser frame...');
}

// Accurate Aspect-Ratio Letterbox Bounding Box Computation
function getRenderedImageRect(img) {
  const cw = img.clientWidth;
  const ch = img.clientHeight;
  const nw = img.naturalWidth || 1280;
  const nh = img.naturalHeight || 800;

  if (currentFitMode === 'fill') {
    return { width: cw, height: ch, left: 0, top: 0, scaleX: cw / nw, scaleY: ch / nh };
  }

  const imgRatio = nw / nh;
  const elemRatio = cw / ch;
  let rw, rh, rx, ry;

  if (elemRatio > imgRatio) {
    rh = ch;
    rw = ch * imgRatio;
    rx = (cw - rw) / 2;
    ry = 0;
  } else {
    rw = cw;
    rh = cw / imgRatio;
    rx = 0;
    ry = (ch - rh) / 2;
  }
  return { width: rw, height: rh, left: rx, top: ry, scaleX: rw / nw, scaleY: rh / nh };
}

function onActionExecuted(data) {
  const a = data.action;
  actionLabel.textContent = `${a.type.toUpperCase()} ${a.element_idx !== undefined ? '[' + a.element_idx + ']' : ''}: ${a.reasoning || ''}`;
  actionToast.style.display = 'flex';

  if (data.bbox && data.bbox.length === 4) {
    const [x, y, w, h] = data.bbox;
    const rect = getRenderedImageRect(frameImg);

    const scaledX = rect.left + (x * rect.scaleX);
    const scaledY = rect.top + (y * rect.scaleY);
    const scaledW = w * rect.scaleX;
    const scaledH = h * rect.scaleY;

    bboxRect.setAttribute('x', scaledX);
    bboxRect.setAttribute('y', scaledY);
    bboxRect.setAttribute('width', scaledW);
    bboxRect.setAttribute('height', scaledH);

    bboxRectBg.setAttribute('x', scaledX);
    bboxRectBg.setAttribute('y', scaledY);
    bboxRectBg.setAttribute('width', scaledW);
    bboxRectBg.setAttribute('height', scaledH);

    const labelY = Math.max(18, scaledY - 4);
    bboxLabelGroup.setAttribute('transform', `translate(${scaledX}, ${labelY - 16})`);
    const labelStr = `${a.type.toUpperCase()} ${a.element_idx !== undefined ? '[' + a.element_idx + ']' : ''}`;
    bboxLabelText.textContent = labelStr;
    const labelWidth = Math.max(52, labelStr.length * 7 + 10);
    bboxLabelBg.setAttribute('width', labelWidth);

    bboxGroup.style.display = '';

    if (bboxHideTimer) clearTimeout(bboxHideTimer);
    bboxHideTimer = setTimeout(() => {
      bboxGroup.style.display = 'none';
      actionToast.style.display = 'none';
    }, 2800);
  }

  logEntry('action', `  ⚡ ${a.type.toUpperCase()} ${a.element_idx !== undefined ? '[' + a.element_idx + ']' : ''} — ${a.reasoning || ''}`);
}

function onVerdict(data) {
  const v = data.verdict;
  const card = stepCards[data.step_id];
  if (card) {
    const icons = { pass: '✅', fail: '❌', blocked: '⚠️', unsure: '❓' };
    const iconEl = card.querySelector('.step-status-icon');
    if (iconEl) iconEl.textContent = icons[v.status] || '?';

    let quote = card.querySelector('.step-verdict-quote');
    if (!quote) {
      quote = document.createElement('div');
      quote.className = 'step-verdict-quote';
      card.querySelector('.step-info-block').appendChild(quote);
    }
    quote.textContent = v.evidence;
  }

  const tagCls = v.status === 'pass' ? 'tag-pass' : v.status === 'fail' ? 'tag-fail' : 'tag-warn';
  logEntry(tagCls, `  [${v.status.toUpperCase()}] ${v.evidence}`);
}

function onBugLogged(data) {
  const card = stepCards[data.step_id];
  if (card) {
    card.className = 'step-card fail';
    const icon = card.querySelector('.step-status-icon');
    if (icon) icon.textContent = '❌';

    const bugDiv = document.createElement('div');
    bugDiv.className = 'defect-card';
    const sev = (data.severity || 'major').toLowerCase();

    bugDiv.innerHTML = `
      <div class="defect-header">
        <span class="defect-title">🐛 Defect: ${esc(data.title)}</span>
        <span class="defect-sev">${esc(sev)}</span>
      </div>
      <div class="defect-actual"><strong>Discrepancy:</strong> ${esc(data.actual)}</div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;">
        ${data.screenshot_annotated ? `
        <button class="apple-btn apple-btn-secondary apple-btn-xs" onclick="openLightbox('${data.screenshot_annotated}', '${esc(data.title)}')">
          🔍 Annotated Defect Image
        </button>` : '<div></div>'}
        <button class="apple-btn apple-btn-primary apple-btn-xs" onclick="exportGitHub('${data.id}')">
          🐙 GitHub Issue
        </button>
      </div>
    `;
    card.appendChild(bugDiv);
  }

  if (data.criterion_id && criteriaMap[data.criterion_id]) {
    criteriaMap[data.criterion_id].style.borderColor = 'rgba(255, 59, 48, 0.4)';
    criteriaMap[data.criterion_id].style.background = '#fff5f5';
  }

  logEntry('bug', `🐛 Defect Recorded: ${data.title} (${data.severity})`);
}

// Provide Screenshots for BOTH Success and Failure cases
function onStepFinished(data) {
  const card = stepCards[data.step_id];
  if (card) {
    const verdict = data.verdict || 'pass';
    if (!card.classList.contains('fail')) {
      card.className = `step-card ${verdict}`;
    }

    // Embed Visual Evidence Thumbnails for EVERY step (Pass or Fail)
    if (data.screenshot_before || data.screenshot_after) {
      let gallery = card.querySelector('.step-evidence-gallery');
      if (!gallery) {
        gallery = document.createElement('div');
        gallery.className = 'step-evidence-gallery';
        card.appendChild(gallery);
      }
      gallery.innerHTML = `
        ${data.screenshot_before ? `
        <button class="evidence-thumb-btn" onclick="openLightbox('${data.screenshot_before}', 'Step ${data.step_id} — Before Action')">
          <img src="/${data.screenshot_before.replace(/^\//,'')}" alt="Before">
          <span>📷 Before Action</span>
        </button>` : ''}
        ${data.screenshot_after ? `
        <button class="evidence-thumb-btn" onclick="openLightbox('${data.screenshot_after}', 'Step ${data.step_id} — After Verification')">
          <img src="/${data.screenshot_after.replace(/^\//,'')}" alt="After">
          <span>📸 After Verification</span>
        </button>` : ''}
      `;
    }
  }

  completedSteps++;
  updateProgress(completedSteps, totalSteps || completedSteps);
  actionToast.style.display = 'none';
}

function onMetrics(data) {
  document.getElementById('provider-name').textContent = `${data.provider} · ${data.fast_model}`;
}

function onRunFinished(data) {
  const verdict = data.status || 'APPROVED';
  const cls = verdict === 'APPROVED' ? 'pass' : verdict === 'REJECTED' ? 'fail' : 'running';
  setStatus(cls, verdict);
  resetRunButtons();

  if (currentRunId) {
    const sec = document.getElementById('report-section');
    document.getElementById('report-link').href = `/api/runs/${currentRunId}/report`;
    document.getElementById('report-pdf-link').href = `/api/runs/${currentRunId}/download/pdf`;
    document.getElementById('report-html-link').href = `/api/runs/${currentRunId}/download/html`;
    document.getElementById('report-status-text').textContent =
      verdict === 'APPROVED' ? 'All criteria passed successfully' : 'Validation finished with defects';
    sec.style.display = 'flex';
  }
  logEntry('info', `✅ Run complete: ${verdict}`);
}

// Handle AI Stuck or LLM Not Responding
function onRunError(data) {
  const errCode = data.error || 'error';
  const msg = data.message || 'Execution error encountered';
  resetRunButtons();

  if (errCode === 'ai_stuck') {
    setStatus('fail', 'Stalled');
    showAlert('AI Execution Halted', msg);
    logEntry('error', '⚠️ ' + msg);
  } else if (errCode === 'llm_not_responding' || errCode === 'step_timeout') {
    setStatus('fail', 'Timeout');
    showAlert('LLM Provider Unresponsive', msg);
    logEntry('error', '⚠️ ' + msg);
  } else if (errCode === 'auth_failed') {
    setStatus('fail', 'Auth Failed');
    showAlert('Authentication Error', 'Invalid test credentials. Please verify your login credentials.');
    logEntry('error', '❌ Authentication failed');
  } else {
    setStatus('fail', 'Error');
    showAlert('Execution Error', msg);
    logEntry('error', '❌ Error: ' + msg);
  }
}

// ── Viewport & Lightbox Controls ──────────────────────────────────────────────
function toggleFitMode() {
  if (currentFitMode === 'fit') {
    currentFitMode = 'fill';
    viewportCanvas.className = 'viewport-canvas mode-fill';
    fitBtn.textContent = '↔ Fill';
  } else if (currentFitMode === 'fill') {
    currentFitMode = 'actual';
    viewportCanvas.className = 'viewport-canvas mode-actual';
    fitBtn.textContent = '1:1 Actual';
  } else {
    currentFitMode = 'fit';
    viewportCanvas.className = 'viewport-canvas';
    fitBtn.textContent = '⤢ Fit';
  }
}

function toggleTheaterMode() {
  isTheaterMode = !isTheaterMode;
  browserSection.classList.toggle('theater-mode', isTheaterMode);
  theaterBtn.textContent = isTheaterMode ? '✕ Normal' : '⛶ Maximize';
}

function openLightbox(src, title) {
  lightboxImg.src = '/' + src.replace(/^\//, '');
  lightboxTitle.textContent = title || 'Visual Evidence';
  lightboxModal.classList.add('open');
}

function closeLightbox(e) {
  if (e && e.target !== lightboxModal && !e.target.classList.contains('lightbox-close')) return;
  lightboxModal.classList.remove('open');
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeLightbox();
    if (isTheaterMode) toggleTheaterMode();
  }
});

// ── Activity drawer ───────────────────────────────────────────────────────────
function toggleDrawer() {
  const drawer = document.getElementById('bottom-drawer');
  const btn = document.getElementById('drawer-toggle-btn');
  drawer.classList.toggle('minimized');
  btn.textContent = drawer.classList.contains('minimized') ? '▲' : '▼';
}

function logEntry(type, text) {
  logCounter++;
  logCountBadge.textContent = logCounter;

  const row = document.createElement('div');
  row.className = 'log-row';

  const ts = new Date().toLocaleTimeString();
  const tagClass = type.startsWith('tag-') ? type : `tag-${type}`;
  const tagLabel = type.replace(/^tag-/, '').toUpperCase();

  row.innerHTML = `
    <span class="log-time">${ts}</span>
    <span class="log-tag ${tagClass}">${tagLabel}</span>
    <span class="log-body">${esc(text)}</span>
  `;
  agentLog.appendChild(row);

  if (autoscrollCb.checked) {
    agentLog.scrollTop = agentLog.scrollHeight;
  }
}

function clearLogs() {
  agentLog.innerHTML = '';
  logCounter = 0;
  logCountBadge.textContent = '0';
}

function copyLogs() {
  const text = Array.from(agentLog.querySelectorAll('.log-row'))
    .map(el => el.innerText)
    .join('\n');
  navigator.clipboard.writeText(text).then(() => {
    alert('📋 Logs copied to clipboard.');
  });
}

function setStatus(cls, text) {
  const pill = document.getElementById('status-pill');
  const txt = document.getElementById('status-text');
  pill.className = 'status-chip ' + cls;
  txt.textContent = text;
}

async function exportGitHub(bugId) {
  try {
    const res = await fetch(`/api/bugs/${bugId}/github`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      alert('✅ GitHub issue created: ' + data.issue_url);
      window.open(data.issue_url, '_blank');
      logEntry('info', `🐙 Created issue: ${data.issue_url}`);
    } else {
      alert('❌ ' + (data.detail || 'GitHub export failed'));
    }
  } catch (e) {
    alert('❌ ' + e.message);
  }
}

function esc(str) {
  return String(str || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
