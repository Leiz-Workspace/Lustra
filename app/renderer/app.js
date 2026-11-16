// Lustra desktop renderer (plain script, no bundler). Talks to main only through window.lustra (preload bridge or mock.js).
const L = window.lustra;

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

function toast(msg, kind = '') {
  const el = h('div', { class: `toast ${kind}` }, msg);
  $('#toasts').append(el);
  setTimeout(() => el.remove(), kind === 'err' ? 7000 : 3500);
}
function errMsg(err) {
  const m = String(err?.message || err || 'lỗi');
  return m.replace(/^Error invoking remote method '[^']+': (Error: )?/, '');
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
  ac: { items: [], index: -1, seq: 0 },
  preview: { page: 0, items: [], selected: new Set(), site: '', module: '', tags: '', showExcluded: false },
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
function parseToken(tok) {
  tok = tok.trim();
  if (!tok) return null;
  if (/^-\([^()]+,[^()]+\)$/.test(tok)) return { kind: 'neg', items: tok.slice(2, -1).split(',').filter(Boolean) };
  if (/^\([^()]+~[^()]+\)$/.test(tok)) return { kind: 'or', items: tok.slice(1, -1).split('~').map((s) => s.trim()).filter(Boolean) };
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
const queryString = () => S.chips.map(chipToString).join(' ');

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
  // keep groups intact while splitting by whitespace: "(a ~ b)" -> "(a~b)"
  const norm = text.replace(/\(\s+/g, '(').replace(/\s+\)/g, ')').replace(/\s*~\s*/g, '~').replace(/\s*,\s*/g, ',');
  norm.split(/\s+/).filter(Boolean).forEach(addChip);
}
function removeChip(id) {
  S.chips = S.chips.filter((c) => c.id !== id);
  S.selected.delete(id);
  renderChips();
  onQueryChanged();
}
function renderChips() {
  const host = $('#chips');
  host.replaceChildren(...S.chips.map((c) => {
    const label = c.kind === 'or' ? c.items.join(' ∨ ') : c.kind === 'neg' ? `−(${c.items.join(' ∧ ')})` : c.text;
    const cls = ['chip', c.kind === 'meta' ? 'meta' : '', c.kind === 'or' ? 'or' : '', c.kind === 'neg' ? 'neg' : '',
      c.exclude ? 'exclude' : '', S.selected.has(c.id) ? 'sel' : ''].join(' ');
    const title = { tag: 'Tag', meta: 'Meta tag', or: 'Nhóm OR: post có ít nhất một tag', neg: 'Nhóm loại trừ: bỏ post có đủ tất cả tag' }[c.kind];
    return h('span', {
      class: cls, title: `${title} — bấm để chọn, bấm đúp để sửa`,
      onclick: (e) => { if (e.target.closest('button')) return; toggleChipSel(c.id); },
      ondblclick: () => editChip(c),
    },
    h('span', { class: 't' }, label),
    (c.kind === 'tag' || c.kind === 'meta') ? h('button', { class: 'pm', title: 'Đảo bao gồm/loại trừ', onclick: () => { c.exclude = !c.exclude; renderChips(); onQueryChanged(); } }, '±') : null,
    h('button', { class: 'x', title: 'Xoá', onclick: () => removeChip(c.id) }, '×'));
  }));
  const n = S.selected.size;
  $('#chip-actions').hidden = n === 0;
  $('#chip-sel-count').textContent = `${n} tag đã chọn`;
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

$('#chip-actions').addEventListener('click', (e) => {
  const act = e.target.closest('button')?.dataset.act;
  if (!act) return;
  const sel = S.chips.filter((c) => S.selected.has(c.id));
  if (act === 'clear') { S.selected.clear(); renderChips(); return; }
  if (act === 'remove') { S.chips = S.chips.filter((c) => !S.selected.has(c.id)); S.selected.clear(); }
  if (act === 'toggle') sel.forEach((c) => { if (c.kind === 'tag' || c.kind === 'meta') c.exclude = !c.exclude; });
  if (act === 'or' || act === 'neg') {
    const items = sel.flatMap((c) => (c.kind === 'or' || c.kind === 'neg') ? c.items : [act === 'neg' ? c.text : chipToString(c)]);
    const uniq = [...new Set(items)];
    if (uniq.length < 2) { toast('Chọn ít nhất 2 tag để gộp nhóm', 'err'); return; }
    const idx = S.chips.findIndex((c) => S.selected.has(c.id));
    S.chips = S.chips.filter((c) => !S.selected.has(c.id));
    S.chips.splice(Math.max(0, idx), 0, { kind: act, items: uniq, id: ++S.chipSeq });
    S.selected.clear();
  }
  if (act === 'split') {
    const out = [];
    for (const c of S.chips) {
      if (S.selected.has(c.id) && (c.kind === 'or' || c.kind === 'neg')) {
        c.items.forEach((it) => { const p = parseToken(c.kind === 'neg' ? `-${it}` : it); if (p) out.push({ ...p, id: ++S.chipSeq }); });
      } else out.push(c);
    }
    S.chips = out;
    S.selected.clear();
  }
  renderChips();
  onQueryChanged();
});

// ---------------- autocomplete ----------------
const tagInput = $('#tag-text');
$('#tag-input').addEventListener('click', () => tagInput.focus());
tagInput.addEventListener('focus', () => $('#tag-input').classList.add('focus'));
tagInput.addEventListener('blur', () => { $('#tag-input').classList.remove('focus'); setTimeout(hideAc, 150); });

const requestAc = debounce(async () => {
  const raw = tagInput.value.trim();
  const base = raw.replace(/^-/, '');
  if (!S.settings?.search?.autocomplete || base.length < 2 || reMeta.test(base) || /[()~,]/.test(base)) { hideAc(); return; }
  const seq = ++S.ac.seq;
  try {
    const items = await L.rpc('tags.autocomplete', { module: S.module, prefix: raw, limit: 12 });
    if (seq !== S.ac.seq) return;
    S.ac.items = items;
    S.ac.index = items.length ? 0 : -1;
    renderAc(base);
  } catch { hideAc(); }
}, 110);

function renderAc(base) {
  const box = $('#ac');
  if (!S.ac.items.length) {
    box.replaceChildren(h('div', { class: 'ac-hint' }, `Không có gợi ý cho “${base}” trong danh sách tag của ${S.module.toUpperCase()}. Danh sách có thể cũ: cập nhật trong mục Tags A–Z.`));
    box.hidden = false;
    return;
  }
  const re = new RegExp(`^(${escapeRe(base)})`, 'i');
  box.replaceChildren(...S.ac.items.map((it, i) => {
    const nm = h('span', { class: 'nm' });
    nm.innerHTML = it.tag.replace(/[&<>]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[ch])).replace(re, '<b>$1</b>');
    return h('div', { class: `ac-item ${i === S.ac.index ? 'on' : ''}`, onmousedown: (e) => { e.preventDefault(); pickAc(i); } },
      h('span', { class: `type-dot t-${it.type}`, title: it.type }), nm, h('span', { class: 'cnt' }, fmt(it.posts)));
  }), h('div', { class: 'ac-hint' }, '↑↓ chọn · Tab/Enter thêm · Shift+Enter thêm dạng loại trừ'));
  box.hidden = false;
}
function hideAc() { $('#ac').hidden = true; S.ac.items = []; S.ac.index = -1; }
function pickAc(i, exclude = false) {
  const it = S.ac.items[i];
  if (!it) return;
  addChip(exclude && !it.insert.startsWith('-') ? `-${it.insert}` : it.insert);
  tagInput.value = '';
  hideAc();
}
tagInput.addEventListener('input', () => {
  const v = tagInput.value;
  if (/\s$/.test(v) && !/[(][^)]*$/.test(v)) { addTokens(v); tagInput.value = ''; hideAc(); return; }
  requestAc();
});
tagInput.addEventListener('keydown', (e) => {
  const acOpen = !$('#ac').hidden && S.ac.items.length;
  if (e.key === 'ArrowDown' && acOpen) { e.preventDefault(); S.ac.index = (S.ac.index + 1) % S.ac.items.length; renderAc(tagInput.value.replace(/^-/, '')); return; }
  if (e.key === 'ArrowUp' && acOpen) { e.preventDefault(); S.ac.index = (S.ac.index - 1 + S.ac.items.length) % S.ac.items.length; renderAc(tagInput.value.replace(/^-/, '')); return; }
  if ((e.key === 'Enter' || e.key === 'Tab') && acOpen && S.ac.index >= 0) { e.preventDefault(); pickAc(S.ac.index, e.shiftKey); return; }
  if (e.key === 'Enter' || (e.key === 'Tab' && tagInput.value.trim())) {
    e.preventDefault();
    if (tagInput.value.trim()) { addTokens(tagInput.value); tagInput.value = ''; hideAc(); } else if (e.key === 'Enter' && e.ctrlKey) startDownload();
    else if (e.key === 'Enter') runPreview(0);
    return;
  }
  if (e.key === 'Escape') { hideAc(); return; }
  if (e.key === 'Backspace' && !tagInput.value && S.chips.length) { editChip(S.chips[S.chips.length - 1]); e.preventDefault(); }
});
tagInput.addEventListener('paste', (e) => {
  const text = e.clipboardData.getData('text');
  if (/\s/.test(text.trim())) { e.preventDefault(); addTokens(text); }
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
function onQueryChanged() { validate(); }

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
  const btn = $('#btn-count');
  btn.disabled = true; btn.textContent = 'Đang đếm…';
  try {
    const r = await L.search.count(q);
    const st = $('#query-status');
    st.className = r.count > 0 ? 'ok' : 'err';
    st.textContent = `≈ ${nf.format(r.count)} kết quả${r.requiredTags?.length ? ` (trước khi lọc cục bộ: ${r.requiredTags.join(', ')})` : ''}`;
    if (r.cfChallenge) toast('Website đang chặn bằng bot challenge. Xem mục Cài đặt → Kết nối.', 'err');
  } catch (err) {
    toast(`Đếm thất bại: ${errMsg(err)}`, 'err');
  } finally {
    btn.disabled = false; btn.textContent = 'Đếm';
  }
});

// ---------------- preview ----------------
async function runPreview(page = 0) {
  const q = currentQuery();
  if (!q.tags) { toast('Chưa có tag nào', 'err'); return; }
  const btn = $('#btn-preview');
  btn.disabled = true; btn.textContent = 'Đang tải…';
  $('#results-info').textContent = 'Đang lấy kết quả…';
  try {
    const r = await L.search.preview(q, page);
    Object.assign(S.preview, { page, items: r.items, site: r.site, module: q.module, tags: q.tags, required: r.requiredTags || [], tasks: r.tasks || [],
      genderStats: r.genderStats || {}, blockedCount: r.blockedCount || 0, hiddenNoTags: r.hiddenNoTags || 0, genderEnabled: !!r.genderEnabled });
    S.preview.selected.clear();
    renderGrid();
  } catch (err) {
    $('#results-info').textContent = `Lỗi: ${errMsg(err)}`;
    toast(`Preview thất bại: ${errMsg(err)}`, 'err');
  } finally {
    btn.disabled = false; btn.textContent = 'Preview';
  }
}
$('#btn-preview').addEventListener('click', () => runPreview(0));
$('#pg-prev').addEventListener('click', () => { if (S.preview.page > 0) runPreview(S.preview.page - 1); });
$('#pg-next').addEventListener('click', () => runPreview(S.preview.page + 1));
$('#show-excluded').addEventListener('change', (e) => { S.preview.showExcluded = e.target.checked; renderGrid(); });

function renderGrid() {
  const P = S.preview;
  const items = P.items.filter((it) => P.showExcluded || !it.excluded);
  const hidden = P.items.length - items.length;
  const parts = [`Trang ${P.page + 1}: ${P.items.length} post`];
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
  if (P.tasks?.length > 1) parts.push(`truy vấn tách thành ${P.tasks.length} task, preview hiển thị task đầu`);
  $('#results-info').textContent = parts.join(' · ');
  $('#results-tools').hidden = false;
  $('#pg-num').textContent = String(P.page + 1);
  $('#grid-empty').hidden = items.length > 0;
  $('#grid-empty').textContent = 'Không có post nào trên trang này.';
  $('#grid').replaceChildren(...items.map((it) => {
    const img = h('img', { loading: 'lazy', alt: '', src: it.preview ? L.thumbUrl(it.preview, P.site) : '' });
    const g = it.gender;
    const why = (it.excludedBy || []).includes('gender') ? 'lọc giới tính' : 'lọc cục bộ';
    const gTitle = g ? `Giới tính: ${GENDER_VI[g.label] || g.label}${g.conf ? ` · tin cậy ${CONF_VI[g.conf]}` : ''}`
      + `${g.evidence?.length ? ` (${g.evidence.join(', ')})` : ''}${g.flags?.length ? ` · cờ: ${g.flags.join(', ')}` : ''}\n` : '';
    const card = h('div', {
      class: `card ${P.selected.has(it.id) ? 'sel' : ''} ${it.excluded ? 'excluded' : ''}`, 'data-why': why,
      title: gTitle + (it.tags || []).slice(0, 40).join(' '),
      onclick: () => { toggleSel(it.id); card.classList.toggle('sel', P.selected.has(it.id)); },
      ondblclick: () => openLightbox(it),
    }, img, h('span', { class: 'tick' }, '✓'),
    h('div', { class: 'badges' },
      it.isVideo ? h('span', { class: 'b vid' }, '▶ video') : null,
      it.ext ? h('span', { class: 'b' }, it.ext) : null,
      it.score !== undefined && it.score !== '' ? h('span', { class: 'b' }, `★ ${it.score}`) : null,
      it.rating ? h('span', { class: 'b' }, it.rating) : null,
      g ? h('span', { class: `b g ${g.conf || 'low'}` }, GENDER_VI[g.label] || '?') : null));
    return card;
  }));
  updateSelButton();
}
function toggleSel(id) {
  const s = S.preview.selected;
  if (s.has(id)) s.delete(id); else s.add(id);
  updateSelButton();
}
function updateSelButton() {
  const n = S.preview.selected.size;
  const b = $('#dl-selected');
  b.disabled = n === 0;
  b.textContent = `Tải đã chọn (${n})`;
}
$('#sel-all').addEventListener('click', () => { S.preview.items.filter((i) => !i.excluded).forEach((i) => S.preview.selected.add(i.id)); renderGrid(); });
$('#sel-none').addEventListener('click', () => { S.preview.selected.clear(); renderGrid(); });
$('#sel-invert').addEventListener('click', () => {
  S.preview.items.filter((i) => !i.excluded).forEach((i) => (S.preview.selected.has(i.id) ? S.preview.selected.delete(i.id) : S.preview.selected.add(i.id)));
  renderGrid();
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
        h('button', { class: 'btn', onclick: () => { toggleSel(it.id); renderGrid(); closeModal(); } }, S.preview.selected.has(it.id) ? 'Bỏ chọn' : 'Chọn'),
        h('button', { class: 'btn ghost', onclick: closeModal }, 'Đóng')))));
}

// ---------------- downloads ----------------
async function startDownload(override = null) {
  const q = override ? { ...currentQuery(), ...override } : currentQuery();
  if (!q.tags) { toast('Chưa có tag nào', 'err'); return; }
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

function upsertJob(job) {
  const prev = S.jobs.get(job.id);
  S.jobs.set(job.id, { ...prev, ...job, showLog: prev?.showLog || false });
  renderJobs();
}
function renderJobs() {
  const list = [...S.jobs.values()].sort((a, b) => b.createdAt - a.createdAt);
  const active = list.filter((j) => ['queued', 'running', 'cancelling'].includes(j.status)).length;
  const badge = $('#badge-jobs');
  badge.hidden = active === 0;
  badge.textContent = String(active);
  $('#jobs-empty').hidden = list.length > 0;
  $('#jobs').replaceChildren(...list.map((j) => {
    const p = j.progress || {};
    const downloading = p.stateName === 'DOWNLOADING' && p.totalAll > 0;
    const pct = j.status === 'done' ? 100 : downloading ? Math.min(100, (100 * (p.processed || 0)) / p.totalAll) : 0;
    const indet = j.status === 'running' && !downloading;
    const meta = [
      h('span', {}, `Module: ${j.module}`),
      p.stateName ? h('span', {}, `Trạng thái: ${STATE_VI[p.stateName] || p.stateName}`) : null,
      p.tasks > 1 ? h('span', {}, `Task ${p.task || 0}/${p.tasks}`) : null,
      p.totalAll ? h('span', {}, `${nf.format(p.processed || 0)} / ${nf.format(p.totalAll)} file`) : p.total ? h('span', {}, `Tìm thấy ${nf.format(p.total)}`) : null,
      (p.success || p.failed) ? h('span', {}, `✓ ${p.success || 0} · ✗ ${p.failed || 0}`) : null,
      p.cfChallenge ? h('span', { style: 'color:var(--danger)' }, 'Bị bot challenge chặn') : null,
      p.genderFiltered ? h('span', {}, `Lọc giới tính: ${nf.format(p.genderFiltered)}`) : null,
      p.blocked ? h('span', {}, `Chặn (an toàn): ${nf.format(p.blocked)}`) : null,
      h('span', { class: 'mono' }, j.dest),
    ];
    return h('div', { class: 'job glass' },
      h('div', { class: 'job-head' },
        h('span', { class: `status ${j.status}` }, JOB_VI[j.status] || j.status),
        h('span', { class: 'job-q', title: j.tags }, j.label || j.tags),
        h('div', { class: 'job-actions' },
          ['queued', 'running'].includes(j.status) ? h('button', { class: 'btn sm danger', onclick: () => L.jobs.cancel(j.id) }, 'Huỷ') : null,
          h('button', { class: 'btn sm ghost', onclick: () => L.app.openPath(j.dest) }, 'Mở thư mục'),
          h('button', { class: 'btn sm ghost', onclick: () => { S.jobs.get(j.id).showLog = !j.showLog; renderJobs(); } }, j.showLog ? 'Ẩn log' : 'Log'))),
      h('div', { class: `bar ${indet ? 'indet' : ''}` }, h('i', { style: `width:${pct}%` })),
      h('div', { class: 'job-meta' }, meta),
      j.showLog ? h('pre', {}, (j.logTail || []).join('\n')) : null);
  }));
}
$('#jobs-clear').addEventListener('click', async () => {
  const list = await L.jobs.clear();
  S.jobs = new Map(list.map((j) => [j.id, j]));
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
    if (st.loading) return;
    st.loading = true;
    const seq = ++st.seq;
    const prefix = st.letter === '#' ? '#' : (st.prefix && st.prefix.startsWith(st.letter) ? st.prefix : st.letter + (st.prefix.startsWith(st.letter) ? '' : st.prefix));
    try {
      const r = await L.rpc('tags.browse', { module: S.module, prefix, offset: append ? st.offset : 0, limit: 150, minPosts: st.minPosts,
        types: [...st.types], sort: st.sort });
      if (seq !== st.seq) return;
      if (!append) { list.replaceChildren(); st.offset = 0; }
      st.total = r.total;
      st.offset += r.items.length;
      list.append(...r.items.map((it) => h('div', { class: 'az-row' },
        h('span', { class: `type-dot t-${it.type}`, title: it.type }), h('span', { class: 'nm', title: it.tag }, it.tag),
        h('span', { class: 'cnt' }, fmt(it.posts)),
        h('button', { class: 'btn sm', title: 'Thêm vào truy vấn', onclick: () => { addChip(it.tag); toast(`Đã thêm ${it.tag}`); } }, '+'),
        h('button', { class: 'btn sm', title: 'Loại trừ', onclick: () => { addChip(`-${it.tag}`); toast(`Đã loại trừ ${it.tag}`); } }, '−'))));
      foot.firstChild.textContent = `${nf.format(st.total)} tag · ${S.module.toUpperCase()}`;
      foot.lastChild.textContent = st.total === 0 ? 'Chưa có danh sách tag cho module này' : st.offset < st.total ? 'Cuộn để tải thêm' : '';
    } catch (err) {
      foot.firstChild.textContent = `Lỗi: ${errMsg(err)}`;
    } finally {
      st.loading = false;
    }
  }
  function reload() {
    $$('button', letters).forEach((b) => b.classList.toggle('on', b.textContent === st.letter));
    load(false);
  }
  list.addEventListener('scroll', () => {
    if (list.scrollTop + list.clientHeight > list.scrollHeight - 200 && st.offset < st.total) load(true);
  });
  const api = { reload, reset: () => reload(), loaded: false };
  azInstances.push(api);
  return api;
}
const azDrawer = createAZ($('#az-drawer-host'));
const azPage = createAZ($('#az-page-host'));
$('#btn-az').addEventListener('click', () => {
  const d = $('#az-drawer');
  d.hidden = !d.hidden;
  if (!d.hidden && !azDrawer.loaded) { azDrawer.loaded = true; azDrawer.reload(); }
});
$('#az-close').addEventListener('click', () => { $('#az-drawer').hidden = true; });

// ---------------- tag lists status / update ----------------
const UPDATABLE = ['rx', 'db', 'xb', 'bb', 'en'];
async function refreshTagStatus() {
  if (!azPage.loaded) { azPage.loaded = true; azPage.reload(); }
  let rows = [];
  try { rows = await L.rpc('tags.status'); } catch { /* ignore */ }
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
      if (!r.appearance.liveTaskbarIcon) showIconNotice();
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
function showIconNotice() {
  const n = $('#icon-notice');
  n.hidden = false;
  n.replaceChildren(
    h('div', {}, 'Icon trong app đã đổi. Phiên làm việc này chạy trên Wayland: icon ở taskbar/dock được lấy từ desktop entry (lustra.desktop) nên chỉ đổi sau khi mở lại app.'),
    h('div', { class: 'row' },
      h('button', { class: 'btn sm', onclick: async () => { const r = await L.app.installLauncher(); toast(r.ok ? `Đã cập nhật launcher: ${r.path}` : r.message, r.ok ? 'ok' : 'err'); } }, 'Cập nhật launcher'),
      h('button', { class: 'btn sm primary', onclick: () => L.app.relaunch() }, 'Khởi động lại ngay')));
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
    // replace objects entirely (deep merge would keep removed keys)
    S.settings.connection.headers = {}; S.settings.connection.cookies = {};
    await saveSettings({ connection: { headers: null, cookies: null } });
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
  const mods = S.modules.filter((m) => m.installed && m.auth?.apiKey);
  $('#api-keys').replaceChildren(...mods.map((m) => {
    const input = h('input', { type: 'password', placeholder: m.auth.apiKeyFormat || 'KEY,USER', autocomplete: 'off' });
    const st = sec.modules?.[m.abbr];
    return h('div', { class: 'setting' }, h('span', {}, h('span', { class: 'mod-abbr' }, m.abbr), ` ${m.name} `, st ? h('span', { class: 'tagpill ok' }, st.encrypted ? 'đã lưu (mã hoá)' : 'đã lưu') : null),
      h('span', { class: 'row' }, input,
        h('button', { class: 'btn sm', onclick: async () => { S.secrets = await L.secrets.set(m.abbr, input.value); input.value = ''; renderApiKeys(); toast('Đã lưu API key', 'ok'); } }, 'Lưu'),
        st ? h('button', { class: 'btn sm ghost', onclick: async () => { S.secrets = await L.secrets.set(m.abbr, ''); renderApiKeys(); } }, 'Xoá') : null));
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
function appendLog(source, text) {
  S.logLines.push(`[${source}] ${text}`);
  if (S.logLines.length > 3000) S.logLines.splice(0, S.logLines.length - 3000);
  if ($('#page-log').classList.contains('active')) {
    const el = $('#log');
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = S.logLines.join('\n');
    if (atBottom) el.scrollTop = el.scrollHeight;
  }
}
$('#log-clear').addEventListener('click', () => { S.logLines = []; $('#log').textContent = ''; });
$$('#nav button[data-page="log"]').forEach((b) => b.addEventListener('click', () => { $('#log').textContent = S.logLines.join('\n'); }));

// ---------------- events from main ----------------
L.on('sidecar:status', (st) => {
  $('#core-dot').className = `dot ${st.ok ? 'ok' : 'err'}`;
  $('#core-status').textContent = st.ok ? `Lõi sẵn sàng · ${st.info?.version || ''}` : `Lõi lỗi: ${st.error}`;
  if (st.ok) { loadModules(); renderAbout(); warmupTags(S.module); }
});
L.on('log', (m) => appendLog(m.source, m.text));
L.on('jobs:update', (job) => upsertJob(job));
L.on('jobs:log', (m) => {
  const j = S.jobs.get(m.id);
  if (!j) return;
  j.logTail = [...(j.logTail || []), m.text].slice(-200);
  if (j.showLog) renderJobs();
});
L.on('sidecar:notify', ({ method, params }) => {
  if (method === 'tags.updateProgress') {
    const cell = $(`#tagupd-${params.module}`);
    if (cell) cell.textContent = `đang tải… ${nf.format(params.count)} tag`;
  } else if (method === 'tags.updateDone') {
    toast(`Tag ${params.module}: ${params.message}`, params.written ? 'ok' : 'err');
    refreshTagStatus();
  }
});
L.on('appearance:changed', (a) => applyAppearance(a));

// files or links dropped on the window must not navigate the app away
['dragover', 'drop'].forEach((ev) => document.addEventListener(ev, (e) => e.preventDefault()));

// ---------------- boot ----------------
(async function boot() {
  const r = await L.settings.get();
  S.settings = r.settings;
  S.secrets = r.secrets;
  S.module = r.settings.search.module || 'rx';
  applyAppearance(r.appearance);
  renderSettings();
  renderModuleButton();
  await loadModules();
  renderApiKeys();
  renderAbout().catch(() => {});
  try { renderJobs(); (await L.jobs.list()).forEach(upsertJob); } catch { /* ignore */ }
  tagInput.focus();
}());
