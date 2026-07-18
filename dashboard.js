// ── XSS escape ────────────────────────────────────────────────────────────────
function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#039;');
}

// Only allow http(s) URLs into an href. esc() escapes < > & " ' but NOT the
// scheme, so a `javascript:` / `data:` URL from an external feed (news links,
// Reddit posts) would survive esc() and execute same-origin JS if clicked —
// which could reach the loopback control/config endpoints. Anything not
// http(s) collapses to '' so the link renders inert.
function safeUrl(u) {
  const s = String(u == null ? '' : u).trim();
  return /^https?:\/\//i.test(s) ? s : '';
}

// ── State ─────────────────────────────────────────────────────────────────────
let allSetups = [], allHistory = [], allDecisions = [];
let setupFilter = 'all', histFilter = 'all', decFilter = 'all';
let setupSort = 'recent';   // scanner board order: 'recent' (newest found) | 'score' (highest score)
let pnlChart = null, filterChart = null;
let equityChart = null, scoreChart = null, rejectChart = null, holdChart = null;
let refreshTimer = null, currentState = null;

// ── Tabs ──────────────────────────────────────────────────────────────────────
function showTab(name, btn) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'history')    renderHistory();
  if (name === 'decisions')  renderDecisions();
  if (name === 'analytics')  renderAnalytics();
  if (name === 'strategies') renderStrategies(currentState);
  if (name === 'news')       { loadNews(); renderNews(); }
  if (name === 'social')     { loadSocial(); renderSocial(); }
}

// Glossary tab is static HTML; this just filters rows by the search box.
function filterGlossary(q) {
  q = (q || '').trim().toLowerCase();
  document.querySelectorAll('#tab-glossary .gl-sec').forEach(sec => {
    let any = false;
    sec.querySelectorAll('.gl-row').forEach(row => {
      const match = !q || row.textContent.toLowerCase().includes(q);
      row.style.display = match ? '' : 'none';
      if (match) any = true;
    });
    sec.style.display = any ? '' : 'none';
  });
}

// ── Settings ──────────────────────────────────────────────────────────────────
function openSettings() {
  if (currentState) {
    document.getElementById('sAccMode').textContent   = currentState.account_mode || '—';
    document.getElementById('sBotStatus').textContent = currentState.bot_status   || '—';
  }
  const saved = localStorage.getItem('theme') || 'auto';
  document.querySelectorAll('#themeBtns .theme-btn').forEach(b => b.classList.toggle('on', b.textContent.toLowerCase() === saved));
  document.getElementById('refreshSel').value = localStorage.getItem('refreshInterval') || '30000';
  renderConfigForm();
  document.getElementById('settingsBd').classList.add('open');
}
function closeSettings() { document.getElementById('settingsBd').classList.remove('open'); }
function onBdClick(e)    { if (e.target === document.getElementById('settingsBd')) closeSettings(); }

function setTheme(theme, btn) {
  localStorage.setItem('theme', theme);
  document.documentElement.setAttribute('data-theme', theme);
  document.querySelectorAll('#themeBtns .theme-btn').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
}

function applyRefresh() {
  const val = parseInt(document.getElementById('refreshSel').value);
  localStorage.setItem('refreshInterval', String(val));
  clearInterval(refreshTimer);
  if (val > 0) refreshTimer = setInterval(loadState, val);
}

function initSettings() {
  const theme = localStorage.getItem('theme') || 'auto';
  document.documentElement.setAttribute('data-theme', theme);
  const ri = parseInt(localStorage.getItem('refreshInterval') || '30000');
  if (ri > 0) refreshTimer = setInterval(loadState, ri);
}

// ── Trading configuration (account-size tiers + overrides) ──────────────────────
let cfgSelectedTier = 'auto';

// Fields shown in the form. pct:true means the config stores a fraction (0.03)
// but the input shows a percent (3).
const CFG_FIELDS = [
  {id:'cfgRisk',   key:'risk_per_trade_pct',    pct:true},
  {id:'cfgMaxPos', key:'max_position_size_pct', pct:true},
  {id:'cfgLoss',   key:'daily_loss_limit_pct',  pct:true},
  {id:'cfgSwing',  key:'max_swing_positions',   pct:false},
  {id:'cfgDay',    key:'max_day_positions',     pct:false},
];
const CFG_STRATS = [
  ['catalyst_long_call','Catalyst'], ['iv_rank','IV-Rank'],
  ['hft_intraday','Day (HFT)'], ['pead','PEAD'], ['bounce','Bounce'],
];
const CFG_TIER_HINTS = {
  micro:    'Under $5k — concentrate: IV-Rank + PEAD only, 2 swing slots, no day-trading.',
  small:    '$5k–$25k — IV-Rank, PEAD, Bounce; 4 swing slots, no day-trading (PDT rule).',
  standard: '$25k+ — all five strategies, full position book (same as the original bot).',
};

function cfgData() { return (currentState && currentState.config) || {}; }
function cfgPresetTier() {
  const c = cfgData();
  return cfgSelectedTier === 'auto' ? (c.effective && c.effective.auto_tier) : cfgSelectedTier;
}

function renderConfigForm() {
  const c = cfgData();
  const eff = c.effective;
  const line = document.getElementById('cfgAcctLine');
  if (!eff) {
    line.innerHTML = '<span class="mu">Start the bot (and serve via dashboard_server.py) to load tier config.</span>';
    return;
  }
  const eq = (currentState.account && currentState.account.equity) || 0;
  cfgSelectedTier = (c.raw && c.raw.tier) || 'auto';
  line.innerHTML = 'Account <b>$' + Number(eq).toLocaleString() + '</b> · auto tier <b>' +
    esc(eff.auto_tier) + '</b> · active <b>' + esc(eff.tier) + '</b> (' + esc(eff.tier_source) + ')';
  document.querySelectorAll('#tierBtns .theme-btn').forEach(b =>
    b.classList.toggle('on', b.dataset.tier === cfgSelectedTier));
  const sc = document.getElementById('cfgStrats');
  if (!sc.dataset.built) {
    sc.innerHTML = CFG_STRATS.map(([k,lbl]) =>
      '<label><input type="checkbox" value="'+esc(k)+'" id="strat_'+esc(k)+'"> '+esc(lbl)+'</label>').join('');
    sc.dataset.built = '1';
  }
  fillConfigFields(eff);   // show the live effective values (tier + any overrides)
  renderAlerts();
}

function renderAlerts() {
  const al = cfgData().alerts || {enabled:true, min_score:60, strategies:CFG_STRATS.map(([k])=>k), watchlist:[]};
  // Channel status comes from the separate state.alerts (channel_status()).
  const ch = (currentState && currentState.alerts) || {};
  const names = [['telegram','Telegram'],['discord','Discord'],['email','Email'],['sms','SMS']];
  const on = names.filter(([k]) => ch[k]).map(([,lbl]) => lbl);
  document.getElementById('alertChannels').innerHTML = on.length
    ? 'Channels connected: <b>' + on.map(esc).join(', ') + '</b>'
    : '<span class="mu">No alert channels configured — set Telegram / Discord / email in .env.</span>';
  const sc = document.getElementById('alStrats');
  if (!sc.dataset.built) {
    sc.innerHTML = CFG_STRATS.map(([k,lbl]) =>
      '<label><input type="checkbox" value="'+esc(k)+'" id="alstrat_'+esc(k)+'"> '+esc(lbl)+'</label>').join('');
    sc.dataset.built = '1';
  }
  document.getElementById('alEnabled').checked = al.enabled !== false;
  document.getElementById('alMinScore').value  = (al.min_score != null ? al.min_score : 60);
  const en = al.strategies || CFG_STRATS.map(([k])=>k);
  CFG_STRATS.forEach(([k]) => { const cb = document.getElementById('alstrat_'+k); if (cb) cb.checked = en.includes(k); });
  document.getElementById('alWatchlist').value = (al.watchlist || []).join(', ');
}

function buildAlertsPayload() {
  return {
    enabled:   document.getElementById('alEnabled').checked,
    min_score: parseInt(document.getElementById('alMinScore').value) || 0,
    strategies: CFG_STRATS.map(([k])=>k)
      .filter(k => { const cb = document.getElementById('alstrat_'+k); return cb && cb.checked; }),
    watchlist: document.getElementById('alWatchlist').value
      .split(/[,\s]+/).map(s => s.trim().toUpperCase()).filter(Boolean),
  };
}

function fillConfigFields(src) {
  CFG_FIELDS.forEach(f => {
    const el = document.getElementById(f.id);
    const v = src[f.key];
    if (el && v != null) el.value = f.pct ? +(v*100).toFixed(2) : v;
  });
  const enabled = src.enabled_strategies || [];
  CFG_STRATS.forEach(([k]) => {
    const cb = document.getElementById('strat_'+k);
    if (cb) cb.checked = enabled.includes(k);
  });
  const mo = document.getElementById('cfgMinOne');
  if (mo) mo.checked = !!src.min_one_contract;
  updateTierHint();
}

function setTier(name, btn) {
  cfgSelectedTier = name;
  document.querySelectorAll('#tierBtns .theme-btn').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  // Reset the fields to the chosen tier's preset — overrides don't carry across
  // tiers. (Auto → the tier the current equity resolves to.)
  const c = cfgData();
  const preset = c.tiers && c.tiers[cfgPresetTier()];
  if (preset) fillConfigFields(preset);
  updateTierHint();
}

function updateTierHint() {
  const t = cfgPresetTier();
  document.getElementById('tierHint').textContent =
    (cfgSelectedTier === 'auto' ? 'Auto-selects by equity → ' : 'Pinned. ') + (CFG_TIER_HINTS[t] || '');
}

function buildConfigPayload() {
  const c = cfgData();
  const preset = (c.tiers && c.tiers[cfgPresetTier()]) || {};
  const overrides = {};
  // Send only values that DIFFER from the tier preset, so untouched fields keep
  // tracking the tier instead of being frozen.
  CFG_FIELDS.forEach(f => {
    const el = document.getElementById(f.id);
    if (!el || el.value === '') return;
    let v = parseFloat(el.value);
    if (isNaN(v)) return;
    if (f.pct) v = +(v/100).toFixed(4);
    const base = preset[f.key];
    if (base == null || Math.abs(base - v) > 1e-9) overrides[f.key] = v;
  });
  const chosen = CFG_STRATS.map(([k]) => k)
    .filter(k => { const cb = document.getElementById('strat_'+k); return cb && cb.checked; });
  const baseEn = (preset.enabled_strategies || []).slice().sort().join(',');
  if (chosen.slice().sort().join(',') !== baseEn) overrides.enabled_strategies = chosen;
  const minOne = document.getElementById('cfgMinOne');
  if (minOne && !!preset.min_one_contract !== minOne.checked) overrides.min_one_contract = minOne.checked;
  return { tier: cfgSelectedTier, overrides, alerts: buildAlertsPayload() };
}

async function saveConfig() {
  const status = document.getElementById('cfgStatus');
  status.className = 'cfg-status';
  status.textContent = 'Saving…';
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(buildConfigPayload()),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok || !j.ok) throw new Error(j.error || ('HTTP ' + res.status));
    status.className = 'cfg-status ok';
    status.textContent = '✓ Saved — applies on the next scan cycle.';
    setTimeout(loadState, 800);
  } catch (e) {
    status.className = 'cfg-status err';
    status.textContent = '✕ ' + (e.message || 'save failed') + ' (needs dashboard_server.py, not http.server).';
  }
}

// ── Load state ────────────────────────────────────────────────────────────────
async function loadState() {
  try {
    const res = await fetch('dashboard_state.json?t=' + Date.now());
    if (!res.ok) throw new Error('not found');
    const s = await res.json();
    currentState = s;
    renderState(s);
  } catch(e) {
    document.getElementById('statusText').textContent = 'State file not found';
    document.getElementById('lastUpdate').textContent = 'Run the bot to generate dashboard_state.json';
  }
  loadNews();   // independent fetch — news refreshes even between state writes
  loadSocial(); // same for social sentiment
}

// ── News feed ────────────────────────────────────────────────────────────────
// news_feed.json is written by the news monitor every ~60s. Fetched directly
// (not via dashboard_state) so a fresh headline shows on the next poll instead
// of waiting for the next executor cycle. Falls back to the slice embedded in
// dashboard_state when the file isn't there yet.
let newsItems = [], newsFilter = 'all', newsSource = 'all';

async function loadNews() {
  try {
    const res = await fetch('news_feed.json?t=' + Date.now());
    if (res.ok) {
      const data = await res.json();
      if (Array.isArray(data)) newsItems = data;
    }
  } catch(e) { /* fall through to embedded copy */ }
  if (!newsItems.length && currentState && Array.isArray(currentState.news)) {
    newsItems = currentState.news;
  }
  if (document.getElementById('tab-news').classList.contains('active')) renderNews();
}

function filterNews(type, btn) {
  newsFilter = type;
  document.querySelectorAll('#newsChips .chip').forEach(c => c.classList.remove('on'));
  btn.classList.add('on');
  renderNews();
}

function filterNewsSource(feed, btn) {
  newsSource = feed;
  document.querySelectorAll('#newsSourceChips .chip').forEach(c => c.classList.remove('on'));
  btn.classList.add('on');
  renderNews();
}

function newsAge(ts) {
  const mins = Math.floor((Date.now() / 1000 - (ts || 0)) / 60);
  if (mins < 1)    return 'just now';
  if (mins < 60)   return mins + 'm ago';
  if (mins < 1440) return Math.floor(mins / 60) + 'h ago';
  return Math.floor(mins / 1440) + 'd ago';
}

function renderNews() {
  const listEl = document.getElementById('newsList');
  if (!listEl) return;

  const asOfEl = document.getElementById('newsAsOf');
  if (asOfEl) asOfEl.textContent = newsItems.length ? `${newsItems.length} headlines` : '';

  let list = newsItems;
  if (newsFilter === 'high') list = list.filter(n => n.impact === 'high');
  else if (newsFilter !== 'all') list = list.filter(n => n.category === newsFilter);

  if (newsSource !== 'all') list = list.filter(n => (n.feed || '') === newsSource);

  const q = (document.getElementById('newsSearch')?.value || '').trim().toUpperCase();
  if (q) list = list.filter(n => (n.ticker || '').toUpperCase().includes(q));

  if (!list.length) {
    listEl.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:12px 0">' +
      'No headlines yet — the news monitor collects every ~60s while running.</div>';
    return;
  }

  const catLabel = { product: 'Product', people: 'Hiring/Firing', ma: 'M&A',
                     earnings: 'Earnings', regulatory: 'Regulatory',
                     analyst: 'Analyst', partnership: 'Partnership',
                     macro: 'Macro', general: 'General' };
  const impactCls = { high: 'b-red', medium: 'b-amber', low: 'b-gray' };

  listEl.innerHTML = list.slice(0, 100).map(n => {
    const senti = (n.sentiment || 0) > 0 ? '<span class="badge b-green">bullish</span>'
                : (n.sentiment || 0) < 0 ? '<span class="badge b-red">bearish</span>' : '';
    const nUrl = safeUrl(n.url);
    const title = nUrl
      ? `<a href="${esc(nUrl)}" target="_blank" rel="noopener noreferrer" style="color:var(--text);text-decoration:none">${esc(n.title)}</a>`
      : esc(n.title || '');
    const moreSrc = (n.source_count || 1) > 1 ? ` <span class="badge b-gray" style="font-size:9px">+${(n.source_count - 1)} more</span>` : '';
    return `<div class="setup-row">
      <div style="min-width:0">
        <div class="setup-sym">${esc(n.ticker || '')}
          <span style="font-size:10px;color:var(--muted);font-weight:400">${newsAge(n.ts)}${n.source ? ' · ' + esc(n.source) : ''}</span>${moreSrc}
        </div>
        <div class="setup-meta" style="color:var(--text2);font-size:12px;margin-top:3px">${title}</div>
        <div class="setup-badges">
          <span class="badge ${impactCls[n.impact] || 'b-gray'}">${esc(n.impact || 'low')}</span>
          <span class="badge b-blue">${esc(catLabel[n.category] || n.category || '')}</span>
          ${senti}
        </div>
      </div>
    </div>`;
  }).join('');
}

// ── Social sentiment ───────────────────────────────────────────────────────────
// social_sentiment.json is written by the news monitor's social sweep (every few
// cycles). Fetched directly like the news feed; falls back to the slice embedded
// in dashboard_state.
let socialItems = [], socialFilter = 'all';

async function loadSocial() {
  try {
    const res = await fetch('social_sentiment.json?t=' + Date.now());
    if (res.ok) {
      const data = await res.json();
      if (Array.isArray(data)) socialItems = data;
    }
  } catch(e) { /* fall through to embedded copy */ }
  if (!socialItems.length && currentState && Array.isArray(currentState.social)) {
    socialItems = currentState.social;
  }
  if (document.getElementById('tab-social').classList.contains('active')) renderSocial();
}

function filterSocial(type, btn) {
  socialFilter = type;
  document.querySelectorAll('#socialChips .chip').forEach(c => c.classList.remove('on'));
  btn.classList.add('on');
  renderSocial();
}

function sentimentBar(bull, bear) {
  const total = bull + bear;
  if (!total) return '<div style="height:8px;border-radius:4px;background:var(--border)"></div>';
  const bp = Math.round(bull / total * 100), rp = 100 - bp;
  return `<div style="display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--border)">
    <div style="width:${bp}%;background:var(--green)"></div>
    <div style="width:${rp}%;background:var(--red)"></div>
  </div>`;
}

function renderSocial() {
  const listEl = document.getElementById('socialList');
  if (!listEl) return;

  const asOfEl = document.getElementById('socialAsOf');
  if (asOfEl) asOfEl.textContent = socialItems.length ? `${socialItems.length} tickers` : '';

  let list = socialItems;
  if (socialFilter !== 'all') list = list.filter(s => (s.net_sentiment || 'neutral') === socialFilter);

  const q = (document.getElementById('socialSearch')?.value || '').trim().toUpperCase();
  if (q) list = list.filter(s => (s.ticker || '').toUpperCase().includes(q));

  if (!list.length) {
    listEl.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:12px 0">' +
      'No social chatter yet — the monitor sweeps Stocktwits &amp; Reddit every few minutes while running.</div>';
    return;
  }

  const netBadge = { bullish: 'b-green', bearish: 'b-red', neutral: 'b-gray' };

  listEl.innerHTML = list.slice(0, 100).map(s => {
    const st = s.stocktwits || {}, rd = s.reddit || {};
    const score = (s.sentiment_score || 0);
    const scoreStr = (score > 0 ? '+' : '') + score.toFixed(2);
    const stLine = (st && (st.messages || 0) > 0)
      ? `<div style="margin-top:6px">
           <div style="display:flex;justify-content:space-between;font-size:10.5px;color:var(--muted);margin-bottom:3px">
             <span>Stocktwits · ${st.messages} msgs</span>
             <span><span style="color:var(--green)">${st.bullish||0} bull</span> · <span style="color:var(--red)">${st.bearish||0} bear</span></span>
           </div>
           ${sentimentBar(st.bullish||0, st.bearish||0)}
         </div>` : '';
    const rdLine = (rd && (rd.mentions || 0) > 0)
      ? `<div style="margin-top:6px;font-size:10.5px;color:var(--muted)">
           Reddit · ${rd.mentions} mention${rd.mentions===1?'':'s'} · sentiment ${(rd.score>0?'+':'')}${(rd.score||0).toFixed(2)}
           ${rd.upvotes ? ' · ' + rd.upvotes + ' upvotes' : ''}${rd.comments ? ' · ' + rd.comments + ' comments' : ''}
         </div>` : '';

    // Top Reddit posts — show the actual discussion, not just counts.
    const posts = (rd && Array.isArray(rd.top_posts)) ? rd.top_posts : [];
    const postSent = { bullish: 'var(--green)', bearish: 'var(--red)', neutral: 'var(--muted)' };
    const postsLine = posts.length
      ? `<div style="margin-top:5px;display:flex;flex-direction:column;gap:3px">
           ${posts.map(p => {
             const t = esc(p.title || '');
             const inner = `<span style="color:${postSent[p.sentiment] || 'var(--muted)'}">●</span> ${t}
               <span style="color:var(--muted)">— r/${esc(p.subreddit||'')} · ⬆${p.ups||0} · 💬${p.comments||0}</span>`;
             const pUrl = safeUrl(p.url);
             return `<div style="font-size:10.5px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${
               pUrl ? `<a href="${esc(pUrl)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">${inner}</a>` : inner
             }</div>`;
           }).join('')}
         </div>` : '';

    // Stock snapshot — company name + price + day change.
    const stk = s.stock || {};
    let stockLine = '';
    if (stk.last != null) {
      const chg = stk.change_pct;
      const chgCol = chg == null ? 'var(--muted)' : (chg >= 0 ? 'var(--green)' : 'var(--red)');
      const chgStr = chg == null ? '' : ` <span style="color:${chgCol}">${chg>=0?'+':''}${chg.toFixed(2)}%</span>`;
      const vol = stk.volume ? ` · vol ${Intl.NumberFormat('en',{notation:'compact'}).format(stk.volume)}` : '';
      stockLine = `<div style="margin-top:3px;font-size:10.5px;color:var(--muted)">
           ${esc(stk.name || s.ticker || '')} · <span style="color:var(--text)">$${stk.last.toFixed(2)}</span>${chgStr}${vol}
         </div>`;
    }

    return `<div class="setup-row">
      <div style="min-width:0;width:100%">
        <div class="setup-sym" style="display:flex;justify-content:space-between;align-items:center">
          <span>${esc(s.ticker || '')}
            <span style="font-size:10px;color:var(--muted);font-weight:400">vol ${s.volume || 0} · score ${scoreStr}</span>
          </span>
          <span class="badge ${netBadge[s.net_sentiment] || 'b-gray'}">${esc(s.net_sentiment || 'neutral')}</span>
        </div>
        ${stockLine}
        ${stLine}
        ${rdLine}
        ${postsLine}
      </div>
    </div>`;
  }).join('');
}

function renderState(s) {
  const acc     = s.account || {};
  const ts       = new Date(s.timestamp);
  const ageMins  = Math.floor((Date.now() - ts.getTime()) / 60000);
  const stateAge = (Date.now() - ts.getTime()) / 1000;   // seconds

  const upEl = document.getElementById('lastUpdate');
  upEl.textContent = ageMins < 1 ? 'Updated just now' : `Updated ${ageMins}m ago`;

  // The "bot may not be running" banner must reflect ACTUAL liveness, not the
  // periodic state-write cadence: the bot only rewrites dashboard_state.json
  // once per scan cycle (≈5 min while open, ≈30 min after hours), so a few-min-
  // old write is normal, not a crash. Judge liveness by the freshest strategy
  // heartbeat (HFT beats ~every 60s, even idle), aged forward by how old this
  // snapshot is. Only warn if the bot has missed its expected cadence by a wide
  // margin (≈ every heartbeat gone silent).
  let intervalSec = s.update_interval_sec;
  if (!intervalSec) intervalSec = (s.bot_status === 'scanning_closed' || s.bot_status === 'idle') ? 1800 : 300;
  const graceSec = intervalSec + 600;   // expected cadence + 10 min slack

  const health = (s.strategy_panel && s.strategy_panel.health) || [];
  const hbAges = health.map(h => h.age_sec).filter(a => a != null);
  const sinceLastBeat = hbAges.length ? Math.min(...hbAges) + stateAge : stateAge;
  const botDown = sinceLastBeat > graceSec;

  document.getElementById('staleBar').style.display = botDown ? 'block' : 'none';

  const statusMap = {
    running:         ['dot-green', 'Running'],
    scanning:        ['dot-amber', 'Scanning'],
    scanning_closed: ['dot-blue',  'Scanning (market closed)'],
    halted:          ['dot-red',   'Halted'],
    idle:            ['dot-gray',  'Market closed'],
    error:           ['dot-red',   'Error'],
  };
  const [dotCls, label] = statusMap[s.bot_status] || ['dot-amber', s.bot_status || 'Unknown'];
  document.getElementById('statusDot').className = 'dot ' + dotCls;
  document.getElementById('statusText').textContent = label;

  document.getElementById('haltBanner').style.display = acc.halted ? 'block' : 'none';

  // Metrics
  document.getElementById('equity').textContent      = '$' + (acc.equity||0).toLocaleString();
  document.getElementById('accountMode').textContent = (s.account_mode||'paper') + ' account';

  const pnl  = acc.daily_pnl || 0;
  const pnlEl = document.getElementById('dayPnl');
  pnlEl.textContent = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toLocaleString();
  pnlEl.className   = 'metric-value ' + (pnl >= 0 ? 'up' : 'dn');
  const pct   = acc.daily_pnl_pct || 0;
  const pctEl = document.getElementById('dayPnlPct');
  pctEl.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '% today';
  pctEl.className   = 'metric-sub ' + (pct >= 0 ? 'up' : 'dn');

  // Total P&L — all-time realized (closed trades) + unrealized (open positions).
  const totEl = document.getElementById('totalPnl');
  const totSub = document.getElementById('totalPnlSub');
  const ps = s.pnl_summary;
  if (!ps) {                                   // pre-restart backend has no pnl_summary yet
    totEl.textContent = '—'; totEl.className = 'metric-value'; totSub.textContent = '';
  } else {
    const money = v => (v >= 0 ? '+$' : '-$') + Math.abs(v||0).toLocaleString('en', {maximumFractionDigits: 0});
    const tot = ps.total || 0;
    totEl.textContent = money(tot);
    totEl.className   = 'metric-value ' + (tot >= 0 ? 'up' : 'dn');
    const pctTxt = ps.total_pct != null ? `${ps.total_pct >= 0 ? '+' : ''}${ps.total_pct.toFixed(1)}% · ` : '';
    totSub.textContent = `${pctTxt}${money(ps.realized)} realized · ${money(ps.unrealized)} open`;
  }

  // Swing and day trades have independent position budgets.
  const swing = acc.swing_positions||0, maxSwing = acc.max_swing||5;
  const day   = acc.day_positions||0,   maxDay   = acc.max_day||5;
  document.getElementById('openPos').textContent    = (acc.open_positions||0) + ' / ' + (acc.max_positions||10);
  document.getElementById('openPosSub').innerHTML   =
    `<span class="b-blue" style="color:var(--blue)">Swing ${swing}/${maxSwing}</span> · ` +
    `<span class="b-purple" style="color:var(--purple)">Day ${day}/${maxDay}</span>`;

  document.getElementById('lossLimit').textContent = '$' + (acc.loss_limit||0).toLocaleString();
  const usedPct  = acc.loss_used_pct || 0;
  const lossUsedEl = document.getElementById('lossUsed');
  lossUsedEl.textContent = '$' + (acc.loss_used||0).toLocaleString() + ' used (' + usedPct.toFixed(0) + '%)';
  lossUsedEl.className   = 'metric-sub ' + (usedPct > 80 ? 'dn' : usedPct > 50 ? 'mu' : 'up');
  const bar = document.getElementById('lossBar');
  bar.style.width      = Math.min(usedPct, 100) + '%';
  bar.style.background = usedPct > 80 ? 'var(--red)' : usedPct > 50 ? 'var(--amber)' : 'var(--green)';

  allSetups    = s.setups        || [];
  allHistory   = s.trade_history || [];
  allDecisions = s.decision_log  || [];

  renderPositions(s.positions || []);
  renderTrades(s.trades || []);
  renderSetups();
  renderGreeks(s);
  renderCharts(s.pnl_history || [], s.filter_stats || {});
  renderStrategies(s);

  if (document.getElementById('tab-history').classList.contains('active'))   renderHistory();
  if (document.getElementById('tab-decisions').classList.contains('active')) renderDecisions();
  if (document.getElementById('tab-analytics').classList.contains('active')) renderAnalytics();
}

// ── Strategies tab ────────────────────────────────────────────────────────────
function _ago(sec) {
  if (sec == null) return 'offline';
  if (sec < 60)   return Math.round(sec) + 's ago';
  if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
  return Math.floor(sec / 3600) + 'h ago';
}

// ── Strategy detail modal ─────────────────────────────────────────────────────
// Static reference: what each strategy is and how it's calculated.
const STRATEGY_INFO = {
  catalyst_long_call: {
    summary: 'Buys long call options ahead of a known catalyst (earnings or major news) to capture a directional move plus the pre-event run-up in implied volatility. A pre-event, multi-day swing.',
    how: [
      'Scan: names with earnings in the next 1–5 days, scored on catalyst proximity, momentum, options flow and news.',
      'Entry: a single near-the-money long call, 7–30 DTE.',
      'Sizing: ~3% of equity per trade (capped at 5%), within a 40% strategy allocation.',
    ],
    exits: 'Managed by the position manager — take-profit / stop-loss on the option mid. Caveat: long premium into earnings fights IV crush (a known negative-EV result from backtesting).',
  },
  iv_rank: {
    summary: 'Sells defined-risk option spreads to collect premium when implied volatility is historically rich — the short-volatility engine. Profits from time decay and falling IV. Preferred structure is an iron condor (bull-put spread fallback).',
    how: [
      'Trigger: IV rank ≥ 75 (the sell-premium regime).',
      'Iron condor: short put ~5% below / long put ~10% below, short call ~5% above / long call ~10% above spot.',
      'Buys the long protective wings FIRST, then sells the short legs (no naked-short risk). 25% allocation.',
    ],
    exits: 'Close at 50% of the credit captured (take-profit), 2× the credit (stop-loss), or 7 DTE — whichever comes first.',
  },
  hft_intraday: {
    summary: 'Intraday momentum scalps on short-dated (0–2 DTE) options, flat by the close. Trades a confluence of intraday signals during the most liquid part of the day.',
    how: [
      'Signals (5-min bars): VWAP reclaim, opening-range breakout, volume spike, range breakout, VWAP bounce.',
      'Entry: ≥ 2 confluent signals and a score ≥ 45, only in the 9:45–14:45 ET prime window.',
      'Sizing by conviction: full size when the proven VWAP+ORB+spike trio fires, half size otherwise. 20% allocation.',
    ],
    exits: 'Asymmetric — take-profit +100%, stop-loss −20%, 60-minute max hold (cut losers fast, let winners run). Flat by end of day.',
  },
  pead: {
    summary: 'Post-earnings / news drift. Buys long options to ride the continuation of a large news-driven gap, entering AFTER the move — the mirror of the pre-event Catalyst trade.',
    how: [
      'Signal: a recent (≤3 days) event — |daily return| ≥ 2.5σ AND ≥ 4% gap AND ≥ 1.8× average volume — that has held since.',
      'Trend gate: only trades drift that agrees with the 50-day SMA slope (the validated edge). Long-only by default.',
      'Entry: ~at-the-money call (up-gap) or put (down-gap), 14–35 DTE (target 21). 15% allocation.',
    ],
    exits: 'Take-profit +80%, stop-loss −35%, drift-fade (spot fills the gap), DTE ≤ 3, or a 16-day max hold.',
  },
  bounce: {
    summary: 'Bear-market capitulation bounce. In a bear regime only, buys short-dated calls on oversold down-gaps — the contrarian bet that panic selling snaps back. The offensive counterpart to the bear-market risk throttle.',
    how: [
      'Regime gate: active only when SPY is below its 200-day SMA (a bear market).',
      'Signal: reuses the drift detector to find oversold down-gaps (capitulation).',
      'Entry: ~at-the-money call, ~14 DTE, ~6-day hold. Half/full size by conviction.',
    ],
    exits: 'Take-profit +60%, stop-loss −35%.',
  },
};
// IV-rank records its closed trades under the structure name — map back to the key.
const STRAT_ALIASES = { bull_put_spread: 'iv_rank', bear_call_spread: 'iv_rank',
                        iron_condor: 'iv_rank',
                        iron_butterfly: 'iv_rank', long_straddle: 'iv_rank' };
const canonStrat = v => STRAT_ALIASES[v] || v;

let openStrategyKey = null;

function openStrategyDetail(key) { openStrategyKey = key; renderStrategyDetail(); document.getElementById('stratModalBd').classList.add('open'); }
function closeStratDetail()      { openStrategyKey = null; document.getElementById('stratModalBd').classList.remove('open'); }
function onStratBdClick(e)       { if (e.target === document.getElementById('stratModalBd')) closeStratDetail(); }
document.addEventListener('keydown', e => { if (e.key === 'Escape' && document.getElementById('stratModalBd').classList.contains('open')) closeStratDetail(); });

function renderStrategyDetail() {
  const key = openStrategyKey;
  if (!key) return;
  const info = STRATEGY_INFO[key] || {};
  const s  = currentState || {};
  const st = ((s.strategy_panel || {}).strategies || []).find(x => x.key === key) || {};
  const money = v => (v >= 0 ? '+$' : '-$') + Math.abs(v || 0).toLocaleString();
  const moneyR = v => (v >= 0 ? '+$' : '-$') + Math.abs(v || 0).toFixed(0);
  const pctTxt = v => (v >= 0 ? '+' : '') + Number(v || 0).toFixed(1) + '%';

  document.getElementById('stratModalTitle').textContent = (st.name || key) + ' — strategy detail';

  const stats = [
    ['Allocation',     st.alloc_pct != null ? st.alloc_pct + '%' : '—', ''],
    ['Open positions', st.positions != null ? st.positions : '—', ''],
    ['Capital used',   st.deployed != null ? '$' + Math.round(st.deployed).toLocaleString() : '—', ''],
    ['Realized P&L',   st.pnl != null ? money(st.pnl) : '—', st.pnl >= 0 ? 'up' : 'dn'],
    ['Unrealized P&L', st.unrealized != null ? money(st.unrealized) : '—', (st.unrealized || 0) >= 0 ? 'up' : 'dn'],
    ['Win rate',       st.trades ? st.win_rate + '% (' + st.trades + ')' : '—', ''],
  ];
  const statsHtml = stats.map(([l, v, cls]) =>
    `<div class="sd-stat"><div class="sd-stat-label">${l}</div><div class="sd-stat-val ${cls}">${v}</div></div>`).join('');

  // Open positions attributed to this strategy (backend tags pos.strategy by symbol).
  const positions = (s.positions || []).filter(p => p.strategy === key);
  const posHtml = positions.length ? positions.map(p => {
    const pnl = p.total_pnl_dollar != null ? p.total_pnl_dollar : (p.pnl_dollar || 0);
    const pct = p.total_pnl_pct    != null ? p.total_pnl_pct    : (p.pnl_pct || 0);
    return `<div class="sd-row"><span>${esc(p.underlying)} <span class="mu">${esc(p.contract_desc || p.position_type || '')}</span></span>
      <span class="${pnl >= 0 ? 'up' : 'dn'} mono">${moneyR(pnl)} (${pctTxt(pct)})</span></div>`;
  }).join('') : '<div class="sd-empty">No open positions.</div>';

  // Recent closed trades for this strategy (newest first, last 8).
  const trades = (s.trade_history || []).filter(t => canonStrat(t.strategy) === key)
    .sort((a, b) => (b.exit_time || '').localeCompare(a.exit_time || '')).slice(0, 8);
  const trHtml = trades.length ? trades.map(t => {
    const pnl = t.pnl_dollar || 0, pct = t.pnl_pct || 0;
    const when = t.exit_time ? fmtSetupTs(t.exit_time) : '';
    const why  = t.exit_reason ? ' · ' + esc(t.exit_reason) : '';
    return `<div class="sd-row"><span>${esc(t.underlying || '')} <span class="mu">${esc(when)}${why}</span></span>
      <span class="${pnl >= 0 ? 'up' : 'dn'} mono">${moneyR(pnl)} (${pctTxt(pct)})</span></div>`;
  }).join('') : '<div class="sd-empty">No closed trades yet.</div>';

  document.getElementById('stratModalBody').innerHTML = `
    <div class="sd-summary">${esc(info.summary || '')}</div>
    <div class="sd-section"><div class="sd-section-title">How it works</div>
      <ul class="sd-how">${(info.how || []).map(h => `<li>${esc(h)}</li>`).join('')}</ul></div>
    ${info.exits ? `<div class="sd-section"><div class="sd-section-title">Exits</div><div class="sd-summary">${esc(info.exits)}</div></div>` : ''}
    <div class="sd-section"><div class="sd-section-title">Current stats</div>
      <div class="sd-stats">${statsHtml}</div></div>
    <div class="sd-section"><div class="sd-section-title">Open positions (${positions.length})</div>
      <div class="sd-list">${posHtml}</div></div>
    <div class="sd-section"><div class="sd-section-title">Recent trades</div>
      <div class="sd-list">${trHtml}</div></div>`;
}

function renderStrategies(s) {
  const sp = (s && s.strategy_panel) || {};

  // Portfolio P&L: realized + unrealized split, each as $ and % return.
  const pg = document.getElementById('pnlGrid');
  if (pg) {
    const ps = s && s.pnl_summary;
    const note = document.getElementById('pnlNote');
    if (!ps) {
      pg.innerHTML = '<div class="mu" style="font-size:12px">No P&L data yet — restart the bot to populate.</div>';
      if (note) note.textContent = '';
    } else {
      const money  = v => (v >= 0 ? '+$' : '-$') + Math.abs(v||0).toLocaleString('en', {maximumFractionDigits: 0});
      const pctTxt = v => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
      const cls    = v => (v||0) >= 0 ? 'up' : 'dn';
      const cell   = (label, val, p) =>
        `<div class="pnl-cell"><div class="pnl-cell-label">${label}</div>
          <div class="pnl-cell-val ${cls(val)}">${money(val)}</div>
          <div class="pnl-cell-sub ${cls(p)}">${pctTxt(p)}</div></div>`;
      pg.innerHTML =
        cell('Total P&L', ps.total||0,      ps.total_pct) +
        cell('Realized',  ps.realized||0,   ps.realized_pct) +
        cell('Unrealized', ps.unrealized||0, ps.unrealized_pct);
      if (note) {
        const unatt = sp.unrealized_unattributed || 0;
        const bits = [];
        if (ps.start_equity) bits.push(`Return measured against $${Math.round(ps.start_equity).toLocaleString()} starting capital`);
        if (Math.abs(unatt) >= 1) bits.push(`unrealized includes ${money(unatt)} broker-only (not attributed to a strategy)`);
        note.textContent = bits.join(' · ');
      }
    }
  }

  // Header: market-regime pill (daily regime + intraday red-day state)
  const reg = sp.regime || { state: 'unknown', detail: '' };
  const rp  = document.getElementById('regimePill');
  if (rp) {
    let m = { bull: ['dot-green', 'Bull market'], bear: ['dot-red', 'Bear market'],
              unknown: ['dot-gray', 'Regime —'] }[reg.state] || ['dot-gray', 'Regime —'];
    let title = reg.detail || 'Market regime';
    // A red/weak intraday tape overrides the pill color — that's the acute risk.
    if (reg.intraday === 'red')       m = ['dot-red',    m[1] + ' · RED day'];
    else if (reg.intraday === 'weak') m = ['dot-amber', m[1] + ' · weak day'];
    if (reg.intraday_detail) title += ' | ' + reg.intraday_detail;
    rp.querySelector('.dot').className = 'dot ' + m[0];
    rp.querySelector('span:last-child').textContent = m[1];
    rp.title = title;
  }

  // Header: systems-health pill
  const health = sp.health || [];
  const down   = health.filter(h => !h.alive).length;
  const syp    = document.getElementById('sysPill');
  if (syp) {
    syp.querySelector('.dot').className = 'dot ' + (health.length === 0 ? 'dot-gray' : down ? 'dot-red' : 'dot-green');
    syp.querySelector('span:last-child').textContent =
      health.length === 0 ? 'Systems —' : down ? (down + ' down') : 'All systems live';
  }

  // Health strip
  const hs = document.getElementById('healthStrip');
  if (hs) hs.innerHTML = health.length ? health.map(h => {
    const dot = h.alive ? 'dot-green' : (h.status === 'offline' ? 'dot-gray' : 'dot-red');
    return `<div class="health-item"><span class="dot ${dot}"></span><div>
      <div class="health-name">${esc(h.name)}</div>
      <div class="health-sub mu">${esc(h.status)} · ${_ago(h.age_sec)}</div></div></div>`;
  }).join('') : '<div class="mu" style="font-size:12px">No heartbeats — start the bot.</div>';

  // Per-strategy cards
  const sg = document.getElementById('stratGrid');
  if (sg) sg.innerHTML = (sp.strategies || []).map(st => {
    const pnlCls = st.pnl >= 0 ? 'up' : 'dn';
    const pnlStr = (st.pnl >= 0 ? '+$' : '-$') + Math.abs(st.pnl).toLocaleString();
    const hasU   = st.unrealized != null;
    const uCls   = (st.unrealized || 0) >= 0 ? 'up' : 'dn';
    const uStr   = hasU ? ((st.unrealized >= 0 ? '+$' : '-$') + Math.abs(st.unrealized).toLocaleString()) : '—';
    const use    = Math.min(st.usage_pct, 100);
    const col    = st.usage_pct > 90 ? 'var(--red)' : st.usage_pct > 70 ? 'var(--amber)' : 'var(--green)';
    return `<div class="strat-card" onclick="openStrategyDetail('${st.key}')" title="Click for what it is, how it's calculated, open positions and recent trades">
      <div class="strat-head"><span class="strat-name">${esc(st.name)}</span>
        <span class="badge b-gray">${st.alloc_pct}% alloc</span></div>
      <div class="strat-row"><span class="mu">Open positions</span><span>${st.positions}</span></div>
      <div class="strat-row"><span class="mu">Capital used</span><span>$${st.deployed.toLocaleString()} / $${st.cap.toLocaleString()}</span></div>
      <div class="conc-track"><div class="conc-fill" style="width:${use}%;background:${col}"></div></div>
      <div class="strat-row" style="margin-top:9px"><span class="mu">Realized P&amp;L</span><span class="${pnlCls}">${pnlStr}</span></div>
      <div class="strat-row"><span class="mu">Unrealized P&amp;L</span><span class="${hasU ? uCls : 'mu'}">${uStr}</span></div>
      <div class="strat-row"><span class="mu">Win rate</span><span>${st.trades ? st.win_rate + '% (' + st.trades + ')' : '—'}</span></div>
      <div class="strat-more">View details →</div>
    </div>`;
  }).join('') || '<div class="mu">No strategy data yet.</div>';

  // Capital-allocation bar (target split)
  const ab = document.getElementById('allocBar');
  if (ab) {
    const cols = { catalyst_long_call: 'var(--blue)', iv_rank: 'var(--purple)',
                   hft_intraday: 'var(--amber)', pead: 'var(--green)' };
    ab.innerHTML = (sp.strategies || []).map(st =>
      `<div class="alloc-seg" style="width:${st.alloc_pct}%;background:${cols[st.key] || 'var(--muted)'}"
            title="${esc(st.name)} — ${st.alloc_pct}%">${st.alloc_pct >= 10 ? esc(st.name) : ''}</div>`
    ).join('');
  }

  // Correlation-exposure meters
  const cm = document.getElementById('concMeters');
  if (cm) {
    const conc = sp.concentration || [];
    cm.innerHTML = conc.length ? conc.map(c => {
      const p   = Math.min(c.count / c.max * 100, 100);
      const col = c.count >= c.max ? 'var(--red)' : c.count >= c.max - 1 ? 'var(--amber)' : 'var(--blue)';
      return `<div class="conc-row"><div class="conc-label"><span>${esc(c.group)}</span>
        <span class="mu">${c.count}/${c.max}</span></div>
        <div class="conc-track"><div class="conc-fill" style="width:${p}%;background:${col}"></div></div></div>`;
    }).join('') : '<div class="mu" style="font-size:12px">No correlated positions open.</div>';
  }

  // Alert channels indicator — which notification channels are configured.
  const ac = document.getElementById('alertChannels');
  if (ac) {
    const a = (s && s.alerts) || {};
    const chans = [['discord','Discord'],['email','Email'],['sms','SMS'],['telegram','Telegram']];
    const on = chans.filter(([k]) => a[k]).map(([, label]) =>
      `<span style="color:var(--green)">● ${label}</span>`);
    ac.innerHTML = on.length
      ? 'Alerts: ' + on.join(' · ')
      : '<span style="color:var(--muted)">Alerts: none configured — set channels in .env</span>';
  }

  // Events feed
  const el = document.getElementById('eventsList');
  if (el) {
    const events = (s && s.events) || [];
    el.innerHTML = events.length ? events.map(e => {
      const sev = { success: 'var(--green)', danger: 'var(--red)',
                    warning: 'var(--amber)', info: 'var(--blue)' }[e.severity] || 'var(--muted)';
      const t = e.iso ? esc(e.iso.replace('T', ' ').slice(5, 16)) : '';
      return `<div class="event-item"><span class="event-dot" style="background:${sev}"></span>
        <div class="event-body"><div class="event-subj">${esc(e.subject)}</div>
        <div class="event-sum mu">${esc(e.summary)}</div></div>
        <div class="event-time mu">${t}</div></div>`;
    }).join('') : '<div class="tbl-empty" style="padding:18px;text-align:center">No events yet</div>';
  }

  // Keep an open strategy-detail modal in sync with fresh polls.
  if (openStrategyKey && document.getElementById('stratModalBd').classList.contains('open')) renderStrategyDetail();
}

// ── Overview: positions ───────────────────────────────────────────────────────
function posTypeBadge(type, isSpread) {
  if (!type) return '<span class="badge b-gray">?</span>';
  const t = type.toLowerCase();
  if (t.includes('spread') || t.includes('condor') || t.includes('butterfly') || t.includes('ratio') || t.includes('combo'))
    return `<span class="badge b-purple">${esc(type)}</span>`;
  if (t.includes('long call'))  return `<span class="badge b-blue">${esc(type)}</span>`;
  if (t.includes('long put'))   return `<span class="badge b-blue">${esc(type)}</span>`;
  if (t.includes('short call')) return `<span class="badge b-amber">${esc(type)}</span>`;
  if (t.includes('short put'))  return `<span class="badge b-amber">${esc(type)}</span>`;
  return `<span class="badge b-gray">${esc(type)}</span>`;
}

function renderPositions(positions) {
  if (!positions.length) {
    document.getElementById('posBody').innerHTML = '<tr class="tbl-empty"><td colspan="6">No open positions</td></tr>';
    return;
  }

  // Backwards-compat: if data is old format (flat legs, no position_type), group by underlying+expiry
  let grouped = positions;
  if (positions.length && !positions[0].position_type) {
    const groups = {};
    positions.forEach(p => {
      const key = (p.underlying || '') + '|' + (p.expiration || '');
      if (!groups[key]) groups[key] = { underlying: p.underlying, expiration: p.expiration, exp_display: p.exp_display, dte: p.dte, exit_trigger: p.exit_trigger, legs: [] };
      const parsed = (p.symbol || '').match(/^[A-Z]+\d{6}([CP])\d{8}$/);
      const optType = parsed ? parsed[1] : '?';
      const strikeRaw = (p.symbol || '').match(/\d{8}$/);
      const strike = strikeRaw ? parseInt(strikeRaw[0]) / 1000 : 0;
      groups[key].legs.push({ symbol: p.symbol, quantity: p.quantity, strike, opt_type: optType, entry_price: p.entry_price, current_price: p.current_price, pnl_pct: p.pnl_pct, pnl_dollar: p.pnl_dollar });
    });
    grouped = Object.values(groups).map(g => {
      const longs = g.legs.filter(l => l.quantity > 0);
      const shorts = g.legs.filter(l => l.quantity < 0);
      const isSpread = g.legs.length > 1;
      let posType = '?';
      if (g.legs.length === 1) {
        const side = g.legs[0].quantity > 0 ? 'Long' : 'Short';
        const t = g.legs[0].opt_type === 'C' ? 'Call' : g.legs[0].opt_type === 'P' ? 'Put' : '?';
        posType = side + ' ' + t;
      } else if (g.legs.length === 2 && longs.length === 1 && shorts.length === 1) {
        const types = new Set(g.legs.map(l => l.opt_type));
        if (types.size === 1) {
          const isPut = types.has('P');
          posType = (longs[0].strike < shorts[0].strike)
            ? (isPut ? 'Bull Put Spread' : 'Bull Call Spread')
            : (isPut ? 'Bear Put Spread' : 'Bear Call Spread');
        } else posType = 'Combo (2-leg)';
      } else posType = (longs.length && shorts.length) ? 'Ratio / Complex Spread' : 'Multi-leg (' + g.legs.length + ')';
      const sorted = g.legs.slice().sort((a,b) => a.strike - b.strike);
      const desc = sorted.map(l => (l.quantity > 0 ? '+' : '') + l.quantity + ' $' + l.strike.toFixed(0) + l.opt_type).join(' / ');
      const totalPnl = g.legs.reduce((s, l) => s + (l.pnl_dollar || 0), 0);
      const totalCost = g.legs.reduce((s, l) => s + Math.abs(l.entry_price * l.quantity * 100), 0);
      const pnlPct = totalCost > 0 ? +(totalPnl / totalCost * 100).toFixed(1) : 0;
      return { underlying: g.underlying, expiration: g.expiration, exp_display: g.exp_display, dte: g.dte, position_type: posType, is_spread: isSpread, contract_desc: desc, legs: sorted, total_pnl_dollar: +totalPnl.toFixed(2), total_pnl_pct: pnlPct, exit_trigger: g.exit_trigger };
    });
    // Also fix the metric card count
    document.getElementById('openPos').textContent = grouped.length + ' / ' + (currentState?.account?.max_positions || 5);
    const slots = (currentState?.account?.max_positions || 5) - grouped.length;
    document.getElementById('openPosSub').textContent = slots + ' slot' + (slots !== 1 ? 's' : '') + ' available';
  }

  document.getElementById('posBody').innerHTML = grouped.map(p => {
    const pnl    = p.total_pnl_dollar != null ? p.total_pnl_dollar : p.pnl_dollar || 0;
    const pnlPct = p.total_pnl_pct != null    ? p.total_pnl_pct    : p.pnl_pct || 0;
    const cls    = pnlPct >= 0 ? 'up' : 'dn';
    const pnlStr = (pnlPct>=0?'+':'')+pnlPct.toFixed(1)+'% ('+(pnl>=0?'+$':'-$')+Math.abs(pnl).toFixed(0)+')';
    const contract = p.contract_desc || p.symbol || '—';
    const posType  = p.position_type || (p.quantity > 0 ? 'Long' : p.quantity < 0 ? 'Short' : '?');

    // Leg detail tooltip for spreads
    let legDetail = '';
    if (p.legs && p.legs.length > 1) {
      legDetail = p.legs.map(l => {
        const side = l.quantity > 0 ? '+' : '';
        return `${side}${l.quantity} $${l.strike.toFixed(0)}${l.opt_type} @ $${l.entry_price.toFixed(2)} → $${l.current_price.toFixed(2)}`;
      }).join('\n');
    }

    return `<tr title="${esc(legDetail)}">
      <td class="sym">${esc(p.underlying)}</td>
      <td>${posTypeBadge(posType, p.is_spread)}</td>
      <td style="font-size:11px;color:var(--text2);font-family:monospace">${esc(contract)}</td>
      <td style="color:var(--muted);font-size:11px">${esc(p.exp_display||p.expiration)} <span style="font-size:10px">(${p.dte}d)</span></td>
      <td class="${cls} mono">${pnlStr}</td>
      <td><span class="badge b-blue">${esc(p.exit_trigger)}</span></td>
    </tr>`;
  }).join('');
}

// ── Overview: today's trades ──────────────────────────────────────────────────
function renderTrades(trades) {
  if (!trades.length) {
    document.getElementById('tradesBody').innerHTML = '<tr class="tbl-empty"><td colspan="4">No trades today</td></tr>';
    return;
  }
  document.getElementById('tradesBody').innerHTML = trades.map(t => {
    const cls   = t.result === 'filled' ? 'b-green' : 'b-red';
    const lbl   = t.result === 'filled' ? 'Filled' : t.result === 'risk' ? 'Blocked' : esc(t.result);
    return `<tr>
      <td class="sym">${esc(t.ticker)}</td>
      <td style="color:var(--muted)">${esc(t.action)}</td>
      <td class="mono" style="color:var(--muted)">${esc(t.time)}</td>
      <td><span class="badge ${cls}">${lbl}</span></td>
    </tr>`;
  }).join('');
}

// ── Overview: setups ──────────────────────────────────────────────────────────
function filterSetups(type, btn) {
  setupFilter = type;
  // Direct children only — leaves the nested sort chips (#setupSortChips) untouched.
  document.querySelectorAll('#setupChips > .chip').forEach(c => c.classList.remove('on'));
  btn.classList.add('on');
  renderSetups();
}
function sortSetups(mode, btn) {
  setupSort = mode;
  document.querySelectorAll('#setupSortChips .chip').forEach(c => c.classList.remove('on'));
  btn.classList.add('on');
  renderSetups();
}
// ── Overview: portfolio greeks ────────────────────────────────────────────────
function renderGreeks(s) {
  const g = s.greeks || {};
  const el = id => document.getElementById(id);
  const priced = !!(g.priced && g.leg_count);
  const sgn = (v, dec = 0) => (v >= 0 ? '+' : '−') +
        Math.abs(Number(v)).toLocaleString('en', {maximumFractionDigits: dec, minimumFractionDigits: dec});

  if (!priced) {
    ['gDelta','gGamma','gTheta','gVega'].forEach(i => { el(i).textContent = '—'; el(i).style.color = ''; });
    el('gVegaBar').style.width = '0%';
    el('gDeltaSub').textContent = 'no open options';
    el('gVegaSub').textContent  = 'per +1 vol pt';
    el('greeksNote').textContent = g.updated ? 'flat — no priced option legs' : '';
    return;
  }

  el('gDelta').textContent = '$' + sgn(g.net_delta);
  el('gGamma').textContent = sgn(g.net_gamma, 1);
  el('gTheta').textContent = '$' + sgn(g.net_theta);
  el('gVega').textContent  = '$' + sgn(g.net_vega);
  el('gDelta').style.color = g.net_delta >= 0 ? 'var(--green)' : 'var(--red)';
  el('gTheta').style.color = g.net_theta >= 0 ? 'var(--green)' : 'var(--red)';   // longs pay theta (red), shorts earn (green)
  el('gVega').style.color  = g.net_vega  >= 0 ? 'var(--blue)'  : 'var(--purple)';
  el('gDeltaSub').textContent = 'per +$1 move';

  // Vega vs the risk limit — bar fills toward the cap, reddens as it approaches.
  const lim = g.vega_limit || 0;
  const usedPct = lim > 0 ? Math.min(Math.abs(g.net_vega) / lim * 100, 100) : 0;
  const bar = el('gVegaBar');
  bar.style.width = usedPct + '%';
  bar.style.background = usedPct > 90 ? 'var(--red)' : usedPct > 60 ? 'var(--amber)'
                       : (g.net_vega >= 0 ? 'var(--blue)' : 'var(--purple)');
  el('gVegaSub').textContent = lim > 0
    ? `${g.net_vega >= 0 ? 'long' : 'short'} vol · ${usedPct.toFixed(0)}% of $${Math.round(lim).toLocaleString()} cap`
    : 'per +1 vol pt';
  el('greeksNote').textContent = `${g.leg_count} priced leg${g.leg_count === 1 ? '' : 's'}`;
}

// ── Scanner edge: did surfaced setups actually move the predicted way? ─────────
function renderScannerEdge(s) {
  const el = document.getElementById('scannerEdge');
  if (!el) return;
  const p = s && s.scanner_perf;
  if (!p || !p.finalized) { el.className = 'scanner-edge'; el.innerHTML = ''; return; }
  const pct = (v) => v == null ? '—' : (v >= 0 ? '+' : '') + v + '%';
  const hr  = p.hit_rate == null ? '—' : p.hit_rate + '%';
  let buckets = '';
  if (p.by_score) {
    buckets = Object.entries(p.by_score).filter(([, v]) => v.n > 0).map(([k, v]) =>
      `<span class="se-bucket">score ${k}: <b>${v.hit_rate == null ? '—' : v.hit_rate + '%'}</b> hit · <b>${pct(v.avg_return)}</b> (${v.n})</span>`
    ).join('');
  }
  el.className = 'scanner-edge show';
  el.innerHTML = `<span>Scanner edge — <b>${hr}</b> directional hit · avg <b>${pct(p.avg_return)}</b> · `
    + `${p.finalized} graded, ${p.open} tracking <span class="mu">(±${p.win_threshold}% = win)</span></span>${buckets}`;
}

// Per-setup forward-return badge — favorable underlying move since the setup was found.
function fwdBadge(s) {
  const r = s.forward_return_pct;
  if (r == null) return '';
  const txt = (r >= 0 ? '▲ +' : '▼ ') + Number(r).toFixed(1) + '%';
  if (s.perf_finalized) {
    const oc = s.outcome;
    const col = oc === 'win' ? 'var(--green)' : oc === 'loss' ? 'var(--red)' : 'var(--muted)';
    const bg  = oc === 'win' ? 'rgba(26,191,130,0.13)' : oc === 'loss' ? 'rgba(224,49,49,0.13)' : 'var(--border2)';
    return `<span class="setup-fwd" style="color:${col};background:${bg}" title="Final: ${oc} — underlying moved ${txt} in the setup's direction">${txt}</span>`;
  }
  return `<span class="setup-fwd" style="color:var(--muted);background:var(--border2)" title="Tracking — underlying ${txt} so far in the setup's direction">${txt} · tracking</span>`;
}

// Small-font "what the scanner picked up" — the concrete signals + readings.
// Full per-signal descriptions go in the title (hover) so the line stays compact.
function setupDetail(s) {
  const bits = [];
  const num = (v, d = 1) => (v >= 0 ? '+' : '') + Number(v).toFixed(d);
  if (s.rsi != null)            bits.push('RSI ' + Math.round(s.rsi));
  if (s.momentum_score != null) bits.push('mom ' + s.momentum_score);
  if (s.roc_5d != null)         bits.push('ROC5 ' + num(s.roc_5d) + '%');
  const vol = s.vol_mult != null ? s.vol_mult : s.vol_surge;
  if (vol != null)              bits.push('vol ' + Number(vol).toFixed(1) + '×');
  if (s.pct_vs_vwap != null)    bits.push('VWAP ' + num(s.pct_vs_vwap) + '%');
  if (s.event_move != null)     bits.push('gap ' + num(s.event_move) + '%'
                                          + (s.move_z != null ? ` (${num(s.move_z)}σ)` : ''));
  if (s.days_since != null)     bits.push(s.days_since + 'd ago');
  if (s.held_frac != null)      bits.push('held ' + Math.round(s.held_frac * 100) + '%');
  if (s.options_flow)           bits.push('flow ✓');
  if (s.news_catalyst)          bits.push('news ✓');
  if (s.iv) {
    if (s.iv.atm_iv != null)      bits.push('IV ' + s.iv.atm_iv + '%');
    if (s.iv.iv_rank != null)     bits.push('IVR ' + s.iv.iv_rank);
    else if (s.iv.iv_hv_ratio != null) bits.push('IV/HV ' + s.iv.iv_hv_ratio + '×');
  }

  // Lead with the named signals that fired (HFT: orb/vwap/spike/…).
  const sigTag = (s.active_signals && s.active_signals.length)
    ? `<span class="sd-sig">${esc(s.active_signals.join(' + '))}</span> · ` : '';
  if (!sigTag && !bits.length) return '';

  // Hover shows the full per-signal descriptions when present.
  let title = '';
  if (s.signal_details && typeof s.signal_details === 'object') {
    title = Object.values(s.signal_details).filter(Boolean).map(String).join('\n');
  }
  const body = bits.join(' · ');
  return `<div class="setup-detail"${title ? ` title="${esc(title)}"` : ''}>${sigTag}${esc(body)}</div>`;
}

// Format a setup's "found"/"seen" timestamp: time-only if today, else date+time.
function fmtSetupTs(ts) {
  const d = new Date(ts);
  if (isNaN(d.getTime())) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay
    ? d.toLocaleTimeString('en', {hour: 'numeric', minute: '2-digit'})
    : d.toLocaleString('en', {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
}
// Recency key for the "Recent" sort: when the setup was first surfaced, with
// graceful fallbacks for older rows that predate the found_at/first_seen fields.
function setupRecency(s) {
  return new Date(s.found_at || s.first_seen || s.last_seen || 0).getTime() || 0;
}
function renderSetups() {
  renderScannerEdge(currentState);
  // "as of" label — setups are persisted across cycles, so show when this scan ran.
  const asOfEl = document.getElementById('setupsAsOf');
  if (asOfEl) {
    const ts = currentState && currentState.setups_updated;
    if (ts && allSetups.length) {
      const d = new Date(ts);
      const mins = Math.floor((Date.now() - d.getTime()) / 60000);
      const when = mins < 1 ? 'just now' : mins < 60 ? `${mins}m ago`
                 : d.toLocaleTimeString('en', {hour: 'numeric', minute: '2-digit'});
      asOfEl.textContent = 'as of ' + when;
    } else {
      asOfEl.textContent = '';
    }
  }

  let list = allSetups;
  if (setupFilter === 'day')      list = allSetups.filter(s => s.trade_style === 'day' || s.day_trade_ok);
  if (setupFilter === 'swing')    list = allSetups.filter(s => (s.trade_style || 'swing') === 'swing');
  if (setupFilter === 'earnings') list = allSetups.filter(s => s.days_until_earnings != null && s.days_until_earnings <= 3);
  if (setupFilter === 'flow')     list = allSetups.filter(s => s.options_flow);
  if (setupFilter === 'news')     list = allSetups.filter(s => s.news_catalyst);

  // Apply the chosen sort. Copy first so we never mutate allSetups (when the
  // filter is 'all', list is the same array reference). Each mode breaks ties
  // with the other key so ordering is always fully determined.
  list = [...list].sort(setupSort === 'score'
    ? (a, b) => (b.setup_score || 0) - (a.setup_score || 0) || setupRecency(b) - setupRecency(a)
    : (a, b) => setupRecency(b) - setupRecency(a) || (b.setup_score || 0) - (a.setup_score || 0));

  if (!list.length) {
    document.getElementById('setupsList').innerHTML = '<div style="color:var(--muted);font-size:12px;padding:12px 0">No setups found</div>';
    return;
  }
  document.getElementById('setupsList').innerHTML = list.map(s => {
    const badges = [];
    // Day/Swing classification badge first — the key "how do I trade this?"
    const style = s.trade_style || 'swing';
    if (style === 'day') {
      badges.push(`<span class="badge b-purple">Day trade${s.direction ? ' · ' + esc(s.direction) : ''}</span>`);
    } else {
      badges.push(`<span class="badge b-blue">Swing</span>`);
      if (s.day_trade_ok) badges.push(`<span class="badge b-purple">Day-OK</span>`);
    }
    if (s.conviction) badges.push(`<span class="badge ${s.conviction === 'high' ? 'b-green' : 'b-gray'}">${esc(s.conviction)}</span>`);
    if (s.days_until_earnings != null) badges.push(`<span class="badge b-green">Earnings ${esc(s.days_until_earnings)}d</span>`);
    if (s.options_flow)   badges.push(`<span class="badge b-blue">Flow</span>`);
    if (s.news_catalyst)  badges.push(`<span class="badge b-amber">News</span>`);
    // IV regime: rich = options expensive (caution for buyers), cheap = good value.
    if (s.iv && s.iv.regime && s.iv.regime !== 'n/a') {
      const r = s.iv.regime;
      const ivCls = r === 'rich' ? 'b-amber' : r === 'cheap' ? 'b-green' : 'b-gray';
      badges.push(`<span class="badge ${ivCls}" title="ATM IV ${s.iv.atm_iv ?? '?'}%${s.iv.iv_rank != null ? ', IV rank ' + s.iv.iv_rank : ''}">IV ${esc(r)}</span>`);
    }
    const score = s.setup_score || 0;
    const barClr = score >= 75 ? 'var(--green)' : score >= 50 ? 'var(--amber)' : 'var(--red)';
    // "Why" line: prefer the scanner's style_reason, fall back to the headline.
    const why = s.style_reason || s.news_headline || '';
    const whyLine = why ? esc(why).slice(0,110) + (why.length > 110 ? '…' : '') : '';
    // Setups are never deleted — they accumulate with the date/time they were
    // found. A setup seen by a scan within the last 20 min is "live"; older ones
    // are historical opportunities (dimmed, but kept on the board).
    const LIVE_WINDOW_MS = 20 * 60 * 1000;
    const lastSeenMs = s.last_seen ? new Date(s.last_seen).getTime() : 0;
    const live  = lastSeenMs && (Date.now() - lastSeenMs) <= LIVE_WINDOW_MS;
    const stale = !live;
    const liveBadge = live ? `<span class="badge b-green">Live</span>` : '';
    // "Found" = when the scanner FIRST surfaced this opportunity.
    const foundTs = s.found_at || s.first_seen;
    const foundLabel = foundTs ? `<span class="setup-found">Found ${fmtSetupTs(foundTs)}</span>` : '';
    let ageLabel = '';
    if (stale && lastSeenMs) {
      const mins = Math.floor((Date.now() - lastSeenMs) / 60000);
      ageLabel = `<span class="setup-found"> · last seen ${mins < 60 ? mins + 'm' : Math.floor(mins/60) + 'h'} ago</span>`;
    }
    return `<div class="setup-row" style="${stale ? 'opacity:0.6' : ''}">
      <div style="min-width:0">
        <div class="setup-sym">${esc(s.ticker)} ${liveBadge} ${fwdBadge(s)}</div>
        <div class="setup-badges">${badges.join('')}</div>
        <div class="setup-found-line">${foundLabel}${ageLabel}</div>
        ${whyLine ? `<div class="setup-meta" title="${esc(why)}">${whyLine}</div>` : ''}
        ${setupDetail(s)}
      </div>
      <div style="display:flex;align-items:center;gap:10px;flex-shrink:0">
        <div>
          <div class="score-num">${score}/100</div>
          <div class="score-track"><div class="score-fill" style="width:${score}%;background:${barClr}"></div></div>
        </div>
      </div>
    </div>`;
  }).join('');
}

// ── Overview: charts ──────────────────────────────────────────────────────────
function getCssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function renderCharts(history, filterStats) {
  const isDark    = document.documentElement.getAttribute('data-theme') === 'dark' ||
                    (document.documentElement.getAttribute('data-theme') !== 'light' &&
                     matchMedia('(prefers-color-scheme: dark)').matches);
  const textClr   = isDark ? 'rgba(255,255,255,0.35)' : 'rgba(0,0,0,0.35)';
  const gridClr   = isDark ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.05)';
  const cGreen    = getCssVar('--green');
  const cRed      = getCssVar('--red');
  const cBlue     = getCssVar('--blue');
  const cAmber    = getCssVar('--amber');
  const cPurple   = getCssVar('--purple');

  // P&L bar chart
  const labels  = history.map(h => { const d = new Date(h.date); return (d.getMonth()+1)+'/'+d.getDate(); });
  const data    = history.map(h => h.pnl);
  const colors  = data.map(v => v >= 0 ? cGreen : cRed);

  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart(document.getElementById('pnlChart'), {
    type: 'bar',
    data: { labels, datasets: [{ data, backgroundColor: colors, borderRadius: 3, borderSkipped: false }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => (ctx.raw>=0?'+$':'-$')+Math.abs(ctx.raw).toFixed(0) } }
      },
      scales: {
        x: { ticks: { color: textClr, font:{size:10}, maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { color: textClr, font:{size:10}, callback: v => (v>=0?'+$':'-$')+Math.abs(v) }, grid: { color: gridClr } }
      }
    }
  });

  // Filter pass-rate donut
  const fs      = filterStats;
  const fData   = [fs.earnings_pct||0, fs.flow_pct||0, fs.news_pct||0, fs.momentum_pct||0];
  const fColors = [cGreen, cBlue, cAmber, cPurple];
  const fLabels = ['Earnings','Flow','News','Momentum'];
  const hasData = fData.some(v => v > 0);

  if (filterChart) filterChart.destroy();
  const fc = document.getElementById('filterChart');
  if (!hasData) {
    filterChart = new Chart(fc, {
      type: 'doughnut',
      data: { labels:['No data'], datasets:[{ data:[1], backgroundColor:['rgba(128,128,128,0.1)'], borderWidth:0 }] },
      options: { responsive:true, maintainAspectRatio:false, cutout:'65%', plugins:{legend:{display:false},tooltip:{enabled:false}} }
    });
    document.getElementById('filterLegend').innerHTML = '<span style="color:var(--muted)">Run the bot to see filter rates</span>';
  } else {
    filterChart = new Chart(fc, {
      type: 'doughnut',
      data: { labels: fLabels, datasets:[{ data: fData, backgroundColor: fColors, borderWidth:0 }] },
      options: { responsive:true, maintainAspectRatio:false, cutout:'65%', plugins:{legend:{display:false},tooltip:{callbacks:{label:ctx=>ctx.label+': '+ctx.raw+'% pass rate'}}} }
    });
    document.getElementById('filterLegend').innerHTML = fColors.map((c,i) =>
      `<span style="display:flex;align-items:center;gap:4px"><span class="legend-dot" style="background:${c}"></span>${fLabels[i]} ${fData[i]}%</span>`
    ).join('');
  }
}

// ── Trade History tab ─────────────────────────────────────────────────────────
function filterHistory(type, btn) {
  histFilter = type;
  document.querySelectorAll('#histChips .chip').forEach(c => c.classList.remove('on'));
  btn.classList.add('on');
  renderHistory();
}

function parseOCC(sym) {
  const m = sym.match(/^([A-Z]+)(\d{6})([CP])(\d{8})$/);
  if (!m) return null;
  return { type: m[3], strike: (parseInt(m[4]) / 1000).toFixed(0) };
}

function exitBadgeCls(r) {
  if (!r) return 'b-gray';
  const l = r.toLowerCase();
  if (l.includes('stop') || l.includes('sl'))           return 'b-red';
  if (l.includes('profit') || l.includes('tp'))         return 'b-green';
  if (l.includes('expir'))                              return 'b-amber';
  if (l.includes('broker'))                             return 'b-gray';
  if (l.includes('external'))                           return 'b-gray';
  return 'b-gray';
}

function renderHistory() {
  const all = allHistory;
  const total = all.length;
  const wins  = all.filter(t => (t.pnl_dollar||0) >= 0).length;
  const sumPnl = all.reduce((s, t) => s + (t.pnl_dollar||0), 0);
  const best   = total ? Math.max(...all.map(t => t.pnl_dollar||0)) : 0;
  const worst  = total ? Math.min(...all.map(t => t.pnl_dollar||0)) : 0;

  // Headline stats are about REALIZED (closed) trades only.
  document.getElementById('hTotal').textContent   = total || '—';
  document.getElementById('hWinRate').textContent = total ? (wins/total*100).toFixed(0)+'%' : '—';
  const tpEl = document.getElementById('hTotalPnl');
  tpEl.textContent = total ? (sumPnl>=0?'+$':'-$')+Math.abs(sumPnl).toLocaleString('en',{maximumFractionDigits:0}) : '—';
  tpEl.className   = 'stat-value ' + (sumPnl >= 0 ? 'up' : 'dn');
  document.getElementById('hBest').textContent  = total ? '+$'+best.toFixed(0)            : '—';
  document.getElementById('hWorst').textContent = total ? '-$'+Math.abs(worst).toFixed(0) : '—';

  const stratBadge = (s) => {
    if (!s || s === 'unknown') return '<span class="badge b-gray">—</span>';
    const map = { catalyst_long_call: 'b-blue', hft_intraday: 'b-purple', iv_rank: 'b-amber', broker: 'b-gray' };
    const short = { catalyst_long_call: 'Catalyst', hft_intraday: 'HFT', iv_rank: 'IV-Rank', broker: 'Broker' };
    return `<span class="badge ${map[s] || 'b-gray'}">${esc(short[s] || s)}</span>`;
  };

  // ── OPEN positions (unrealized) — shown so the tab reflects what you hold,
  //    not just what has closed. Pulled from the live positions feed. ──────────
  let openRows = '';
  const positions = (currentState && currentState.positions) || [];
  const passesFilter = (pnl) => histFilter === 'all'
        || (histFilter === 'win' && pnl >= 0)
        || (histFilter === 'loss' && pnl < 0);
  positions.forEach(p => {
    const pnl    = p.total_pnl_dollar != null ? p.total_pnl_dollar : (p.pnl_dollar || 0);
    const pnlPct = p.total_pnl_pct    != null ? p.total_pnl_pct    : (p.pnl_pct || 0);
    if (!passesFilter(pnl)) return;
    const cls = pnl >= 0 ? 'up' : 'dn';
    openRows += `<tr style="background:rgba(43,122,212,0.04)">
      <td class="mono" style="color:var(--muted)">—</td>
      <td class="sym">${esc(p.underlying)}</td>
      <td>${stratBadge(p.source === 'broker' ? 'broker' : (p.strategy || 'broker'))}</td>
      <td style="color:var(--text2);font-size:11px;font-family:monospace">${esc(p.contract_desc || p.position_type || '—')}</td>
      <td class="mono" style="color:var(--muted)">${p.legs ? p.legs.length + ' leg' + (p.legs.length!==1?'s':'') : (p.quantity||'')}</td>
      <td class="mono" style="color:var(--muted)">—</td>
      <td class="mono" style="color:var(--muted)">—</td>
      <td class="${cls} mono">${(pnl>=0?'+$':'-$')+Math.abs(pnl).toFixed(0)}</td>
      <td class="${cls} mono">${(pnlPct>=0?'+':'')+pnlPct.toFixed(1)}%</td>
      <td class="mono" style="color:var(--muted)">${esc(p.exp_display||'')} ${p.dte!=null?'('+p.dte+'d)':''}</td>
      <td class="mono" style="color:var(--muted)">—</td>
      <td><span class="badge b-blue">Open</span></td>
    </tr>`;
  });

  // ── CLOSED trades (realized) ──────────────────────────────────────────────
  let list = all.slice().reverse();
  if (histFilter === 'win')  list = list.filter(t => (t.pnl_dollar||0) >= 0);
  if (histFilter === 'loss') list = list.filter(t => (t.pnl_dollar||0) < 0);

  const closedRows = list.map(t => {
    const pnlCls  = (t.pnl_dollar||0) >= 0 ? 'up' : 'dn';
    const pnlStr  = ((t.pnl_dollar||0)>=0?'+$':'-$') + Math.abs(t.pnl_dollar||0).toFixed(0);
    const pctStr  = ((t.pnl_pct||0)>=0?'+':'') + (t.pnl_pct||0).toFixed(1) + '%';
    const date    = t.exit_time ? new Date(t.exit_time).toLocaleDateString('en',{month:'numeric',day:'numeric'}) : '—';
    const hold    = t.hold_hours != null
                    ? (t.hold_hours < 1 ? Math.round(t.hold_hours*60)+'m' : t.hold_hours.toFixed(1)+'h')
                    : '—';
    const parsed  = parseOCC(t.option_symbol || '');
    const contract = parsed
                    ? `${parsed.type==='C'?'Call':'Put'} $${parsed.strike}`
                    : esc((t.option_symbol||'').slice(0,14));
    return `<tr>
      <td class="mono" style="color:var(--muted)">${date}</td>
      <td class="sym">${esc(t.underlying)}</td>
      <td>${stratBadge(t.strategy)}</td>
      <td style="color:var(--muted);font-size:11px">${contract}</td>
      <td class="mono">${t.quantity}</td>
      <td class="mono">$${(t.entry_price||0).toFixed(2)}</td>
      <td class="mono">$${(t.exit_price||0).toFixed(2)}</td>
      <td class="${pnlCls} mono">${pnlStr}</td>
      <td class="${pnlCls} mono">${pctStr}</td>
      <td class="mono" style="color:var(--muted)">${hold}</td>
      <td class="mono" style="color:var(--muted)">${t.setup_score != null ? t.setup_score : '—'}</td>
      <td><span class="badge ${exitBadgeCls(t.exit_reason)}">${esc(t.exit_reason||'—')}</span></td>
    </tr>`;
  }).join('');

  if (!openRows && !closedRows) {
    document.getElementById('historyBody').innerHTML =
      '<tr class="tbl-empty"><td colspan="12">No open positions or closed trades yet — they\'ll appear here as the bot trades.</td></tr>';
    return;
  }
  document.getElementById('historyBody').innerHTML = openRows + closedRows;
}

// ── Decision Log tab ──────────────────────────────────────────────────────────
function filterDecisions(type, btn) {
  decFilter = type;
  document.querySelectorAll('#decChips .chip').forEach(c => c.classList.remove('on'));
  btn.classList.add('on');
  renderDecisions();
}

function decActionCls(a) {
  const map = { traded:'b-green', rejected:'b-red', considered:'b-amber', exited:'b-blue', halt:'b-red', skipped:'b-gray' };
  return map[a] || 'b-gray';
}

function renderDecisions() {
  let list = (allDecisions || []).slice().reverse();
  if (decFilter !== 'all') list = list.filter(d => d.action === decFilter);

  if (!list.length) {
    document.getElementById('decisionsList').innerHTML =
      '<div style="color:var(--muted);font-size:12px;padding:16px 0">No decisions recorded yet — run the bot to see logs here.</div>';
    return;
  }

  document.getElementById('decisionsList').innerHTML = list.slice(0,100).map(d => {
    const dt  = new Date(d.timestamp);
    const ts  = `${dt.getMonth()+1}/${dt.getDate()} ${dt.getHours().toString().padStart(2,'0')}:${dt.getMinutes().toString().padStart(2,'0')}`;
    const strat = d.strategy ? esc(d.strategy).replace(/_/g,' ') : '';
    const scoreStr = d.score != null ? String(d.score) : '';
    return `<div class="d-row">
      <div class="d-time">${ts}</div>
      <div class="d-body">
        <span class="d-ticker">${esc(d.ticker)}</span>
        <span style="margin-left:6px"><span class="badge ${decActionCls(d.action)}">${esc(d.action||'?')}</span></span>
        <div class="d-reason">${esc(d.reason || 'No reason recorded')}</div>
        ${strat ? `<div class="d-strat">${strat}</div>` : ''}
      </div>
      <div class="d-score">${scoreStr}</div>
    </div>`;
  }).join('');
}

// ══════════════════════════════════════════════════════════════════════════════
// ANALYTICS TAB
// ══════════════════════════════════════════════════════════════════════════════
function isDarkMode() {
  const t = document.documentElement.getAttribute('data-theme');
  if (t === 'dark')  return true;
  if (t === 'light') return false;
  return matchMedia('(prefers-color-scheme: dark)').matches;
}
function chartTheme() {
  const isDark = isDarkMode();
  return {
    textClr: isDark ? 'rgba(255,255,255,0.35)' : 'rgba(0,0,0,0.40)',
    gridClr: isDark ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.05)',
    isDark,
  };
}
function hexAlpha(hex, a) {
  hex = (hex || '').trim();
  if (hex.length === 4) hex = '#'+hex[1]+hex[1]+hex[2]+hex[2]+hex[3]+hex[3];
  if (hex.length < 7) return 'rgba(128,128,128,'+a+')';
  const r = parseInt(hex.slice(1,3), 16);
  const g = parseInt(hex.slice(3,5), 16);
  const b = parseInt(hex.slice(5,7), 16);
  return `rgba(${r},${g},${b},${a})`;
}

function renderAnalytics() {
  renderMetrics();
  renderEquityCurve();
  renderStrategyStats();
  renderScoreBuckets();
  renderRejectionReasons();
  renderHoldVsPnl();
  renderFunnel();
}

// ── 0. Headline performance metrics ──────────────────────────────────────────
function renderMetrics() {
  const m = (currentState && currentState.metrics) || null;
  const set = (id, txt, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = txt;
    if (cls) el.className = 'stat-value ' + cls;
  };
  if (!m) {
    ['mSharpe','mMaxDD','mPF','mExp','mWR','mTR'].forEach(id => set(id, '—'));
    return;
  }
  const t = m.trades || {};

  // Sharpe
  set('mSharpe', m.sharpe == null ? '—' : m.sharpe.toFixed(2),
      m.sharpe == null ? '' : (m.sharpe >= 1 ? 'up' : m.sharpe < 0 ? 'dn' : ''));

  // Max drawdown (always shown as a negative/red magnitude)
  const dd = m.max_drawdown && m.max_drawdown.pct != null ? m.max_drawdown.pct : null;
  set('mMaxDD', dd == null ? '—' : '-' + dd.toFixed(1) + '%', 'dn');

  // Profit factor. Backend sends null when there are no losing trades
  // (JSON-safe; can't send Infinity). Show ∞ when there are wins but no
  // losses, otherwise — (no data).
  let pf = t.profit_factor;
  if (pf == null) {
    const undefinedButGood = (t.count > 0 && t.losses === 0 && t.wins > 0);
    set('mPF', undefinedButGood ? '∞' : '—', undefinedButGood ? 'up' : '');
  } else if (!isFinite(pf)) {
    set('mPF', '∞', 'up');
  } else {
    set('mPF', Number(pf).toFixed(2), Number(pf) >= 1 ? 'up' : 'dn');
  }

  // Expectancy ($/trade)
  const exp = t.expectancy || 0;
  set('mExp', (exp >= 0 ? '+$' : '-$') + Math.abs(exp).toFixed(0), exp >= 0 ? 'up' : 'dn');

  // Win rate
  set('mWR', t.count ? (t.win_rate || 0).toFixed(0) + '%' : '—',
      (t.win_rate || 0) >= 50 ? 'up' : '');

  // Total return
  const tr = m.total_return_pct;
  set('mTR', tr == null ? '—' : (tr >= 0 ? '+' : '') + tr.toFixed(1) + '%',
      tr == null ? '' : (tr >= 0 ? 'up' : 'dn'));
}

// ── 1. Equity curve ──────────────────────────────────────────────────────────
function renderEquityCurve() {
  const curve = (currentState && currentState.equity_curve) || [];
  const meta  = document.getElementById('equityMeta');

  if (!curve.length) {
    meta.innerHTML = '<span class="an-kpi">No equity snapshots yet — bot must run at least once</span>';
    if (equityChart) { equityChart.destroy(); equityChart = null; }
    return;
  }

  const latest    = curve[curve.length - 1];
  const first     = curve[0];
  const peak      = curve.reduce((m, p) => p.equity > m ? p.equity : m, -Infinity);
  const drawdown  = peak > 0 ? ((peak - latest.equity) / peak * 100) : 0;
  const totalChg  = first.equity > 0 ? ((latest.equity - first.equity) / first.equity * 100) : 0;
  const chgCls    = totalChg >= 0 ? 'up' : 'dn';
  const ddCls     = drawdown >= 5 ? 'dn' : drawdown >= 2 ? 'mu' : 'mu';

  meta.innerHTML =
    `<span class="an-kpi">Current <strong>$${latest.equity.toLocaleString('en',{maximumFractionDigits:0})}</strong></span>` +
    `<span class="an-kpi">Since start <strong class="${chgCls}">${totalChg >= 0 ? '+' : ''}${totalChg.toFixed(1)}%</strong></span>` +
    `<span class="an-kpi">Drawdown from peak <strong class="${ddCls}">${drawdown.toFixed(1)}%</strong></span>` +
    `<span class="an-kpi">Peak <strong>$${peak.toLocaleString('en',{maximumFractionDigits:0})}</strong></span>`;

  // Downsample if too many points
  const maxPts = 150;
  const step   = Math.max(1, Math.ceil(curve.length / maxPts));
  const samp   = curve.filter((_, i) => i % step === 0 || i === curve.length - 1);

  const labels = samp.map(p => {
    const d = new Date(p.timestamp);
    return `${d.getMonth()+1}/${d.getDate()} ${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}`;
  });
  const data   = samp.map(p => p.equity);

  const { textClr, gridClr, isDark } = chartTheme();
  const trend  = data.length > 1 && data[data.length - 1] >= data[0];
  const lineClr = trend ? getCssVar('--green') : getCssVar('--red');
  const fillClr = hexAlpha(lineClr, isDark ? 0.12 : 0.10);

  if (equityChart) equityChart.destroy();
  equityChart = new Chart(document.getElementById('equityChart'), {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data, borderColor: lineClr, borderWidth: 1.5,
        pointRadius: 0, pointHoverRadius: 4,
        tension: 0.25, fill: true, backgroundColor: fillClr,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => '$' + ctx.raw.toLocaleString('en', {maximumFractionDigits: 0}) } }
      },
      scales: {
        x: { ticks: { color: textClr, font:{size:9}, maxTicksLimit: 7, maxRotation: 0 }, grid: { display: false } },
        y: { ticks: { color: textClr, font:{size:10}, callback: v => '$' + (v/1000).toFixed(1) + 'k' }, grid: { color: gridClr } }
      }
    }
  });
}

// ── 2. Strategy performance ──────────────────────────────────────────────────
function renderStrategyStats() {
  const el = document.getElementById('strategyContent');

  // Per-strategy aggregates from CLOSED trades (real P&L) + decisions (signal count)
  const closed   = allHistory;
  const traded   = allDecisions.filter(d => d.action === 'traded');

  if (!closed.length && !traded.length) {
    el.innerHTML = '<div class="no-data">No trade activity recorded yet</div>';
    return;
  }

  // Build a map keyed by strategy name pulling from both sources.
  const groups = {};
  const get = (s) => (groups[s] = groups[s] || {
    trades: 0, wins: 0, pnl: 0, scores: [], signals: 0, last: null
  });

  closed.forEach(t => {
    const s = t.strategy || 'unknown';
    const g = get(s);
    g.trades++;
    if ((t.pnl_dollar || 0) >= 0) g.wins++;
    g.pnl += (t.pnl_dollar || 0);
    if (t.exit_time && (!g.last || t.exit_time > g.last)) g.last = t.exit_time;
  });
  traded.forEach(d => {
    const s = d.strategy || 'unknown';
    const g = get(s);
    g.signals++;
    if (d.score != null) g.scores.push(d.score);
    if (d.timestamp && (!g.last || d.timestamp > g.last)) g.last = d.timestamp;
  });

  const sorted = Object.entries(groups)
    .sort((a, b) => (b[1].pnl - a[1].pnl) || (b[1].trades - a[1].trades));

  const rows = sorted.map(([strat, g]) => {
    const display = strat.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    const wr      = g.trades ? Math.round(g.wins / g.trades * 100) + '%' : '—';
    const pnlStr  = g.trades ? ((g.pnl >= 0 ? '+$' : '-$') + Math.abs(g.pnl).toLocaleString('en', {maximumFractionDigits: 0})) : '—';
    const pnlCls  = g.trades ? (g.pnl >= 0 ? 'up' : 'dn') : 'mu';
    const avgScr  = g.scores.length ? (g.scores.reduce((a,c)=>a+c,0)/g.scores.length).toFixed(0) : '—';
    let lastStr = '—';
    if (g.last) {
      const ageDays = Math.floor((Date.now() - new Date(g.last).getTime()) / 86400000);
      lastStr = ageDays <= 0 ? 'today' : ageDays === 1 ? '1d ago' : ageDays + 'd ago';
    }
    return `<div class="strat-row">
      <div style="min-width:0">
        <div class="strat-name">${esc(display)}</div>
        <div class="strat-name-sub">${g.signals} signals · ${g.trades} closed · last ${lastStr}</div>
      </div>
      <div class="strat-chips">
        <div class="strat-chip"><span class="strat-num ${pnlCls}">${pnlStr}</span><span class="strat-lbl">Net P&amp;L</span></div>
        <div class="strat-chip"><span class="strat-num">${wr}</span><span class="strat-lbl">Win Rate</span></div>
        <div class="strat-chip"><span class="strat-num">${avgScr}</span><span class="strat-lbl">Avg Score</span></div>
      </div>
    </div>`;
  }).join('');

  // Day vs Swing summary (from server-computed metrics.by_trade_type)
  const bt = (currentState && currentState.metrics && currentState.metrics.by_trade_type) || {};
  const ttPill = (label, m, color) => {
    if (!m || !m.count) return `<div class="strat-chip"><span class="strat-num mu">—</span><span class="strat-lbl">${label}</span></div>`;
    const net = (m.gross_profit||0) - (m.gross_loss||0);
    const cls = net >= 0 ? 'up' : 'dn';
    return `<div class="strat-chip">
      <span class="strat-num ${cls}">${m.count}</span>
      <span class="strat-lbl" style="color:${color}">${label} ${(m.win_rate||0).toFixed(0)}%W</span>
    </div>`;
  };
  const dayswing = `<div class="strat-row" style="border-bottom:2px solid var(--border2)">
    <div style="min-width:0"><div class="strat-name">Day vs Swing</div>
      <div class="strat-name-sub">closed trades by trade type</div></div>
    <div class="strat-chips">
      ${ttPill('Day', bt.day, 'var(--purple)')}
      ${ttPill('Swing', bt.swing, 'var(--blue)')}
    </div>
  </div>`;

  el.innerHTML = dayswing + rows;
}

// ── 3. Score → outcome buckets ───────────────────────────────────────────────
function renderScoreBuckets() {
  const trades = allHistory.filter(t => t.setup_score != null && t.pnl_pct != null);

  const buckets = [
    { label: '0–25',   min: 0,  max: 25,    trades: [] },
    { label: '25–50',  min: 25, max: 50,    trades: [] },
    { label: '50–75',  min: 50, max: 75,    trades: [] },
    { label: '75–100', min: 75, max: 101,   trades: [] },
  ];
  trades.forEach(t => {
    const b = buckets.find(b => t.setup_score >= b.min && t.setup_score < b.max);
    if (b) b.trades.push(t.pnl_pct);
  });

  const labels = buckets.map(b => b.label);
  const avgs   = buckets.map(b => b.trades.length ? +(b.trades.reduce((a,c)=>a+c,0)/b.trades.length).toFixed(1) : 0);
  const counts = buckets.map(b => b.trades.length);

  const cGreen = getCssVar('--green');
  const cRed   = getCssVar('--red');
  const cMuted = getCssVar('--surface3') || 'rgba(128,128,128,0.2)';
  const colors = buckets.map((b, i) =>
    b.trades.length === 0 ? cMuted : avgs[i] >= 0 ? cGreen : cRed
  );

  const { textClr, gridClr } = chartTheme();

  if (scoreChart) scoreChart.destroy();
  scoreChart = new Chart(document.getElementById('scoreChart'), {
    type: 'bar',
    data: { labels, datasets: [{ data: avgs, backgroundColor: colors, borderRadius: 4, borderSkipped: false }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => {
              const c = counts[ctx.dataIndex];
              if (!c) return 'No trades in this range';
              const v = ctx.raw;
              return [`Avg P&L: ${v >= 0 ? '+' : ''}${v}%`, `${c} trade${c !== 1 ? 's' : ''}`];
            }
          }
        }
      },
      scales: {
        x: { ticks: { color: textClr, font: {size: 11} }, grid: { display: false } },
        y: { ticks: { color: textClr, font: {size: 10}, callback: v => (v >= 0 ? '+' : '') + v + '%' }, grid: { color: gridClr } }
      }
    }
  });
}

// ── 4. Rejection reasons ─────────────────────────────────────────────────────
function categorizeRejection(reason) {
  const r = (reason || '').toLowerCase();
  if (r.includes('score') || r.includes('threshold') || r.includes('below'))             return 'Score too low';
  if (r.includes('loss limit') || r.includes('halt') || r.includes('daily loss'))        return 'Daily loss limit';
  if (r.includes('max position') || r.includes('position limit') || r.includes('slots')) return 'Position limit';
  if (r.includes('market closed') || r.includes('not open') || r.includes('hours'))      return 'Market closed';
  if (r.includes('contract') || r.includes('qualifying') || r.includes('no option'))     return 'No qualifying contract';
  if (r.includes('iv') || r.includes('flow') || r.includes('momentum') || r.includes('filter')) return 'Filter failed';
  if (r.includes('earnings'))                                                            return 'Earnings timing';
  if (r.includes('liquid') || r.includes('spread') || r.includes('volume'))              return 'Liquidity';
  return 'Other';
}

function renderRejectionReasons() {
  const rejected = allDecisions.filter(d => d.action === 'rejected');
  const legend   = document.getElementById('rejectLegend');

  if (!rejected.length) {
    if (rejectChart) rejectChart.destroy();
    rejectChart = new Chart(document.getElementById('rejectChart'), {
      type: 'doughnut',
      data: { labels:['No rejections yet'], datasets:[{ data:[1], backgroundColor:['rgba(128,128,128,0.1)'], borderWidth:0 }] },
      options: { responsive:true, maintainAspectRatio:false, cutout:'62%', plugins:{legend:{display:false},tooltip:{enabled:false}} }
    });
    legend.innerHTML = '<span style="color:var(--muted)">No rejection data yet</span>';
    return;
  }

  const cats = {};
  rejected.forEach(d => {
    const c = categorizeRejection(d.reason);
    cats[c] = (cats[c] || 0) + 1;
  });

  const sorted  = Object.entries(cats).sort((a,b) => b[1] - a[1]);
  const labels  = sorted.map(([k]) => k);
  const data    = sorted.map(([,v]) => v);
  const palette = [getCssVar('--red'), getCssVar('--amber'), getCssVar('--blue'), getCssVar('--purple'), getCssVar('--green'), getCssVar('--muted')];
  const colors  = labels.map((_, i) => palette[i % palette.length]);

  if (rejectChart) rejectChart.destroy();
  rejectChart = new Chart(document.getElementById('rejectChart'), {
    type: 'doughnut',
    data: { labels, datasets: [{ data, backgroundColor: colors, borderWidth: 0 }] },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '62%',
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => `${ctx.label}: ${ctx.raw} (${Math.round(ctx.raw / rejected.length * 100)}%)` } }
      }
    }
  });

  legend.innerHTML = colors.map((c, i) =>
    `<span style="display:flex;align-items:center;gap:5px"><span class="legend-dot" style="background:${c}"></span>${esc(labels[i])} <strong style="color:var(--text2)">${data[i]}</strong></span>`
  ).join('');
}

// ── 5. Hold time vs P&L scatter ──────────────────────────────────────────────
function renderHoldVsPnl() {
  const trades = allHistory.filter(t => t.hold_hours != null && t.pnl_pct != null);

  if (trades.length < 1) {
    if (holdChart) holdChart.destroy();
    holdChart = new Chart(document.getElementById('holdChart'), {
      type: 'scatter',
      data: { datasets: [{ data: [], backgroundColor: 'rgba(0,0,0,0)' }] },
      options: {
        responsive:true, maintainAspectRatio:false,
        plugins:{ legend:{display:false}, tooltip:{enabled:false} },
        scales: {
          x: { ticks: { display:false }, grid: { display:false } },
          y: { ticks: { display:false }, grid: { display:false } }
        }
      }
    });
    return;
  }

  const cGreen = getCssVar('--green');
  const cRed   = getCssVar('--red');
  const points = trades.map(t => ({
    x: t.hold_hours, y: t.pnl_pct,
    ticker: t.underlying, exitReason: t.exit_reason
  }));

  const { textClr, gridClr } = chartTheme();

  if (holdChart) holdChart.destroy();
  holdChart = new Chart(document.getElementById('holdChart'), {
    type: 'scatter',
    data: {
      datasets: [{
        data: points.map(p => ({ x: p.x, y: p.y })),
        backgroundColor: points.map(p => p.y >= 0 ? hexAlpha(cGreen, 0.55) : hexAlpha(cRed, 0.55)),
        borderColor:     points.map(p => p.y >= 0 ? cGreen : cRed),
        borderWidth: 1, pointRadius: 4, pointHoverRadius: 6
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => {
              const p = points[ctx.dataIndex];
              const lines = [
                `${p.ticker}: ${(p.y >= 0 ? '+' : '') + p.y.toFixed(1)}%`,
                `Hold: ${p.x < 1 ? Math.round(p.x*60)+'m' : p.x.toFixed(1)+'h'}`,
              ];
              if (p.exitReason) lines.push(`Exit: ${p.exitReason}`);
              return lines;
            }
          }
        }
      },
      scales: {
        x: {
          title: { display: true, text: 'Hold time (hours)', color: textClr, font:{size:10} },
          ticks: { color: textClr, font:{size:10}, callback: v => v < 1 ? Math.round(v*60)+'m' : v+'h' },
          grid:  { color: gridClr }
        },
        y: {
          title: { display: true, text: 'P&L %', color: textClr, font:{size:10} },
          ticks: { color: textClr, font:{size:10}, callback: v => (v >= 0 ? '+' : '') + v + '%' },
          grid:  { color: gridClr }
        }
      }
    }
  });
}

// ── 6. Scan → trade funnel ───────────────────────────────────────────────────
function renderFunnel() {
  const total = allDecisions.length;
  const el    = document.getElementById('funnelContent');

  if (!total) {
    el.innerHTML = '<div class="no-data">No scan data yet</div>';
    return;
  }

  const scanned    = new Set(allDecisions.map(d => d.ticker)).size;
  const considered = allDecisions.filter(d => d.action === 'considered' || d.action === 'traded').length;
  const traded     = allDecisions.filter(d => d.action === 'traded').length;
  const rejected   = allDecisions.filter(d => d.action === 'rejected').length;
  const skipped    = allDecisions.filter(d => d.action === 'skipped').length;

  const steps = [
    { lbl: 'Unique tickers scanned',     n: scanned,    color: getCssVar('--blue'),  conv: 100 },
    { lbl: 'Met scanner criteria',       n: considered, color: getCssVar('--amber'), conv: scanned    > 0 ? Math.round(considered / scanned    * 100) : 0 },
    { lbl: 'Trade taken',                n: traded,     color: getCssVar('--green'), conv: considered > 0 ? Math.round(traded     / considered * 100) : 0 },
  ];
  const max = steps[0].n || 1;

  el.innerHTML = steps.map(s => {
    const w = Math.round(s.n / max * 100);
    return `<div class="funnel-step">
      <div class="funnel-row">
        <span class="funnel-lbl">${s.lbl}</span>
        <span class="funnel-num">${s.n.toLocaleString()}</span>
      </div>
      <div class="funnel-track"><div class="funnel-fill" style="width:${w}%;background:${s.color}"></div></div>
      <div class="funnel-conv">${s.conv}% conversion</div>
    </div>`;
  }).join('') +
  `<div class="funnel-foot">
    <span>Rejected: <strong>${rejected}</strong></span>
    <span>Skipped: <strong>${skipped}</strong></span>
    <span>Total decisions: <strong>${total}</strong></span>
  </div>`;
}

// ── Init ──────────────────────────────────────────────────────────────────────
initSettings();
loadState();
