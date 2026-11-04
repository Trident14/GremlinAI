/* GremlinAI — UI polish: presets, running state, verdict overlay, confetti */
(function () {
  const $ = (id) => document.getElementById(id);

  // ---- Preset stories (one-click demos) ----
  const PRESETS = {
    saucedemo: {
      url: 'https://www.saucedemo.com',
      story: 'As a shopper, I can log in, add a backpack to my cart, and see it in the cart.\n' +
             'AC1: Valid login opens the products page.\n' +
             'AC2: Adding the backpack updates the cart badge to 1.\n' +
             'AC3: The cart page lists the backpack.',
      user: 'standard_user', pass: 'secret_sauce', login: 'https://www.saucedemo.com',
    },
    herokuapp: {
      url: 'https://the-internet.herokuapp.com/login',
      story: 'As a user, I can log in with valid credentials.\n' +
             'AC1: Valid login shows a success message.\n' +
             'AC2: A secure area page is displayed after login.',
      user: 'tomsmith', pass: 'SuperSecretPassword!', login: 'https://the-internet.herokuapp.com/login',
    },
  };
  const DEMO_AUTH = { user: 'demo@gremlin.test', pass: 'Passw0rd!' };
  const DEMO = {
    demo: { path: '', story: 'As a shopper, I can log in, add a Gremlin Mug to my cart, and check out.\n' +
      'AC1: Valid login opens the product list.\nAC2: Adding the mug updates the cart badge to 1.\n' +
      'AC3: Checkout requires a shipping address (placing an order with an empty address must show an error).\n' +
      'AC4: A valid order shows an order confirmation.', login: true },
    clean: { path: '?bugs=off', story: 'As a shopper, I can log in, add a Gremlin Mug to my cart, and check out.\n' +
      'AC1: Valid login opens the product list.\nAC2: Adding the mug updates the cart badge to 1.\n' +
      'AC3: Checkout requires a shipping address (placing an order with an empty address must show an error).\n' +
      'AC4: A valid order shows an order confirmation.', login: true },
    signup: { path: 'signup.html', story: 'As a new visitor, I can create an account.\n' +
      'AC1: Entering an invalid email such as abc shows a validation error.\nAC2: A valid name, email, country, password and accepted terms shows a welcome message.', login: false },
  };
  window.loadPreset = function (key) {
    if (DEMO[key]) {
      const d = DEMO[key], base = window.__demoUrl || (location.origin + '/demo/');
      $('url-input').value = base + d.path; $('story-input').value = d.story;
      if (d.login) {
        $('auth-username').value = DEMO_AUTH.user; $('auth-password').value = DEMO_AUTH.pass;
        $('auth-login-url').value = base + d.path; $('auth-prelogin').checked = false; $('auth-disclosure').open = true;
      } else { $('auth-prelogin').checked = false; $('auth-username').value = ''; $('auth-password').value = ''; }
      return;
    }
    const p = PRESETS[key]; if (!p) return;
    $('url-input').value = p.url;
    $('story-input').value = p.story;
    $('auth-username').value = p.user; $('auth-password').value = p.pass;
    $('auth-login-url').value = p.login;
    $('auth-prelogin').checked = false; $('auth-disclosure').open = true;
  };

  // ---- Show the real provider / model / price in the header ----
  fetch('/api/info').then(r => r.json()).then(i => {
    window.__demoUrl = i.demo_url;
    const el = $('provider-name'); if (!el) return;
    el.textContent = i.fast_provider + ' · ' + i.fast_model;
    const p = i.fast_price;
    $('provider-badge').title = 'Fast: ' + i.fast_model + ' | Vision: ' + i.vision_model +
      (p ? ' | $' + p.input_per_1m + ' in / $' + p.output_per_1m + ' out per 1M tokens' : ' | price unknown (add to pricing.json)');
  }).catch(() => {});

  // ---- Insert preset chips under the story box ----
  const story = $('story-input');
  if (story) {
    const row = document.createElement('div');
    row.className = 'preset-row';
    row.innerHTML = '<span class="label-hint">Try a preset:</span>' +
      '<button type="button" class="preset-chip" onclick="loadPreset(\'demo\')">Demo shop (with bugs)</button>' +
      '<button type="button" class="preset-chip" onclick="loadPreset(\'clean\')">Demo shop (clean)</button>' +
      '<button type="button" class="preset-chip" onclick="loadPreset(\'signup\')">Demo signup form</button>' +
      '<button type="button" class="preset-chip" onclick="loadPreset(\'saucedemo\')">Sauce Demo</button>' +
      '<button type="button" class="preset-chip" onclick="loadPreset(\'herokuapp\')">Login page</button>';
    story.parentElement.appendChild(row);
  }

  // ---- Hide hero while running (watch status chip) ----
  const pill = $('status-pill');
  if (pill) {
    const sync = () => { if (pill.classList.contains('running')) document.body.classList.add('is-running'); };
    new MutationObserver(sync).observe(pill, { attributes: true, attributeFilter: ['class'] });
  }

  // ---- Verdict overlay ----
  function confetti() {
    const colors = ['#3155f6', '#10162f', '#7890ff', '#69708a'];
    for (let i = 0; i < 90; i++) {
      const c = document.createElement('i');
      c.className = 'confetti';
      c.style.left = Math.random() * 100 + 'vw';
      c.style.background = colors[i % colors.length];
      c.style.setProperty('--dx', (Math.random() * 160 - 80) + 'px');
      c.style.animationDuration = 2.2 + Math.random() * 1.8 + 's';
      c.style.animationDelay = Math.random() * 0.6 + 's';
      document.body.appendChild(c);
      setTimeout(() => c.remove(), 5000);
    }
  }
  window.closeVerdict = () => $('verdict-overlay').classList.remove('show');

  async function showVerdict(data) {
    const verdict = (data && data.status) || 'APPROVED';
    let passed = 0, total = 0, bugs = 0;
    try {
      const r = await fetch('/api/runs/' + data.run_id);
      const j = await r.json();
      total = (j.step_results || []).length;
      passed = (j.step_results || []).filter(s => s.verdict_status === 'pass').length;
      bugs = (j.bug_reports || []).length;
    } catch (e) { /* overlay still works without stats */ }
    const pct = total ? Math.round(passed / total * 100) : 0;
    const key = verdict === 'APPROVED' ? 'APPROVED' : verdict === 'REJECTED' ? 'REJECTED' : 'NEEDS';
    const colors = { APPROVED: '#15803d', REJECTED: '#d92d20', NEEDS: '#3155f6' };
    const ring = $('verdict-ring');
    ring.style.setProperty('--c', colors[key]);
    ring.dataset.label = pct + '%';
    ring.style.setProperty('--p', 0);
    $('verdict-word').textContent = verdict;
    $('verdict-word').className = 'verdict-word ' + key;
    $('verdict-sub').textContent = key === 'APPROVED' ? 'All acceptance criteria passed.' :
      key === 'REJECTED' ? 'Defects found. See the bug reports.' : 'Some steps need human review.';
    $('vs-passed').textContent = passed + '/' + total;
    $('vs-bugs').textContent = bugs;
    $('verdict-report').href = '/api/runs/' + data.run_id + '/report';
    $('verdict-dl-pdf').href = '/api/runs/' + data.run_id + '/download/pdf';
    $('verdict-dl-html').href = '/api/runs/' + data.run_id + '/download/html';
    $('verdict-overlay').classList.add('show');
    // animate ring (animate via JS: CSS can't transition conic angle without @property)
    let cur = 0; const t = setInterval(() => {
      cur = Math.min(pct, cur + 3); ring.style.setProperty('--p', cur);
      if (cur >= pct) clearInterval(t);
    }, 20);
    if (key === 'APPROVED') confetti();
  }

  // Wrap the existing handler (handleEvent resolves onRunFinished at call time)
  const orig = window.onRunFinished;
  if (typeof orig === 'function') {
    window.onRunFinished = function (data) { orig(data); showVerdict(data); };
  }
})();
