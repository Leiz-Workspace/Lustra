// Lustra desktop renderer (plain script, no bundler). Talks to main only through window.lustra (preload bridge or mock.js).
// In Electron the preload bridge exists before this script runs; in a plain browser mock.js is loaded first (see start()).
let L = window.lustra;

// ---------------- helpers ----------------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
}
const fmt = (n) => {
  n = Number(n) || 0;
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}K`;
  return String(n);
};
const nf = new Intl.NumberFormat('vi-VN');
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
// write only on change: no style/layout invalidation for values that did not move
const setText = (el, t) => { if (el.textContent !== t) el.textContent = t; };
const setHidden = (el, hide) => { if (el.hidden !== hide) el.hidden = hide; };
const slot = (el, text) => { setHidden(el, text == null); if (text != null) setText(el, text); };

// Deferred DOM work (job progress, log lines) runs at most once per animation frame, however many events arrived.
const frameTasks = new Set();
let frameReq = 0;
function runFrame() {
  frameReq = 0;
  const tasks = [...frameTasks];
  frameTasks.clear();
  for (const fn of tasks) {
    try { fn(); } catch (err) { console.error(err); }
  }
}
function onFrame(fn) {
  frameTasks.add(fn);
  if (!frameReq) frameReq = requestAnimationFrame(runFrame);
}

// Append-only <pre> log. New lines are buffered and appended once per frame as a block-level chunk (so layout only
// touches the new block, not the whole text); old lines are dropped chunk by chunk only after `slack` extra lines
// piled up. Follows new output only while the user is at the bottom.
const LOG_CHUNK = 250;
function createLogView(pre, max, slack = Math.ceil(max / 4)) {
  const chunks = []; // [{ node, n }] in document order
  let count = 0;
  let pending = [];
  let stick = true;
  const atBottom = () => pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  pre.addEventListener('scroll', () => { stick = atBottom(); }, { passive: true });
  const chunked = (lines) => {
    const frag = document.createDocumentFragment();
    for (let i = 0; i < lines.length; i += LOG_CHUNK) {
      const part = lines.slice(i, i + LOG_CHUNK);
      const node = h('span', { class: 'log-chunk' }, part.join('\n'));
      chunks.push({ node, n: part.length });
      count += part.length;
      frag.append(node);
    }
    return frag;
  };
  const reveal = () => { if (stick && pre.isConnected) pre.scrollTop = pre.scrollHeight; };
  function flush() {
    if (!pending.length) return;
    const add = pending;
    pending = [];
    if (!pre.isConnected) return;
    if (count + add.length > max + slack) {
      const h0 = stick ? 0 : pre.scrollHeight;
      const t0 = stick ? 0 : pre.scrollTop;
      while (chunks.length && count + add.length - chunks[0].n >= max) {
        const c = chunks.shift();
        c.node.remove();
        count -= c.n;
      }
      // keep the lines the user is reading in place while the top of the log is trimmed
      if (!stick) pre.scrollTop = Math.max(0, t0 - (h0 - pre.scrollHeight));
    }
    pre.append(chunked(add));
    reveal();
  }
  return {
    push(lines) {
      pending.push(...lines);
      if (pending.length > max) pending.splice(0, pending.length - max);
      onFrame(flush);
    },
    reset(lines = []) {
      pending = [];
      chunks.length = 0;
      count = 0;
      pre.replaceChildren(chunked(lines.slice(-max)));
      reveal();
    },
    clear() { stick = true; this.reset([]); },
    reveal,
  };
}

const ago = (ts) => {
  const s = (Date.now() - (Number(ts) || 0)) / 1000;
  if (s < 60) return 'vừa xong';
  if (s < 3600) return `${Math.floor(s / 60)} phút trước`;
  if (s < 86400) return `${Math.floor(s / 3600)} giờ trước`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} ngày trước`;
  return new Date(ts).toLocaleDateString('vi-VN');
};

function toast(msg, kind = '') {
  const el = h('div', { class: `toast ${kind}` }, msg);
  $('#toasts').append(el);
  setTimeout(() => el.remove(), kind === 'err' ? 7000 : 3500);
}
function errMsg(err) {
  const m = String(err?.message || err || 'lỗi');
  return m.replace(/^Error invoking remote method '[^']+': (Error: )?/, '');
}
// Optional sidecar methods (newer than main / the core): the first "not allowed / not found" answer turns the feature off
// for the session instead of retrying on every keystroke.
const unsupported = new Set();
const isUnsupported = (err) => /not allowed|method not found|unknown method|no handler|-32601/i.test(errMsg(err));
async function rpcOptional(method, params) {
  if (unsupported.has(method)) return null;
  try {
    return await L.rpc(method, params);
  } catch (err) {
    if (isUnsupported(err)) { unsupported.add(method); return null; }
    throw err;
  }
}
function openModal(content) {
  const card = $('#modal-card');
  card.replaceChildren(content);
  $('#modal').hidden = false;
}
function closeModal() { $('#modal').hidden = true; $('#modal-card').replaceChildren(); }
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#modal').hidden) closeModal(); });

function prompt2(title, fields) {
  return new Promise((resolve) => {
    const inputs = fields.map((f) => h('input', { type: f.type || 'text', placeholder: f.placeholder || '', value: f.value || '' }));
    const form = h('div', {},
      h('h2', { text: title }),
      h('div', { class: 'form-grid' }, fields.map((f, i) => h('label', { class: 'field' }, h('span', { text: f.label }), inputs[i]))),
      h('div', { class: 'modal-actions' },
        h('button', { class: 'btn ghost', onclick: () => { closeModal(); resolve(null); } }, 'Huỷ'),
        h('button', { class: 'btn primary', onclick: () => { const v = inputs.map((x) => x.value.trim()); closeModal(); resolve(v); } }, 'OK')));
    openModal(form);
    inputs[0]?.focus();
  });
}

// ---------------- state ----------------
const S = {
  settings: null, appearance: null, secrets: null, info: null,
  modules: [], module: 'rx',
  chips: [], chipSeq: 0, selected: new Set(),
  // moved: the user picked a suggestion with the arrow keys (only then Enter takes a suggestion instead of the typed text)
  ac: { items: [], index: -1, seq: 0, moved: false, fuzzy: false },
  preview: { page: 0, items: [], selected: new Set(), site: '', module: '', tags: '', showExcluded: false, seq: 0, hasMore: true },
  countSeq: 0,
  tagLists: null, // modules that have a local tag list (tags.status), null until known
  jobs: new Map(),
  logLines: [],
};
const META_PREFIXES = ['id', 'score', 'rating', 'user', 'md5', 'width', 'height', 'parent', 'source', 'updated', 'sort', 'order', 'date',
  'status', 'fav', 'favorited_by', 'favoritedby', 'pool', 'set', 'poster', 'filetype', 'limit', 'age', 'mpixels', 'ratio', 'favcount', 'tagcount'];
const reMeta = new RegExp(`^(?:${META_PREFIXES.join('|')})(?:[:=]|[<>]=?)`);
const POST_URL = {
  rx: (id) => `https://rule34.xxx/index.php?page=post&s=view&id=${id}`,
  db: (id) => `https://danbooru.donmai.us/posts/${id}`,
  en: (id) => `https://e621.net/posts/${id}`,
};
const STATE_VI = {
  IDLE: 'Chờ', LAUNCHING: 'Khởi động', SEARCHING: 'Đang tìm', SCANNING_PAGES1: 'Quét trang (1/2)', SCANNING_PAGES2: 'Quét trang (2/2)',
  FILTERING_ITEMS1: 'Lọc (1/4)', FILTERING_ITEMS2: 'Lọc (2/4)', FILTERING_ITEMS3: 'Lọc (3/4)', FILTERING_ITEMS4: 'Lọc (4/4)',
  DOWNLOADING: 'Đang tải', DONE: 'Hoàn tất',
};
const GENDER_VI = { only_female: 'Nữ', only_male: 'Nam', both: 'Nam+Nữ', none: 'Không có', unknown: '?' };
const CONF_VI = { high: 'cao', medium: 'vừa', low: 'thấp' };
const JOB_VI = { queued: 'Chờ', running: 'Đang chạy', cancelling: 'Đang huỷ', cancelled: 'Đã huỷ', done: 'Xong', failed: 'Lỗi' };

const moduleInfo = (abbr) => S.modules.find((m) => m.abbr === abbr);
const hasCap = (abbr, cap) => {
  const caps = moduleInfo(abbr)?.capabilities;
  const name = { videoFormat: 'video_format_choice', parchi: 'parent_child', gender: 'gender_tags' }[cap] || cap;
  if (Array.isArray(caps)) return caps.includes(name);
  if (cap === 'videoFormat') return abbr === 'rs';
  if (cap === 'parchi') return ['rx', 'db', 'en', 'xb', 'bb'].includes(abbr);
  if (cap === 'gender') return ['rx', 'db', 'en', 'bb'].includes(abbr);
  return true;
};

// ---------------- navigation ----------------
function showPage(name) {
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.page === name));
  $$('.page').forEach((p) => p.classList.toggle('active', p.id === `page-${name}`));
  if (name === 'tags') refreshTagStatus();
  if (name === 'extensions') renderExtensions();
  if (name === 'log') showLogPage();
  if (name === 'downloads') jobViews.forEach((v) => v.logView?.reveal());
}
$$('#nav button').forEach((b) => b.addEventListener('click', () => showPage(b.dataset.page)));

// ---------------- appearance ----------------
function applyAppearance(a) {
  S.appearance = a;
  document.documentElement.dataset.theme = a.theme;
  $('#brand-icon').src = a.iconUrl;
  const bg = a.background || {};
  const bgEl = $('#bg');
  bgEl.className = bg.mode === 'image' ? 'image' : bg.mode === 'none' ? 'none' : '';
  $('#bg-img').src = a.backgroundUrl;
  const root = document.documentElement.style;
  root.setProperty('--bg-blur', `${bg.mode === 'icon' ? (bg.blur ?? 4) : 0}px`);
  root.setProperty('--bg-opacity', String(bg.opacity ?? 0.9));
  root.setProperty('--bg-dim', String(bg.dim ?? 0.18));
  renderAppearanceSettings();
}

// ---------------- modules ----------------
async function loadModules() {
  try {
    S.modules = await L.rpc('ext.list');
  } catch (err) {
    toast(`Không đọc được danh sách extension: ${errMsg(err)}`, 'err');
    return;
  }
  const installed = S.modules.filter((m) => m.installed);
  if (!installed.some((m) => m.abbr === S.module)) S.module = installed[0]?.abbr || 'rx';
  renderModuleButton();
  if (S.settings) { renderApiKeys(); renderTagLimits(); }
}
async function loadTagLists() {
  try {
    const rows = await L.rpc('tags.status');
    S.tagLists = new Set(rows.filter((r) => r.size > 0).map((r) => r.module));
    S.chips.forEach(decorateChip);
    scheduleResolve();
  } catch { /* sidecar not ready yet: retried when it reports ready */ }
}
function renderModuleButton() {
  const m = moduleInfo(S.module);
  $('#module-abbr').textContent = S.module;
  $('#module-name').textContent = m?.name || S.module;
  $('#f-vformat-wrap').hidden = !hasCap(S.module, 'videoFormat');
  $('#f-parchi-wrap').hidden = !hasCap(S.module, 'parchi');
  renderGenderFilter();
}

// ---------------- gender filter (tier 0: tags) ----------------
function genderState() {
  const g = S.settings?.download?.gender || {};
  return { select: Array.isArray(g.select) ? g.select : [], conf: g.conf || 'medium', strict: !!g.strict };
}
function renderGenderFilter() {
  if (!S.settings) return;
  const g = genderState();
  $$('#f-gender .pill').forEach((b) => b.classList.toggle('on', g.select.includes(b.dataset.g)));
  $('#f-gender-conf').value = g.conf;
  $('#f-gender-strict').checked = g.strict;
  const hint = [];
  if (g.select.length) hint.push('đang bật: danh sách chặn an toàn cũng được áp dụng');
  if (g.select.length && !hasCap(S.module, 'gender')) hint.push(`${S.module.toUpperCase()} ít tag giới tính, phần lớn post sẽ là "Không rõ"`);
  $('#f-gender-hint').textContent = hint.join(' · ');
}
function saveGender(patch) {
  const g = { ...genderState(), ...patch };
  S.settings.download.gender = g;
  saveSettings({ download: { gender: g } });
  renderGenderFilter();
}
$('#f-gender').addEventListener('click', (e) => {
  const b = e.target.closest('.pill');
  if (!b) return;
  const labels = b.dataset.g === 'unknown' ? ['none', 'unknown'] : [b.dataset.g];
  const sel = new Set(genderState().select);
  const on = labels.every((l) => sel.has(l));
  labels.forEach((l) => (on ? sel.delete(l) : sel.add(l)));
  saveGender({ select: [...sel] });
});
$('#f-gender-conf').addEventListener('change', (e) => saveGender({ conf: e.target.value }));
$('#f-gender-strict').addEventListener('change', (e) => saveGender({ strict: e.target.checked }));
function renderModuleMenu() {
  const menu = $('#module-menu');
  const installed = S.modules.filter((m) => m.installed);
  menu.replaceChildren(
    ...installed.map((m) => h('button', { class: m.abbr === S.module ? 'on' : '', onclick: () => setModule(m.abbr) },
      h('span', { class: 'mod-abbr' }, m.abbr), h('span', {}, m.name), m.default ? h('span', { class: 'tagpill acc', style: 'margin-left:auto' }, 'mặc định') : null)),
    h('div', { class: 'menu-foot' }, `${S.modules.length - installed.length} extension khác có thể cài trong mục Extensions`),
  );
}
function warmupTags(abbr) {
  // load the module tag list in the sidecar ahead of the first keystroke (~1 s for large lists)
  L.rpc('tags.autocomplete', { module: abbr, prefix: 'zz', limit: 1 }).catch(() => {});
}
function setModule(abbr) {
  S.module = abbr;
  warmupTags(abbr);
  $('#module-menu').hidden = true;
  renderModuleButton();
  L.settings.set({ search: { module: abbr } });
  azInstances.forEach((az) => az.reset());
  renderChips(); // chip hints (post counts, aliases) belong to the module's tag list
  onQueryChanged();
}
$('#module-btn').addEventListener('click', (e) => {
  e.stopPropagation();
  const menu = $('#module-menu');
  if (menu.hidden) renderModuleMenu();
  menu.hidden = !menu.hidden;
});
document.addEventListener('click', (e) => { if (!e.target.closest('#module-picker')) $('#module-menu').hidden = true; });

// ---------------- tag chips ----------------
// Query text -> tokens. Whitespace splits tokens, except inside an OR group "( a ~ b )" or an exclusion group "-( a , b )".
// Tags may contain parentheses themselves (star_(sky), :( ), so only a word starting with "(" or "-(" opens a group, and it
// only takes in the next word across a group boundary ("(", "~", "," or ")"). open: the text ends inside an unclosed group.
const parenDepth = (s) => { let d = 0; for (const ch of s) { if (ch === '(') d++; else if (ch === ')') d--; } return d; };
function splitQuery(text) {
  const words = String(text || '').trim().split(/\s+/).filter(Boolean);
  const tokens = [];
  let open = false;
  for (let i = 0; i < words.length; i++) {
    let tok = words[i];
    if (/^-?\(/.test(tok)) {
      while (parenDepth(tok) > 0 && i + 1 < words.length && (/[(~,]$/.test(tok) || /^[)~,]/.test(words[i + 1]))) tok += words[++i];
      if (parenDepth(tok) > 0 && i === words.length - 1) open = true;
    }
    tokens.push(tok);
  }
  return { tokens, open };
}
function parseToken(tok) {
  tok = tok.trim();
  if (!tok) return null;
  // groups mirror the core parser (tags_parser.py): parts may contain parentheses, "(star_(sky)~cloud)", "-(star_(sky),cloud)"
  let m = /^-\((.+)\)$/.exec(tok);
  if (m && m[1].includes(',')) {
    const items = m[1].split(',').map((s) => s.trim()).filter(Boolean);
    if (items.length > 1) return { kind: 'neg', items };
  }
  m = /^\((.+)\)$/.exec(tok);
  if (m && m[1].includes('~')) {
    const items = m[1].split('~').map((s) => s.trim()).filter(Boolean);
    if (items.length > 1) return { kind: 'or', items };
  }
  let exclude = false;
  let text = tok;
  if (text.length > 1 && text.startsWith('-')) { exclude = true; text = text.slice(1); }
  return { kind: reMeta.test(text) ? 'meta' : 'tag', text, exclude };
}
function chipToString(c) {
  if (c.kind === 'or') return `(${c.items.join('~')})`;
  if (c.kind === 'neg') return `-(${c.items.join(',')})`;
  return `${c.exclude ? '-' : ''}${c.text}`;
}
const chipsToString = (chips) => chips.map(chipToString).join(' ');
const queryString = () => chipsToString(S.chips);

function addChip(token) {
  const c = parseToken(token);
  if (!c) return;
  const s = chipToString(c);
  if (S.chips.some((x) => chipToString(x) === s)) return;
  S.chips.push({ ...c, id: ++S.chipSeq });
  renderChips();
  onQueryChanged();
}
function addTokens(text) {
  splitQuery(text).tokens.forEach(addChip);
}
function removeChip(id) {
  S.chips = S.chips.filter((c) => c.id !== id);
  S.selected.delete(id);
  renderChips();
  onQueryChanged();
}
function setChips(chips) {
  S.chips = chips;
  S.selected.clear();
  renderChips();
  onQueryChanged();
}

const CHIP_TITLE = { tag: 'Tag', meta: 'Meta tag', or: 'Nhóm OR: post có ít nhất một tag', neg: 'Nhóm loại trừ: bỏ post có đủ tất cả tag' };
const chipViews = new Map(); // chip id -> { root, info, title }: tag hints are patched into a chip without rebuilding the row
function renderChips() {
  chipViews.clear();
  $('#chips').replaceChildren(...S.chips.map((c) => {
    const label = c.kind === 'or' ? c.items.join(' ∨ ') : c.kind === 'neg' ? `−(${c.items.join(' ∧ ')})` : c.text;
    const cls = ['chip', c.kind === 'meta' ? 'meta' : '', c.kind === 'or' ? 'or' : '', c.kind === 'neg' ? 'neg' : '',
      c.exclude ? 'exclude' : '', S.selected.has(c.id) ? 'sel' : ''].join(' ');
    const title = `${CHIP_TITLE[c.kind]} — bấm để chọn, bấm đúp để sửa`;
    const info = h('span', { class: 'ci' });
    const root = h('span', {
      class: cls, title,
      onclick: (e) => { if (e.target.closest('button')) return; toggleChipSel(c.id); },
      ondblclick: (e) => { if (!e.target.closest('button')) editChip(c); },
    },
    h('span', { class: 't' }, label), info,
    (c.kind === 'tag' || c.kind === 'meta') ? h('button', { class: 'pm', title: 'Đảo bao gồm/loại trừ', onclick: () => { c.exclude = !c.exclude; renderChips(); onQueryChanged(); } }, '±') : null,
    h('button', { class: 'x', title: 'Xoá', onclick: () => removeChip(c.id) }, '×'));
    chipViews.set(c.id, { root, info, title });
    decorateChip(c);
    return root;
  }));
  const n = S.selected.size;
  $('#chip-actions').hidden = n === 0;
  $('#chip-sel-count').textContent = `${n} tag đã chọn`;
  scheduleResolve();
}
function toggleChipSel(id) {
  if (S.selected.has(id)) S.selected.delete(id); else S.selected.add(id);
  renderChips();
}
function editChip(c) {
  removeChip(c.id);
  const input = $('#tag-text');
  input.value = chipToString(c);
  input.focus();
}

// ----- chip hints from the module tag list (tags.resolve): post count, alias of a renamed tag, unknown tag -----
const resolveCache = new Map(); // `${module}\n${tag}` -> row | null (request pending) | false (no answer for this tag)
const resolveQueue = new Map(); // module -> Set(tag)
const resolveHasList = new Map(); // module -> the sidecar has a tag list for it (tags.resolve hasList)
const RESOLVE_BATCH = 64;
const rkey = (module, tag) => `${module}\n${tag}`;
function chipTags(c) {
  // tag names and wildcard patterns; metatags and %escaped% tags (no validation on purpose) have nothing to look up
  const raw = c.kind === 'tag' ? [c.text] : (c.kind === 'or' || c.kind === 'neg') ? c.items : [];
  return raw.map((t) => t.replace(/^-/, '').trim().toLowerCase()).filter((t) => t && !reMeta.test(t) && !/^%.*%$/.test(t));
}
function scheduleResolve() {
  if (unsupported.has('tags.resolve') || !L) return;
  const module = S.module;
  if (resolveHasList.get(module) === false || (S.tagLists && !S.tagLists.has(module))) return; // no tag list: nothing to look up
  let queued = false;
  for (const c of S.chips) {
    for (const t of chipTags(c)) {
      const k = rkey(module, t);
      if (resolveCache.has(k)) continue;
      resolveCache.set(k, null);
      if (!resolveQueue.has(module)) resolveQueue.set(module, new Set());
      resolveQueue.get(module).add(t);
      queued = true;
    }
  }
  if (queued) flushResolve();
}
const flushResolve = debounce(async () => {
  const batches = [...resolveQueue];
  resolveQueue.clear();
  for (const [module, set] of batches) {
    const tags = [...set];
    for (let i = 0; i < tags.length; i += RESOLVE_BATCH) {
      const part = tags.slice(i, i + RESOLVE_BATCH);
      let r = null;
      try {
        r = await rpcOptional('tags.resolve', { module, tags: part });
      } catch { /* transient: forget the pending marks so the next render asks again */ }
      if (!r) { part.forEach((t) => resolveCache.delete(rkey(module, t))); continue; }
      if (typeof r.hasList === 'boolean') resolveHasList.set(module, r.hasList);
      // one item per input tag, same order (the sidecar normalizes the names, so match by position)
      const items = Array.isArray(r.items) ? r.items : [];
      const byName = new Map(items.map((it) => [String(it.tag).toLowerCase(), it]));
      part.forEach((t, k) => resolveCache.set(rkey(module, t), (items.length === part.length ? items[k] : byName.get(t)) || false));
    }
    if (module === S.module) S.chips.forEach(decorateChip);
  }
  if (resolveCache.size > 5000) for (const [k, v] of resolveCache) if (v !== null) resolveCache.delete(k);
}, 180);
function chipHints(c) {
  const module = S.module;
  // "unknown tag" only means something when the module has a tag list
  const listKnown = resolveHasList.has(module) ? resolveHasList.get(module) : !!S.tagLists?.has(module);
  const rows = chipTags(c).map((t) => [t, resolveCache.get(rkey(module, t))]).filter(([, r]) => r);
  return {
    rows,
    aliases: rows.filter(([t, r]) => r.alias && r.canonical && r.canonical !== t),
    unknown: listKnown ? rows.filter(([, r]) => !r.known && r.type !== 'meta') : [],
  };
}
function decorateChip(c) {
  const v = chipViews.get(c.id);
  if (!v) return;
  const { rows, aliases, unknown } = chipHints(c);
  const kids = [];
  const lines = [];
  for (const [t, r] of rows) {
    if (r.alias && r.canonical && r.canonical !== t) lines.push(`${t}: alias của ${r.canonical}${r.posts ? ` (${nf.format(r.posts)} post)` : ''}`);
    else if (!r.known && r.type === 'wildcard') lines.push(`${t}: mẫu wildcard không khớp tag nào trong danh sách tag của ${S.module.toUpperCase()}`);
    else if (!r.known && r.type !== 'meta') lines.push(`${t}: không có trong danh sách tag của ${S.module.toUpperCase()} (gõ sai hoặc danh sách cũ?)`);
    else if (r.posts) lines.push(`${t}: ${nf.format(r.posts)} post`);
  }
  if (c.kind === 'tag' && rows.length === 1 && rows[0][1].posts > 0 && !unknown.length) kids.push(h('span', { class: 'cnt' }, fmt(rows[0][1].posts)));
  if (aliases.length) {
    const label = c.kind === 'tag' ? `→ ${aliases[0][1].canonical}` : `→ ${aliases.length} alias`;
    kids.push(h('button', { class: 'alias', title: `Thay bằng tag chuẩn: ${aliases.map(([t, r]) => `${t} → ${r.canonical}`).join(', ')}`,
      onclick: () => replaceAliases(c) }, label));
  }
  if (unknown.length) kids.push(h('span', { class: 'warn', title: lines.filter((l) => /không (có|khớp)/.test(l)).join('\n') }, '?'));
  v.info.replaceChildren(...kids);
  v.root.classList.toggle('unknown', unknown.length > 0);
  v.root.classList.toggle('aliased', aliases.length > 0);
  v.root.title = lines.length ? `${v.title}\n${lines.join('\n')}` : v.title;
}
function replaceAliases(c) {
  const map = new Map(chipHints(c).aliases.map(([t, r]) => [t, r.canonical]));
  if (!map.size) return;
  const swap = (t) => { const neg = t.startsWith('-'); const base = neg ? t.slice(1) : t; const to = map.get(base.toLowerCase()); return to ? `${neg ? '-' : ''}${to}` : t; };
  const next = { ...c };
  if (c.kind === 'tag') next.text = swap(c.text);
  else next.items = [...new Set(c.items.map(swap))];
  const s = chipToString(next);
  // the canonical tag may already be in the query: then the alias chip just goes away
  const chips = S.chips.some((x) => x.id !== c.id && chipToString(x) === s) ? S.chips.filter((x) => x.id !== c.id) : S.chips.map((x) => (x.id === c.id ? next : x));
  S.chips = chips;
  S.selected.delete(c.id);
  renderChips();
  onQueryChanged();
  toast(`Đã thay alias: ${[...map].map(([a, b]) => `${a} → ${b}`).join(', ')}`, 'ok');
}

// ----- grouping: merging chips into an OR / exclusion group can change what the query means -----
function planGroup(act) {
  const sel = S.chips.filter((c) => S.selected.has(c.id));
  const items = [];
  const warn = [];
  if (act === 'or') {
    for (const c of sel) {
      if (c.kind === 'or') items.push(...c.items);
      else if (c.kind === 'neg') {
        items.push(...c.items);
        warn.push(`Nhóm loại trừ ${chipToString(c)} trở thành các tag bao gồm: post chỉ cần có một trong ${c.items.join(', ')}.`);
      } else if (c.exclude) {
        items.push(`-${c.text}`);
        warn.push(c.kind === 'meta' ? `Meta tag phủ định -${c.text} không dùng được trong nhóm OR (truy vấn sẽ không hợp lệ).`
          : `-${c.text} trong nhóm OR nghĩa là post không có ${c.text} cũng khớp: kết quả rộng hơn rất nhiều.`);
      } else items.push(c.text);
    }
  } else {
    let excluded = 0;
    for (const c of sel) {
      if (c.kind === 'neg') { items.push(...c.items); excluded++; } else if (c.kind === 'or') {
        items.push(...c.items);
        warn.push(`Nhóm OR ${chipToString(c)} trong nhóm loại trừ thành điều kiện “có đủ tất cả”, không còn là “một trong”.`);
      } else {
        items.push(c.text);
        if (c.exclude) excluded++;
        else warn.push(`${c.text} đang là tag bắt buộc, sẽ đổi thành tag bị loại trừ (trong nhóm).`);
        if (c.kind === 'meta') warn.push(`Meta tag ${c.text} trong nhóm loại trừ chỉ được so với tên tag của post nên gần như không bao giờ khớp.`);
      }
    }
    if (excluded) warn.push('Trước: mỗi tag/nhóm bị loại trừ bỏ mọi post khớp với nó. Sau: chỉ bỏ post có đủ tất cả tag trong nhóm mới, nên kết quả rộng hơn.');
  }
  const uniq = [...new Set(items)];
  if (uniq.length < 2) return { error: 'Chọn ít nhất 2 tag để gộp nhóm' };
  const idx = S.chips.findIndex((c) => S.selected.has(c.id));
  const chips = S.chips.filter((c) => !S.selected.has(c.id));
  chips.splice(Math.max(0, idx), 0, { kind: act, items: uniq, id: ++S.chipSeq });
  return { chips, warn };
}
function confirmQueryChange(title, chips, warn) {
  return new Promise((resolve) => {
    const done = (ok) => { closeModal(); resolve(ok); };
    openModal(h('div', { class: 'qchange' },
      h('h2', { text: title }),
      h('div', { class: 'qc-row' }, h('span', { class: 'muted' }, 'Trước'), h('code', {}, queryString())),
      h('div', { class: 'qc-row' }, h('span', { class: 'muted' }, 'Sau'), h('code', {}, chipsToString(chips))),
      h('ul', { class: 'qc-warn' }, warn.map((w) => h('li', {}, w))),
      h('div', { class: 'modal-actions' },
        h('button', { class: 'btn ghost', onclick: () => done(false) }, 'Huỷ'),
        h('button', { class: 'btn primary', onclick: () => done(true) }, 'Vẫn gộp'))));
    $('#modal-card .btn.primary')?.focus();
  });
}

$('#chip-actions').addEventListener('click', async (e) => {
  const act = e.target.closest('button')?.dataset.act;
  if (!act) return;
  const sel = S.chips.filter((c) => S.selected.has(c.id));
  if (act === 'clear') { S.selected.clear(); renderChips(); return; }
  if (act === 'remove') { setChips(S.chips.filter((c) => !S.selected.has(c.id))); return; }
  if (act === 'toggle') {
    sel.forEach((c) => { if (c.kind === 'tag' || c.kind === 'meta') c.exclude = !c.exclude; });
    renderChips();
    onQueryChanged();
    return;
  }
  if (act === 'or' || act === 'neg') {
    const plan = planGroup(act);
    if (plan.error) { toast(plan.error, 'err'); return; }
    if (plan.warn.length && !(await confirmQueryChange('Gộp nhóm sẽ đổi nghĩa truy vấn', plan.chips, plan.warn))) return;
    setChips(plan.chips);
    return;
  }
  if (act === 'split') {
    const out = [];
    for (const c of S.chips) {
      if (S.selected.has(c.id) && (c.kind === 'or' || c.kind === 'neg')) {
        c.items.forEach((it) => { const p = parseToken(c.kind === 'neg' ? `-${it}` : it); if (p) out.push({ ...p, id: ++S.chipSeq }); });
      } else out.push(c);
    }
    setChips(out);
  }
});

// ---------------- autocomplete ----------------
const tagInput = $('#tag-text');
$('#tag-input').addEventListener('click', () => tagInput.focus());
tagInput.addEventListener('focus', () => $('#tag-input').classList.add('focus'));
tagInput.addEventListener('blur', () => { $('#tag-input').classList.remove('focus'); setTimeout(hideAc, 150); });

const acBase = (raw) => raw.replace(/^-/, '');
function acEligible(raw) {
  const base = acBase(raw);
  // a group being typed ("(a~b", "-(a,b") is not a single tag; tags with parentheses ("star_(s") are
  return !!S.settings?.search?.autocomplete && base.length >= 2 && !reMeta.test(base) && !/^\(|[~,]/.test(base);
}
const requestAc = debounce(async () => {
  const raw = tagInput.value.trim();
  if (!acEligible(raw)) { hideAc(); return; }
  const base = acBase(raw);
  const seq = ++S.ac.seq;
  const module = S.module;
  try {
    let items = await L.rpc('tags.autocomplete', { module, prefix: raw, limit: 12 });
    if (seq !== S.ac.seq) return; // a newer keystroke (or a pick / hide) happened meanwhile
    let fuzzy = false;
    if (!items.length && base.length >= 3) {
      const neg = raw.startsWith('-');
      const near = await rpcOptional('tags.fuzzy', { module, text: base.toLowerCase(), limit: 8 }).catch(() => null);
      if (seq !== S.ac.seq) return;
      items = (near || []).map((it) => ({ ...it, insert: `${neg ? '-' : ''}${it.tag}` }));
      fuzzy = items.length > 0;
    }
    if (tagInput.value.trim() !== raw) return;
    Object.assign(S.ac, { items, fuzzy, index: items.length ? 0 : -1, moved: false });
    renderAc(base);
  } catch {
    if (seq === S.ac.seq) hideAc();
  }
}, 110);
// Called on every keystroke before the debounced request: answers still in flight are now stale (seq), and suggestions
// that no longer match the typed prefix disappear at once instead of lingering until the next answer.
function pruneAc() {
  S.ac.seq++;
  const raw = tagInput.value.trim();
  if ($('#ac').hidden || S.ac.fuzzy || !acEligible(raw)) { hideAc(); return; }
  const base = acBase(raw).toLowerCase();
  const neg = raw.startsWith('-');
  const keep = S.ac.items.filter((it) => it.tag.toLowerCase().startsWith(base)).map((it) => ({ ...it, insert: `${neg ? '-' : ''}${it.tag}` }));
  if (!keep.length) { hideAc(); return; }
  Object.assign(S.ac, { items: keep, index: 0, moved: false });
  renderAc(acBase(raw));
}

function renderAc(base) {
  const box = $('#ac');
  if (!S.ac.items.length) {
    box.replaceChildren(h('div', { class: 'ac-hint' }, `Không có gợi ý cho “${base}” trong danh sách tag của ${S.module.toUpperCase()}. Danh sách có thể cũ: cập nhật trong mục Tags A–Z.`));
    box.hidden = false;
    return;
  }
  const lo = base.toLowerCase();
  const { moved, index, fuzzy } = S.ac;
  box.replaceChildren(
    ...(fuzzy ? [h('div', { class: 'ac-head' }, `Không có tag bắt đầu bằng “${base}”. Có phải bạn muốn: …`)] : []),
    ...S.ac.items.map((it, i) => {
      const nm = !fuzzy && it.tag.toLowerCase().startsWith(lo)
        ? h('span', { class: 'nm' }, h('b', {}, it.tag.slice(0, lo.length)), it.tag.slice(lo.length))
        : h('span', { class: 'nm' }, it.tag);
      const state = i === index ? (moved ? 'on' : 'cand') : '';
      return h('div', { class: `ac-item ${fuzzy ? 'fuzzy' : ''} ${state}`, onmousedown: (e) => { e.preventDefault(); pickAc(i, e.shiftKey); } },
        h('span', { class: `type-dot t-${it.type}`, title: it.type }), nm,
        state === 'cand' ? h('kbd', { title: 'Tab để nhận gợi ý này' }, 'Tab') : null,
        h('span', { class: 'cnt' }, fmt(it.posts)));
    }),
    h('div', { class: 'ac-hint' }, moved
      ? '↑↓ chọn · Enter/Tab thêm gợi ý đang chọn · Shift+Enter thêm dạng loại trừ'
      : 'Enter thêm đúng chữ đã gõ · Tab nhận gợi ý đầu · ↑↓ chọn gợi ý khác · Shift+Enter loại trừ'));
  box.hidden = false;
}
function hideAc() {
  S.ac.seq++; // whatever is still in flight must not reopen the list
  const box = $('#ac');
  box.hidden = true;
  box.replaceChildren(); // no stale suggestion nodes left behind for the next query
  Object.assign(S.ac, { items: [], index: -1, moved: false, fuzzy: false });
}
function pickAc(i, exclude = false) {
  const it = S.ac.items[i];
  if (!it) return;
  const ins = it.insert || it.tag;
  addChip(exclude && !ins.startsWith('-') ? `-${ins}` : ins);
  tagInput.value = '';
  hideAc();
}
function commitTyped(exclude = false) {
  const typed = tagInput.value.trim();
  if (!typed) return false;
  // Shift+Enter on a single typed tag adds it as an exclusion, like it does for a suggestion
  addTokens(exclude && !/\s/.test(typed) && !typed.startsWith('-') && !typed.startsWith('(') ? `-${typed}` : typed);
  tagInput.value = '';
  hideAc();
  return true;
}
tagInput.addEventListener('input', () => {
  const v = tagInput.value;
  if (/\s$/.test(v) && !splitQuery(v).open) { addTokens(v); tagInput.value = ''; hideAc(); return; }
  pruneAc();
  requestAc();
});
tagInput.addEventListener('keydown', (e) => {
  if (e.isComposing || e.keyCode === 229) return; // IME (Vietnamese input methods): Enter confirms the composition
  const n = S.ac.items.length;
  const acOpen = !$('#ac').hidden && n > 0;
  const base = () => acBase(tagInput.value.trim());
  if (e.key === 'ArrowDown' && acOpen) {
    e.preventDefault();
    S.ac.index = S.ac.moved ? (S.ac.index + 1) % n : 0;
    S.ac.moved = true;
    renderAc(base());
    return;
  }
  if (e.key === 'ArrowUp' && acOpen) {
    e.preventDefault();
    S.ac.index = S.ac.moved ? (S.ac.index - 1 + n) % n : n - 1;
    S.ac.moved = true;
    renderAc(base());
    return;
  }
  if (e.key === 'Tab' && acOpen && S.ac.index >= 0) { e.preventDefault(); pickAc(S.ac.index, e.shiftKey); return; }
  if (e.key === 'Enter' && acOpen) {
    // Enter adds exactly what was typed ("cat" stays "cat", never "cat_ears"), unless a suggestion was picked with the
    // arrow keys or the typed text is itself one of the suggestions
    const exact = S.ac.items.findIndex((it) => it.tag.toLowerCase() === base().toLowerCase());
    const i = S.ac.moved && S.ac.index >= 0 ? S.ac.index : exact;
    if (i >= 0) { e.preventDefault(); pickAc(i, e.shiftKey); return; }
  }
  if (e.key === 'Enter' || (e.key === 'Tab' && tagInput.value.trim())) {
    e.preventDefault();
    if (commitTyped(e.key === 'Enter' && e.shiftKey)) return;
    if (e.key === 'Enter' && e.ctrlKey) startDownload();
    else if (e.key === 'Enter') runPreview(0);
    return;
  }
  if (e.key === 'Escape') { hideAc(); return; }
  if (e.key === 'Backspace' && !tagInput.value && S.chips.length) { editChip(S.chips[S.chips.length - 1]); e.preventDefault(); }
});
tagInput.addEventListener('paste', (e) => {
  const text = e.clipboardData.getData('text');
  if (looksLikeFiles(text)) { // file names / paths / post links: search by file instead of adding them as tags
    e.preventDefault();
    openFilesSearch(text.trim());
    return;
  }
  if (!/\s/.test(text.trim())) return;
  e.preventDefault();
  const v = tagInput.value;
  addTokens(v.slice(0, tagInput.selectionStart ?? v.length) + text + v.slice(tagInput.selectionEnd ?? v.length));
  tagInput.value = '';
  hideAc();
});

// ---------------- query validation / count ----------------
const validate = debounce(async () => {
  const q = queryString();
  $('#query-text').textContent = q ? `${S.module}: ${q}` : '';
  const st = $('#query-status');
  st.className = ''; st.textContent = '';
  $('#tag-input').classList.remove('invalid');
  if (!q) return;
  try {
    const r = await L.rpc('tags.validate', { module: S.module, tags: q });
    if (!r.valid) {
      $('#tag-input').classList.add('invalid');
      st.className = 'err';
      st.textContent = 'Cú pháp tag không hợp lệ (bọc tag trong %…% để bỏ qua kiểm tra)';
    }
  } catch { /* sidecar not ready */ }
}, 250);
function onQueryChanged() {
  S.countSeq++; // a count still running belongs to the previous query
  validate();
  markRelatedStale();
}

function currentQuery() {
  return {
    module: S.module, tags: queryString(),
    dateMin: $('#f-datemin').value, dateMax: $('#f-datemax').value,
    gender: genderState(),
  };
}
function downloadOptions() {
  return {
    images: $('#f-images').checked, videos: $('#f-videos').checked, videoFormat: $('#f-vformat').value, lowRes: $('#f-lowres').checked,
    threads: Number($('#f-threads').value) || 4, limit: Number($('#f-limit').value) || 0, reverse: $('#f-order').value === 'desc',
    parchi: $('#f-parchi').checked,
  };
}

$('#btn-count').addEventListener('click', async () => {
  const q = currentQuery();
  if (!q.tags) { toast('Chưa có tag nào', 'err'); return; }
  recordHistory(q);
  const seq = ++S.countSeq;
  const btn = $('#btn-count');
  btn.disabled = true; btn.textContent = 'Đang đếm…';
  try {
    const r = await L.search.count(q);
    if (seq !== S.countSeq) return; // the query changed (or a newer count started) meanwhile
    const st = $('#query-status');
    st.className = r.count > 0 ? 'ok' : 'err';
    const parts = r.taskCounts?.length > 1 ? ` (tổng ${r.taskCounts.length} truy vấn con, có thể trùng)` : '';
    st.textContent = `${r.approximate ? '≤ ' : ''}${nf.format(r.count)} kết quả${parts}`
      + `${r.requiredTags?.length ? ` (trước khi lọc cục bộ: ${r.requiredTags.join(', ')})` : ''}`;
    if (r.cfChallenge) toast('Website đang chặn bằng bot challenge. Xem mục Cài đặt → Kết nối.', 'err');
  } catch (err) {
    if (seq === S.countSeq) toast(`Đếm thất bại: ${errMsg(err)}`, 'err');
  } finally {
    btn.disabled = false; btn.textContent = 'Đếm';
  }
});

// ---------------- preview ----------------
// "max" asks for the module's own page size (the sidecar caps pageSize to it, e.g. rx 1000, db 200)
const PAGE_SIZE_MAX = 100000;
function previewPageSize() {
  const v = $('#pv-size').value;
  return v === 'max' ? PAGE_SIZE_MAX : Number(v) || 200;
}
async function runPreview(page = 0) {
  const q = { ...currentQuery(), pageSize: previewPageSize() };
  if (!q.tags) { toast('Chưa có tag nào', 'err'); return; }
  recordHistory(q);
  // every request gets a sequence number: an older answer arriving late never replaces a newer one
  const seq = ++S.preview.seq;
  const btn = $('#btn-preview');
  btn.disabled = true; btn.textContent = 'Đang tải…';
  $('#results-info').textContent = 'Đang lấy kết quả…';
  try {
    const r = await L.search.preview(q, page);
    if (seq !== S.preview.seq) return;
    Object.assign(S.preview, { page, items: r.items, site: r.site, module: q.module, tags: q.tags, required: r.requiredTags || [], tasks: r.tasks || [],
      genderStats: r.genderStats || {}, blockedCount: r.blockedCount || 0, hiddenNoTags: r.hiddenNoTags || 0, genderEnabled: !!r.genderEnabled,
      pageSize: r.pageSize || 0, hasMore: r.hasMore !== false, merged: typeof r.hasMore === 'boolean' });
    S.preview.selected.clear();
    renderGrid();
  } catch (err) {
    if (seq !== S.preview.seq) return;
    $('#results-info').textContent = `Lỗi: ${errMsg(err)}`;
    toast(`Preview thất bại: ${errMsg(err)}`, 'err');
  } finally {
    if (seq === S.preview.seq) { btn.disabled = false; btn.textContent = 'Preview'; }
  }
}
$('#btn-preview').addEventListener('click', () => runPreview(0));
$('#pg-prev').addEventListener('click', () => { if (S.preview.page > 0) runPreview(S.preview.page - 1); });
$('#pg-next').addEventListener('click', () => { if (S.preview.hasMore) runPreview(S.preview.page + 1); });
$('#pv-size').addEventListener('change', () => { if (S.preview.tags && S.preview.tags === queryString()) runPreview(0); });
$('#show-excluded').addEventListener('change', (e) => { S.preview.showExcluded = e.target.checked; renderGrid(); });

function renderGrid() {
  const P = S.preview;
  const items = P.items.filter((it) => P.showExcluded || !it.excluded);
  const hidden = P.items.length - items.length;
  // merged multi-task pages hold up to pageSize posts of every sub-query
  const size = !P.pageSize ? '' : P.merged && P.tasks?.length > 1 ? ` (${P.pageSize}/trang mỗi truy vấn con)` : ` (tối đa ${P.pageSize}/trang)`;
  const parts = [`Trang ${P.page + 1}: ${P.items.length} post${size}`];
  const byGender = P.items.filter((it) => (it.excludedBy || []).includes('gender')).length;
  const byOther = P.items.filter((it) => it.excluded && !(it.excludedBy || []).includes('gender')).length;
  if (byOther) parts.push(`${byOther} bị lọc cục bộ`);
  if (P.genderEnabled) {
    parts.push(`${byGender} loại theo giới tính`);
    const unknown = P.genderStats?.unknown || 0;
    if (unknown) parts.push(`${unknown} không rõ`);
    if (P.blockedCount) parts.push(`${P.blockedCount} bị chặn (an toàn)`);
    if (P.hiddenNoTags) parts.push(`${P.hiddenNoTags} ẩn vì site không trả tag để kiểm tra`);
  }
  if (hidden) parts.push('post bị lọc đang ẩn');
  if (P.required?.length) parts.push(`tag kiểm tra cục bộ: ${P.required.join(', ')}`);
  if (P.tasks?.length > 1) {
    parts.push(P.merged ? `gộp ${P.tasks.length} truy vấn con (đã khử trùng, id giảm dần)` : `truy vấn tách thành ${P.tasks.length} task, preview hiển thị task đầu`);
  }
  if (!P.hasMore) parts.push('trang cuối');
  $('#results-info').textContent = parts.join(' · ');
  $('#results-tools').hidden = false;
  $('#pg-num').textContent = String(P.page + 1);
  $('#pg-prev').disabled = P.page === 0;
  $('#pg-next').disabled = !P.hasMore;
  $('#pg-next').title = P.hasMore ? 'Trang sau' : 'Không còn trang sau';
  $('#grid-empty').hidden = items.length > 0;
  $('#grid-empty').textContent = 'Không có post nào trên trang này.';
  gridCards.clear();
  $('#grid').replaceChildren(...items.map((it) => {
    const img = h('img', { loading: 'lazy', alt: '', src: it.preview ? L.thumbUrl(it.preview, P.site) : '' });
    const g = it.gender;
    const why = (it.excludedBy || []).includes('gender') ? 'lọc giới tính' : 'lọc cục bộ';
    const gTitle = g ? `Giới tính: ${GENDER_VI[g.label] || g.label}${g.conf ? ` · tin cậy ${CONF_VI[g.conf]}` : ''}`
      + `${g.evidence?.length ? ` (${g.evidence.join(', ')})` : ''}${g.flags?.length ? ` · cờ: ${g.flags.join(', ')}` : ''}\n` : '';
    const card = h('div', {
      class: `card ${P.selected.has(it.id) ? 'sel' : ''} ${it.excluded ? 'excluded' : ''}`, 'data-why': why,
      title: gTitle + (it.tags || []).slice(0, 40).join(' '),
      onclick: () => toggleSel(it.id),
      ondblclick: () => openLightbox(it),
    }, img, h('span', { class: 'tick' }, '✓'),
    h('div', { class: 'badges' },
      it.isVideo ? h('span', { class: 'b vid' }, '▶ video') : null,
      it.ext ? h('span', { class: 'b' }, it.ext) : null,
      it.score !== undefined && it.score !== '' ? h('span', { class: 'b' }, `★ ${it.score}`) : null,
      it.rating ? h('span', { class: 'b' }, it.rating) : null,
      g ? h('span', { class: `b g ${g.conf || 'low'}` }, GENDER_VI[g.label] || '?') : null));
    const same = gridCards.get(it.id);
    if (same) same.push(card); else gridCards.set(it.id, [card]);
    return card;
  }));
  updateSelButton();
}
// post id -> its card(s) in the current grid: selection changes touch only those nodes, never rebuild the grid
const gridCards = new Map();
function toggleSel(id) {
  const s = S.preview.selected;
  if (s.has(id)) s.delete(id); else s.add(id);
  gridCards.get(id)?.forEach((card) => card.classList.toggle('sel', s.has(id)));
  updateSelButton();
}
function syncSelection() {
  // bulk change: without .no-anim every card would start its own border-color transition (~3x the cost)
  const grid = $('#grid');
  grid.classList.add('no-anim');
  const s = S.preview.selected;
  for (const [id, cards] of gridCards) for (const card of cards) card.classList.toggle('sel', s.has(id));
  updateSelButton();
  onFrame(() => onFrame(() => grid.classList.remove('no-anim')));
}
function updateSelButton() {
  const n = S.preview.selected.size;
  const b = $('#dl-selected');
  b.disabled = n === 0;
  b.textContent = `Tải đã chọn (${n})`;
}
$('#sel-all').addEventListener('click', () => { S.preview.items.filter((i) => !i.excluded).forEach((i) => S.preview.selected.add(i.id)); syncSelection(); });
$('#sel-none').addEventListener('click', () => { S.preview.selected.clear(); syncSelection(); });
$('#sel-invert').addEventListener('click', () => {
  S.preview.items.filter((i) => !i.excluded).forEach((i) => (S.preview.selected.has(i.id) ? S.preview.selected.delete(i.id) : S.preview.selected.add(i.id)));
  syncSelection();
});
$('#dl-selected').addEventListener('click', () => {
  const ids = [...S.preview.selected];
  if (!ids.length) return;
  const module = S.preview.module;
  // danbooru understands id:1,2,3 natively (free metatag); other modules use an OR group of ids (one request per id)
  let tags;
  if (module === 'db') tags = `id:${ids.join(',')}`;
  else if (ids.length === 1) tags = `id:${ids[0]}`;
  else {
    if (ids.length > 200) { toast('Tối đa 200 post mỗi lần với module này', 'err'); return; }
    tags = `(${ids.map((i) => `id:${i}`).join('~')})`;
  }
  startDownload({ module, tags, label: `${ids.length} post đã chọn từ: ${S.preview.tags}` });
});

function openLightbox(it) {
  const src = it.sample && !it.isVideo ? it.sample : it.preview;
  const addTag = (t, ex) => { addChip(ex ? `-${t}` : t); toast(`Đã thêm ${ex ? '−' : ''}${t}`); };
  const link = POST_URL[S.preview.module]?.(it.id);
  openModal(h('div', { class: 'lightbox' },
    h('div', {}, h('img', { src: src ? L.thumbUrl(src, S.preview.site) : '', alt: '' })),
    h('div', {},
      h('h2', {}, `#${it.id} `, h('span', { class: 'muted' }, [it.ext, it.width && `${it.width}×${it.height}`, it.date].filter(Boolean).join(' · '))),
      link ? h('p', {}, h('a', { href: '#', onclick: (e) => { e.preventDefault(); L.app.openExternal(link); } }, 'Mở post trên website ↗')) : null,
      it.gender ? h('p', {}, h('b', {}, `Giới tính: ${GENDER_VI[it.gender.label] || it.gender.label}`),
        h('span', { class: 'muted' }, ` · tin cậy ${CONF_VI[it.gender.conf] || '—'} · từ tag`
          + `${it.gender.evidence?.length ? ` (${it.gender.evidence.join(', ')})` : ''}${it.gender.flags?.length ? ` · cờ: ${it.gender.flags.join(', ')}` : ''}`)) : null,
      h('p', { class: 'muted' }, 'Bấm + để thêm tag vào truy vấn, − để loại trừ.'),
      h('div', { class: 'lb-tags' }, (it.tags || []).map((t) => h('span', { class: 'lb-tag' }, t,
        h('button', { title: 'Thêm', onclick: () => addTag(t, false) }, '+'), h('button', { title: 'Loại trừ', onclick: () => addTag(t, true) }, '−')))),
      h('div', { class: 'modal-actions' },
        h('button', { class: 'btn', onclick: () => { toggleSel(it.id); closeModal(); } }, S.preview.selected.has(it.id) ? 'Bỏ chọn' : 'Chọn'),
        h('button', { class: 'btn ghost', onclick: closeModal }, 'Đóng')))));
}

// ---------------- downloads ----------------
async function startDownload(override = null) {
  const q = override ? { ...currentQuery(), ...override } : currentQuery();
  if (!q.tags) { toast('Chưa có tag nào', 'err'); return; }
  if (!override?.tags) recordHistory(q); // "download selected" builds an id: query, not something the user typed
  const path = $('#f-path').value.trim() || S.settings.download.path;
  try {
    if (S.settings.download.warnNonEmpty) {
      const r = await L.rpc('files.nonEmpty', { path });
      if (r.nonEmpty && !S.settings.download.subfolderPerQuery
        && !window.confirm(`Thư mục “${path}” không trống, kết quả có thể lẫn với lượt tải khác. Vẫn tiếp tục?`)) return;
    }
    const job = await L.jobs.start({ ...q, path, options: downloadOptions() });
    upsertJob(job);
    toast(`Đã thêm job: ${q.label || q.tags}`, 'ok');
  } catch (err) {
    toast(`Không tạo được job: ${errMsg(err)}`, 'err');
  }
}
$('#btn-download').addEventListener('click', () => startDownload());

// Each job owns one DOM subtree that is created once and then patched in place (at most once per frame), so buttons
// never get swapped out from under the cursor and an open log keeps its scroll position.
const JOB_LOG_MAX = 200;
const JOB_ACTIVE = new Set(['queued', 'running', 'cancelling']);
const jobViews = new Map(); // job id -> view
const dirtyJobs = new Set();
let jobsResort = false;

function upsertJob(job) {
  // logTail only seeds a job we have not seen yet; afterwards every line arrives once through 'jobs:log'
  const { logTail, ...rest } = job;
  const j = S.jobs.get(job.id);
  if (j) Object.assign(j, rest);
  else {
    S.jobs.set(job.id, { ...rest, showLog: false, log: (logTail || []).slice(-JOB_LOG_MAX) });
    jobsResort = true;
  }
  dirtyJobs.add(job.id);
  onFrame(flushJobs);
}
function renderJobs() {
  S.jobs.forEach((_, id) => dirtyJobs.add(id));
  jobsResort = true;
  onFrame(flushJobs);
}
function jobProgress(j) {
  const p = j.progress || {};
  // streaming: files download while search pages are still being scanned, so totalAll can keep growing
  const downloading = (p.stateName === 'DOWNLOADING' || !!p.streaming) && p.totalAll > 0;
  const pct = j.status === 'done' ? 100 : downloading ? Math.min(100, (100 * (p.processed || 0)) / p.totalAll) : 0;
  return { p, pct, indet: j.status === 'running' && !downloading };
}
function createJobView(id) {
  const job = () => S.jobs.get(id);
  const m = {
    module: h('span'), state: h('span'), task: h('span'), files: h('span'),
    scan: h('span', { class: 'hint', title: 'Vẫn đang quét thêm trang kết quả, tổng số file có thể còn tăng' }, 'đang quét thêm…'),
    counts: h('span'), cf: h('span', { class: 'danger' }, 'Bị bot challenge chặn'), error: h('span', { class: 'danger' }),
    partial: h('span', { class: 'warn' }, 'Xong, một số file lỗi'), gender: h('span'), blocked: h('span'), dest: h('span', { class: 'mono' }),
  };
  Object.values(m).forEach((el) => { el.hidden = true; });
  const v = {
    m, width: null, pre: null, logView: null,
    status: h('span', { class: 'status' }),
    q: h('span', { class: 'job-q' }),
    cancel: h('button', { class: 'btn sm danger', onclick: () => L.jobs.cancel(id) }, 'Huỷ'),
    logBtn: h('button', { class: 'btn sm ghost', onclick: () => toggleJobLog(id) }, 'Log'),
    fill: h('i'),
  };
  v.bar = h('div', { class: 'bar' }, v.fill);
  v.root = h('div', { class: 'job glass', dataset: { id } },
    h('div', { class: 'job-head' }, v.status, v.q,
      h('div', { class: 'job-actions' }, v.cancel,
        h('button', { class: 'btn sm ghost', onclick: () => { const j = job(); if (j) L.app.openPath(j.dest); } }, 'Mở thư mục'),
        v.logBtn)),
    v.bar, h('div', { class: 'job-meta' }, Object.values(m)));
  return v;
}
function updateJobView(v, j) {
  const { p, pct, indet } = jobProgress(j);
  const cls = `status ${j.status}`;
  if (v.status.className !== cls) v.status.className = cls;
  setText(v.status, JOB_VI[j.status] || j.status);
  setText(v.q, j.label || j.tags);
  if (v.q.title !== j.tags) v.q.title = j.tags;
  setHidden(v.cancel, !['queued', 'running'].includes(j.status));
  setText(v.logBtn, j.showLog ? 'Ẩn log' : 'Log');
  v.bar.classList.toggle('indet', indet);
  const width = indet ? '' : `${pct}%`; // inline width would override the indeterminate animation's width
  if (v.width !== width) { v.width = width; v.fill.style.width = width; }
  const { m } = v;
  slot(m.module, `Module: ${j.module}`);
  slot(m.state, p.stateName && j.status !== 'failed' ? `Trạng thái: ${STATE_VI[p.stateName] || p.stateName}` : null);
  slot(m.task, p.tasks > 1 ? `Task ${p.task || 0}/${p.tasks}` : null);
  const files = p.totalAll ? `${nf.format(p.processed || 0)} / ${nf.format(p.totalAll)} file` : p.total ? `Tìm thấy ${nf.format(p.total)}` : null;
  slot(m.files, files);
  setHidden(m.scan, !(files && p.scanning && j.status === 'running'));
  slot(m.counts, (p.success || p.failed) ? `✓ ${p.success || 0} · ✗ ${p.failed || 0}` : null);
  setHidden(m.cf, !p.cfChallenge);
  slot(m.error, j.status === 'failed' && j.error ? `Lỗi: ${j.error}` : null);
  setHidden(m.partial, !(j.status === 'done' && j.exitCode === 3));
  slot(m.gender, p.genderFiltered ? `Lọc giới tính: ${nf.format(p.genderFiltered)}` : null);
  slot(m.blocked, p.blocked ? `Chặn (an toàn): ${nf.format(p.blocked)}` : null);
  slot(m.dest, j.dest ? String(j.dest) : null);
}
function flushJobs() {
  const host = $('#jobs');
  for (const [id, v] of jobViews) if (!S.jobs.has(id)) { v.root.remove(); jobViews.delete(id); }
  for (const id of dirtyJobs) {
    const j = S.jobs.get(id);
    if (!j) continue;
    let v = jobViews.get(id);
    if (!v) { v = createJobView(id); jobViews.set(id, v); jobsResort = true; }
    updateJobView(v, j);
  }
  dirtyJobs.clear();
  if (jobsResort) {
    jobsResort = false;
    // newest first; only nodes that are out of place are moved (a new job is inserted, existing ones stay put)
    let i = 0;
    for (const j of [...S.jobs.values()].sort((a, b) => b.createdAt - a.createdAt)) {
      const el = jobViews.get(j.id)?.root;
      if (!el) continue;
      if (host.children[i] !== el) host.insertBefore(el, host.children[i] || null);
      i++;
    }
  }
  let active = 0;
  for (const j of S.jobs.values()) if (JOB_ACTIVE.has(j.status)) active++;
  const badge = $('#badge-jobs');
  setHidden(badge, active === 0);
  setText(badge, String(active));
  setHidden($('#jobs-empty'), S.jobs.size > 0);
}
function toggleJobLog(id) {
  const j = S.jobs.get(id);
  const v = jobViews.get(id);
  if (!j || !v) return;
  j.showLog = !j.showLog;
  if (j.showLog) {
    v.pre = h('pre', { class: 'job-log' });
    v.root.append(v.pre);
    v.logView = createLogView(v.pre, JOB_LOG_MAX, 50);
    v.logView.reset(j.log);
  } else {
    v.pre?.remove();
    v.pre = null;
    v.logView = null;
  }
  setText(v.logBtn, j.showLog ? 'Ẩn log' : 'Log');
}
function appendJobLog(id, text) {
  const j = S.jobs.get(id);
  if (!j) return;
  j.log.push(text);
  if (j.log.length > JOB_LOG_MAX + 50) j.log.splice(0, j.log.length - JOB_LOG_MAX);
  if (j.showLog) jobViews.get(id)?.logView?.push([text]);
}
$('#jobs-clear').addEventListener('click', async () => {
  const list = await L.jobs.clear();
  const keep = new Set(list.map((j) => j.id));
  for (const id of [...S.jobs.keys()]) if (!keep.has(id)) S.jobs.delete(id);
  list.forEach(upsertJob);
  renderJobs();
});

// ---------------- A-Z tag browser (drawer + page) ----------------
const azInstances = [];
function createAZ(host) {
  const st = { letter: 'a', prefix: '', types: new Set(), minPosts: 0, sort: 'posts', offset: 0, total: 0, loading: false, seq: 0 };
  const letters = h('div', { class: 'az-letters' }, [...'abcdefghijklmnopqrstuvwxyz', '#'].map((ch) =>
    h('button', { class: ch === st.letter ? 'on' : '', onclick: () => { st.letter = ch; st.prefix = ''; prefixInput.value = ''; reload(); } }, ch)));
  const prefixInput = h('input', { type: 'text', placeholder: 'Lọc theo tiền tố (vd: big_)…', oninput: debounce(() => { st.prefix = prefixInput.value.trim().toLowerCase(); reload(); }, 200) });
  const minSel = h('select', { onchange: () => { st.minPosts = Number(minSel.value); reload(); } },
    [0, 10, 100, 1000, 10000].map((v) => h('option', { value: v }, v ? `≥ ${fmt(v)} post` : 'Mọi số post')));
  const sortSeg = h('div', { class: 'seg' },
    h('button', { class: 'on', dataset: { s: 'posts' } }, 'Phổ biến'), h('button', { dataset: { s: 'az' } }, 'A→Z'));
  sortSeg.addEventListener('click', (e) => {
    const s = e.target.dataset.s;
    if (!s) return;
    st.sort = s;
    $$('button', sortSeg).forEach((b) => b.classList.toggle('on', b.dataset.s === s));
    reload();
  });
  const types = h('div', { class: 'az-types' }, ['general', 'artist', 'character', 'copyright', 'metadata'].map((t) =>
    h('button', { class: 'pill', onclick: (e) => { if (st.types.has(t)) st.types.delete(t); else st.types.add(t); e.currentTarget.classList.toggle('on'); reload(); } },
      h('span', { class: `type-dot t-${t}` }), t)));
  const list = h('div', { class: 'az-list' });
  const foot = h('div', { class: 'az-foot' }, h('span', {}, ''), h('span', { class: 'muted' }, ''));
  host.replaceChildren(letters, h('div', { class: 'az-tools' }, prefixInput, minSel, sortSeg), types, list, foot);

  async function load(append) {
    // paging waits for the running load; a reload (letter, prefix, filter or module change) always starts and makes
    // whatever is still in flight stale through the sequence number
    if (append && st.loading) return;
    const seq = ++st.seq;
    st.loading = true;
    const module = S.module;
    // a typed prefix is used as is ("big_" -> big_*, never letter + prefix); otherwise the selected letter
    const prefix = st.prefix || st.letter;
    try {
      const r = await L.rpc('tags.browse', { module, prefix, offset: append ? st.offset : 0, limit: 150, minPosts: st.minPosts,
        types: [...st.types], sort: st.sort });
      if (seq !== st.seq || module !== S.module) return;
      if (!append) { list.replaceChildren(); list.scrollTop = 0; st.offset = 0; }
      st.total = r.total;
      st.offset += r.items.length;
      list.append(...r.items.map((it) => h('div', { class: 'az-row' },
        h('span', { class: `type-dot t-${it.type}`, title: it.type }), h('span', { class: 'nm', title: it.tag }, it.tag),
        h('span', { class: 'cnt' }, fmt(it.posts)),
        h('button', { class: 'btn sm', title: 'Thêm vào truy vấn', onclick: () => { addChip(it.tag); toast(`Đã thêm ${it.tag}`); } }, '+'),
        h('button', { class: 'btn sm', title: 'Loại trừ', onclick: () => { addChip(`-${it.tag}`); toast(`Đã loại trừ ${it.tag}`); } }, '−'))));
      foot.firstChild.textContent = `${nf.format(st.total)} tag · ${module.toUpperCase()}${st.prefix ? ` · tiền tố “${st.prefix}”` : ''}`;
      foot.lastChild.textContent = st.total === 0 ? (st.prefix ? 'Không có tag nào với tiền tố này' : 'Chưa có danh sách tag cho module này')
        : st.offset < st.total ? 'Cuộn để tải thêm' : '';
    } catch (err) {
      if (seq === st.seq) foot.firstChild.textContent = `Lỗi: ${errMsg(err)}`;
    } finally {
      if (seq === st.seq) st.loading = false;
    }
  }
  function reload() {
    // the highlighted letter follows a typed prefix
    const on = st.prefix ? (/^[a-z]/.test(st.prefix) ? st.prefix[0] : '') : st.letter;
    $$('button', letters).forEach((b) => b.classList.toggle('on', b.textContent === on));
    load(false);
  }
  list.addEventListener('scroll', () => {
    if (list.scrollTop + list.clientHeight > list.scrollHeight - 200 && st.offset < st.total) load(true);
  });
  // module change: only an instance that was opened before reloads now, the others load when first shown
  const api = { reload, reset: () => { if (api.loaded) reload(); }, loaded: false };
  azInstances.push(api);
  return api;
}
const azDrawer = createAZ($('#az-drawer-host'));
const azPage = createAZ($('#az-page-host'));
$('#btn-az').addEventListener('click', () => {
  const d = $('#az-drawer');
  d.hidden = !d.hidden;
  if (!d.hidden) $('#hist-drawer').hidden = true;
  if (!d.hidden && !azDrawer.loaded) { azDrawer.loaded = true; azDrawer.reload(); }
});
$('#az-close').addEventListener('click', () => { $('#az-drawer').hidden = true; });

// ---------------- search history / saved queries (settings.search.history, settings.search.saved) ----------------
// Both lists are written whole (main's deep merge replaces arrays); main validates and caps them as well.
const HISTORY_MAX = 50;
const SAVED_MAX = 200;
const QUERY_MAX_LEN = 2000;
const NAME_MAX_LEN = 80;
const searchList = (key) => (Array.isArray(S.settings?.search?.[key]) ? S.settings.search[key] : []);
async function saveSearchList(key, list) {
  S.settings.search[key] = list; // shown at once; the saved copy (validated by main) replaces it when it comes back
  try { await saveSettings({ search: { [key]: list } }); } catch (err) { toast(`Không lưu được: ${errMsg(err)}`, 'err'); }
  if (!$('#hist-drawer').hidden) renderHistory();
}
function recordHistory(q) {
  const tags = String(q.tags || '').trim().slice(0, QUERY_MAX_LEN);
  if (!tags || !S.settings) return;
  const list = searchList('history').filter((e) => !(e.module === q.module && e.tags === tags));
  list.unshift({ module: q.module, tags, at: Date.now() });
  saveSearchList('history', list.slice(0, HISTORY_MAX));
}
function loadQuery(e) {
  if (e.module !== S.module) {
    if (!moduleInfo(e.module)?.installed) { toast(`Module ${e.module} chưa được cài`, 'err'); return; }
    setModule(e.module);
  }
  S.chips = [];
  S.selected.clear();
  splitQuery(e.tags).tokens.forEach((t) => { const c = parseToken(t); if (c) S.chips.push({ ...c, id: ++S.chipSeq }); });
  renderChips();
  onQueryChanged();
  $('#hist-drawer').hidden = true;
  tagInput.focus();
}
async function saveQuery(src = null) {
  const q = src || currentQuery();
  const tags = String(q.tags || '').trim();
  if (!tags) { toast('Chưa có tag nào', 'err'); return; }
  const v = await prompt2('Lưu truy vấn', [{ label: `Tên (${q.module}: ${tags.length > 60 ? `${tags.slice(0, 60)}…` : tags})`, value: tags.slice(0, NAME_MAX_LEN) }]);
  const name = v?.[0]?.slice(0, NAME_MAX_LEN);
  if (!name) return;
  const list = searchList('saved').filter((e) => e.name !== name);
  if (list.length >= SAVED_MAX) { toast(`Tối đa ${SAVED_MAX} truy vấn đã lưu`, 'err'); return; }
  list.unshift({ name, module: q.module, tags: tags.slice(0, QUERY_MAX_LEN), at: Date.now() });
  await saveSearchList('saved', list);
  toast(`Đã lưu truy vấn “${name}”`, 'ok');
}
async function renameSaved(e) {
  const v = await prompt2('Đổi tên truy vấn', [{ label: 'Tên mới', value: e.name }]);
  const name = v?.[0]?.slice(0, NAME_MAX_LEN);
  if (!name || name === e.name) return;
  if (searchList('saved').some((x) => x.name === name) && !window.confirm(`Đã có truy vấn tên “${name}”. Ghi đè?`)) return;
  saveSearchList('saved', searchList('saved').filter((x) => x.name !== name).map((x) => (x.name === e.name ? { ...x, name } : x)));
}
function deleteSaved(e) {
  if (!window.confirm(`Xoá truy vấn đã lưu “${e.name}”?`)) return;
  saveSearchList('saved', searchList('saved').filter((x) => x.name !== e.name));
}
let histTab = 'history';
function renderHistory() {
  $$('#hist-tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === histTab));
  $('#hist-clear').hidden = histTab !== 'history' || !searchList('history').length;
  const host = $('#hist-list');
  const stop = (fn) => (ev) => { ev.stopPropagation(); fn(); };
  if (histTab === 'history') {
    const list = searchList('history');
    host.replaceChildren(...(list.length ? list.map((e) => h('div', { class: 'hist-row', title: `${e.module}: ${e.tags}`, onclick: () => loadQuery(e) },
      h('span', { class: 'mod-abbr' }, e.module),
      h('div', { class: 'hist-main' }, h('code', {}, e.tags), h('span', { class: 'muted' }, ago(e.at))),
      h('button', { class: 'btn sm ghost', title: 'Lưu truy vấn này', onclick: stop(() => saveQuery(e)) }, '☆'),
      h('button', { class: 'btn sm ghost', title: 'Xoá khỏi lịch sử', onclick: stop(() => saveSearchList('history', searchList('history').filter((x) => !(x.module === e.module && x.tags === e.tags)))) }, '×')))
      : [h('div', { class: 'empty' }, 'Chưa có lịch sử. Mỗi lần Đếm, Preview hoặc Tải xuống, truy vấn được ghi lại ở đây.')]));
  } else {
    const list = searchList('saved');
    host.replaceChildren(...(list.length ? list.map((e) => h('div', { class: 'hist-row', title: `${e.module}: ${e.tags}`, onclick: () => loadQuery(e) },
      h('span', { class: 'mod-abbr' }, e.module),
      h('div', { class: 'hist-main' }, h('b', {}, e.name), h('code', {}, e.tags)),
      h('button', { class: 'btn sm ghost', title: 'Đổi tên', onclick: stop(() => renameSaved(e)) }, 'Đổi tên'),
      h('button', { class: 'btn sm ghost danger', title: 'Xoá', onclick: stop(() => deleteSaved(e)) }, 'Xoá')))
      : [h('div', { class: 'empty' }, 'Chưa có truy vấn nào được lưu. Bấm “☆ Lưu truy vấn” để lưu truy vấn hiện tại.')]));
  }
}
$('#btn-history').addEventListener('click', () => {
  const d = $('#hist-drawer');
  d.hidden = !d.hidden;
  if (!d.hidden) { $('#az-drawer').hidden = true; renderHistory(); }
});
$('#hist-close').addEventListener('click', () => { $('#hist-drawer').hidden = true; });
$('#hist-tabs').addEventListener('click', (e) => { const t = e.target.closest('button')?.dataset.tab; if (t) { histTab = t; renderHistory(); } });
$('#hist-save').addEventListener('click', () => saveQuery());
$('#btn-save-query').addEventListener('click', () => saveQuery());

// ---------------- search by file name / post link ----------------
// Pasted, dropped or picked file names, paths and post / file links -> 'id:' / 'md5:' query (sidecar files.identify).
// Only names are used: dropped and picked files are never opened or read.
const reFileish = new RegExp([
  '\\.(?:jpe?g|jfif|png|gif|webp|avif|bmp|webm|mp4|mkv|mov|m4v|swf)(?=\\s|$|[?#])', // a media file name
  '^\\s*https?://\\S+', // a link
  '(?:^|[^0-9a-f])[0-9a-f]{32}(?:[^0-9a-f]|$)', // an md5
].join('|'), 'im');
const looksLikeFiles = (text) => reFileish.test(String(text || ''));
const FILE_NOTE_VI = {
  'not recognized': 'Không nhận ra',
  'info list file, not a post': 'File danh sách info, không phải post',
  'no post id or md5 in the link': 'Link không có id post hoặc md5',
  'unknown site, current module assumed': 'site lạ, dùng module đang chọn',
};
function namesFromTransfer(dt) {
  const names = [...(dt?.files || [])].map((f) => f.name);
  if (!names.length) {
    const uris = (dt?.getData('text/uri-list') || dt?.getData('text/plain') || '').split(/\r?\n/).filter((s) => s && !s.startsWith('#'));
    names.push(...uris);
  }
  return names;
}
function openFilesSearch(initial = '') {
  const area = h('textarea', {
    class: 'files-input', rows: '6', spellcheck: 'false',
    placeholder: 'Mỗi dòng một mục: tên file (db_12345.jpg, 12345.jpg, <md5>.png), đường dẫn, link post hoặc link ảnh',
  });
  area.value = initial;
  const picker = h('input', { type: 'file', multiple: true, hidden: true });
  const list = h('div', { class: 'files-list' });
  const actions = h('div', { class: 'files-queries' });
  let seq = 0;
  const run = async () => {
    const my = ++seq;
    const lines = area.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (!lines.length) { list.replaceChildren(); actions.replaceChildren(); return; }
    let r;
    try {
      r = await rpcOptional('files.identify', { names: lines, module: S.module });
    } catch (err) {
      if (my === seq) list.replaceChildren(h('div', { class: 'err' }, errMsg(err)));
      return;
    }
    if (my !== seq) return; // a newer edit is being identified
    if (!r) { list.replaceChildren(h('div', { class: 'err' }, 'Lõi Python chưa hỗ trợ tìm theo tên file (cần cập nhật).')); return; }
    const ok = r.items.filter((it) => it.kind).length;
    list.replaceChildren(
      h('div', { class: 'muted files-sum' }, `Nhận ra ${ok}/${r.items.length} mục${lines.length > r.max ? ` (chỉ xét ${r.max} mục đầu)` : ''}`),
      ...r.items.map((it) => h('div', { class: `files-item${it.kind ? '' : ' bad'}` },
        h('span', { class: 'files-name mono', title: it.name }, it.name),
        h('span', { class: 'files-res' }, it.kind
          ? `${it.module} · ${it.kind} ${it.kind === 'md5' ? `${it.value.slice(0, 12)}…` : it.value}${it.note ? ` (${FILE_NOTE_VI[it.note] || it.note})` : ''}`
          : (FILE_NOTE_VI[it.note] || it.note || 'Không nhận ra')))),
    );
    // the current module first, then by number of items
    const qs = [...r.queries].sort((a, b) => (b.module === S.module) - (a.module === S.module) || b.count - a.count);
    actions.replaceChildren(...qs.map((q, i) => {
      const mi = moduleInfo(q.module);
      const name = mi?.name || q.module.toUpperCase();
      return h('button', {
        class: `btn ${i === 0 ? 'primary' : ''}`,
        title: q.tags,
        onclick: () => {
          if (!mi?.installed) { toast(`Module ${name} chưa được cài (Extensions)`, 'err'); return; }
          closeModal();
          loadQuery({ module: q.module, tags: q.tags });
          runPreview(0);
          if (q.dropped) toast(`Chỉ tìm ${q.count} mục đầu trên ${name}: mỗi mục là một lượt tìm riêng, tối đa 64`, 'err');
        },
      }, `Tìm ${q.count} mục trên ${name}`);
    }));
  };
  const runSoon = debounce(run, 250);
  area.addEventListener('input', runSoon);
  const addNames = (names) => {
    if (!names.length) return;
    area.value = [area.value.trim(), ...names].filter(Boolean).join('\n');
    run();
  };
  picker.addEventListener('change', () => { addNames([...picker.files].map((f) => f.name)); picker.value = ''; });
  area.addEventListener('drop', (e) => { e.preventDefault(); e.stopPropagation(); addNames(namesFromTransfer(e.dataTransfer)); });
  openModal(h('div', { class: 'files-modal' },
    h('h3', {}, 'Tìm theo tên file / link'),
    h('p', { class: 'muted' }, 'Dán hoặc kéo-thả file đã tải, đường dẫn, link post hay link ảnh. Lustra chỉ đọc tên file, không đọc nội dung. '
      + 'Nhận ra: tên Lustra đặt (db_12345…, 12345…), tên gốc md5, tên tải từ Danbooru (__…__md5), link post của các site đã biết.'),
    area,
    h('div', { class: 'files-tools' },
      h('button', { class: 'btn sm', onclick: () => picker.click() }, 'Chọn file…'),
      h('button', { class: 'btn sm ghost', onclick: () => { area.value = ''; run(); area.focus(); } }, 'Xoá hết'),
      picker),
    list,
    h('div', { class: 'modal-actions' }, actions, h('button', { class: 'btn ghost', onclick: closeModal }, 'Đóng'))));
  area.focus();
  if (initial) run();
}
$('#btn-files').addEventListener('click', () => openFilesSearch());
// files / links dropped on the search bar open the same dialog (the window itself never navigates, see below)
$('.searchbar').addEventListener('drop', (e) => {
  const names = namesFromTransfer(e.dataTransfer);
  if (!names.length) return;
  e.preventDefault();
  e.stopPropagation();
  openFilesSearch(names.join('\n'));
});
$('#hist-clear').addEventListener('click', () => { if (window.confirm('Xoá toàn bộ lịch sử tìm kiếm?')) saveSearchList('history', []); });

// ---------------- related tags (search.related: tags that co-occur in the first results page) ----------------
const related = { seq: 0, tags: '', module: '', items: [], sample: 0 };
function relatedSupported() { return typeof L?.search?.related === 'function'; }
async function loadRelated() {
  const q = currentQuery();
  if (!q.tags) { toast('Chưa có tag nào', 'err'); return; }
  const seq = ++related.seq;
  $('#related').hidden = false;
  $('#related-info').textContent = 'Đang lấy tag liên quan…';
  $('#related-refresh').disabled = true;
  try {
    const r = await L.search.related(q);
    if (seq !== related.seq) return;
    Object.assign(related, { tags: q.tags, module: q.module, items: r.items || [], sample: r.sample || 0 });
    renderRelated();
  } catch (err) {
    if (seq === related.seq) $('#related-info').textContent = `Lỗi: ${errMsg(err)}`;
  } finally {
    if (seq === related.seq) $('#related-refresh').disabled = false;
  }
}
function renderRelated() {
  const inQuery = new Set(S.chips.flatMap((c) => (c.kind === 'or' || c.kind === 'neg' ? c.items : [c.text])).map((t) => t.replace(/^-/, '').toLowerCase()));
  const { sample } = related;
  const showMeta = $('#related-meta').checked;
  const items = showMeta ? related.items : related.items.filter((it) => it.type !== 'metadata');
  const hiddenMeta = related.items.length - items.length;
  $('#related-info').textContent = related.items.length
    ? `hay đi kèm trong ${sample} post đầu của “${related.tags}”${hiddenMeta ? ` · ẩn ${hiddenMeta} tag metadata` : ''}`
    : sample ? 'Không có tag nào đi kèm' : 'Website này không trả tag trong kết quả tìm kiếm';
  $('#related-list').replaceChildren(...items.map((it) => {
    const has = inQuery.has(it.tag.toLowerCase());
    return h('span', { class: `rel-item ${has ? 'in' : ''}`, title: `${it.tag}: ${it.count}/${sample} post đầu · ${nf.format(it.posts || 0)} post trên site` },
      h('span', { class: `type-dot t-${it.type}` }), h('span', { class: 'nm' }, it.tag), h('span', { class: 'cnt' }, `${it.count}`),
      h('button', { title: 'Thêm vào truy vấn', disabled: has, onclick: () => { addChip(it.tag); renderRelated(); } }, '+'),
      h('button', { title: 'Loại trừ', disabled: has, onclick: () => { addChip(`-${it.tag}`); renderRelated(); } }, '−'));
  }));
  markRelatedStale();
}
function markRelatedStale() {
  if ($('#related').hidden || !related.tags) return;
  const stale = related.tags !== queryString() || related.module !== S.module;
  $('#related').classList.toggle('stale', stale);
  $('#related-refresh').textContent = stale ? 'Làm mới theo truy vấn mới' : 'Làm mới';
}
$('#btn-related').addEventListener('click', () => loadRelated());
$('#related-refresh').addEventListener('click', () => loadRelated());
$('#related-meta').addEventListener('change', () => { if (related.tags) renderRelated(); });
$('#related-close').addEventListener('click', () => { related.seq++; $('#related').hidden = true; });

// ---------------- tag lists status / update ----------------
const UPDATABLE = ['rx', 'db', 'xb', 'bb', 'en'];
async function refreshTagStatus() {
  if (!azPage.loaded) { azPage.loaded = true; azPage.reload(); }
  let rows = [];
  try {
    rows = await L.rpc('tags.status');
    S.tagLists = new Set(rows.filter((r) => r.size > 0).map((r) => r.module));
  } catch { /* ignore */ }
  const byMod = Object.fromEntries(rows.map((r) => [r.module, r]));
  const mods = S.modules.filter((m) => m.installed).map((m) => m.abbr);
  const table = $('#tag-status');
  table.replaceChildren(
    h('tr', {}, h('th', {}, 'Module'), h('th', {}, 'File'), h('th', {}, 'Dung lượng'), h('th', {}, 'Cập nhật'), h('th', {}, '')),
    ...mods.map((m) => {
      const r = byMod[m];
      return h('tr', {},
        h('td', {}, h('span', { class: 'mod-abbr' }, m)),
        h('td', { class: 'mono', title: r?.path || '' }, r ? r.path.split(/[\\/]/).slice(-2).join('/') : '—'),
        h('td', {}, r ? `${(r.size / 1048576).toFixed(1)} MB` : '—'),
        h('td', { id: `tagupd-${m}` }, r?.mtime ? new Date(r.mtime * 1000).toLocaleString('vi-VN') : 'chưa có'),
        h('td', {}, UPDATABLE.includes(m)
          ? h('button', { class: 'btn sm', onclick: async (e) => {
            e.currentTarget.disabled = true;
            try { await L.tags.update(m); toast(`Đang cập nhật danh sách tag ${m}…`); } catch (err) { toast(errMsg(err), 'err'); }
          } }, 'Cập nhật')
          : h('span', { class: 'muted' }, 'không có nguồn')));
    }));
  $('#tags-autoupdate').checked = !!S.settings.tags.autoUpdate;
  $('#tags-autoupdate-days').value = S.settings.tags.autoUpdateDays;
}
$('#tags-autoupdate').addEventListener('change', (e) => saveSettings({ tags: { autoUpdate: e.target.checked } }));
$('#tags-autoupdate-days').addEventListener('change', (e) => saveSettings({ tags: { autoUpdateDays: Math.max(1, Number(e.target.value) || 7) } }));

// ---------------- extensions ----------------
async function renderExtensions() {
  await loadModules();
  $('#ext-grid').replaceChildren(...S.modules.map((m) => extCard(m, false)));
  $('#catalog-url').value = S.settings.advanced.catalogUrl || '';
  renderGen();
}
function extCard(m, remote) {
  const media = (m.media || []).map((x) => h('span', { class: 'tagpill' }, { image: 'ảnh', video: 'video', audio: 'âm thanh' }[x] || x));
  const badges = [
    m.source === 'builtin' ? h('span', { class: 'tagpill acc' }, 'built-in') : null,
    m.default ? h('span', { class: 'tagpill acc' }, 'mặc định') : null,
    m.installed ? h('span', { class: 'tagpill ok' }, 'đã cài') : null,
    h('span', { class: 'tagpill' }, `v${m.version}`),
  ];
  let action;
  if (remote) action = h('button', { class: 'btn sm', onclick: () => installExt({ url: m.url, sha256: m.sha256 }, m.name) }, 'Cài');
  else if (m.source === 'builtin') action = h('span', { class: 'muted' }, 'Luôn bật');
  else if (m.installed) action = h('button', { class: 'btn sm danger', onclick: () => uninstallExt(m) }, 'Gỡ');
  else action = h('button', { class: 'btn sm primary', onclick: () => installExt({ id: m.id }, m.name) }, 'Cài đặt');
  return h('div', { class: 'ext glass' },
    h('div', { class: 'ext-head' }, h('span', { class: 'mod-abbr' }, m.abbr || '?'), h('b', {}, m.name)),
    h('div', { class: 'tags' }, badges, media),
    h('p', {}, m.description || ''),
    m.site ? h('a', { href: '#', class: 'muted', onclick: (e) => { e.preventDefault(); L.app.openExternal(m.site); } }, m.site) : null,
    h('div', { class: 'row' }, action));
}
async function installExt(params, name) {
  try {
    const r = params.id ? await L.ext.installCatalog(params.id) : await L.ext.installRemote({ ...params, name });
    toast(r?.restartRequired ? `Đã cài ${name}. Khởi động lại app để dùng phiên bản mới.` : `Đã cài ${name}`, 'ok');
    renderExtensions();
  } catch (err) { if (!/cancelled/.test(errMsg(err))) toast(`Cài thất bại: ${errMsg(err)}`, 'err'); }
}
async function uninstallExt(m) {
  if (!window.confirm(`Gỡ extension ${m.name}?`)) return;
  try {
    await L.rpc('ext.uninstall', { id: m.id });
    toast(`Đã gỡ ${m.name}. Khởi động lại để giải phóng hoàn toàn.`, 'ok');
    renderExtensions();
  } catch (err) { toast(errMsg(err), 'err'); }
}
$('#ext-refresh').addEventListener('click', renderExtensions);
$('#ext-zip').addEventListener('click', async () => {
  const file = await L.dialogs.zip();
  if (!file) return;
  const v = await prompt2('Cài extension từ file .zip', [{ label: 'SHA-256 của file (bắt buộc, lấy từ nơi phát hành)', placeholder: 'e3b0c442…' }]);
  if (!v) return;
  try { await L.ext.installZip(file, v[0]); toast('Đã cài extension', 'ok'); renderExtensions(); } catch (err) { toast(errMsg(err), 'err'); }
});
$('#catalog-load').addEventListener('click', async () => {
  const url = $('#catalog-url').value.trim();
  if (!url) return;
  await saveSettings({ advanced: { catalogUrl: url } });
  try {
    const items = await L.rpc('ext.remoteCatalog', { url });
    $('#remote-grid').replaceChildren(...items.map((m) => extCard({ ...m, media: m.media || [] }, true)));
    if (!items.length) toast('Catalog trống');
  } catch (err) { toast(`Không tải được catalog: ${errMsg(err)}`, 'err'); }
});

// ---------------- extension generator ----------------
// A session turns a site link into a draft (manifest.json, downloader.py, test fixtures, report): it never installs.
// "Cài vào app…" asks main, which re-checks the draft offline, packs it and confirms with a native dialog.
const GEN_RULES = {
  R1: 'chỉ truy cập công khai hoặc API chính thức bằng khoá của bạn',
  R2: 'robots.txt của site không cho phép',
  R3: 'điều khoản của site cấm tải tự động',
  R4: 'giới hạn tốc độ / số yêu cầu tới site',
  R5: 'site có chống bot (Cloudflare…): Lustra không vượt qua',
  R6: 'không tạo link cho bài bị ẩn / giới hạn',
  R7: 'không hỗ trợ DRM / luồng mã hoá',
  R8: 'nội dung bất hợp pháp',
  R9: 'bộ lọc an toàn của Lustra phải giữ nguyên',
  R10: 'chỉ https, địa chỉ công khai, đúng site đó; code chỉ dùng thư viện cho phép',
  'model-refusal': 'model từ chối yêu cầu',
  budget: 'hết ngân sách chi phí / lượt gọi model',
};
const GEN_STATUS = {
  ready: ['sẵn sàng cài', 'ok'], failed: ['chưa đạt', 'err'], refused: ['bị từ chối', 'warn'], cancelled: ['đã huỷ', ''],
  error: ['lỗi', 'err'], unknown: ['không rõ', ''],
};
const G = { mode: 'ai', running: false, key: null, session: '', restored: false };

function genStatusPill(status) {
  const [label, kind] = GEN_STATUS[status] || [status, ''];
  return h('span', { class: `tagpill ${kind}` }, label);
}
function setGenMode(mode) {
  G.mode = mode === 'template' ? 'template' : 'ai';
  $$('#gen-mode button').forEach((b) => b.classList.toggle('on', b.dataset.mode === G.mode));
  $('#gen-budget-wrap').hidden = G.mode !== 'ai';
  $('#gen-hint').textContent = G.mode === 'ai'
    ? 'Claude khảo sát site, viết / sửa downloader.py khi site không chạy Gelbooru / Danbooru và chạy thử tới khi đạt. Tính phí Claude API theo lượng dùng, dừng khi chạm mức tối đa.'
    : 'Miễn phí, không cần khoá: chỉ cho site chạy Gelbooru 0.2 (DAPI) hoặc Danbooru, dùng template có sẵn của Lustra.';
  renderGenNote();
}
function renderGenNote() {
  const note = $('#gen-note');
  if (G.mode === 'ai' && G.key && !G.key.set) {
    note.replaceChildren('Chưa có khoá Claude API. ',
      h('a', { href: '#', onclick: (e) => { e.preventDefault(); showPage('settings'); $('#ai-key').focus(); } }, 'Thêm trong Cài đặt → AI tạo extension'),
      ', hoặc chọn "Không dùng AI" cho site Gelbooru / Danbooru.');
    note.hidden = false;
  } else note.hidden = true;
}
function genSetRunning(running) {
  G.running = running;
  $('#gen-start').disabled = running;
  $('#gen-cancel').hidden = !running;
  if (running) $('#gen-dot').className = 'dot busy';
}
function genAppend(text, cls = '') {
  const log = $('#gen-log');
  const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  log.append(h('span', { class: cls }, `${text}\n`));
  while (log.childNodes.length > 400) log.firstChild.remove();
  if (atEnd) log.scrollTop = log.scrollHeight;
}
function onGenEvent(ev, replay = false) {
  if (!ev || typeof ev.type !== 'string') return;
  if (ev.type === 'start') {
    G.session = ev.session || '';
    $('#gen-run').hidden = false;
    if (!replay) $('#gen-log').replaceChildren();
    $('#gen-phase').textContent = 'Đang bắt đầu…';
    $('#gen-usage').textContent = '';
    genAppend(`Phiên ${ev.session} · ${ev.url} · ${ev.mode === 'ai' ? `AI (${ev.model})` : 'không dùng AI'}`);
  } else if (ev.type === 'phase') {
    $('#gen-phase').textContent = ev.state === 'writing' ? `Claude đang soạn lệnh: ${ev.label}…` : `${ev.label}…`;
  } else if (ev.type === 'log') {
    const model = ev.source === 'model';
    genAppend(model ? `Claude: ${ev.text}` : `• ${ev.text}`, model ? 'model' : ev.error ? 'err' : '');
  } else if (ev.type === 'usage') {
    $('#gen-usage').textContent = `$${Number(ev.usd || 0).toFixed(2)} / $${Number(ev.maxUsd || 0).toFixed(2)} · ${ev.turns} lượt`;
  } else if (ev.type === 'done') {
    const [label, kind] = GEN_STATUS[ev.status] || [ev.status, ''];
    $('#gen-run').hidden = false;
    $('#gen-phase').textContent = `Kết thúc: ${label}`;
    $('#gen-dot').className = `dot ${kind === 'ok' ? 'ok' : kind === 'err' ? 'err' : ''}`;
    if (ev.summary) genAppend(ev.summary, kind === 'err' ? 'err' : '');
    if (!replay) {
      toast(ev.status === 'ready' ? 'Đã tạo bản nháp extension: xem lại rồi bấm "Cài vào app…"' : `Tạo extension: ${label}`, ev.status === 'ready' ? 'ok' : 'err');
      renderGenDrafts();
    }
  } else if (ev.type === 'exit') {
    genSetRunning(false);
    renderGenDrafts();
  }
}
async function renderGen() {
  if (!L.gen) { $('#gen-panel').hidden = true; return; }
  try {
    const st = await L.gen.status();
    G.key = st.key;
    if (!$('#gen-budget').value) $('#gen-budget').value = st.defaults?.maxUsd ?? 2;
    setGenMode(G.mode);
    genSetRunning(st.running);
    if (!G.restored && st.events?.length) { // renderer reloaded: rebuild the log of the last / running session
      $('#gen-log').replaceChildren();
      st.events.forEach((e) => onGenEvent(e, true));
      if (st.running) genSetRunning(true);
    }
    G.restored = true;
  } catch (err) { toast(`Trình tạo extension: ${errMsg(err)}`, 'err'); }
  renderGenDrafts();
}
async function renderGenDrafts() {
  if (!L.gen) return;
  let items = [];
  try { items = await L.gen.list(); } catch (err) { toast(`Không đọc được bản nháp: ${errMsg(err)}`, 'err'); }
  $('#gen-drafts').replaceChildren(...(items.length ? items.map(genDraftRow) : [h('p', { class: 'muted' }, 'Chưa có bản nháp nào.')]));
}
function genDraftRow(d) {
  const at = d.finishedAt || d.startedAt;
  const when = at ? new Date(at).toLocaleString('vi-VN') : d.session;
  const running = G.running && G.session === d.session;
  const act = (label, fn, cls = 'btn sm ghost') => h('button', { class: cls, onclick: fn }, label);
  const meta = [d.site || d.url, when, d.mode === 'ai' ? `$${Number(d.usd || 0).toFixed(2)}` : d.mode === 'template' ? 'không AI' : ''];
  return h('div', { class: 'gen-draft' },
    h('div', { class: 'gen-draft-head' },
      d.abbr ? h('span', { class: 'mod-abbr' }, d.abbr) : null,
      h('b', {}, d.name || d.url || d.session),
      running ? h('span', { class: 'tagpill acc' }, 'đang chạy') : genStatusPill(d.status),
      d.engine ? h('span', { class: 'tagpill' }, d.engine) : null,
      h('span', { class: 'muted' }, meta.filter(Boolean).join(' · '))),
    d.summary ? h('p', {}, d.summary.length > 400 ? `${d.summary.slice(0, 400)}…` : d.summary) : null,
    h('div', { class: 'row' },
      act('Chi tiết', () => showGenDetail(d.session)),
      d.draftDir ? act('Mở thư mục', () => L.gen.open(d.session)) : null,
      d.draftDir && !running ? act('Xuất .zip…', () => exportGenDraft(d)) : null,
      d.status === 'ready' && d.draftDir && !running ? act('Cài vào app…', () => installGenDraft(d), 'btn sm primary') : null,
      running ? null : act('Xoá', () => removeGenDraft(d), 'btn sm ghost danger')));
}
async function exportGenDraft(d) {
  try {
    const r = await L.gen.exportZip(d.session);
    if (!r) return;
    const copy = () => navigator.clipboard?.writeText(r.sha256).then(() => toast('Đã chép SHA-256', 'ok'), () => {});
    openModal(h('div', { class: 'gen-detail' },
      h('h2', { text: 'Đã xuất extension' }),
      h('p', {}, r.path),
      h('p', { class: 'muted' }, 'SHA-256 (cần khi cài bằng "Cài từ file .zip…" hoặc chia sẻ cho người khác):'),
      h('pre', {}, r.sha256),
      h('div', { class: 'modal-actions' }, h('button', { class: 'btn ghost', onclick: copy }, 'Chép SHA-256'),
        h('button', { class: 'btn primary', onclick: closeModal }, 'Đóng'))));
  } catch (err) { toast(`Không xuất được: ${errMsg(err)}`, 'err'); }
}
async function installGenDraft(d) {
  try {
    const r = await L.gen.install(d.session);
    toast(r?.restartRequired ? `Đã cài ${d.name}. Khởi động lại app để dùng phiên bản mới.` : `Đã cài ${d.name || d.extId}`, 'ok');
    renderExtensions();
  } catch (err) { if (!/cancelled/.test(errMsg(err))) toast(`Cài thất bại: ${errMsg(err)}`, 'err'); }
}
async function removeGenDraft(d) {
  try {
    if (await L.gen.remove(d.session)) { toast('Đã xoá bản nháp', 'ok'); renderGenDrafts(); }
  } catch (err) { toast(errMsg(err), 'err'); }
}
async function showGenDetail(session) {
  let d;
  try { d = await L.gen.get(session); } catch (err) { toast(errMsg(err), 'err'); return; }
  const mark = (c) => (c.ok ? '✓' : c.severity === 'warning' ? '!' : '✗');
  const checks = (d.checks || []).map((c) => h('tr', {}, h('td', {}, `${mark(c)} ${c.id}`), h('td', {}, String(c.detail || '').slice(0, 400))));
  const rules = (d.ruleIds || []).map((r) => `${r}: ${GEN_RULES[r] || ''}`);
  const u = d.usage || {};
  const info = [
    ['Site', d.site || d.url || '—'],
    ['Extension', d.extId ? `${d.name} (${d.extId} · ${d.abbr}) v${d.version}` : '—'],
    ['Engine', d.engine || '—'],
    ['Cách tạo', d.mode === 'ai' ? `AI · ${d.model}` : 'template có sẵn, không AI'],
    ['Chạy thử', `${d.testRuns || 0} lần · ${d.requests || 0} yêu cầu tới site`],
    d.mode === 'ai' ? ['Chi phí', `$${Number(d.usd || 0).toFixed(3)} · ${nf.format(u.input || 0)} token vào · ${nf.format(u.output || 0)} token ra · `
      + `${nf.format(u.cacheRead || 0)} token cache · ${d.turns || 0} lượt`] : null,
    ['Tệp', (d.files || []).join(', ') || '—'],
    d.error ? ['Lỗi', d.error] : null,
  ].filter(Boolean);
  openModal(h('div', { class: 'gen-detail' },
    h('h2', {}, `${d.name || d.url || session} `, genStatusPill(d.status)),
    d.summary ? h('pre', {}, d.summary) : null,
    rules.length ? h('div', { class: 'notice' }, `Quy tắc: ${rules.join('; ')}`) : null,
    h('table', { class: 'table' }, info.map(([k, v]) => h('tr', {}, h('th', {}, k), h('td', {}, v)))),
    checks.length ? h('h3', {}, 'Kiểm tra') : null,
    checks.length ? h('table', { class: 'table' }, checks) : null,
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn primary', onclick: closeModal }, 'Đóng'))));
}
$('#gen-mode').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setGenMode(b.dataset.mode); });
$('#gen-url').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !G.running) $('#gen-start').click(); });
$('#gen-start').addEventListener('click', async () => {
  const url = $('#gen-url').value.trim();
  if (!url) { toast('Nhập link website', 'err'); $('#gen-url').focus(); return; }
  const media = ['image', 'video', 'audio'].filter((m) => $(`#gen-m-${m}`).checked);
  if (!media.length) { toast('Chọn ít nhất một loại media', 'err'); return; }
  genSetRunning(true);
  $('#gen-run').hidden = false;
  $('#gen-log').replaceChildren();
  $('#gen-usage').textContent = '';
  $('#gen-phase').textContent = 'Đang khởi động…';
  try {
    await L.gen.start({ url, mode: G.mode, media, query: $('#gen-query').value.trim(), maxUsd: Number($('#gen-budget').value) || undefined });
  } catch (err) {
    genSetRunning(false);
    $('#gen-dot').className = 'dot err';
    $('#gen-phase').textContent = 'Không bắt đầu được';
    toast(errMsg(err), 'err');
  }
});
$('#gen-cancel').addEventListener('click', async () => {
  try { if (await L.gen.cancel()) $('#gen-phase').textContent = 'Đang huỷ…'; } catch (err) { toast(errMsg(err), 'err'); }
});

// Settings → AI tạo extension: the key is write-only (main stores it encrypted, the renderer only learns whether one is set)
function renderAiSettings() {
  if (!L.gen) { $('#ai-key').closest('.panel').hidden = true; return; }
  const st = S.secrets?.ai || { set: false, encryption: false };
  $('#ai-key-note').textContent = st.encryption
    ? 'Khoá được mã hoá bằng keyring của hệ điều hành và không hiện lại trên giao diện; chỉ tiến trình tạo extension nhận được khoá.'
    : 'Không có keyring hệ điều hành: Lustra không lưu khoá Claude API dạng chữ thường. Chế độ "Không dùng AI" vẫn dùng được.';
  $('#ai-key-state').replaceChildren(st.set ? h('span', { class: 'tagpill ok' }, 'đã lưu (mã hoá)') : '');
  $('#ai-key').placeholder = st.set ? 'Khoá mới (trống = giữ nguyên)' : 'sk-ant-…';
  $('#ai-key').disabled = !st.encryption;
  $('#ai-key-save').disabled = !st.encryption;
  $('#ai-key-clear').hidden = !st.set;
  $('#ai-model').value = S.settings.ai?.model || 'claude-opus-5-5';
  $('#ai-budget').value = S.settings.ai?.maxUsd ?? 2;
}
function onAiKeyStatus(st) {
  S.secrets = { ...S.secrets, ai: st };
  G.key = st;
  renderAiSettings();
  renderGenNote();
}
$('#ai-key-save').addEventListener('click', async () => {
  const v = $('#ai-key').value.trim();
  if (!v) { toast(S.secrets?.ai?.set ? 'Ô trống: khoá đã lưu được giữ nguyên.' : 'Nhập khoá trước khi lưu.', S.secrets?.ai?.set ? '' : 'err'); return; }
  try {
    onAiKeyStatus(await L.gen.setKey(v));
    $('#ai-key').value = '';
    toast('Đã lưu khoá Claude API (mã hoá)', 'ok');
  } catch (err) { toast(`Không lưu được khoá: ${errMsg(err)}`, 'err'); }
});
$('#ai-key').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#ai-key-save').click(); });
$('#ai-key-clear').addEventListener('click', async () => {
  if (!window.confirm('Xoá khoá Claude API khỏi máy? Khoá không khôi phục được.')) return;
  try { onAiKeyStatus(await L.gen.clearKey()); toast('Đã xoá khoá Claude API', 'ok'); } catch (err) { toast(errMsg(err), 'err'); }
});
$('#ai-model').addEventListener('change', (e) => saveSettings({ ai: { model: e.target.value } }));
$('#ai-budget').addEventListener('change', async (e) => {
  await saveSettings({ ai: { maxUsd: Number(e.target.value) || 2 } });
  $('#ai-budget').value = S.settings.ai.maxUsd;
  $('#gen-budget').value = S.settings.ai.maxUsd;
});

// ---------------- settings ----------------
async function saveSettings(patch) {
  const r = await L.settings.set(patch);
  S.settings = r.settings;
  S.secrets = r.secrets;
  return r;
}
function renderAppearanceSettings() {
  const a = S.appearance;
  if (!a) return;
  const names = { ember: 'Ember', graphite: 'Graphite', paper: 'Paper', oled: 'OLED' };
  const swatch = { ember: 'linear-gradient(135deg,#1a1512 55%,#ff6a1a 55%)', graphite: 'linear-gradient(135deg,#18181a 55%,#e8e8e8 55%)',
    paper: 'linear-gradient(135deg,#fffcf8 55%,#e2570b 55%)', oled: 'linear-gradient(135deg,#000 55%,#ff6a1a 55%)' };
  $('#theme-row').replaceChildren(...a.themes.map((t) => h('button', {
    class: `theme-sw ${t === a.theme ? 'on' : ''}`, style: `background:${swatch[t]}`,
    onclick: () => saveSettings({ appearance: { theme: t } }),
  }, h('span', { class: 'sw-label' }, names[t] || t))));
  $('#icon-row').replaceChildren(...a.icons.map((ic) => h('button', {
    class: `icon-sw ${ic.id === a.icon ? 'on' : ''}`, title: ic.name,
    onclick: async () => {
      const r = await saveSettings({ appearance: { icon: ic.id } });
      showIconNotice(r.iconSync, r.appearance);
    },
  }, h('img', { src: ic.url, alt: ic.name }))));
  const bg = a.background;
  $$('#bg-mode button').forEach((b) => b.classList.toggle('on', b.dataset.mode === bg.mode));
  $('#bg-image-row').hidden = bg.mode !== 'image';
  $('#bg-blur-row').hidden = bg.mode !== 'icon';
  $('#bg-image-name').textContent = bg.image ? bg.image.split(/[\\/]/).pop() : 'chưa chọn';
  $('#bg-blur').value = bg.blur;
  $('#bg-opacity').value = Math.round((bg.opacity ?? 0.9) * 100);
  $('#bg-dim-range').value = Math.round((bg.dim ?? 0.18) * 100);
}
// sync: what main updated outside the window (iconSync of settings:set): launcher 'updated' | 'unchanged' | 'missing' |
// 'foreign' | 'unsupported', shortcuts (Windows), restartForDock (Wayland reads the launcher when the app starts)
function showIconNotice(sync, a) {
  const n = $('#icon-notice');
  const done = [];
  if (sync?.launcher === 'updated') done.push('launcher');
  if (sync?.shortcuts) done.push(`${sync.shortcuts} shortcut`);
  if (sync?.error) toast(`Không cập nhật được icon ngoài app: ${sync.error}`, 'err');
  const relaunch = h('button', { class: 'btn sm primary', onclick: () => L.app.relaunch() }, 'Khởi động lại ngay');
  if (a?.liveTaskbarIcon !== false) {
    n.hidden = true;
    toast(`Đã đổi icon${done.length ? ` (cả ${done.join(', ')})` : ''}`, 'ok');
    return;
  }
  // Wayland: the dock / taskbar icon comes from the launcher (lustra.desktop) only
  n.hidden = false;
  if (sync?.launcher === 'missing') {
    n.replaceChildren(
      h('div', {}, 'Icon trong app đã đổi. Trên Wayland, dock/taskbar lấy icon từ launcher (lustra.desktop) nhưng máy chưa có launcher của Lustra, '
        + 'nên dock đang hiện icon chung. Tạo launcher (Lustra sẽ tự cập nhật nó mỗi khi bạn đổi icon) rồi mở lại app.'),
      h('div', { class: 'row' },
        h('button', {
          class: 'btn sm', onclick: async () => {
            const r = await L.app.installLauncher();
            toast(r.ok ? `Đã tạo launcher: ${r.path}` : r.message, r.ok ? 'ok' : 'err');
            if (r.ok) showIconNotice({ launcher: 'updated', restartForDock: true }, a);
          },
        }, 'Tạo launcher'), relaunch));
  } else if (sync?.launcher === 'foreign') {
    n.replaceChildren(
      h('div', {}, 'Icon trong app đã đổi. Launcher lustra.desktop trên máy do công cụ khác tạo (ví dụ công cụ tích hợp AppImage) nên Lustra không sửa nó; '
        + 'dock trên Wayland vẫn hiện icon của launcher đó.'));
  } else {
    n.replaceChildren(
      h('div', {}, 'Icon trong app và launcher đã đổi. Dock trên Wayland chỉ đọc launcher khi app mở: khởi động lại để dock hiện icon mới.'),
      h('div', { class: 'row' }, relaunch));
  }
}
$('#bg-mode').addEventListener('click', async (e) => {
  const mode = e.target.dataset.mode;
  if (!mode) return;
  if (mode === 'image' && !S.appearance.background.image) {
    const file = await L.dialogs.image();
    if (!file) return;
    await L.appearance.setBackgroundImage(file);
    return;
  }
  saveSettings({ appearance: { background: { mode } } });
});
$('#bg-pick').addEventListener('click', async () => {
  const file = await L.dialogs.image();
  if (file) await L.appearance.setBackgroundImage(file);
});
const saveBg = debounce((patch) => saveSettings({ appearance: { background: patch } }), 150);
$('#bg-blur').addEventListener('input', (e) => { document.documentElement.style.setProperty('--bg-blur', `${e.target.value}px`); saveBg({ blur: Number(e.target.value) }); });
$('#bg-opacity').addEventListener('input', (e) => { document.documentElement.style.setProperty('--bg-opacity', String(e.target.value / 100)); saveBg({ opacity: e.target.value / 100 }); });
$('#bg-dim-range').addEventListener('input', (e) => { document.documentElement.style.setProperty('--bg-dim', String(e.target.value / 100)); saveBg({ dim: e.target.value / 100 }); });
$('#safe-preview').addEventListener('change', (e) => { document.body.classList.toggle('safe', e.target.checked); saveSettings({ search: { safePreview: e.target.checked } }); });

function bindSetting(id, path, kind = 'checkbox') {
  const el = $(id);
  const keys = path.split('.');
  let v = S.settings;
  for (const k of keys) v = v?.[k];
  if (kind === 'checkbox') el.checked = !!v; else el.value = v ?? '';
  el.addEventListener('change', () => {
    let val = kind === 'checkbox' ? el.checked : kind === 'number' ? Number(el.value) || 0 : el.value;
    const patch = {};
    let cur = patch;
    keys.forEach((k, i) => { cur[k] = i === keys.length - 1 ? val : {}; cur = cur[k]; });
    saveSettings(patch);
    val = null;
  });
}
function renderSettings() {
  const s = S.settings;
  bindSetting('#s-path', 'download.path', 'text');
  bindSetting('#s-subfolder', 'download.subfolderPerQuery');
  bindSetting('#s-prefix', 'download.prefix');
  bindSetting('#s-append', 'download.appendInfo');
  bindSetting('#s-preserve', 'download.preserveDate');
  bindSetting('#s-dump-tags', 'download.dumpTags');
  bindSetting('#s-dump-sources', 'download.dumpSources');
  bindSetting('#s-dump-comments', 'download.dumpComments');
  bindSetting('#s-dump-hashes', 'download.dumpHashes');
  bindSetting('#s-infomode', 'download.infoMode', 'text');
  bindSetting('#s-warn', 'download.warnNonEmpty');
  bindSetting('#s-concurrent', 'download.maxConcurrentJobs', 'number');
  $('#c-proxy').value = s.connection.proxy || '';
  $('#c-ignore').checked = !!s.connection.ignoreProxy;
  $('#c-nodown').checked = !!s.connection.proxyNoDownload;
  $('#c-timeout').value = s.connection.timeout || 0;
  $('#c-retries').value = s.connection.retries || 0;
  $('#c-headers').value = Object.keys(s.connection.headers || {}).length ? JSON.stringify(s.connection.headers, null, 1) : '';
  $('#c-cookies').value = Object.keys(s.connection.cookies || {}).length ? JSON.stringify(s.connection.cookies, null, 1) : '';
  $('#a-python').value = s.advanced.pythonPath || '';
  $('#a-verbose').checked = !!s.advanced.verbose;
  $('#a-catalog').value = s.advanced.catalogUrl || '';
  $('#safe-preview').checked = !!s.search.safePreview;
  document.body.classList.toggle('safe', !!s.search.safePreview);
  // search page defaults
  $('#f-path').value = s.download.path;
  $('#f-threads').value = s.download.threads;
  $('#f-images').checked = s.download.images;
  $('#f-videos').checked = s.download.videos;
  $('#f-lowres').checked = s.download.lowRes;
  $('#f-order').value = s.download.reverse ? 'desc' : 'asc';
  $('#f-limit').value = s.download.limit;
  $('#f-parchi').checked = s.download.parchi;
  renderApiKeys();
  renderTagLimits();
}
$('#s-path-pick').addEventListener('click', async () => {
  const p = await L.dialogs.folder($('#s-path').value);
  if (p) { $('#s-path').value = p; $('#f-path').value = p; saveSettings({ download: { path: p } }); }
});
$('#f-path-pick').addEventListener('click', async () => { const p = await L.dialogs.folder($('#f-path').value); if (p) $('#f-path').value = p; });
$('#f-path-open').addEventListener('click', () => L.app.openPath($('#f-path').value));
$('#c-save').addEventListener('click', async () => {
  const parse = (id) => {
    const t = $(id).value.trim();
    if (!t) return {};
    const v = JSON.parse(t);
    if (typeof v !== 'object' || Array.isArray(v)) throw new Error('phải là JSON object');
    return v;
  };
  try {
    const headers = parse('#c-headers');
    const cookies = parse('#c-cookies');
    const proxy = $('#c-proxy').value.trim();
    if (proxy && !/^(https?|socks5h?):\/\/([^@/]+@)?[^/:]+:\d+\/?$/i.test(proxy)) throw new Error('proxy dạng type://[user:pass@]host:port');
    // one save: main replaces the headers / cookies maps as a whole (removed entries go away) and rejects invalid
    // entries before writing anything
    await saveSettings({ connection: { proxy, ignoreProxy: $('#c-ignore').checked, proxyNoDownload: $('#c-nodown').checked,
      timeout: Number($('#c-timeout').value) || 0, retries: Number($('#c-retries').value) || 0, headers, cookies } });
    toast('Đã lưu cài đặt kết nối', 'ok');
  } catch (err) { toast(`Không lưu được: ${errMsg(err)}`, 'err'); }
});
$('#a-save').addEventListener('click', async () => {
  await saveSettings({ advanced: { pythonPath: $('#a-python').value.trim(), verbose: $('#a-verbose').checked, catalogUrl: $('#a-catalog').value.trim() } });
  toast('Đã lưu. Đổi Python cần khởi động lại app.', 'ok');
});
function renderApiKeys() {
  const sec = S.secrets || { modules: {}, encryption: false };
  $('#keys-note').textContent = sec.encryption
    ? 'Key được mã hoá bằng keyring của hệ điều hành (safeStorage) và không bao giờ gửi tới giao diện.'
    : 'Không có keyring hệ điều hành: key sẽ được lưu dạng văn bản thường trong settings.json.';
  const canClear = typeof L.secrets?.clear === 'function';
  const mods = S.modules.filter((m) => m.installed && m.auth?.apiKey);
  $('#api-keys').replaceChildren(...mods.map((m) => {
    const st = sec.modules?.[m.abbr];
    const input = h('input', { type: 'password', placeholder: st ? 'Key mới (trống = giữ nguyên)' : (m.auth.apiKeyFormat || 'KEY,USER'),
      title: st ? 'Nhập key mới để thay key đã lưu. Để trống thì key đã lưu được giữ nguyên.' : '', autocomplete: 'off' });
    const save = async () => {
      // an empty field never deletes the stored key: that is what "Xoá key" is for
      if (!input.value.trim()) { toast(st ? 'Ô trống: key đã lưu được giữ nguyên.' : 'Nhập key trước khi lưu.', st ? '' : 'err'); return; }
      try {
        S.secrets = await L.secrets.set(m.abbr, input.value);
        input.value = '';
        renderApiKeys();
        toast(`Đã lưu API key cho ${m.name}`, 'ok');
      } catch (err) { toast(`Không lưu được key: ${errMsg(err)}`, 'err'); }
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
    const clear = async () => {
      if (!window.confirm(`Xoá API key của ${m.name}? Key sẽ bị xoá khỏi máy và không khôi phục được.`)) return;
      try {
        S.secrets = await L.secrets.clear(m.abbr);
        renderApiKeys();
        toast(`Đã xoá API key của ${m.name}`, 'ok');
      } catch (err) { toast(`Không xoá được key: ${errMsg(err)}`, 'err'); }
    };
    return h('div', { class: 'setting key-row' }, h('span', {}, h('span', { class: 'mod-abbr' }, m.abbr), ` ${m.name} `, st ? h('span', { class: 'tagpill ok' }, st.encrypted ? 'đã lưu (mã hoá)' : 'đã lưu') : null),
      h('span', { class: 'row' }, input,
        h('button', { class: 'btn sm', onclick: save }, 'Lưu'),
        st && canClear ? h('button', { class: 'btn sm ghost danger', onclick: clear }, 'Xoá key') : null));
  }));
}
// Account tag limit per module (settings.search.tagLimits, 0 = module default): main passes it to the core as -tags_limit.
// Shown for modules whose manifest declares a tag limit (syntax.maxTags), and always for Danbooru.
function renderTagLimits() {
  const limits = S.settings?.search?.tagLimits || {};
  const mods = S.modules.filter((m) => m.installed && (m.syntax?.maxTags > 0 || m.abbr === 'db'));
  $('#tag-limits').replaceChildren(...mods.map((m) => {
    const def = m.syntax?.maxTags || (m.abbr === 'db' ? 2 : 0);
    const input = h('input', { type: 'number', min: 0, max: 100, step: 1, class: 'tiny', value: Number(limits[m.abbr]) || 0, title: `0 = mặc định của module (${def})` });
    input.addEventListener('change', async () => {
      const n = Math.max(0, Math.min(100, Math.floor(Number(input.value) || 0)));
      input.value = n;
      try {
        await saveSettings({ search: { tagLimits: { [m.abbr]: n } } });
        toast(n ? `${m.name}: tối đa ${n} tag mỗi lần tìm` : `${m.name}: dùng giới hạn mặc định (${def} tag)`, 'ok');
      } catch (err) { toast(`Không lưu được: ${errMsg(err)}`, 'err'); }
    });
    return h('div', { class: 'setting' },
      h('span', {}, h('span', { class: 'mod-abbr' }, m.abbr), ` ${m.name} `, h('span', { class: 'muted' }, `(mặc định ${def})`)),
      h('span', { class: 'row' }, input, h('span', { class: 'muted' }, 'tag')));
  }));
}
async function renderAbout() {
  const i = await L.app.info();
  S.info = i;
  $('#brand-ver').textContent = `v${i.version}`;
  $('#about').textContent = [
    `Lustra desktop ${i.version} · Electron ${i.electron} · ${i.platform}`,
    `Lõi Python: ${i.sidecar ? `${i.sidecar.name} ${i.sidecar.version} (Python ${i.sidecar.python})` : 'chưa sẵn sàng'}`,
    `Python: ${i.python}`, `Dữ liệu: ${i.userData}`, `Mã nguồn: ${i.repo}`,
    '', 'Lustra là bản phái sinh của Ruxx (github.com/trickerer01/Ruxx), giấy phép MIT.',
  ].join('\n');
}

// ---------------- log ----------------
// S.logLines is the model (trimmed in batches); the <pre> is only touched while the log page is visible, otherwise
// it is rebuilt once when the page is opened.
const LOG_MAX = 3000;
const LOG_SLACK = 500;
const logView = createLogView($('#log'), LOG_MAX, LOG_SLACK);
let logStale = false;
function appendLog(source, text) {
  const line = `[${source}] ${text}`;
  S.logLines.push(line);
  if (S.logLines.length > LOG_MAX + LOG_SLACK) S.logLines.splice(0, S.logLines.length - LOG_MAX);
  if ($('#page-log').classList.contains('active')) logView.push([line]);
  else logStale = true;
}
function showLogPage() {
  if (logStale) { logStale = false; logView.reset(S.logLines); } else logView.reveal();
}
$('#log-clear').addEventListener('click', () => { S.logLines = []; logStale = false; logView.clear(); });

// ---------------- events from main ----------------
let coreOk = false;
function onSidecarStatus(st) {
  if (!st) return;
  $('#core-dot').className = `dot ${st.ok ? 'ok' : st.error === 'starting' ? '' : 'err'}`;
  $('#core-status').textContent = st.ok ? `Lõi sẵn sàng · ${st.info?.version || ''}`
    : st.error === 'starting' ? 'Đang khởi động lõi…' : `Lõi lỗi: ${st.error}`;
  if (st.ok && !coreOk) { loadModules(); renderAbout(); warmupTags(S.module); loadTagLists(); }
  coreOk = !!st.ok;
}
function listen() {
  L.on('sidecar:status', onSidecarStatus);
  L.on('log', (m) => appendLog(m.source, m.text));
  L.on('jobs:update', (job) => upsertJob(job));
  L.on('jobs:log', (m) => appendJobLog(m.id, m.text));
  L.on('sidecar:notify', ({ method, params }) => {
    if (method === 'tags.updateProgress') {
      const cell = $(`#tagupd-${params.module}`);
      if (cell) cell.textContent = `đang tải… ${nf.format(params.count)} tag`;
    } else if (method === 'app.notice') {
      // main process notices (ex. settings.json was corrupt: backed up, API keys recovered)
      toast(`${params.title ? `${params.title}: ` : ''}${params.message || ''}`, params.level === 'info' ? 'ok' : 'err');
    } else if (method === 'tags.updateDone') {
      toast(`Tag ${params.module}: ${params.message}`, params.written ? 'ok' : 'err');
      if (params.written) {
        for (const k of [...resolveCache.keys()]) if (k.startsWith(`${params.module}\n`)) resolveCache.delete(k);
        resolveHasList.delete(params.module);
      }
      refreshTagStatus();
      loadTagLists();
    }
  });
  L.on('appearance:changed', (a) => applyAppearance(a));
  if (L.gen) L.on('gen:event', (ev) => onGenEvent(ev));
}

// files or links dropped on the window must not navigate the app away
['dragover', 'drop'].forEach((ev) => document.addEventListener(ev, (e) => e.preventDefault()));

// ---------------- boot ----------------
async function boot() {
  const r = await L.settings.get();
  S.settings = r.settings;
  S.secrets = r.secrets;
  S.module = r.settings.search.module || 'rx';
  applyAppearance(r.appearance);
  renderSettings();
  renderAiSettings();
  renderModuleButton();
  $('#btn-related').hidden = !relatedSupported();
  await loadModules();
  renderAbout().catch(() => {});
  try { renderJobs(); (await L.jobs.list()).forEach(upsertJob); } catch { /* ignore */ }
  // the first status event may have been sent before this page loaded
  try { onSidecarStatus(await L.sidecarStatus()); } catch { /* ignore */ }
  tagInput.focus();
}
function start() {
  L = window.lustra;
  listen();
  boot();
}
// The preload bridge is defined before any page script runs, so in Electron start() runs synchronously and mock.js is
// never fetched. In a plain browser (design work, UI tests) the mock bridge is loaded first.
if (window.lustra) start();
else if (/\bElectron\//.test(navigator.userAgent)) {
  $('#core-dot').className = 'dot err';
  $('#core-status').textContent = 'Lỗi: không có cầu nối tới tiến trình chính (preload)';
} else {
  const mock = document.createElement('script');
  mock.src = 'mock.js';
  mock.addEventListener('load', start);
  document.head.append(mock);
}
