// Lustra desktop: Electron main process
// - spawns the Python sidecar (python -m lustra.server, JSON-RPC over stdio) for queries
// - runs downloads as separate CLI processes (LUSTRA_IPC=1 python -m lustra ...) with progress on stdout
// - owns settings (userData/settings.json), secrets (safeStorage), thumbnails cache (lustra-thumb://), appearance
'use strict';

const {
  app, BrowserWindow, ipcMain, dialog, shell, nativeImage, protocol, net, safeStorage, session, Menu, Notification,
} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const fsp = fs.promises;
const os = require('node:os');
const crypto = require('node:crypto');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const netlib = require('node:net');
const dnsp = require('node:dns').promises;

// Linux: Electron derives the window WM_CLASS (X11) and app_id (Wayland) from the desktop name ('lustra.desktop' -> 'lustra'),
// so launchers must use StartupWMClass=lustra (lowercase) for docks to group the window with its launcher
const DESKTOP_ID = 'lustra';
const { pngsToIco } = require('./icon-utils');
const { shq, hasTagBlock, withoutTagBlock, withTagBlock, scheduleWhen } = require('./tag-schedule');

app.setName('Lustra');
if (process.env.LUSTRA_USER_DATA) app.setPath('userData', path.resolve(process.env.LUSTRA_USER_DATA));
if (process.platform === 'linux' && typeof app.setDesktopName === 'function') app.setDesktopName(`${DESKTOP_ID}.desktop`);
// <input type=date> follows the Chromium UI language: Vietnamese shows dd/mm/yyyy like the rest of the UI (before 'ready')
app.commandLine.appendSwitch('lang', 'vi');

const REPO_ROOT = path.resolve(__dirname, '..');
const CORE_NAME = process.platform === 'win32' ? 'lustra-core.exe' : 'lustra-core';
const ICON_DIR = path.join(__dirname, 'assets', 'icons');
const ICONS = [
  { id: 'full-mask-orange', name: 'Full mask · Orange' },
  { id: 'exposed-jaw-orange', name: 'Exposed jaw · Orange' },
  { id: 'full-mask-monochrome', name: 'Full mask · Mono' },
  { id: 'exposed-jaw-monochrome', name: 'Exposed jaw · Mono' },
];
const THEMES = ['ember', 'graphite', 'paper', 'oled'];
const THUMB_CACHE_MAX = 512 * 1024 * 1024;
const THUMB_SIZE_MAX = 25 * 1024 * 1024;
const THUMB_TIMEOUT_MS = 20000;
const PROGRESS_PREFIX = '@@LUSTRA_PROGRESS ';
const SETTINGS_VERSION = 3;
const RE_ABBR = /^[a-z0-9]{2,4}$/; // module abbreviation (extension manifest 'abbr')
const HISTORY_MAX = 50;
const SAVED_MAX = 200;
const QUERY_TAGS_MAX = 2000;
const SAVED_NAME_MAX = 80;
const QUIT_GRACE_MS = 5000; // quitting with running jobs: time given to 'cancel' before the processes are killed

const DEFAULT_SETTINGS = {
  version: SETTINGS_VERSION,
  appearance: {
    theme: 'ember',
    icon: 'full-mask-orange',
    background: { mode: 'icon', image: '', blur: 4, opacity: 0.9, dim: 0.18 },
  },
  search: {
    module: 'rx', autocomplete: true, previewOnSearch: true, safePreview: false, filtersOpen: true,
    tagLimits: {}, // abbr -> max tags per search of the user's account (0 / missing = module default)
    history: [], // [{module, tags, at}] newest first
    saved: [], // [{name, module, tags, at}]
  },
  download: {
    path: path.join(os.homedir(), 'Downloads', 'Lustra'), threads: 4, images: true, videos: true, audio: true, videoFormat: '',
    // op 'exclude': the picked labels are dropped; mode 'model': image model; verifyMedia: videos / animated pictures labeled by the
    // model are checked again on several frames once downloaded (-gender_verify_media), a mismatch is moved to _gender_mismatch/
    gender: { select: [], conf: 'medium', strict: false, op: 'keep', mode: 'tags', verifyMedia: false },
    lowRes: false, prefix: true, appendInfo: false, reverse: false, limit: 0, parchi: false, preserveDate: false,
    skipKnown: false, // -skip_known: skip posts found in the local download index (core lustra/dlindex.py)
    // -safety_blacklist: drop posts whose tags suggest minors (core lustra/safety.py); its own filter, off by default,
    // independent of the gender filter (product decision 2026-11-18). Saved with each query (q.safetyBlacklist)
    safetyBlacklist: false,
    subfolderPerQuery: false, dumpTags: false, dumpSources: false, dumpComments: false, dumpHashes: false, infoMode: 'run',
    warnNonEmpty: true, maxConcurrentJobs: 1, notify: true,
  },
  connection: {
    proxy: '', ignoreProxy: false, proxyNoDownload: false, timeout: 0, retries: 0, headers: {}, cookies: {},
  },
  // schedule: the system scheduler updates the lists even while the app is closed (crontab / Task Scheduler, see tagScheduleStatus)
  tags: { autoUpdate: false, autoUpdateDays: 7, lastAutoUpdate: {}, schedule: { enabled: false, frequency: 'weekly' } },
  // the official signed catalog (tools/build_catalog.py, docs/operations.md 3d); the user may point it elsewhere
  advanced: { pythonPath: '', verbose: false, catalogUrl: 'https://github.com/Leiz-Workspace/Lustra/releases/download/catalog/index.json' },
  ai: { model: 'claude-opus-5-5', maxUsd: 2 }, // extension generator (the Claude API key itself is a secret, see AI_SECRET)
  // gender image model: runs in a model server (python -m lustra.model_server), the app only calls its API (token: MODEL_SECRET).
  // enabled: the user turned it on (the renderer checks the server's GPU / VRAM first); off: jobs filter by tags only
  genderModel: { url: 'http://127.0.0.1:8765', enabled: false },
  window: { width: 1360, height: 860 },
};

// ---------- utils ----------

const userData = () => app.getPath('userData');
const settingsFile = () => path.join(userData(), 'settings.json');
let settings = structuredClone(DEFAULT_SETTINGS);
let secrets = {}; // module abbr -> { enc: base64, plain?: string }
let mainWindow = null;

function deepMerge(base, patch) {
  if (patch === null || typeof patch !== 'object' || Array.isArray(patch)) return patch;
  const out = { ...(base && typeof base === 'object' && !Array.isArray(base) ? base : {}) };
  for (const [k, v] of Object.entries(patch)) out[k] = (v && typeof v === 'object' && !Array.isArray(v)) ? deepMerge(out[k], v) : v;
  return out;
}

function writeJsonAtomic(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp`;
  const fd = fs.openSync(tmp, 'w', 0o600);
  try {
    fs.writeFileSync(fd, JSON.stringify(data, null, 2));
    try { fs.fsyncSync(fd); } catch { /* not supported on this filesystem */ } // a crash must not leave a truncated settings file
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(tmp, file);
  try { fs.chmodSync(file, 0o600); } catch { /* not supported on this filesystem */ }
}

const isPlainObject = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);

// ---------- security helpers ----------

const RE_EXT_ID = /^[a-z][a-z0-9_-]{1,31}$/;
const RE_SHA256 = /^[0-9a-fA-F]{64}$/;
const AI_SECRET = '@anthropic'; // secrets key of the Claude API key: never an extension abbr (those match RE_ABBR)
const MODEL_SECRET = '@gendermodel'; // token of the gender model server
const RE_MODEL_URL = /^https?:\/\/[^\s/?#@]+(\/[^\s?#]*)?$/; // the core checks the rest (plain http only on this computer / LAN)
const RE_GEN_SESSION = /^\d{8}-\d{6}-[0-9a-f]{6}$/; // extension generator session folder (lustra/aigen/session.py)
const GEN_MODELS = ['claude-opus-5-5', 'claude-sonnet-5-5'];
const approvedPaths = new Set(); // files picked by the user through native dialogs (renderer cannot invent paths)

function redact(text) {
  return String(text)
    .replace(/([?&](?:api_key|login|user_id)=)[^&\s"]+/g, '$1<REDACTED>')
    .replace(/(-api_key|-cookies?|-headers?|-proxy)(\s+|=)(\S+)/g, '$1$2<REDACTED>');
}

async function confirmMain(message, detail, buttons = ['Huỷ', 'Đồng ý']) {
  const opts = { type: 'warning', buttons, defaultId: 0, cancelId: 0, noLink: true, message, detail };
  const parent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
  const r = await (parent ? dialog.showMessageBox(parent, opts) : dialog.showMessageBox(opts));
  return r.response === 1;
}

function isPrivateAddress(ip) {
  if (netlib.isIPv4(ip)) {
    const [a, b] = ip.split('.').map(Number);
    return a === 0 || a === 10 || a === 127 || a >= 224 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31)
      || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127);
  }
  const v = ip.toLowerCase();
  if (v.startsWith('::ffff:')) return isPrivateAddress(v.slice(7));
  return v === '::' || v === '::1' || /^f[cd]/.test(v) || /^fe[89ab]/.test(v);
}

async function assertPublicHost(urlString) {
  const url = new URL(urlString);
  if (url.protocol !== 'https:' && url.protocol !== 'http:') throw new Error('unsupported scheme');
  const host = url.hostname.replace(/^\[|\]$/g, '');
  if (!host || host === 'localhost' || /\.(local|localhost|internal|lan|home)$/i.test(host)) throw new Error('blocked host');
  const addrs = netlib.isIP(host) ? [{ address: host }] : await dnsp.lookup(host, { all: true });
  if (!addrs.length || addrs.some((a) => isPrivateAddress(a.address))) throw new Error('blocked host');
}

// ---------- settings ----------

const startupNotices = []; // problems found before the window existed: shown once the renderer has loaded
let settingsWriteBlocked = false; // a corrupt settings file that could not be backed up is never overwritten
let settingsLoadInfo = {}; // reported by the self-test

function sanitizeSecrets(obj) {
  const out = {};
  if (!isPlainObject(obj)) return out;
  for (const [k, v] of Object.entries(obj)) {
    if (!(RE_ABBR.test(k) || k === AI_SECRET || k === MODEL_SECRET) || !isPlainObject(v)) continue;
    if (typeof v.enc === 'string' && v.enc) out[k] = { enc: v.enc };
    else if (k === AI_SECRET) continue; // the Claude API key is only ever stored encrypted
    else if (typeof v.plain === 'string' && v.plain) out[k] = { plain: v.plain };
  }
  return out;
}

function matchingBrace(text, start) {
  let depth = 0;
  let inString = false;
  for (let i = start; i < text.length; i++) {
    const c = text[i];
    if (inString) {
      if (c === '\\') i++;
      else if (c === '"') inString = false;
    } else if (c === '"') inString = true;
    else if (c === '{') depth++;
    else if (c === '}' && --depth === 0) return i;
  }
  return -1;
}

function recoverSecrets(text) {
  // corrupt settings file: the top-level "secrets" block is written last. Parse it as a whole, else entry by entry (truncated file)
  const starts = [...text.matchAll(/"secrets"\s*:\s*\{/g)];
  if (!starts.length) return {};
  const m = starts[starts.length - 1];
  const start = m.index + m[0].length - 1;
  const end = matchingBrace(text, start);
  if (end > 0) {
    try { return sanitizeSecrets(JSON.parse(text.slice(start, end + 1))); } catch { /* entry by entry */ }
  }
  const out = {};
  const re = /"([a-z0-9]{2,4})"\s*:\s*\{\s*"(enc|plain)"\s*:\s*("(?:[^"\\\n]|\\.)*")\s*\}/g;
  for (const e of text.slice(start).matchAll(re)) {
    try { out[e[1]] = { [e[2]]: JSON.parse(e[3]) }; } catch { /* broken entry */ }
  }
  return sanitizeSecrets(out);
}

function backupCorruptSettings() {
  const file = settingsFile();
  const backup = `${file}.corrupt-${new Date().toISOString().replace(/[:.]/g, '-')}`;
  try {
    fs.copyFileSync(file, backup);
  } catch {
    try { fs.renameSync(file, backup); } catch { return ''; } // unreadable file: move it aside instead
  }
  try { fs.chmodSync(backup, 0o600); } catch { /* not supported on this filesystem */ }
  return backup;
}

function readSettingsFile() {
  // -> { raw } for a valid file, {} when there is none, { error, recovered } when it is corrupt or unreadable
  let text;
  try {
    text = fs.readFileSync(settingsFile(), 'utf8');
  } catch (err) {
    return err.code === 'ENOENT' ? {} : { error: err, recovered: {} };
  }
  try {
    const raw = JSON.parse(text);
    if (!isPlainObject(raw)) throw new Error('not a JSON object');
    return { raw };
  } catch (err) {
    return { error: err, recovered: recoverSecrets(text) };
  }
}

function sanitizeQueryList(list, max, named) {
  // search history (named = false: deduplicated by module + tags, newest first) and saved queries (named = true)
  if (!Array.isArray(list)) return null;
  const out = [];
  const seen = new Set();
  for (const e of list) {
    if (out.length >= max) break;
    if (!isPlainObject(e) || typeof e.module !== 'string' || !RE_ABBR.test(e.module) || typeof e.tags !== 'string') continue;
    if (!e.tags.trim() || e.tags.length > QUERY_TAGS_MAX || !Number.isFinite(e.at) || e.at < 0) continue;
    const item = { module: e.module, tags: e.tags, at: Math.floor(e.at) };
    if (named) {
      if (typeof e.name !== 'string') continue;
      const auto = sanitizeAuto(e.auto);
      out.push({ name: e.name.slice(0, SAVED_NAME_MAX), ...item, ...(auto ? { auto } : {}) });
    } else {
      const key = `${e.module}\n${e.tags}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(item);
    }
  }
  return out;
}

// ---------- saved queries downloaded again on a schedule (only newer posts) ----------
const AUTO_EVERY_HOURS = [6, 12, 24, 72, 168];
const AUTO_CHECK_MS = 5 * 60 * 1000;
const autoKeyOf = (e) => `${e.module}\n${e.tags}`;

function sanitizeAuto(a) {
  if (!isPlainObject(a) || !AUTO_EVERY_HOURS.includes(a.every)) return null;
  const num = (v) => (Number.isFinite(v) && v >= 0 ? Math.floor(v) : 0);
  return {
    every: a.every, maxId: num(a.maxId), lastRun: num(a.lastRun), lastCount: num(a.lastCount),
    lastStatus: ['', 'baseline', 'running', 'done', 'failed', 'cancelled'].includes(a.lastStatus) ? a.lastStatus : '',
    lastError: typeof a.lastError === 'string' ? a.lastError.slice(0, 200) : '',
  };
}

function mergeAutoRuntime(incoming) {
  // the renderer writes the whole saved list from its copy: main owns the run fields (last run, max id, status)
  const current = new Map((settings.search.saved || []).map((e) => [autoKeyOf(e), e.auto]));
  for (const e of incoming) {
    const cur = current.get(autoKeyOf(e));
    if (e.auto && cur) Object.assign(e.auto, { maxId: cur.maxId, lastRun: cur.lastRun, lastCount: cur.lastCount, lastStatus: cur.lastStatus, lastError: cur.lastError });
  }
}

function coreMaxId(module) {
  // highest post id of the website right now (lustra -get_maxid): the start point of "only posts newer than today"
  return new Promise((resolve, reject) => {
    connectionArgs(module).then((secretArgs) => {
      const proc = spawnCore('cli', ['-module', module, '-get_maxid'], { LUSTRA_IPC: '1', LUSTRA_SECRET_ARGS: JSON.stringify(secretArgs) });
      let maxId = 0;
      let error = '';
      const timer = setTimeout(() => { try { proc.kill(); } catch { /* gone */ } }, 90000);
      readline.createInterface({ input: proc.stdout }).on('line', (line) => {
        if (!line.startsWith(PROGRESS_PREFIX)) return;
        try {
          const p = JSON.parse(line.slice(PROGRESS_PREFIX.length));
          if (p.stateName === 'DONE') { maxId = Number(p.maxId) || 0; error = p.error || ''; }
        } catch { /* ignore */ }
      });
      proc.stdin.end();
      proc.on('error', (e) => { clearTimeout(timer); reject(e); });
      proc.on('close', () => {
        clearTimeout(timer);
        if (maxId > 0) resolve(maxId);
        else reject(new Error(error || 'không lấy được id mới nhất của website'));
      });
    }, reject);
  });
}

async function setSavedAuto(key, every, from) {
  const e = (settings.search.saved || []).find((x) => autoKeyOf(x) === key);
  if (!e) throw new Error('không tìm thấy truy vấn đã lưu');
  if (!every) {
    delete e.auto;
  } else {
    if (!AUTO_EVERY_HOURS.includes(every)) throw new Error('chu kỳ không hợp lệ');
    if (e.auto) e.auto.every = every; // keeps where it stopped
    else if (from === 'now') e.auto = sanitizeAuto({ every, maxId: await coreMaxId(e.module), lastRun: Date.now(), lastStatus: 'baseline' });
    else e.auto = sanitizeAuto({ every, maxId: 0, lastRun: 0 }); // first run: everything that matches (files already downloaded are skipped)
  }
  saveSettings();
  setTimeout(() => runDueSaved().catch(() => {}), 2000);
  return { settings, appearance: appearanceState(), secrets: secretsStatus() };
}

let autoRunning = false;
async function runDueSaved() {
  if (autoRunning || jobs.closing || process.env.LUSTRA_SELFTEST_DIR) return;
  autoRunning = true;
  try {
    const now = Date.now();
    let modules = null;
    for (const e of settings.search.saved || []) {
      const a = e.auto;
      if (!a?.every || now - a.lastRun < a.every * 3600e3) continue;
      if (jobs.active().some((j) => j.autoKey === autoKeyOf(e))) continue;
      try {
        modules = modules || await sidecar.call('ext.list', {});
        const info = modules.find((m) => m.abbr === e.module && m.installed);
        if (!info) throw new Error(`module ${e.module} chưa được cài`);
        // only newer posts: 'id:>N' where the site uses ':' metatags, and files already in the download index are skipped anyway
        const idFilter = a.maxId > 0 && (info.syntax?.idSep || ':') === ':' ? ` id:>${a.maxId}` : '';
        await jobs.start({ module: e.module, tags: `${e.tags}${idFilter}`, label: `Tự cập nhật: ${e.name}`, path: settings.download.path,
          options: { skipKnown: true }, autoKey: autoKeyOf(e), allowDuplicate: false });
        Object.assign(a, { lastRun: now, lastStatus: 'running', lastError: '' });
      } catch (err) {
        Object.assign(a, { lastRun: now, lastStatus: 'failed', lastError: String(err.message || err).slice(0, 200) });
      }
      saveSettings();
    }
  } finally {
    autoRunning = false;
  }
}

function autoJobFinished(job) {
  const e = (settings.search.saved || []).find((x) => autoKeyOf(x) === job.autoKey);
  if (!e?.auto) return;
  const p = job.progress || {};
  if (job.status === 'done') {
    Object.assign(e.auto, { lastStatus: 'done', lastCount: Number(p.success) || 0, lastError: '', maxId: Math.max(e.auto.maxId, Number(p.maxId) || 0) });
  } else {
    Object.assign(e.auto, { lastStatus: job.status === 'cancelled' ? 'cancelled' : 'failed', lastError: String(job.error || '').slice(0, 200) });
  }
  saveSettings();
}

function sanitizeTagLimits(obj) {
  if (!isPlainObject(obj)) return null;
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    const n = typeof v === 'string' && v.trim() ? Number(v) : v;
    if (RE_ABBR.test(k) && Number.isInteger(n) && n >= 0 && n <= 1000) out[k] = n;
  }
  return out;
}

// RFC 9110 token; a leading '-' would look like a CLI option
const RE_HTTP_NAME = /^[!#$%&'*+.^_`|~0-9A-Za-z][!#$%&'*+.^_`|~0-9A-Za-z-]*$/;

function normalizeHttpMap(map, kind, strict) {
  // headers / cookies {name: value}. strict (settings:set): an invalid entry is an error shown to the user, otherwise dropped
  if (map === null || map === undefined) return {};
  if (!isPlainObject(map)) {
    if (strict) throw new Error(`${kind}: phải là JSON object`);
    return {};
  }
  const out = {};
  for (const [k, v] of Object.entries(map)) {
    const name = String(k).trim();
    const value = typeof v === 'string' ? v : (typeof v === 'number' || typeof v === 'boolean') ? String(v) : null;
    let problem = '';
    if (!RE_HTTP_NAME.test(name)) problem = 'tên không hợp lệ';
    else if (value === null) problem = 'giá trị phải là chuỗi';
    else if (/[\r\n\0]/.test(value)) problem = 'giá trị không được chứa ký tự xuống dòng';
    else if (kind === 'Cookie' && value.includes(';')) problem = 'giá trị không được chứa dấu ;';
    if (problem) {
      if (strict) throw new Error(`${kind} "${name.slice(0, 40)}": ${problem}`);
      continue;
    }
    out[name] = value;
  }
  return out;
}

function sanitizeSettings() {
  for (const [k, v] of Object.entries(DEFAULT_SETTINGS)) if (isPlainObject(v) && !isPlainObject(settings[k])) settings[k] = structuredClone(v);
  if (!isPlainObject(settings.appearance.background)) settings.appearance.background = structuredClone(DEFAULT_SETTINGS.appearance.background);
  if (!THEMES.includes(settings.appearance.theme)) settings.appearance.theme = 'ember';
  if (!ICONS.some((i) => i.id === settings.appearance.icon)) settings.appearance.icon = ICONS[0].id;
  const s = settings.search;
  s.history = sanitizeQueryList(s.history, HISTORY_MAX, false) || [];
  s.saved = sanitizeQueryList(s.saved, SAVED_MAX, true) || [];
  s.tagLimits = sanitizeTagLimits(s.tagLimits) || {};
  settings.connection.headers = normalizeHttpMap(settings.connection.headers, 'Header', false);
  settings.connection.cookies = normalizeHttpMap(settings.connection.cookies, 'Cookie', false);
  if (!GEN_MODELS.includes(settings.ai.model)) settings.ai.model = DEFAULT_SETTINGS.ai.model;
  settings.ai.maxUsd = clampUsd(settings.ai.maxUsd, DEFAULT_SETTINGS.ai.maxUsd);
  settings.genderModel.url = sanitizeModelUrl(settings.genderModel.url) ?? DEFAULT_SETTINGS.genderModel.url;
  settings.genderModel.enabled = settings.genderModel.enabled === true;
}

// the image model is used only when it is turned on and has an address; otherwise the gender filter goes by tags
const genderModelOn = () => settings.genderModel.enabled === true && Boolean(settings.genderModel.url);

function sanitizeModelUrl(u) {
  // '' = no model server: the "tags + model" gender mode is off; null = invalid
  if (u === '') return '';
  return typeof u === 'string' && u.length <= 300 && RE_MODEL_URL.test(u.trim()) ? u.trim().replace(/\/+$/, '') : null;
}

function clampUsd(v, fallback) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.min(Math.max(Math.round(n * 100) / 100, 0.1), 20) : fallback;
}

function migrateSettings(from) {
  // v1/v2 -> v3: earlier default backgrounds were too blurred, the icon outline must stay visible
  const bg = settings.appearance.background;
  const oldDefaults = [[48, 0.32, 0.55], [14, 0.9, 0.18]];
  if (from < 3 && oldDefaults.some(([b, o, d]) => bg.blur === b && bg.opacity === o && bg.dim === d)) {
    Object.assign(bg, { blur: 4, opacity: 0.9, dim: 0.18 });
  }
  settings.version = SETTINGS_VERSION;
}

function loadSettings() {
  const { raw, error, recovered } = readSettingsFile();
  // the file version, not the merged default: a v1 file has no "version" key (the original code read 3 from the defaults)
  const fileVersion = raw ? (Number.isInteger(raw.version) && raw.version > 0 ? raw.version : 1) : SETTINGS_VERSION;
  secrets = raw ? sanitizeSecrets(raw.secrets) : (recovered || {});
  if (raw) delete raw.secrets;
  settings = deepMerge(DEFAULT_SETTINGS, raw || {});
  sanitizeSettings();
  migrateSettings(fileVersion);
  settingsLoadInfo = { fileVersion: raw ? fileVersion : null, corrupt: Boolean(error), migrated: Boolean(raw && fileVersion < SETTINGS_VERSION) };
  if (error) {
    // a corrupt file must not cost the user their API keys: keep a copy, salvage the secrets block, tell the user
    const backup = backupCorruptSettings();
    const names = Object.keys(secrets);
    const text = `File cài đặt bị hỏng (${String(error.message || error).slice(0, 200)}). `
      + (backup ? `Đã lưu bản sao: ${backup}. ` : 'Không sao lưu được file nên app sẽ không ghi đè lên nó. ')
      + 'Cài đặt được đặt lại mặc định; '
      + (names.length ? `đã khôi phục API key: ${names.join(', ')}.` : 'không khôi phục được API key nào.');
    startupNotices.push({ level: 'warn', title: 'File cài đặt bị hỏng', text });
    Object.assign(settingsLoadInfo, { backup, recovered: names });
    console.warn(`settings: ${text}`);
    if (!backup) settingsWriteBlocked = true;
    else saveSettings();
  } else if (raw && fileVersion < SETTINGS_VERSION) {
    saveSettings();
    startupNotices.push({ level: 'info', text: `settings: đã chuyển từ phiên bản ${fileVersion} lên ${SETTINGS_VERSION}` });
  }
}

function saveSettings() {
  if (settingsWriteBlocked) return;
  writeJsonAtomic(settingsFile(), { ...settings, secrets });
}

function flushStartupNotices() {
  for (const n of startupNotices.splice(0)) {
    send('log', { source: 'settings', text: n.text });
    if (n.level !== 'warn') continue;
    send('sidecar:notify', { method: 'app.notice', params: { level: n.level, title: n.title, message: n.text } });
    if (!process.env.LUSTRA_SELFTEST_DIR && mainWindow && !mainWindow.isDestroyed()) {
      dialog.showMessageBox(mainWindow, { type: 'warning', message: n.title, detail: n.text, buttons: ['OK'], noLink: true }).catch(() => {});
    }
  }
}

function send(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, payload);
}

function iconPath(id) {
  return path.join(ICON_DIR, `${id}.png`);
}

function resolvePython() {
  const candidates = [
    settings.advanced.pythonPath,
    process.env.LUSTRA_PYTHON,
    path.join(REPO_ROOT, '.venv', process.platform === 'win32' ? 'Scripts\\python.exe' : 'bin/python'),
  ].filter(Boolean);
  for (const c of candidates) if (fs.existsSync(c)) return c;
  return process.platform === 'win32' ? 'python' : 'python3';
}

function bundledCore() {
  // frozen core (PyInstaller one-dir, see tools/build_core.py): resources/core in the packaged app, LUSTRA_CORE to test one in development
  if (process.env.LUSTRA_CORE) return path.resolve(process.env.LUSTRA_CORE);
  if (!app.isPackaged) return '';
  const core = path.join(process.resourcesPath, 'core', CORE_NAME);
  return fs.existsSync(core) ? core : '';
}

function coreCommand(kind, args = []) {
  // kind: 'server' (JSON-RPC sidecar) | 'cli' (download job). Packaged app: the bundled core (the Python path setting is
  // only a fallback when it is missing: the app's sources are not on disk); development: python -m lustra[.server]
  const core = bundledCore();
  // 'aigen' (extension generator worker) and 'extgen' (draft tools: check / pack) are commands of the same core
  if (core) return { cmd: core, args: [kind, ...args], cwd: userData(), frozen: true };
  const mod = { server: 'lustra.server', cli: 'lustra', aigen: 'lustra.aigen.worker', extgen: 'lustra.extgen', index: 'lustra.dlindex_tools',
    tags: 'lustra.tags_updater' }[kind];
  if (!mod) throw new Error(`unknown core command ${kind}`);
  return { cmd: resolvePython(), args: ['-m', mod, ...args], cwd: REPO_ROOT, frozen: false };
}

function pythonEnv(extra = {}, frozen = false) {
  const sep = process.platform === 'win32' ? ';' : ':';
  const env = {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    LUSTRA_DATA_DIR: userData(),
    LUSTRA_CATALOG_AUTOLOAD: '0', // only builtin and installed extensions: catalog ones must be installed first
    PYTHONPATH: [REPO_ROOT, process.env.PYTHONPATH].filter(Boolean).join(sep),
    ...extra,
  };
  if (frozen) delete env.PYTHONPATH; // the frozen core carries its own modules
  return env;
}

function spawnCore(kind, args, extraEnv = {}) {
  fs.mkdirSync(userData(), { recursive: true });
  const c = coreCommand(kind, args);
  const proc = spawn(c.cmd, c.args, { cwd: c.cwd, env: pythonEnv(extraEnv, c.frozen), stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  proc.coreCmd = c.cmd;
  return proc;
}

// ---------- secrets (API keys) ----------

let encryptionAvailable = false;

async function initEncryption() {
  try { encryptionAvailable = await safeStorage.isAsyncEncryptionAvailable(); } catch { encryptionAvailable = false; }
  if (encryptionAvailable && process.platform === 'linux') {
    try { if (safeStorage.getSelectedStorageBackend() === 'basic_text') encryptionAvailable = false; } catch { /* older api */ }
  }
}

async function setSecret(module, value) {
  // an empty value never deletes a key (an untouched empty field saved by the UI): clearSecret does that
  if (!RE_ABBR.test(String(module)) || !value) return;
  if (encryptionAvailable) secrets[module] = { enc: (await safeStorage.encryptStringAsync(value)).toString('base64') };
  else secrets[module] = { plain: value }; // no OS keyring available: stored as plain text (reported to UI)
  saveSettings();
}

function clearSecret(module) {
  if (!RE_ABBR.test(String(module)) || !Object.hasOwn(secrets, module)) return;
  delete secrets[module];
  saveSettings();
}

async function getSecret(module) {
  if (!Object.hasOwn(secrets, module)) return '';
  const s = secrets[module];
  if (!s) return '';
  if (!s.enc) return s.plain || '';
  try {
    const { result, shouldReEncrypt } = await safeStorage.decryptStringAsync(Buffer.from(s.enc, 'base64'));
    if (shouldReEncrypt) await setSecret(module, result);
    return result;
  } catch {
    return '';
  }
}

// gender model server token: never sent to the renderer; given to the core as a secret argument (sidecar rpc params, job env)
function modelTokenStatus() {
  const s = secrets[MODEL_SECRET];
  return { set: Boolean(s), encrypted: Boolean(s?.enc), encryption: encryptionAvailable };
}

async function setModelToken(value) {
  value = typeof value === 'string' ? value.trim() : '';
  if (!value) return modelTokenStatus(); // an empty field never deletes the token: clearModelToken does that
  if (!/^[\x21-\x7e]{8,256}$/.test(value)) throw new Error('token: 8–256 ký tự ASCII, không có dấu cách');
  if (encryptionAvailable) secrets[MODEL_SECRET] = { enc: (await safeStorage.encryptStringAsync(value)).toString('base64') };
  else secrets[MODEL_SECRET] = { plain: value }; // no OS keyring: plain text in the 0600 settings file (reported to the UI)
  saveSettings();
  return modelTokenStatus();
}

function clearModelToken() {
  if (Object.hasOwn(secrets, MODEL_SECRET)) {
    delete secrets[MODEL_SECRET];
    saveSettings();
  }
  return modelTokenStatus();
}

async function genderSecretArgs(g) {
  if (g?.mode !== 'model' || !genderModelOn()) return [];
  const token = await getSecret(MODEL_SECRET);
  return token ? ['-gender_model_token', token] : [];
}

// ---------- extension generator (lustra.aigen worker) ----------
// A session turns a website link into a draft folder (manifest.json, downloader.py, recorded test fixtures, report.json) under
// <userData>/extgen/drafts/<session>/. Nothing is installed by a session: "install" is a separate user action that re-checks
// the draft, packs it and asks for a native confirmation. The Claude API key never reaches the renderer: main decrypts it and
// writes it to the worker's stdin only.

function aiKeyStatus() {
  return { set: Boolean(secrets[AI_SECRET]?.enc), encryption: encryptionAvailable };
}

async function setAiKey(value) {
  value = typeof value === 'string' ? value.trim() : '';
  if (!value) return aiKeyStatus(); // an empty field never deletes the key: clearAiKey does that
  if (!/^[\x21-\x7e]{20,400}$/.test(value)) throw new Error('khoá Claude API không hợp lệ');
  if (!encryptionAvailable) {
    throw new Error('Hệ điều hành không có kho khoá mã hoá (Linux: gnome-keyring / KWallet): Lustra không lưu khoá Claude API dạng chữ thường.');
  }
  secrets[AI_SECRET] = { enc: (await safeStorage.encryptStringAsync(value)).toString('base64') };
  saveSettings();
  return aiKeyStatus();
}

function clearAiKey() {
  if (Object.hasOwn(secrets, AI_SECRET)) {
    delete secrets[AI_SECRET];
    saveSettings();
  }
  return aiKeyStatus();
}

const draftsRoot = () => path.join(userData(), 'extgen', 'drafts');

function genSessionDir(session) {
  if (!RE_GEN_SESSION.test(String(session))) throw new Error('invalid draft session');
  return path.join(draftsRoot(), String(session));
}

async function readGenReport(session) {
  const dir = genSessionDir(session);
  let rep;
  try { rep = JSON.parse(await fsp.readFile(path.join(dir, 'report.json'), 'utf8')); } catch { rep = { status: 'unknown' }; }
  if (!isPlainObject(rep)) rep = { status: 'unknown' };
  // the draft folder comes from the session and the extension id, never from a path written in the file
  const extId = RE_EXT_ID.test(String(rep.extId || '')) ? rep.extId : '';
  const draft = extId ? path.join(dir, extId) : '';
  return { ...rep, session, extId, draftDir: draft && fs.existsSync(path.join(draft, 'manifest.json')) ? draft : '' };
}

async function listGenDrafts() {
  let names = [];
  try { names = (await fsp.readdir(draftsRoot())).filter((n) => RE_GEN_SESSION.test(n)).sort().reverse().slice(0, 100); } catch { /* none yet */ }
  const out = [];
  for (const n of names) {
    const { log, ...rep } = await readGenReport(n);
    out.push(rep);
  }
  return out;
}

function runExtgen(args, timeoutMs = 180000) {
  // python -m lustra.extgen <args> (JSON on stdout): check / pack of a draft, no network
  return new Promise((resolve, reject) => {
    const proc = spawnCore('extgen', args);
    let out = '';
    let err = '';
    const timer = setTimeout(() => { try { proc.kill(); } catch { /* gone */ } }, timeoutMs);
    proc.stdout.on('data', (d) => { out += d; });
    proc.stderr.on('data', (d) => { err = (err + d).slice(-4000); });
    proc.on('error', (e) => { clearTimeout(timer); reject(e); });
    proc.on('close', (code) => {
      clearTimeout(timer);
      let data = null;
      try { data = JSON.parse(out); } catch { /* not json */ }
      if (!data) return reject(new Error(`extgen ${args[0]}: exit ${code} ${redact(err).split('\n').slice(-3).join(' ')}`));
      resolve({ code, data });
    });
    proc.stdin.end();
  });
}

class Generator {
  constructor() {
    this.proc = null;
    this.session = '';
    this.events = [];
    this.done = null;
    this.stderr = [];
    this.killTimer = null;
    this.confirm = confirmMain; // native confirmation (replaced by the self-test)
  }

  status() {
    return { running: Boolean(this.proc), session: this.session, events: this.events.slice(-300), key: aiKeyStatus(), models: GEN_MODELS,
      defaults: { model: settings.ai.model, maxUsd: settings.ai.maxUsd } };
  }

  async start(opts) {
    if (this.proc) throw new Error('một phiên tạo extension đang chạy');
    if (!isPlainObject(opts)) throw new Error('invalid options');
    let url = String(opts.url || '').trim();
    let repair = '';
    if (opts.repair !== undefined) { // fix a draft that did not pass: same site, ai mode, the old session stays untouched
      const old = await readGenReport(String(opts.repair));
      if (!old.draftDir || old.status === 'ready') throw new Error('chỉ sửa được bản nháp chưa đạt còn thư mục bản nháp');
      repair = old.session;
      url = String(old.url || old.site || '');
    }
    if (!url || url.length > 2000 || /\s/.test(url)) throw new Error('link website không hợp lệ');
    const mode = repair ? 'ai' : opts.mode === 'template' ? 'template' : 'ai';
    const media = ['image', 'video', 'audio'].filter((m) => Array.isArray(opts.media) && opts.media.includes(m));
    const cfg = {
      cmd: 'start', url, mode, media: media.length ? media : ['image', 'video'],
      query: typeof opts.query === 'string' ? opts.query.trim().slice(0, 200) : '',
      model: GEN_MODELS.includes(opts.model) ? opts.model : settings.ai.model,
      maxUsd: clampUsd(opts.maxUsd, settings.ai.maxUsd),
      ...(repair ? { repair } : {}),
    };
    let apiKey = '';
    if (mode === 'ai') {
      apiKey = await getSecret(AI_SECRET);
      if (!apiKey) throw new Error('Chưa có khoá Claude API: thêm trong Cài đặt → AI tạo extension, hoặc dùng chế độ không AI.');
    }
    this.events = [];
    this.done = null;
    this.stderr = [];
    this.session = '';
    const proc = spawnCore('aigen', []);
    this.proc = proc;
    readline.createInterface({ input: proc.stdout }).on('line', (line) => this._line(line));
    readline.createInterface({ input: proc.stderr }).on('line', (line) => {
      this.stderr.push(redact(line).slice(0, 500));
      if (this.stderr.length > 40) this.stderr.shift();
    });
    proc.on('error', (e) => this._exit(null, e));
    proc.on('exit', (code) => this._exit(code));
    proc.stdin.on('error', () => { /* worker gone */ });
    proc.stdin.write(`${JSON.stringify({ ...cfg, apiKey })}\n`); // the key goes through stdin only: not argv, env or files
    apiKey = '';
    return { started: true, mode, model: mode === 'ai' ? cfg.model : '' };
  }

  cancel() {
    if (!this.proc) return false;
    try { this.proc.stdin.write('{"cmd":"cancel"}\n'); } catch { /* gone */ }
    clearTimeout(this.killTimer);
    this.killTimer = setTimeout(() => this.kill(), 15000); // a request to the model can take a while to stop
    return true;
  }

  kill() {
    try { this.proc?.kill(); } catch { /* gone */ }
  }

  _line(line) {
    if (!line.startsWith('@@GEN ')) return;
    let ev;
    try { ev = JSON.parse(line.slice(6)); } catch { return; }
    if (!isPlainObject(ev) || typeof ev.type !== 'string') return;
    if (ev.type === 'start' && RE_GEN_SESSION.test(String(ev.session))) this.session = ev.session;
    if (ev.type === 'done') {
      this.done = ev;
      if (isPlainObject(ev.report)) delete ev.report.log; // the full log stays in report.json
    }
    this._event(ev);
  }

  _event(ev) {
    this.events.push(ev);
    if (this.events.length > 500) this.events.shift();
    send('gen:event', ev);
  }

  _exit(code, error) {
    if (!this.proc) return;
    this.proc = null;
    clearTimeout(this.killTimer);
    if (!this.done) {
      const why = error ? error.message : `mã thoát ${code}`;
      this._event({ type: 'done', status: 'error', session: this.session, summary: `Tiến trình tạo extension dừng bất thường (${why}). ${this.stderr.slice(-3).join(' ')}`.trim() });
    }
    send('gen:event', { type: 'exit', session: this.session });
  }

  async exportZip(session) {
    const rep = await readGenReport(session);
    if (!rep.draftDir) throw new Error('bản nháp không có manifest.json');
    const r = await dialog.showSaveDialog(mainWindow, {
      title: 'Xuất extension ra file .zip',
      defaultPath: path.join(app.getPath('downloads'), `${rep.extId}-${rep.version || '1.0.0'}.zip`),
      filters: [{ name: 'Lustra extension', extensions: ['zip'] }],
    });
    if (r.canceled || !r.filePath) return null;
    const { code, data } = await runExtgen(['pack', rep.draftDir, '--out', r.filePath]);
    if (code !== 0 || !data.sha256) throw new Error(data.error || 'không đóng gói được bản nháp');
    return { path: data.path, sha256: data.sha256 };
  }

  async install(session) {
    // explicit user action only: offline re-check (the files may have been edited since), pack, native confirmation, install
    const rep = await readGenReport(session);
    if (this.proc && this.session === session) throw new Error('phiên này vẫn đang chạy');
    if (rep.status !== 'ready' || !rep.draftDir) throw new Error('chỉ cài được bản nháp đã chạy thử đạt (hoặc xuất zip rồi tự cài)');
    const check = await runExtgen(['check', rep.draftDir]);
    if (check.code !== 0) {
      const failedChecks = (check.data.checks || []).filter((c) => !c.ok && c.severity === 'error');
      const denied = failedChecks.find((c) => String(c.id).startsWith('domain:')); // domain denylist: its own clear message
      if (denied) throw new Error(String(denied.detail || 'site nằm trong danh sách chặn của Lustra'));
      const failed = failedChecks.map((c) => c.id);
      throw new Error(`bản nháp không còn qua kiểm tra: ${failed.join(', ') || check.data.error || 'lỗi'}`);
    }
    const zip = path.join(genSessionDir(session), `${rep.extId}.zip`);
    const { code, data } = await runExtgen(['pack', rep.draftDir, '--out', zip]);
    if (code !== 0 || !RE_SHA256.test(String(data.sha256 || ''))) throw new Error(data.error || 'không đóng gói được bản nháp');
    const origin = rep.mode === 'ai' ? `AI (${rep.model || 'Claude'})` : 'template có sẵn của Lustra';
    const ok = await this.confirm(`Cài extension "${rep.name || rep.extId}" (${rep.abbr || '?'}) vào Lustra?`,
      `Site: ${rep.site || rep.url}\nTạo bởi: ${origin}. Chạy thử và kiểm tra lại: đạt.\nsha256: ${data.sha256}\n\n`
      + 'Extension là code Python chạy trên máy bạn với quyền của bạn. Hãy xem downloader.py trước khi cài (nút "Mở thư mục").',
      ['Huỷ', 'Cài']);
    if (!ok) throw new Error('cancelled');
    return sidecar.call('ext.install', { archivePath: zip, sha256: data.sha256 });
  }

  async remove(session) {
    const dir = genSessionDir(session);
    if (this.proc && this.session === session) throw new Error('phiên này vẫn đang chạy');
    const rep = await readGenReport(session);
    const ok = await this.confirm(`Xoá bản nháp "${rep.name || rep.extId || session}"?`,
      'Thư mục bản nháp (manifest.json, downloader.py, dữ liệu chạy thử) sẽ bị xoá. Extension đã cài (nếu có) không bị ảnh hưởng.', ['Huỷ', 'Xoá']);
    if (!ok) return false;
    await fsp.rm(dir, { recursive: true, force: true });
    return true;
  }
}

const generator = new Generator();

// ---------- sidecar (JSON-RPC) ----------

class Sidecar {
  constructor() {
    this.proc = null;
    this.pending = new Map();
    this.nextId = 1;
    this.ready = null;
    this.info = null;
    this.restarts = 0;
    this.stopping = false;
  }

  start() {
    const py = coreCommand('server').cmd;
    this.info = null;
    this.status = { ok: false, error: 'starting' };
    this.ready = new Promise((resolve, reject) => {
      this._resolveReady = resolve;
      this._rejectReady = reject;
    });
    this.ready.catch(() => {}); // callers get the rejection through call(); avoid unhandled rejection warnings
    let proc;
    try {
      proc = spawnCore('server', []);
    } catch (err) {
      this._setStatus({ ok: false, error: `Không chạy được Python (${py}): ${err.message}` });
      this._rejectReady(err);
      return;
    }
    this.proc = proc;
    proc.stdin.on('error', () => {}); // EPIPE after the process died must not crash the main process
    let ended = false;
    const onEnd = (reason) => {
      if (ended) return; // 'error' and 'exit' can both fire
      ended = true;
      if (this.proc === proc) this.proc = null;
      for (const { reject, timer } of this.pending.values()) { clearTimeout(timer); reject(new Error(`sidecar: ${reason}`)); }
      this.pending.clear();
      this._rejectReady(new Error(`sidecar: ${reason}`)); // no-op when already ready
      this._setStatus({ ok: false, error: reason });
      if (!this.stopping && this.restarts < 5) {
        this.restarts += 1;
        setTimeout(() => this.start(), 1000 * this.restarts);
      }
    };
    proc.on('error', (err) => onEnd(`Không chạy được Python (${py}): ${err.message}`));
    readline.createInterface({ input: proc.stdout }).on('line', (line) => this._onLine(line));
    readline.createInterface({ input: proc.stderr }).on('line', (line) => send('log', { source: 'sidecar', text: redact(line) }));
    proc.on('exit', (code, signal) => onEnd(`sidecar exited (${code ?? signal})`));
  }

  _setStatus(st) {
    this.status = st;
    send('sidecar:status', st);
  }

  _onLine(line) {
    let msg;
    try { msg = JSON.parse(line); } catch { send('log', { source: 'sidecar', text: line }); return; }
    if (msg.id !== undefined && msg.id !== null && this.pending.has(msg.id)) {
      const { resolve, reject, timer } = this.pending.get(msg.id);
      clearTimeout(timer);
      this.pending.delete(msg.id);
      if (msg.error) {
        const err = new Error(msg.error.message);
        err.code = msg.error.code;
        err.data = msg.error.data;
        reject(err);
      } else resolve(msg.result);
      return;
    }
    if (msg.method === 'ready') {
      this.info = msg.params;
      this.restarts = 0;
      this._setStatus({ ok: true, info: msg.params });
      this._resolveReady(msg.params);
    } else if (msg.method === 'log') {
      send('log', { source: msg.params.scope || 'core', text: msg.params.text });
    } else if (msg.method) {
      if (msg.method === 'tags.updateDone') onTagsUpdateDone(msg.params || {});
      send('sidecar:notify', { method: msg.method, params: msg.params });
    }
  }

  async call(method, params = {}, timeoutMs = 120000) {
    const deadline = Date.now() + timeoutMs;
    // the timeout also covers waiting for the sidecar to start (the original code could wait forever)
    let waitTimer;
    await Promise.race([
      this.ready,
      new Promise((_, reject) => { waitTimer = setTimeout(() => reject(new Error(`timeout: ${method} (sidecar not ready)`)), timeoutMs); }),
    ]).finally(() => clearTimeout(waitTimer));
    if (!this.proc) throw new Error('sidecar is not running');
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`timeout: ${method}`));
      }, Math.max(1000, deadline - Date.now()));
      this.pending.set(id, { resolve, reject, timer });
      this.proc.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    });
  }

  stop() {
    this.stopping = true;
    try { this.proc?.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method: 'app.shutdown' })}\n`); } catch { /* ignore */ }
    setTimeout(() => { try { this.proc?.kill(); } catch { /* ignore */ } }, 1500);
  }
}

const sidecar = new Sidecar();

// ---------- CLI arguments ----------

function toCliDate(iso) {
  // '2024-01-31' -> '31-01-2024'
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
  return m ? `${m[3]}-${m[2]}-${m[1]}` : '';
}

async function connectionArgs(module) {
  const c = settings.connection;
  const a = [];
  if (c.proxy) a.push('-proxy', c.proxy);
  if (c.ignoreProxy) a.push('-noproxy');
  if (c.proxyNoDownload) a.push('-proxynodown');
  if (c.timeout > 0) a.push('-timeout', String(c.timeout));
  if (c.retries > 0) a.push('-retries', String(c.retries));
  // one '-header name=value' per entry, not '-headers <json>': the core's JSON option strips every backslash (values with
  // '"' or '\' broke); name=value is split at the first '=' and token names never contain one
  for (const [opt, kind, map] of [['-header', 'Header', c.headers], ['-cookie', 'Cookie', c.cookies]]) {
    for (const [name, value] of Object.entries(normalizeHttpMap(map, kind, false))) a.push(opt, `${name}=${value}`);
  }
  const key = await getSecret(module);
  if (key) a.push('-api_key', key);
  if (settings.advanced.verbose) a.push('-verbose');
  return a;
}

function genderArgs(g) {
  // content filter (character gender); the safety blacklist is a separate option, see safetyArgs
  const labels = ['only_female', 'only_male', 'both', 'none', 'unknown'];
  const sel = [...new Set((g && Array.isArray(g.select) ? g.select : []).filter((x) => labels.includes(x)))];
  if (!sel.length) return [];
  const exclude = g.op === 'exclude';
  if (exclude && sel.length === labels.length) return []; // dropping every label would leave nothing: not a filter
  const a = exclude ? ['-gender_exclude', sel.join(',')] : ['-gender', sel.join(',')];
  if (['low', 'medium', 'high'].includes(g.conf)) a.push('-gender_conf', g.conf);
  if (g.mode === 'model' && genderModelOn()) {
    a.push('-gender_mode', 'model', '-gender_model_url', settings.genderModel.url);
    if (g.verifyMedia === true) a.push('-gender_verify_media'); // checks downloaded files: only with the model
  }
  if (g.strict) a.push('-gender_strict');
  return a;
}

function safetyArgs(q) {
  // '-safety_blacklist': only when the query asks for it (the check box of the filters panel); never implied by the gender filter
  return q && q.safetyBlacklist === true ? ['-safety_blacklist'] : [];
}

function searchArgs(q) {
  const a = [];
  if (q.dateMin) a.push('-mindate', toCliDate(q.dateMin));
  if (q.dateMax) a.push('-maxdate', toCliDate(q.dateMax));
  return a;
}

function tagLimitArgs(module) {
  // account tag limit (danbooru: 2 free, 6 Gold, 12 Platinum); 0 / missing = module default
  const n = settings.search.tagLimits?.[module];
  return Number.isInteger(n) && n > 0 ? ['-tags_limit', String(n)] : [];
}

function asQuery(q) {
  if (!isPlainObject(q) || typeof q.module !== 'string' || !RE_ABBR.test(q.module)) throw new Error('invalid query');
  return q;
}

function sanitizeFolderName(s) {
  return String(s).replace(/[<>:"/\\|?*\x00-\x1f]+/g, '_').replace(/\s+/g, ' ').trim().slice(0, 60) || 'query';
}

function resolveUserPath(p) {
  // '~/x' and relative paths are resolved against the home folder (lustra.server resolve_user_path does the same)
  const s = String(p || '').trim();
  if (s === '~') return os.homedir();
  if (s.startsWith('~/') || s.startsWith('~\\')) return path.join(os.homedir(), s.slice(2));
  return path.isAbsolute(s) ? path.normalize(s) : path.join(os.homedir(), s);
}

function downloadArgs(q) {
  const d = { ...settings.download, ...(q.options || {}) };
  let dest = resolveUserPath(q.path || d.path);
  // the renderer always sends the path field: the original '!q.path' made this option a no-op
  if (d.subfolderPerQuery) dest = path.join(dest, `${q.module}_${sanitizeFolderName(q.tags)}`);
  const a = ['-path', dest];
  if (!d.images) a.push('-skip_img');
  if (!d.videos) a.push('-skip_vid');
  if (d.audio === false) a.push('-skip_aud'); // only modules with audio media produce audio files
  if (d.videoFormat === 'mp4') a.push('-mp4');
  if (d.videoFormat === 'webm') a.push('-webm');
  if (d.lowRes) a.push('-lowres');
  if (d.threads) a.push('-threads', String(Math.max(1, Math.min(8, d.threads))));
  if (d.prefix) a.push('-prefix');
  if (d.appendInfo) a.push('-append_info');
  if (d.reverse) a.push('-reverse');
  if (d.limit > 0) a.push('-dlimit', String(d.limit));
  if (d.parchi) a.push('-include_parchi');
  if (d.preserveDate) a.push('-preserve_date');
  if (d.skipKnown === true) a.push('-skip_known');
  if (d.dumpTags) a.push('-dump_tags');
  if (d.dumpSources) a.push('-dump_sources');
  if (d.dumpComments) a.push('-dump_comments');
  if (d.dumpHashes) a.push('-dump_hashes');
  if (d.infoMode === 'item') a.push('-dump_per_item');
  if (d.infoMode === 'merge') a.push('-merge_lists');
  return { args: a, dest };
}

// ---------- download jobs (separate CLI processes) ----------

function notifyJob(job) {
  // a job ended while Lustra is not the focused window: system notification (Settings: download.notify)
  if (!settings.download.notify || job.label === 'selftest' || !['done', 'failed'].includes(job.status)) return;
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isFocused()) return;
  try {
    if (!Notification.isSupported()) return;
    const p = job.progress || {};
    const body = job.status === 'done'
      ? `${p.success || 0} file ${job.kind === 'import' ? 'đã nhập vào index' : 'đã tải'}${p.failed ? `, ${p.failed} lỗi` : ''}`
      : `Lỗi: ${String(job.error || '').slice(0, 120)}`;
    const n = new Notification({ title: `${job.status === 'done' ? 'Xong' : 'Lỗi'}: ${job.module} · ${job.label || job.tags}`.slice(0, 120), body });
    n.on('click', () => { if (mainWindow && !mainWindow.isDestroyed()) { mainWindow.show(); mainWindow.focus(); send('nav:page', 'downloads'); } });
    n.show();
  } catch { /* notifications are optional */ }
}

class Jobs {
  constructor() {
    this.jobs = new Map();
    this.seq = 0;
  }

  public(job) {
    const { proc, logTail, args, secretArgs, moduleSecretArgs, testCommand, query, importQuery, coreKind, ...rest } = job;
    return { ...rest, logTail: logTail.slice(-40) };
  }

  emit(job) {
    send('jobs:update', this.public(job));
  }

  async _build(q) {
    if (this.closing) throw new Error('app is closing');
    if (!q || !q.module || !String(q.tags || '').trim()) throw new Error('module and tags are required');
    asQuery(q);
    // kept for 'run again' (no secrets: they are added per run); a job of a multi-site group runs again on its own
    const query = structuredClone({ ...q, allowDuplicate: undefined, group: undefined });
    const id = `job-${Date.now()}-${++this.seq}`;
    const { args, dest } = downloadArgs(q);
    const job = {
      id, module: q.module, tags: String(q.tags).trim(), dest, status: 'queued', progress: {}, createdAt: Date.now(),
      startedAt: 0, endedAt: 0, exitCode: null, logTail: [], label: q.label || '',
      secretArgs: [...(await connectionArgs(q.module)), ...(await genderSecretArgs(q.gender))],
      // '--': tags like '-webm' or '-v' are never parsed as options
      args: ['-module', q.module, ...searchArgs(q), ...tagLimitArgs(q.module), ...genderArgs(q.gender), ...safetyArgs(q), ...args, '--',
        ...String(q.tags).trim().split(/\s+/)],
      query,
      autoKey: typeof q.autoKey === 'string' ? q.autoKey : '',
      group: isPlainObject(q.group) ? q.group : null, // multi-site download (startMulti only)
    };
    return job;
  }

  _duplicate(job) {
    // the same download twice (double click, a query run again by mistake): the renderer asks, then retries with allowDuplicate
    const same = this.active().find((j) => j.args.join('\0') === job.args.join('\0'));
    return same ? new Error(`DUPLICATE: một job giống hệt đang ${same.status === 'queued' ? 'chờ' : 'chạy'}`) : null;
  }

  async start(q) {
    const job = await this._build(q);
    const dup = q.allowDuplicate === true ? null : this._duplicate(job);
    if (dup) throw dup;
    return this._add(job);
  }

  // one query on several sites: one job per module, each waits for the previous site's job to end (see _waiting). Forced options:
  // '-skip_known' (a post another site of the group already brought, same md5 in the index, is skipped) and module prefixed file
  // names (post 123 of two sites never share a file name). Tags are sent as typed to every site: their syntax may differ
  async startMulti(q, modules) {
    asQuery(q);
    const list = [...new Set([q.module, ...(Array.isArray(modules) ? modules : [])])];
    if (list.length < 2 || list.length > MULTI_SITE_MAX || !list.every((m) => typeof m === 'string' && RE_ABBR.test(m))) {
      throw new Error(`cần 2–${MULTI_SITE_MAX} module khác nhau`);
    }
    const group = { id: `group-${Date.now()}-${++this.seq}`, size: list.length, modules: list };
    const options = { ...(isPlainObject(q.options) ? q.options : {}), skipKnown: true, prefix: true };
    const built = [];
    for (const [index, module] of list.entries()) built.push(await this._build({ ...q, module, options, group: { ...group, index } }));
    const dup = q.allowDuplicate === true ? null : built.map((j) => this._duplicate(j)).find(Boolean);
    if (dup) throw dup;
    return built.map((job) => this._add(job));
  }

  // library import into the download index (lustra/dlindex_tools.py 'import'): a core job like a download (progress lines,
  // 'cancel'). The folder must come from the native dialog (index:pickFolder): the renderer cannot name an arbitrary path
  async startImport(opts) {
    if (this.closing) throw new Error('app is closing');
    if (!isPlainObject(opts)) throw new Error('invalid import options');
    const folder = String(opts.folder || '');
    if (!approvedImportFolders.has(folder)) throw new Error('choose the folder with the folder dialog first');
    if (!fs.existsSync(folder) || !fs.statSync(folder).isDirectory()) throw new Error('thư mục không còn');
    const module = String(opts.module || '');
    if (module && !RE_ABBR.test(module)) throw new Error('invalid module');
    const importQuery = { folder, module, recursive: opts.recursive !== false, verifyMd5: opts.verifyMd5 === true, dryRun: opts.dryRun === true };
    const moduleSecretArgs = {}; // the files may belong to any module: each one gets its own key (the core reads them, then drops them)
    for (const abbr of Object.keys(secrets).filter((k) => RE_ABBR.test(k))) {
      const key = await getSecret(abbr);
      if (key) moduleSecretArgs[abbr] = ['-api_key', key];
    }
    const job = {
      id: `job-${Date.now()}-${++this.seq}`, kind: 'import', coreKind: 'index', module: module || '*', tags: folder, dest: folder,
      status: 'queued', progress: {}, createdAt: Date.now(), startedAt: 0, endedAt: 0, exitCode: null, logTail: [],
      label: importQuery.dryRun ? 'Kiểm tra thư mục (không gửi request)' : 'Nhập thư viện vào index',
      secretArgs: await connectionArgs(''), moduleSecretArgs, importQuery, autoKey: '', group: null,
      args: ['import', folder, ...(module ? ['--module', module] : []), ...(importQuery.recursive ? [] : ['--no-recursive']),
        ...(importQuery.verifyMd5 ? ['--verify-md5'] : []), ...(importQuery.dryRun ? ['--dry-run'] : [])],
    };
    const dup = opts.allowDuplicate === true ? null : this._duplicate(job);
    if (dup) throw dup;
    return this._add(job);
  }

  rerun(id, allowDuplicate = false) {
    const job = this.jobs.get(id);
    if (job?.importQuery) return this.startImport({ ...job.importQuery, allowDuplicate });
    if (!job?.query) throw new Error('job không còn thông tin để chạy lại');
    return this.start({ ...job.query, allowDuplicate });
  }

  _waiting(job) {
    // multi-site group: a site's job starts once every earlier site's job ended, so their files are in the index (-skip_known)
    if (!job.group) return false;
    for (const j of this.jobs.values()) {
      if (j.group?.id === job.group.id && j.group.index < job.group.index && ['queued', 'running', 'cancelling'].includes(j.status)) return true;
    }
    return false;
  }

  _add(job) {
    this.jobs.set(job.id, job);
    this.emit(job);
    this.pump();
    return this.public(job);
  }

  running() {
    return [...this.jobs.values()].filter((j) => j.status === 'running').length;
  }

  active() {
    return [...this.jobs.values()].filter((j) => ['queued', 'running', 'cancelling'].includes(j.status));
  }

  pump() {
    if (this.closing) return;
    const max = Math.max(1, settings.download.maxConcurrentJobs || 1);
    for (const job of this.jobs.values()) {
      if (this.running() >= max) break;
      if (job.status === 'queued' && !this._waiting(job)) this._spawn(job);
    }
  }

  _fail(job, error) {
    job.status = 'failed';
    job.error = error;
    job.endedAt = Date.now();
    delete job.proc;
    this.emit(job);
  }

  _spawn(job) {
    job.status = 'running';
    job.startedAt = Date.now();
    let proc;
    try {
      fs.mkdirSync(job.dest, { recursive: true });
      const env = { LUSTRA_IPC: '1', LUSTRA_SECRET_ARGS: JSON.stringify(job.secretArgs || []) };
      if (job.moduleSecretArgs) env.LUSTRA_SECRET_MODULE_ARGS = JSON.stringify(job.moduleSecretArgs); // library import
      if (job.testCommand) { // self-test only: a stand-in process (never set from the renderer)
        proc = spawn(job.testCommand.cmd, job.testCommand.args, { env: { ...process.env, ...job.testCommand.env }, stdio: ['pipe', 'pipe', 'pipe'] });
      } else proc = spawnCore(job.coreKind || 'cli', job.args, env);
    } catch (err) {
      // a failed job must not block the queue (the original code threw here and the queue stalled forever)
      this._fail(job, err.message);
      setImmediate(() => this.pump());
      return;
    }
    job.proc = proc;
    proc.stdin.on('error', () => {});
    const onLine = (line) => {
      if (line.startsWith(PROGRESS_PREFIX)) {
        try { job.progress = { ...job.progress, ...JSON.parse(line.slice(PROGRESS_PREFIX.length)) }; } catch { /* ignore */ }
        this.emit(job);
        return;
      }
      const text = redact(line);
      job.logTail.push(text);
      if (job.logTail.length > 400) job.logTail.splice(0, job.logTail.length - 400);
      send('jobs:log', { id: job.id, text });
    };
    readline.createInterface({ input: proc.stdout }).on('line', onLine);
    readline.createInterface({ input: proc.stderr }).on('line', onLine);
    let finished = false;
    const finish = (code, spawnError) => {
      if (finished) return; // 'error' and 'close' can both fire
      finished = true;
      job.exitCode = code;
      job.endedAt = Date.now();
      // exit codes of lustra_cmd.run_cmd: 0 ok, 3 some items failed, 130 cancelled, other = error (2 = invalid arguments)
      if (job.status === 'cancelling' || code === 130 || job.progress.cancelled) job.status = 'cancelled';
      else if (!spawnError && (code === 0 || code === 3)) job.status = 'done';
      else {
        job.status = 'failed';
        job.error = spawnError || job.progress.error || (job.progress.cfChallenge ? 'cloudflare challenge' : `exit code ${code}`);
      }
      delete job.proc;
      this.emit(job);
      this.pump();
      notifyJob(job);
      if (job.autoKey) autoJobFinished(job);
    };
    proc.on('error', (err) => { onLine(`ERROR: ${err.message}`); finish(null, err.message); });
    // 'close' (not 'exit'): stdout is fully read, the final DONE line is already parsed
    proc.on('close', (code, signal) => finish(code ?? (signal ? 130 : null)));
    this.emit(job);
  }

  cancel(id) {
    const job = this.jobs.get(id);
    if (!job) return false;
    if (job.status === 'queued') {
      job.status = 'cancelled';
      this.emit(job);
      this.pump();
      return true;
    }
    if (job.status === 'running' && job.proc) {
      job.status = 'cancelling';
      this.emit(job);
      try { job.proc.stdin.write('cancel\n'); } catch { /* ignore */ }
      const p = job.proc;
      setTimeout(() => { if (p.exitCode === null) p.kill(); }, 15000);
      return true;
    }
    return false;
  }

  clearFinished() {
    for (const [id, j] of this.jobs) if (['done', 'failed', 'cancelled'].includes(j.status)) this.jobs.delete(id);
    return this.list();
  }

  list() {
    return [...this.jobs.values()].map((j) => this.public(j));
  }

  async shutdown(graceMs = QUIT_GRACE_MS) {
    // quit / relaunch confirmed: queued jobs are dropped, running ones get 'cancel' (the core stops cleanly), processes
    // still alive after the grace period are killed (SIGTERM, then SIGKILL)
    this.closing = true;
    for (const j of this.jobs.values()) {
      if (j.status !== 'queued') continue;
      j.status = 'cancelled';
      j.endedAt = Date.now();
      this.emit(j);
    }
    const live = [...this.jobs.values()].filter((j) => j.proc && isAlive(j.proc));
    for (const j of live) {
      if (j.status === 'running') { j.status = 'cancelling'; this.emit(j); }
      try { j.proc.stdin.write('cancel\n'); } catch { /* ignore */ }
    }
    const procs = live.map((j) => j.proc);
    await Promise.all(procs.map((p) => waitExit(p, graceMs)));
    const stubborn = procs.filter(isAlive);
    for (const p of stubborn) { try { p.kill(); } catch { /* ignore */ } }
    await Promise.all(stubborn.map((p) => waitExit(p, 2000)));
    for (const p of stubborn.filter(isAlive)) { try { p.kill('SIGKILL'); } catch { /* ignore */ } }
    return { stopped: procs.length, killed: stubborn.length };
  }

  killAll() {
    // last resort at exit: shutdown() normally ran already
    for (const j of this.jobs.values()) {
      if (!j.proc || !isAlive(j.proc)) continue;
      try { j.proc.stdin.write('cancel\n'); } catch { /* ignore */ }
      try { j.proc.kill(); } catch { /* ignore */ }
    }
  }
}

function isAlive(p) {
  return p.exitCode === null && p.signalCode === null;
}

function waitExit(p, ms) {
  if (!isAlive(p)) return Promise.resolve(true);
  return new Promise((resolve) => {
    const onExit = () => { clearTimeout(timer); resolve(true); };
    const timer = setTimeout(() => { p.off('exit', onExit); resolve(false); }, ms);
    p.once('exit', onExit);
  });
}

const jobs = new Jobs();

// ---------- thumbnails ----------

const thumbDir = () => path.join(userData(), 'cache', 'thumbs');

async function pruneThumbCache() {
  try {
    const dir = thumbDir();
    const files = await fsp.readdir(dir);
    const stats = await Promise.all(files.map(async (f) => ({ f, s: await fsp.stat(path.join(dir, f)) })));
    let total = stats.reduce((acc, x) => acc + x.s.size, 0);
    stats.sort((a, b) => a.s.atimeMs - b.s.atimeMs);
    for (const { f, s } of stats) {
      if (total <= THUMB_CACHE_MAX) break;
      await fsp.unlink(path.join(dir, f)).catch(() => {});
      total -= s.size;
    }
  } catch { /* no cache yet */ }
}

const thumbInflight = new Map();

async function fetchThumb(target, referer) {
  await assertPublicHost(target);
  const headers = { 'User-Agent': `Lustra/${app.getVersion()}` };
  if (referer && /^https?:\/\//i.test(referer)) headers.Referer = referer;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), THUMB_TIMEOUT_MS);
  try {
    // redirects are refused: their target could not be checked against private addresses
    const res = await net.fetch(target, { headers, redirect: 'error', signal: ctrl.signal });
    if (!res.ok) throw new Error(`upstream ${res.status}`);
    const type = (res.headers.get('content-type') || '').toLowerCase();
    if (!type.startsWith('image/') && !type.startsWith('video/')) throw new Error('not an image');
    if (Number(res.headers.get('content-length') || 0) > THUMB_SIZE_MAX) throw new Error('too large');
    const reader = res.body.getReader();
    const chunks = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.length;
      if (total > THUMB_SIZE_MAX) { ctrl.abort(); throw new Error('too large'); }
      chunks.push(value);
    }
    return { buf: Buffer.concat(chunks), type };
  } finally {
    clearTimeout(timer);
  }
}

async function streamMedia(target, referer, range) {
  // in-app player (Preview lightbox): one upstream request per range, streamed through, not cached; video / audio only
  await assertPublicHost(target);
  const headers = { 'User-Agent': `Lustra/${app.getVersion()}` };
  if (referer && /^https?:\/\//i.test(referer)) headers.Referer = referer;
  if (range && /^bytes=\d*-\d*$/.test(range)) headers.Range = range;
  const res = await net.fetch(target, { headers, redirect: 'error' });
  const type = (res.headers.get('content-type') || '').toLowerCase();
  if (!res.ok || !(type.startsWith('video/') || type.startsWith('audio/') || type === 'application/ogg')) {
    try { await res.body?.cancel(); } catch { /* ignore */ }
    return new Response(res.ok ? 'not a media file' : `upstream ${res.status}`, { status: res.ok ? 415 : 502 });
  }
  const out = { 'content-type': type, 'accept-ranges': res.headers.get('accept-ranges') || 'none' };
  for (const h of ['content-length', 'content-range']) if (res.headers.get(h)) out[h] = res.headers.get(h);
  return new Response(res.body, { status: res.status, headers: out });
}

function registerThumbProtocol() {
  protocol.handle('lustra-thumb', async (request) => {
    const u = new URL(request.url);
    const target = u.searchParams.get('u') || '';
    const referer = u.searchParams.get('r') || '';
    if (!/^https?:\/\//i.test(target)) return new Response('bad url', { status: 400 });
    if (u.host === 'm') {
      try {
        return await streamMedia(target, referer, request.headers.get('range') || '');
      } catch (err) {
        return new Response(String(err.message || err), { status: /blocked|scheme/.test(String(err.message)) ? 403 : 502 });
      }
    }
    const key = crypto.createHash('sha1').update(target).digest('hex');
    const ext = (path.extname(new URL(target).pathname) || '.img').slice(0, 6).toLowerCase();
    const file = path.join(thumbDir(), `${key}${ext}`);
    try {
      const buf = await fsp.readFile(file);
      return new Response(buf, { headers: { 'content-type': mimeOf(ext), 'cache-control': 'max-age=86400' } });
    } catch { /* not cached */ }
    try {
      if (!thumbInflight.has(key)) {
        thumbInflight.set(key, fetchThumb(target, referer).finally(() => thumbInflight.delete(key)));
      }
      const { buf, type } = await thumbInflight.get(key);
      const tmp = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
      fsp.mkdir(thumbDir(), { recursive: true })
        .then(() => fsp.writeFile(tmp, buf)).then(() => fsp.rename(tmp, file))
        .catch(() => fsp.unlink(tmp).catch(() => {}));
      return new Response(buf, { headers: { 'content-type': type } });
    } catch (err) {
      return new Response(String(err.message || err), { status: /blocked|scheme/.test(String(err.message)) ? 403 : 502 });
    }
  });
}

function mimeOf(ext) {
  return {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp',
    '.mp4': 'video/mp4', '.webm': 'video/webm',
  }[ext.toLowerCase()] || 'application/octet-stream';
}

async function applyProxyToSession() {
  const c = settings.connection;
  if (!c.proxy || c.ignoreProxy) {
    await session.defaultSession.setProxy({ mode: 'system' });
    return;
  }
  // chromium proxy rules: http://host:port, socks5://host:port (socks5h behaves like socks5: dns is resolved by the proxy)
  const rule = c.proxy.replace(/^socks5h:/i, 'socks5:').replace(/\/\/[^@/]*@/, '//');
  await session.defaultSession.setProxy({ proxyRules: rule });
}

// ---------- appearance ----------

function backgroundUrl() {
  const bg = settings.appearance.background;
  if (bg.mode === 'image' && bg.image && fs.existsSync(bg.image)) return pathToFileURL(bg.image).href;
  return pathToFileURL(iconPath(settings.appearance.icon)).href;
}

function appearanceState() {
  return {
    ...settings.appearance,
    iconUrl: pathToFileURL(iconPath(settings.appearance.icon)).href,
    backgroundUrl: backgroundUrl(),
    icons: ICONS.map((i) => ({ ...i, url: pathToFileURL(iconPath(i.id)).href })),
    themes: THEMES,
    // window icon changes live on Windows/X11/macOS; Wayland compositors read the icon from the .desktop entry only
    liveTaskbarIcon: !isWaylandLinux(),
    platform: process.platform,
  };
}

// ---------- app icon outside the window (dock, launcher, shortcuts) ----------
// The window / taskbar icon changes live on Windows and X11, the macOS dock icon can be set while running. Elsewhere the icon is
// read from files: the Linux launcher (Wayland docks use nothing else) and the shortcuts made by the Windows installer. They are
// rewritten when the icon changes. The icon of the app file itself (AppImage, .exe, .app bundle) is fixed at build time.
const LAUNCHER_MARKER = 'X-Lustra-Launcher=true';
const ICON_FILE_PREFIX = 'lustra-icon-';
const isWaylandLinux = () => process.platform === 'linux' && !!(process.env.WAYLAND_DISPLAY || process.env.XDG_SESSION_TYPE === 'wayland');

function applyWindowIcon() {
  const img = nativeImage.createFromPath(iconPath(settings.appearance.icon));
  if (img.isEmpty()) return;
  if (process.platform === 'darwin') app.dock?.setIcon(img);
  else mainWindow?.setIcon(img);
}

const outsideIconDir = () => path.join(userData(), 'icons');

function desktopEntryPath() {
  return path.join(os.homedir(), '.local', 'share', 'applications', `${DESKTOP_ID}.desktop`);
}

function launcherEntry(iconFile) {
  // AppImage: process.execPath points into the temporary mount, $APPIMAGE is the file the user runs
  const quote = (s) => `"${String(s).replace(/(["`$\\])/g, '\\$1')}"`;
  const exec = process.env.APPIMAGE ? quote(process.env.APPIMAGE)
    : app.isPackaged ? quote(process.execPath) : `${quote(process.execPath)} ${quote(__dirname)}`;
  return [
    '[Desktop Entry]', 'Type=Application', 'Name=Lustra', 'Comment=Booru downloader', `Exec=${exec} %U`,
    `Icon=${iconFile}`, 'Terminal=false', 'Categories=Network;Graphics;', `StartupWMClass=${DESKTOP_ID}`, LAUNCHER_MARKER, '',
  ].join('\n');
}

const launcherIconFile = () => path.join(outsideIconDir(), `${ICON_FILE_PREFIX}${settings.appearance.icon}.png`);

async function writeLauncher() {
  const iconFile = launcherIconFile();
  await fsp.mkdir(outsideIconDir(), { recursive: true });
  // a new file name per icon: desktop shells cache icons by path, an overwritten file is often not picked up
  await fsp.copyFile(iconPath(settings.appearance.icon), iconFile);
  const file = desktopEntryPath();
  await fsp.mkdir(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp-${process.pid}`;
  await fsp.writeFile(tmp, launcherEntry(iconFile));
  await fsp.rename(tmp, file); // the shell watches this folder: it only ever sees a complete file
  for (const f of await fsp.readdir(outsideIconDir()).catch(() => [])) {
    const old = path.join(outsideIconDir(), f);
    if (old !== iconFile && f.endsWith('.png') && (f.startsWith(ICON_FILE_PREFIX) || f === 'lustra.png')) await fsp.unlink(old).catch(() => {});
  }
  return file;
}

async function installLauncher() {
  if (process.platform !== 'linux') return { ok: false, message: 'Chỉ cần trên Linux' };
  return { ok: true, path: await writeLauncher(), restartForDock: isWaylandLinux() };
}

async function launcherState() {
  // 'missing' | 'ours' | 'foreign': a launcher written by something else (ex. AppImage integration tools) is never touched
  let text;
  try { text = await fsp.readFile(desktopEntryPath(), 'utf8'); } catch { return 'missing'; }
  const ours = text.includes(LAUNCHER_MARKER) || (/^Name=Lustra$/m.test(text) && /^Comment=Booru downloader$/m.test(text));
  return ours ? 'ours' : 'foreign';
}

async function syncLauncher() {
  // -> 'unsupported' | 'missing' | 'foreign' | 'unchanged' | 'updated'
  if (process.platform !== 'linux') return 'unsupported';
  const state = await launcherState();
  if (state !== 'ours') return state;
  const current = await fsp.readFile(desktopEntryPath(), 'utf8').catch(() => '');
  if (current === launcherEntry(launcherIconFile()) && fs.existsSync(launcherIconFile())) return 'unchanged';
  await writeLauncher();
  return 'updated';
}

async function writeIco(iconId) {
  const img = nativeImage.createFromPath(iconPath(iconId));
  if (img.isEmpty()) throw new Error(`icon ${iconId} not found`);
  const pngs = [256, 48, 32, 16].map((size) => ({ size, data: img.resize({ width: size, height: size, quality: 'best' }).toPNG() }));
  const file = path.join(outsideIconDir(), `${ICON_FILE_PREFIX}${iconId}.ico`);
  await fsp.mkdir(outsideIconDir(), { recursive: true });
  await fsp.writeFile(file, pngsToIco(pngs));
  return file;
}

async function listShortcuts(root, depth = 1) {
  const out = [];
  for (const d of await fsp.readdir(root, { withFileTypes: true }).catch(() => [])) {
    const full = path.join(root, d.name);
    if (d.isFile() && d.name.toLowerCase().endsWith('.lnk')) out.push(full);
    else if (d.isDirectory() && depth > 0) out.push(...await listShortcuts(full, depth - 1));
  }
  return out;
}

async function syncWindowsShortcuts({ startup = false } = {}) {
  // shortcuts of the installer (Start menu, desktop, pinned taskbar) pointing at this exe get the selected icon
  if (process.platform !== 'win32' || !app.isPackaged) return 0;
  const appData = app.getPath('appData');
  const roots = [path.join(appData, 'Microsoft', 'Windows', 'Start Menu', 'Programs'), app.getPath('desktop'),
    path.join(appData, 'Microsoft', 'Internet Explorer', 'Quick Launch', 'User Pinned', 'TaskBar')];
  const exe = path.resolve(process.execPath).toLowerCase();
  const ours = [];
  for (const root of roots) {
    for (const lnk of await listShortcuts(root)) {
      try {
        const d = shell.readShortcutLink(lnk);
        if (path.resolve(d.target || '').toLowerCase() === exe) ours.push([lnk, d]);
      } catch { /* not a readable shortcut */ }
    }
  }
  // at startup with the default icon and untouched shortcuts there is nothing to do (the exe icon is the default one)
  if (startup && settings.appearance.icon === DEFAULT_SETTINGS.appearance.icon && ours.every(([, d]) => !d.icon)) return 0;
  if (!ours.length) return 0;
  const ico = await writeIco(settings.appearance.icon);
  let n = 0;
  for (const [lnk, d] of ours) {
    if (d.icon && path.resolve(d.icon).toLowerCase() === ico.toLowerCase()) continue;
    if (shell.writeShortcutLink(lnk, 'update', { icon: ico, iconIndex: 0 })) n++;
  }
  return n;
}

async function syncIcon({ startup = false } = {}) {
  // -> { launcher, shortcuts, restartForDock }: what was updated outside the window
  const r = { launcher: 'unsupported', shortcuts: 0, restartForDock: false };
  try {
    if (!startup || process.platform === 'darwin') applyWindowIcon(); // the window got its icon at creation, the dock did not
    r.launcher = await syncLauncher();
    r.shortcuts = await syncWindowsShortcuts({ startup });
  } catch (err) {
    r.error = err.message;
    send('log', { source: 'icon', text: `icon sync failed: ${err.message}` });
  }
  // Wayland docks read the launcher when the app starts: the running window keeps its icon until the app is reopened
  r.restartForDock = isWaylandLinux() && ['updated', 'unchanged'].includes(r.launcher) && !startup;
  return r;
}

// ---------- tag lists auto update ----------

function onTagsUpdateDone(params) {
  // tags.update only starts a background job: the date is recorded when it finished without error
  if (params.ok && params.module) {
    settings.tags.lastAutoUpdate[params.module] = Date.now();
    saveSettings();
  }
}

async function maybeAutoUpdateTags() {
  if (!settings.tags.autoUpdate) return;
  const days = Math.max(1, settings.tags.autoUpdateDays || 7) * 86400000;
  for (const module of ['rx', 'db']) {
    const last = settings.tags.lastAutoUpdate[module] || 0;
    if (Date.now() - last < days) continue;
    if (module === 'rx' && !(await getSecret('rx'))) continue; // rx needs the user's own key: nothing to try without it
    try {
      await sidecar.call('tags.update', { module, apiKey: await getSecret(module) });
    } catch (err) {
      send('log', { source: 'tags', text: `auto update ${module} failed: ${err.message}` });
    }
  }
}

// ---------- scheduled tag list update (system scheduler) ----------
// The system scheduler runs 'lustra-core tags' (development: python -m lustra.tags_updater) even while the app is closed, for the
// modules that need no key; the lists go to <userData>/tags, where the app looks for the newest list of each module. A scheduled
// run cannot read the app's keys (OS keyring): rx keeps the in-app update. Linux / macOS: a marked block of the user's crontab
// (the rest of the crontab is left as it is); Windows: a Task Scheduler task; AppImage: unsupported (its core only exists while
// the app runs). The entry is written again at every start, so a new install path or version is picked up.
// 'auto': the core picks the modules when it runs (lustra/tags_updater.py tag_sources): every installed module with a tag source
// that needs no key of the user, the reviewed ones (db, xb, bb, en) and extensions made from a known engine. An extension
// installed later is covered without writing the entry again.
const TAG_SCHEDULE_MODULES = 'auto';
const WIN_TASK = 'Lustra tag lists';
const tagScheduleLog = () => path.join(userData(), 'logs', 'tags-schedule.log');

function tagScheduleSupport() {
  if (process.env.APPIMAGE) {
    return { ok: false, reason: 'Bản AppImage không hỗ trợ lịch hệ thống: lõi của app chỉ tồn tại khi app đang chạy. Dùng bản .deb, hoặc cron trong tools/cron/.' };
  }
  if (process.platform === 'win32') return { ok: true, kind: 'schtasks' };
  const r = spawnSync('crontab', ['-l'], { encoding: 'utf8' });
  if (r.error) return { ok: false, reason: 'Máy không có lệnh crontab (cài gói cron hoặc cronie).' };
  return { ok: true, kind: 'crontab' };
}

function tagScheduleArgs() {
  return ['--modules', TAG_SCHEDULE_MODULES, '--out', path.join(userData(), 'tags'), '--min-posts', '2', '--log', tagScheduleLog()];
}

function tagScheduleCommand() {
  // POSIX shell line for crontab: absolute paths only (cron has a minimal PATH), no secret in it
  const c = coreCommand('tags', tagScheduleArgs());
  const env = `LUSTRA_DATA_DIR=${shq(userData())}${c.frozen ? '' : ` PYTHONPATH=${shq(REPO_ROOT)}`}`;
  return `cd ${shq(c.cwd)} && ${env} ${[c.cmd, ...c.args].map(shq).join(' ')}`;
}

const tagScheduleWhen = (frequency) => scheduleWhen(userData(), frequency);

function readCrontab() {
  const r = spawnSync('crontab', ['-l'], { encoding: 'utf8' });
  if (r.status === 0) return r.stdout;
  if (/no crontab/i.test(r.stderr || '')) return '';
  throw new Error(`không đọc được crontab: ${(r.stderr || r.error?.message || '').trim()}`); // never overwrite a crontab we could not read
}

function writeCrontab(text) {
  const r = spawnSync('crontab', ['-'], { input: text.endsWith('\n') ? text : `${text}\n`, encoding: 'utf8' });
  if (r.status !== 0) throw new Error(`không ghi được crontab: ${(r.stderr || r.error?.message || '').trim()}`);
}

function tagScheduleStatus() {
  const sup = tagScheduleSupport();
  const sched = settings.tags.schedule || {};
  let lastRun = 0;
  try { lastRun = fs.statSync(tagScheduleLog()).mtimeMs; } catch { /* never ran */ }
  let installed = false;
  if (sup.ok && sup.kind === 'crontab') {
    try { installed = hasTagBlock(readCrontab()); } catch { /* unreadable: not ours */ }
  }
  if (sup.ok && sup.kind === 'schtasks') installed = spawnSync('schtasks', ['/Query', '/TN', WIN_TASK], { windowsHide: true }).status === 0;
  const frequency = sched.frequency === 'daily' ? 'daily' : 'weekly';
  return {
    ...sup, installed, enabled: sched.enabled === true, frequency, time: tagScheduleWhen(frequency).time, log: tagScheduleLog(), lastRun,
  };
}

function applyTagSchedule(enabled, frequency) {
  const sup = tagScheduleSupport();
  if (!sup.ok) throw new Error(sup.reason);
  const freq = frequency === 'daily' ? 'daily' : 'weekly';
  fs.mkdirSync(path.dirname(tagScheduleLog()), { recursive: true });
  const when = tagScheduleWhen(freq);
  if (sup.kind === 'crontab') {
    const current = readCrontab();
    if (enabled) writeCrontab(withTagBlock(current, `${when.cron} ${tagScheduleCommand()}`));
    else if (hasTagBlock(current)) writeCrontab(withoutTagBlock(current));
  } else if (enabled) {
    const c = coreCommand('tags', tagScheduleArgs());
    if (!c.frozen) throw new Error('Trên Windows, lịch hệ thống chỉ dùng được với bản cài đặt (lõi đóng gói)');
    const tr = [c.cmd, ...c.args].map((a) => (/[\s"]/.test(a) ? `"${a}"` : a)).join(' ');
    const r = spawnSync('schtasks', ['/Create', '/F', '/TN', WIN_TASK, '/SC', freq === 'daily' ? 'DAILY' : 'WEEKLY',
      ...(freq === 'daily' ? [] : ['/D', 'MON']), '/ST', when.time, '/TR', tr], { encoding: 'utf8', windowsHide: true });
    if (r.status !== 0) throw new Error(`schtasks: ${(r.stderr || r.stdout || r.error?.message || '').trim()}`);
  } else {
    spawnSync('schtasks', ['/Delete', '/F', '/TN', WIN_TASK], { windowsHide: true });
  }
  settings.tags.schedule = { enabled, frequency: freq };
  saveSettings();
  return tagScheduleStatus();
}

async function scheduledTagModules() {
  // what 'auto' covers now (shown in Tags A-Z): installed modules with a tag source that needs no key; [] while the core is not up
  try {
    const rows = await sidecar.call('tags.sources', {}, 5000);
    return (Array.isArray(rows) ? rows : []).filter((s) => s.key !== 'required').map((s) => ({ module: s.module, name: s.name }));
  } catch { return []; }
}

function refreshTagSchedule() {
  // at start: write the entry again, so it runs the core of the version installed now
  if (settings.tags.schedule?.enabled !== true) return;
  try { applyTagSchedule(true, settings.tags.schedule.frequency); } catch (err) {
    send('log', { source: 'tags', text: `scheduled tag update not refreshed: ${err.message}` });
  }
}

// ---------- IPC ----------

const RENDERER_URL = pathToFileURL(path.join(__dirname, 'renderer', 'index.html')).href;

function trustedSender(e) {
  const url = e.senderFrame?.url || '';
  return url === RENDERER_URL || url.startsWith(`${RENDERER_URL}#`) || url.startsWith(`${RENDERER_URL}?`);
}

function handle(channel, fn) {
  ipcMain.handle(channel, (e, ...a) => {
    if (!trustedSender(e)) throw new Error('untrusted sender');
    return fn(e, ...a);
  });
}

function registerIpc() {
  handle('sidecar:getStatus', () => sidecar.status || { ok: false, error: 'starting' });
  handle('rpc', async (_e, method, params) => {
    // installs never go through the generic rpc channel (see ext:* handlers): they need main-side checks and confirmation
    const allowed = ['app.info', 'ext.list', 'ext.uninstall', 'ext.remoteCatalog', 'tags.autocomplete', 'tags.browse',
      'tags.validate', 'tags.status', 'tags.sources', 'tags.resolve', 'tags.fuzzy', 'files.nonEmpty', 'files.identify', 'index.search', 'index.stats'];
    if (!allowed.includes(method)) throw new Error(`method not allowed: ${method}`);
    params = params && typeof params === 'object' ? params : {};
    if (method === 'ext.uninstall' && !RE_EXT_ID.test(String(params.id || ''))) throw new Error('invalid extension id');
    if (method === 'ext.remoteCatalog') {
      const url = String(settings.advanced.catalogUrl || '');
      if (!/^https:\/\//i.test(url)) throw new Error('catalog url must start with https://');
      params = { url };
    }
    return sidecar.call(method, params);
  });
  handle('ext:installCatalog', async (_e, id) => {
    if (!RE_EXT_ID.test(String(id || ''))) throw new Error('invalid extension id');
    return sidecar.call('ext.install', { id });
  });
  // remote catalog: only the id comes from the renderer. The entry (archive url, sha256) is taken from the catalog the core
  // verified (Ed25519 signature, lustra/trust.py); the install verifies it again and pins the sha256 the user confirmed
  handle('ext:installRemote', async (_e, item) => {
    const id = String(item?.id || '');
    if (!RE_EXT_ID.test(id)) throw new Error('invalid extension id');
    const catalogUrl = String(settings.advanced.catalogUrl || '');
    if (!/^https:\/\//i.test(catalogUrl)) throw new Error('catalog url must start with https://');
    const entries = await sidecar.call('ext.remoteCatalog', { url: catalogUrl });
    const entry = (Array.isArray(entries) ? entries : []).find((x) => isPlainObject(x) && x.id === id);
    if (!entry || !RE_SHA256.test(String(entry.sha256 || ''))) throw new Error(`extension "${id}" không có trong catalog`);
    if (entry.denied) throw new Error(String(entry.denied.message || 'site nằm trong danh sách chặn của Lustra'));
    const trust = entry.signed ? `Catalog có chữ ký hợp lệ (khoá ${String(entry.keyId || '?').slice(0, 16)}).`
      : 'CẢNH BÁO: catalog CHƯA KÝ, chỉ được nhận vì đang bật LUSTRA_CATALOG_ALLOW_UNSIGNED (chế độ nhà phát triển).';
    const ok = await confirmMain(`Cài extension "${String(entry.name || id).slice(0, 80)}"?`,
      `Extension là code Python sẽ chạy trên máy bạn với quyền của bạn.\nNguồn: ${String(entry.url || '').slice(0, 300)}\n`
      + `sha256: ${entry.sha256}\n${trust}\nChỉ cài từ nguồn bạn tin cậy.`);
    if (!ok) throw new Error('cancelled');
    return sidecar.call('ext.install', { catalogUrl, remoteId: id, sha256: entry.sha256 });
  });
  handle('tags:update', async (_e, module) => sidecar.call('tags.update', { module, apiKey: await getSecret(module) }));
  handle('tags:scheduleStatus', async () => ({ ...tagScheduleStatus(), modules: await scheduledTagModules() }));
  handle('tags:schedule', async (_e, o) => ({ ...applyTagSchedule(o?.enabled === true, o?.frequency), modules: await scheduledTagModules() }));
  handle('search:count', async (_e, q) => sidecar.call('search.count', {
    module: asQuery(q).module, tags: q.tags, args: [...(await connectionArgs(q.module)), ...searchArgs(q), ...tagLimitArgs(q.module)],
  }));
  handle('search:preview', async (_e, q, page) => {
    const params = {
      module: asQuery(q).module, tags: q.tags, page: page || 0,
      // the preview downloads nothing: no media check (a model server older than /v1/frames would fail it)
      args: [...(await connectionArgs(q.module)), ...searchArgs(q), ...tagLimitArgs(q.module),
        ...genderArgs(isPlainObject(q.gender) ? { ...q.gender, verifyMedia: false } : q.gender), ...safetyArgs(q),
        ...(await genderSecretArgs(q.gender))],
    };
    if (Number.isInteger(q.pageSize) && q.pageSize > 0) params.pageSize = q.pageSize; // the sidecar caps it to the module page size
    return sidecar.call('search.preview', params, 180000);
  });
  handle('search:related', async (_e, q) => {
    // tags co-occurring in the first results page: same connection / date args as the preview, no content filter
    const params = {
      module: asQuery(q).module, tags: q.tags, args: [...(await connectionArgs(q.module)), ...searchArgs(q), ...tagLimitArgs(q.module)],
    };
    if (Number.isInteger(q.limit) && q.limit > 0) params.limit = Math.min(q.limit, 200);
    return sidecar.call('search.related', params, 180000);
  });

  handle('settings:get', () => ({ settings, appearance: appearanceState(), secrets: secretsStatus() }));
  handle('settings:set', async (_e, patch) => {
    if (!isPlainObject(patch)) throw new Error('invalid settings patch');
    delete patch.secrets;
    delete patch.version;
    const py = patch.advanced?.pythonPath;
    if (py !== undefined && py !== settings.advanced.pythonPath) {
      // the python path is executed by the app: changing it requires a native confirmation
      const valid = py === '' || (typeof py === 'string' && path.isAbsolute(py) && fs.existsSync(py) && fs.statSync(py).isFile());
      if (!valid || (py && !(await confirmMain('Dùng chương trình này làm Python cho Lustra?', py)))) delete patch.advanced.pythonPath;
    }
    if (isPlainObject(patch.search)) {
      // history / saved queries are written whole by the renderer: drop malformed entries, cap sizes
      for (const [key, max, named] of [['history', HISTORY_MAX, false], ['saved', SAVED_MAX, true]]) {
        if (patch.search[key] === undefined) continue;
        const v = sanitizeQueryList(patch.search[key], max, named);
        if (v === null) delete patch.search[key];
        else {
          if (named) mergeAutoRuntime(v);
          patch.search[key] = v;
        }
      }
      if (patch.search.tagLimits !== undefined) {
        const v = sanitizeTagLimits(patch.search.tagLimits);
        if (v === null) delete patch.search.tagLimits;
        else patch.search.tagLimits = v;
      }
    }
    const replaceMaps = {}; // headers / cookies are edited as a whole: replaced, not merged (a removed entry must go away)
    if (isPlainObject(patch.connection)) {
      for (const [key, kind] of [['headers', 'Header'], ['cookies', 'Cookie']]) {
        if (patch.connection[key] === undefined) continue;
        replaceMaps[key] = normalizeHttpMap(patch.connection[key], kind, true);
        delete patch.connection[key];
      }
    }
    if (isPlainObject(patch.genderModel) && patch.genderModel.url !== undefined) {
      const u = sanitizeModelUrl(patch.genderModel.url);
      if (u === null) throw new Error('Địa chỉ model server phải có dạng http://máy:cổng hoặc https://…');
      patch.genderModel.url = u;
    }
    settings = deepMerge(settings, patch);
    settings.genderModel.enabled = settings.genderModel.enabled === true;
    Object.assign(settings.connection, replaceMaps);
    if (!GEN_MODELS.includes(settings.ai.model)) settings.ai.model = DEFAULT_SETTINGS.ai.model;
    settings.ai.maxUsd = clampUsd(settings.ai.maxUsd, DEFAULT_SETTINGS.ai.maxUsd);
    saveSettings();
    if (patch?.connection) await applyProxyToSession();
    let iconSync;
    if (patch?.appearance) {
      if (patch.appearance.icon) iconSync = await syncIcon();
      send('appearance:changed', appearanceState());
    }
    return { settings, appearance: appearanceState(), secrets: secretsStatus(), iconSync };
  });
  handle('secrets:set', async (_e, module, value) => {
    await setSecret(module, typeof value === 'string' ? value.trim() : ''); // empty: no change
    return secretsStatus();
  });
  handle('secrets:clear', (_e, module) => { clearSecret(module); return secretsStatus(); });

  handle('jobs:start', (_e, q) => jobs.start(isPlainObject(q) ? { ...q, autoKey: undefined, group: undefined } : q)); // only the scheduler sets autoKey
  // gender image model: runs in a model server (lustra/model_server.py); the app only calls its API, the token stays in main
  handle('gmodel:status', async () => {
    if (!settings.genderModel.url) return { url: '', reachable: false, local: false, error: 'off' };
    return sidecar.call('gender.modelStatus', { url: settings.genderModel.url, token: await getSecret(MODEL_SECRET) }, 20000);
  });
  // turning the model on loads it on the server now (status after: the device it really runs on); off frees its memory
  handle('gmodel:load', async () => {
    if (!settings.genderModel.url) throw new Error('Chưa có địa chỉ model server');
    return sidecar.call('gender.modelLoad', { url: settings.genderModel.url, token: await getSecret(MODEL_SECRET) }, 200000);
  });
  handle('gmodel:unload', async () => {
    if (!settings.genderModel.url) return { url: '', reachable: false, local: false, error: 'off' };
    return sidecar.call('gender.modelUnload', { url: settings.genderModel.url, token: await getSecret(MODEL_SECRET) }, 20000);
  });
  handle('gmodel:setToken', (_e, value) => setModelToken(value));
  handle('gmodel:clearToken', () => clearModelToken());
  handle('saved:auto', (_e, key, every, from) => setSavedAuto(String(key || ''), Number(every) || 0, from === 'now' ? 'now' : 'all'));
  handle('jobs:cancel', (_e, id) => jobs.cancel(id));
  handle('jobs:list', () => jobs.list());
  handle('jobs:clear', () => jobs.clearFinished());
  handle('jobs:rerun', (_e, id, allowDuplicate) => jobs.rerun(String(id || ''), allowDuplicate === true));

  handle('dialog:folder', async (_e, current) => {
    const r = await dialog.showOpenDialog(mainWindow, { properties: ['openDirectory', 'createDirectory'], defaultPath: current || undefined });
    return r.canceled ? '' : r.filePaths[0];
  });
  handle('dialog:image', async () => {
    const r = await dialog.showOpenDialog(mainWindow, {
      properties: ['openFile'], filters: [{ name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif'] }],
    });
    if (r.canceled) return '';
    approvedPaths.add(r.filePaths[0]);
    return r.filePaths[0];
  });
  handle('dialog:zip', async () => {
    const r = await dialog.showOpenDialog(mainWindow, { properties: ['openFile'], filters: [{ name: 'Lustra extension', extensions: ['zip'] }] });
    if (r.canceled) return '';
    approvedPaths.add(r.filePaths[0]);
    return r.filePaths[0];
  });
  handle('ext:installZip', async (_e, file, sha256) => {
    if (!approvedPaths.has(file)) throw new Error('choose the archive with the file dialog first');
    if (!RE_SHA256.test(String(sha256 || '').trim())) throw new Error('sha256 must be 64 hex characters');
    const ok = await confirmMain(`Cài extension từ ${path.basename(file)}?`,
      'Extension là code Python sẽ chạy trên máy bạn với quyền của bạn. Chỉ cài từ nguồn bạn tin cậy.');
    if (!ok) throw new Error('cancelled');
    return sidecar.call('ext.install', { archivePath: file, sha256: String(sha256).trim() });
  });

  // extension generator: drafts only; installing one is its own confirmed action (Generator.install)
  handle('gen:status', () => generator.status());
  handle('gen:start', (_e, opts) => generator.start(opts));
  handle('gen:cancel', () => generator.cancel());
  handle('gen:list', () => listGenDrafts());
  handle('gen:get', (_e, session) => readGenReport(session));
  handle('gen:open', async (_e, session) => {
    const rep = await readGenReport(session);
    return shell.openPath(rep.draftDir || genSessionDir(session));
  });
  handle('gen:export', (_e, session) => generator.exportZip(session));
  handle('gen:install', (_e, session) => generator.install(session));
  handle('gen:remove', (_e, session) => generator.remove(session));
  handle('gen:setKey', (_e, value) => setAiKey(value));
  handle('gen:clearKey', () => clearAiKey());

  handle('appearance:setBackgroundImage', async (_e, file) => {
    if (!file) {
      settings.appearance.background.mode = 'icon';
    } else {
      if (!approvedPaths.has(file)) throw new Error('choose the image with the file dialog first');
      const ext = path.extname(file).toLowerCase();
      if (!['.png', '.jpg', '.jpeg', '.webp', '.gif'].includes(ext)) throw new Error('unsupported image type');
      const dest = path.join(userData(), 'backgrounds', `${crypto.randomUUID()}${ext}`);
      await fsp.mkdir(path.dirname(dest), { recursive: true });
      await fsp.copyFile(file, dest);
      const old = settings.appearance.background.image;
      if (old && old.startsWith(path.join(userData(), 'backgrounds'))) fsp.unlink(old).catch(() => {});
      settings.appearance.background = { ...settings.appearance.background, mode: 'image', image: dest };
    }
    saveSettings();
    const st = appearanceState();
    send('appearance:changed', st);
    return st;
  });

  handle('app:relaunch', () => requestQuit(true)); // false: the user kept the app running (download jobs)
  handle('app:info', () => {
    const core = coreCommand('server');
    return {
      version: app.getVersion(), electron: process.versions.electron, platform: process.platform, userData: userData(),
      python: core.cmd, core: core.frozen ? 'bundled' : 'python', packaged: app.isPackaged, repo: REPO_ROOT, sidecar: sidecar.info,
      encryption: encryptionAvailable, launcherInstalled: fs.existsSync(desktopEntryPath()),
    };
  });
  handle('launcher:install', () => installLauncher());
  handle('shell:open', (_e, p) => {
    // folders only (download destinations): files could be executables or .desktop launchers
    const target = String(p || '');
    if (!path.isAbsolute(target) || !fs.existsSync(target) || !fs.statSync(target).isDirectory()) return 'not a folder';
    return shell.openPath(target);
  });
  handle('shell:external', (_e, url) => (/^https?:\/\//.test(url) ? shell.openExternal(url) : false));
  // library page: a downloaded media file is opened with the default app; anything else (an executable, a .desktop file a
  // website could have named) is refused. 'reveal' only shows the file in its folder
  handle('library:open', (_e, p) => {
    const target = String(p || '');
    const ext = path.extname(target).slice(1).toLowerCase();
    if (!path.isAbsolute(target) || !LIBRARY_MEDIA_EXT.has(ext)) return 'chỉ mở được file ảnh / video / âm thanh';
    try { if (!fs.statSync(target).isFile()) return 'không phải file'; } catch { return 'file không còn trên đĩa'; }
    return shell.openPath(target);
  });
  handle('library:reveal', (_e, p) => {
    const target = String(p || '');
    if (!path.isAbsolute(target)) return false;
    if (fs.existsSync(target)) shell.showItemInFolder(target);
    else if (fs.existsSync(path.dirname(target)) && fs.statSync(path.dirname(target)).isDirectory()) shell.openPath(path.dirname(target));
    return true;
  });
}

// ---------- download index tools: clean (sidecar), library import (job), multi-site downloads (jobs) ----------

const MULTI_SITE_MAX = 8;
const approvedImportFolders = new Set(); // folders picked with the native dialog for a library import (renderer paths are refused)

// native confirmation of an index clean; the self-test replaces it
let confirmIndexClean = async () => {
  const r = await dialog.showMessageBox(mainWindow, {
    type: 'question', buttons: ['Huỷ', 'Dọn index'], defaultId: 1, cancelId: 0, noLink: true,
    message: 'Dọn index các lần tải?',
    detail: 'Xoá khỏi index những dòng có file không còn trên đĩa. Chỉ xoá dòng trong index (CSDL của Lustra): không xoá, '
      + 'di chuyển hay đọc file nào. Có thể chạy trong lúc đang tải.\n\nDòng mà cả thư mục chứa file cũng không còn (ổ cứng ngoài '
      + 'chưa cắm, thư mục mạng chưa kết nối) được giữ lại, trừ khi bạn đánh dấu ô bên dưới. Hãy cắm ổ / kết nối thư mục trước khi dọn.',
    checkboxLabel: 'Xoá cả dòng có thư mục không còn', checkboxChecked: false,
  });
  return { ok: r.response === 1, missingFolders: r.checkboxChecked === true };
};

function registerIndexIpc() {
  // clean: rows only, after a native confirmation; a dry run (count only) needs none. The direct rpc methods are not allowlisted
  handle('index:clean', async (_e, opts) => {
    if (opts !== undefined && opts !== null && !isPlainObject(opts)) throw new Error('invalid options');
    if (opts?.dryRun === true) return sidecar.call('index.clean', { dryRun: true, missingFolders: opts.missingFolders === true });
    const answer = await confirmIndexClean();
    if (!answer.ok) throw new Error('cancelled');
    return sidecar.call('index.clean', { missingFolders: answer.missingFolders === true });
  });
  handle('index:cleanCancel', () => sidecar.call('index.cleanCancel', {}));
  handle('index:pickFolder', async () => {
    const r = await dialog.showOpenDialog(mainWindow, { title: 'Chọn thư mục thư viện để nhập vào index', properties: ['openDirectory'] });
    if (r.canceled || !r.filePaths[0]) return '';
    approvedImportFolders.add(r.filePaths[0]);
    return r.filePaths[0];
  });
  handle('index:import', (_e, opts) => jobs.startImport(opts));
  handle('jobs:startMulti', (_e, q, modules) => jobs.startMulti(isPlainObject(q) ? { ...q, autoKey: undefined, group: undefined } : q, modules));
}

const LIBRARY_MEDIA_EXT = new Set(['jpg', 'jpeg', 'png', 'gif', 'webp', 'mp4', 'webm', 'mp3', 'ogg', 'oga', 'opus', 'flac', 'm4a', 'wav']);

function secretsStatus() {
  return {
    encryption: encryptionAvailable,
    modules: Object.fromEntries(Object.entries(secrets).filter(([k]) => k !== AI_SECRET && k !== MODEL_SECRET)
      .map(([k, v]) => [k, { set: true, encrypted: Boolean(v.enc) }])),
    ai: aiKeyStatus(),
    genderModel: modelTokenStatus(),
  };
}

// ---------- quitting ----------

let quitApproved = false; // the user confirmed (or nothing needed confirming): before-quit / window close pass through
let quitFlow = null;
// native confirmation, replaced by the self-test
let askQuit = (message, detail, okLabel) => confirmMain(message, detail, ['Ở lại', okLabel]);

function jobsNeedingConfirm() {
  return jobs.active().filter((j) => j.status === 'queued' || j.status === 'running');
}

async function confirmQuit(relaunch) {
  const pending = jobsNeedingConfirm();
  const running = pending.filter((j) => j.status === 'running').length;
  const parts = [running && `${running} job đang tải`, pending.length - running && `${pending.length - running} job đang chờ`].filter(Boolean);
  return askQuit(relaunch ? 'Khởi động lại Lustra?' : 'Thoát Lustra?',
    `Đang có ${parts.join(' và ')}. Nếu tiếp tục, các job này sẽ bị huỷ.`,
    relaunch ? 'Huỷ job và khởi động lại' : 'Huỷ job và thoát');
}

function requestQuit(relaunch = false) {
  // quit / relaunch with download jobs queued or running: ask first, then cancel them gracefully before exiting
  if (!quitFlow) {
    quitFlow = (async () => {
      try {
        if (!quitApproved && jobsNeedingConfirm().length && !(await confirmQuit(relaunch))) return false;
        quitApproved = true;
        if (jobs.active().length) await jobs.shutdown();
        // AppImage: the running binary lives in a mount that goes away with this process, start the AppImage file again
        if (relaunch) app.relaunch(process.env.APPIMAGE ? { execPath: process.env.APPIMAGE, args: process.argv.slice(1) } : undefined);
        app.quit();
        return true;
      } finally {
        quitFlow = null;
      }
    })();
  }
  return quitFlow;
}

// ---------- window ----------

function setupMenu() {
  if (process.platform !== 'darwin') {
    Menu.setApplicationMenu(null); // no menu bar on Windows / Linux (text fields handle their own shortcuts)
    return;
  }
  // macOS: Cmd+C / Cmd+V / Cmd+A / Cmd+Z only reach text fields through menu roles
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { role: 'appMenu' },
    { role: 'editMenu', label: 'Sửa' },
    { role: 'windowMenu', label: 'Cửa sổ' },
  ]));
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: settings.window.width, height: settings.window.height, minWidth: 1080, minHeight: 680,
    title: 'Lustra', icon: iconPath(settings.appearance.icon), backgroundColor: '#0d0b0a', show: false,
    autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.webContents.once('did-finish-load', () => flushStartupNotices());
  mainWindow.on('close', (e) => {
    if (!quitApproved && jobs.active().length) {
      e.preventDefault();
      requestQuit(false);
      return;
    }
    const [width, height] = mainWindow.getSize();
    settings.window = { width, height };
    saveSettings();
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  // dropping a file/link or any script navigation must never replace the app page (preload would expose window.lustra)
  mainWindow.webContents.on('will-navigate', (e, url) => { if (url !== RENDERER_URL) e.preventDefault(); });
  mainWindow.webContents.on('will-attach-webview', (e) => e.preventDefault());
  mainWindow.webContents.session.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
  if (process.argv.includes('--lustra-devtools')) mainWindow.webContents.openDevTools({ mode: 'detach' });
}

// ---------- self test (dev only: LUSTRA_SELFTEST_DIR=<dir> electron .) ----------

async function runSelfTest(dir) {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const shot = async (name) => {
    const img = await mainWindow.webContents.capturePage();
    await fsp.mkdir(dir, { recursive: true });
    await fsp.writeFile(path.join(dir, `${name}.png`), img.toPNG());
  };
  const js = (code) => mainWindow.webContents.executeJavaScript(code, true);
  const quick = Boolean(process.env.LUSTRA_SELFTEST_QUICK); // no network steps: contract, settings and quit checks only
  const report = { settingsLoad: settingsLoadInfo, core: coreCommand('server') };
  try {
    await sidecar.ready;
    await wait(1500);
    if (!quick) { // UI walkthrough (needs network to danbooru)
      await shot('01-search-empty');
      report.preview = await js(`(async () => {
        document.querySelector('#module-btn').click();
        [...document.querySelectorAll('#module-menu button')].find((b) => b.textContent.includes('Danbooru'))?.click();
        const inp = document.querySelector('#tag-text');
        for (const t of ${JSON.stringify((process.env.LUSTRA_SELFTEST_TAGS || 'scenery sky cloud rating:g').split(' '))}) {
          inp.value = t + ' '; inp.dispatchEvent(new Event('input', { bubbles: true }));
        }
        inp.value = 'sun'; inp.dispatchEvent(new Event('input', { bubbles: true }));
        // first use loads the module tag list in the sidecar (~1 s for danbooru's 766k tags)
        for (let i = 0; i < 20 && !document.querySelector('#ac .ac-item'); i++) await new Promise((r) => setTimeout(r, 250));
        const ac = [...document.querySelectorAll('#ac .ac-item .nm')].map((e) => e.textContent);
        return { ac, query: document.querySelector('#query-text').textContent };
      })()`);
      await shot('02-autocomplete');
      report.previewResult = await js(`(async () => {
        const inp = document.querySelector('#tag-text'); inp.value = ''; inp.dispatchEvent(new Event('input', { bubbles: true }));
        document.querySelector('#btn-count').click();
        document.querySelector('#btn-preview').click();
        for (let i = 0; i < 60 && document.querySelector('#btn-preview').disabled !== false; i++) await new Promise((r) => setTimeout(r, 500));
        await new Promise((r) => setTimeout(r, 1500));
        for (let i = 0; i < 40 && document.querySelector('#btn-count').textContent !== 'Đếm'; i++) await new Promise((r) => setTimeout(r, 500));
        return { info: document.querySelector('#results-info').textContent, cards: document.querySelectorAll('#grid .card').length,
          status: document.querySelector('#query-status').textContent };
      })()`);
      await wait(4000);
      await shot('03-preview');
      // search by file name / post link: dialog -> files.identify -> query -> preview
      report.filesSearch = await js(`(async () => {
        document.querySelector('#btn-files').click();
        const area = document.querySelector('.files-input');
        area.value = 'db_5000.jpg\\nhttps://danbooru.donmai.us/posts/5001\\nIMG_1.jpg';
        area.dispatchEvent(new Event('input', { bubbles: true }));
        for (let i = 0; i < 20 && !document.querySelector('.files-queries .btn'); i++) await new Promise((r) => setTimeout(r, 250));
        return { items: [...document.querySelectorAll('.files-item')].map((e) => e.textContent) };
      })()`);
      await shot('03a-files-dialog');
      report.filesSearch = { ...report.filesSearch, ...await js(`(async () => {
        const btn = document.querySelector('.files-queries .btn');
        const label = btn ? btn.textContent : null;
        if (btn) btn.click();
        await new Promise((r) => setTimeout(r, 300));
        for (let i = 0; i < 60 && document.querySelector('#btn-preview').disabled !== false; i++) await new Promise((r) => setTimeout(r, 500));
        await new Promise((r) => setTimeout(r, 1500));
        return { label, query: document.querySelector('#query-text').textContent, cards: document.querySelectorAll('#grid .card').length };
      })()`) };
      await wait(1500);
      await shot('03b-files-search');
      await js(`document.querySelector('#btn-az').click()`);
      await wait(2500);
      await shot('04-az-drawer');
      await js(`document.querySelector('#az-close').click(); document.querySelector('[data-page="extensions"]').click()`);
      await wait(1500);
      await shot('05-extensions');
      await js(`document.querySelector('[data-page="settings"]').click()`);
      await wait(800);
      await shot('06-settings');
      await js(`document.querySelector('.theme-sw:nth-child(3)').click()`);
      await wait(800);
      await shot('07-settings-paper');
      await js(`document.querySelector('.theme-sw:nth-child(1)').click(); document.querySelector('[data-page="tags"]').click()`);
      await wait(2500);
      await shot('08-tags');
      if (process.env.LUSTRA_SELFTEST_DOWNLOAD) {
        report.download = await js(`(async () => {
          const job = await window.lustra.jobs.start({ module: 'db', tags: ${JSON.stringify(process.env.LUSTRA_SELFTEST_DOWNLOAD)},
            path: ${JSON.stringify(path.join(dir, 'dl'))}, options: { limit: 2, threads: 1 } });
          document.querySelector('[data-page="downloads"]').click();
          for (let i = 0; i < 120; i++) {
            const j = (await window.lustra.jobs.list()).find((x) => x.id === job.id);
            if (j && ['done', 'failed', 'cancelled'].includes(j.status)) return { status: j.status, progress: j.progress, exit: j.exitCode };
            await new Promise((r) => setTimeout(r, 500));
          }
          return { status: 'timeout' };
        })()`);
        // batch B: a module that is not installed must fail with a reason, not hang the queue
        report.badModuleJob = await js(`(async () => {
          const job = await window.lustra.jobs.start({ module: 'rs', tags: 'scenery -webm', path: ${JSON.stringify(path.join(dir, 'dl2'))} });
          for (let i = 0; i < 60; i++) {
            const j = (await window.lustra.jobs.list()).find((x) => x.id === job.id);
            if (j && ['done', 'failed', 'cancelled'].includes(j.status)) return { status: j.status, exit: j.exitCode, error: j.error };
            await new Promise((r) => setTimeout(r, 500));
          }
          return { status: 'timeout' };
        })()`);
        await wait(800);
        await shot('09-downloads');
      }
    }
    report.coreStatus = await js('window.lustra.sidecarStatus()');
    report.contract = await selfTestContract(js, quick);
    if (process.env.LUSTRA_SELFTEST_SECURITY) {
      report.security = await js(`(async () => {
        const jobs = await window.lustra.jobs.list();
        const loadImg = (src) => new Promise((resolve) => {
          const img = new Image();
          img.onload = () => resolve('loaded');
          img.onerror = () => resolve('blocked');
          img.src = src;
        });
        let bypass = 'denied';
        try { await window.lustra.ext.installZip('/etc/passwd', '0'.repeat(64)); bypass = 'ALLOWED'; } catch (e) { bypass = 'denied: ' + e.message; }
        let shellOpen = '';
        try { shellOpen = await window.lustra.app.openPath('/etc/passwd'); } catch (e) { shellOpen = 'err: ' + e.message; }
        return {
          jobKeys: jobs[0] ? Object.keys(jobs[0]).sort() : [],
          thumbLoopback: await loadImg(window.lustra.thumbUrl('http://127.0.0.1:9/x.jpg', '')),
          thumbMetadata: await loadImg(window.lustra.thumbUrl('http://169.254.169.254/latest/meta-data', '')),
          zipWithoutDialog: bypass,
          openFile: shellOpen,
        };
      })()`);
      const before = mainWindow.webContents.getURL();
      await js(`location.href = 'https://example.com/'`).catch(() => {});
      await wait(1500);
      report.security.navigationBlocked = mainWindow.webContents.getURL() === before;
      report.security.stillHasBridge = await js('typeof window.lustra === "object"');
    }
    report.quitGuard = await selfTestQuitGuard(dir);
    report.generator = await selfTestGenerator(js);
    report.indexTools = await selfTestIndexTools(js, shot);
    if (process.env.LUSTRA_SELFTEST_ICONS) {
      // writes ~/.local/share/applications/lustra.desktop: run it with a scratch HOME only
      const read = () => fs.readFileSync(desktopEntryPath(), 'utf8');
      const iconLine = () => (read().match(/^Icon=(.*)$/m) || [])[1] || '';
      const setIcon = (id) => js(`window.lustra.settings.set({ appearance: { icon: '${id}' } }).then((r) => r.iconSync)`);
      const r = { home: os.homedir() };
      try { fs.unlinkSync(desktopEntryPath()); } catch { /* none */ }
      r.missing = await setIcon('exposed-jaw-orange');
      r.install = await js('window.lustra.app.installLauncher()');
      const firstIcon = iconLine();
      r.changed = await setIcon('full-mask-monochrome');
      const secondIcon = iconLine();
      r.unchanged = await setIcon('full-mask-monochrome');
      Object.assign(r, {
        firstIcon: path.basename(firstIcon), secondIcon: path.basename(secondIcon), iconFiles: fs.readdirSync(outsideIconDir()),
        oldRemoved: !fs.existsSync(firstIcon), newExists: fs.existsSync(secondIcon), marker: read().includes(LAUNCHER_MARKER),
        wmClass: /^StartupWMClass=lustra$/m.test(read()),
      });
      fs.writeFileSync(desktopEntryPath(), '[Desktop Entry]\nName=Other\nIcon=x\n');
      r.foreign = await setIcon('exposed-jaw-orange');
      r.foreignUntouched = read().includes('Name=Other');
      fs.unlinkSync(desktopEntryPath());
      await setIcon(DEFAULT_SETTINGS.appearance.icon);
      r.ok = r.missing?.launcher === 'missing' && r.install?.ok === true && r.changed?.launcher === 'updated'
        && r.unchanged?.launcher === 'unchanged' && r.oldRemoved && r.newExists && r.marker && r.wmClass
        && r.foreign?.launcher === 'foreign' && r.foreignUntouched;
      report.iconSync = r;
    }
    report.sidecar = sidecar.info;
  } catch (err) {
    report.error = String(err && err.stack || err);
  }
  await fsp.writeFile(path.join(dir, 'report.json'), JSON.stringify(report, null, 2));
  quitApproved = true; // a job left over by a failed step must not open the quit confirmation
  await jobs.shutdown(3000);
  app.quit();
}

async function selfTestContract(js, quick) {
  // batch D wiring: allowlisted sidecar methods, related / pageSize, secrets, settings validation, headers, tag limits, locale
  const out = await js(`(async () => {
    const r = {};
    const t = async (name, fn) => {
      try { r[name] = { ok: true, sample: JSON.stringify(await fn()).slice(0, 400) }; } catch (e) { r[name] = { ok: false, error: String(e.message || e).slice(0, 300) }; }
    };
    await t('resolve', () => window.lustra.rpc('tags.resolve', { module: 'db', tags: ['scenery', 'sky', 'skyy', 'rating:g'] }));
    await t('fuzzy', () => window.lustra.rpc('tags.fuzzy', { module: 'db', text: 'scenrey', limit: 5 }));
    await t('notAllowed', () => window.lustra.rpc('ext.install', { id: 'x' }));
    await t('indexStats', () => window.lustra.rpc('index.stats'));
    await t('indexSearch', () => window.lustra.rpc('index.search', { tags: 'scenery', pageSize: 5 }));
    if (!${quick}) {
      await t('related', () => window.lustra.search.related({ module: 'db', tags: 'scenery rating:g', limit: 10 }));
      await t('previewPageSize', async () => {
        const p = await window.lustra.search.preview({ module: 'db', tags: 'scenery rating:g', pageSize: 5 }, 0);
        return { items: (p.items || []).length, pageSize: p.pageSize, hasMore: p.hasMore };
      });
    }
    let s = await window.lustra.secrets.set('zz', 'selftest-key');
    const setOk = Boolean(s.modules.zz);
    s = await window.lustra.secrets.set('zz', '   ');
    const emptyKeeps = Boolean(s.modules.zz);
    s = await window.lustra.secrets.clear('zz');
    r.secrets = { setOk, emptyKeeps, cleared: !s.modules.zz, ok: setOk && emptyKeeps && !s.modules.zz };
    const prev = (await window.lustra.settings.get()).settings.search;
    const history = [{ module: 'db', tags: 'a b', at: 2 }, { module: 'db', tags: 'a b', at: 1 }, { module: 'DB!', tags: 'x', at: 1 },
      { module: 'db', tags: 'x'.repeat(2001), at: 1 }, 'junk', { module: 'rx', tags: 'c', at: 3 }, { module: 'rx', tags: 'd', at: 'x' }];
    const saved = Array.from({ length: 205 }, (_, i) => ({ name: 'n'.repeat(100), module: 'db', tags: 't' + i, at: i }));
    const v = (await window.lustra.settings.set({ search: { history, saved, tagLimits: { db: 6, rx: 'x', 'bad key': 3 } } })).settings.search;
    r.settingsValidation = { history: v.history.length, saved: v.saved.length, nameLen: v.saved[0].name.length, tagLimits: v.tagLimits };
    r.settingsValidation.ok = v.history.length === 2 && v.saved.length === 200 && v.saved[0].name.length === 80 && v.tagLimits.db === 6 && !('rx' in v.tagLimits);
    await window.lustra.settings.set({ search: { history: prev.history || [], saved: prev.saved || [], tagLimits: { db: (prev.tagLimits || {}).db || 0 } } });
    r.headersSave = await window.lustra.settings.set({ connection: { headers: ${JSON.stringify({ 'X-Test': 'a"b\\c' })}, cookies: ${JSON.stringify({ sid: '"q\\z"' })} } })
      .then(() => 'saved', (e) => e.message);
    r.badHeaderName = await window.lustra.settings.set({ connection: { headers: { 'bad name': 'v' } } }).then(() => 'ACCEPTED', () => 'rejected');
    r.lang = { navigator: navigator.language, dateInput: (() => { const d = document.createElement('input'); d.type = 'date'; return d.type; })() };
    return r;
  })()`);
  // main side: what the core receives (secrets go through LUSTRA_SECRET_ARGS, see Jobs._spawn)
  const args = await connectionArgs('zz');
  out.httpArgs = args.filter((_a, i) => args[i - 1] === '-header' || args[i - 1] === '-cookie');
  out.httpArgsOk = out.httpArgs.includes('X-Test=a"b\\c') && out.httpArgs.includes('sid="q\\z"');
  settings.connection.headers = {};
  settings.connection.cookies = {};
  const limits = settings.search.tagLimits;
  settings.search.tagLimits = { db: 6 };
  out.tagLimitArgs = tagLimitArgs('db');
  settings.search.tagLimits = limits;
  // download index: '-skip_known' only when the option is on (off by default, old behavior unchanged)
  const hasSkip = (options) => downloadArgs({ module: 'db', tags: 'x', options }).args.includes('-skip_known');
  out.skipKnownArgs = { on: hasSkip({ skipKnown: true }), off: hasSkip({ skipKnown: false }), default: hasSkip({}) };
  out.skipKnownArgs.ok = out.skipKnownArgs.on && !out.skipKnownArgs.off && out.skipKnownArgs.default === Boolean(settings.download.skipKnown);
  // scheduled tag update: the command line only (a self-test never writes the user's crontab / tasks)
  {
    const cmd = tagScheduleCommand();
    out.tagSchedule = {
      cmd, when: tagScheduleWhen('weekly').cron,
      ok: (cmd.includes("'tags' '--modules' 'auto'") || cmd.includes("'lustra.tags_updater' '--modules' 'auto'"))
        && cmd.includes(shq(path.join(userData(), 'tags'))) && cmd.includes(shq(tagScheduleLog())) && !/api.?key/i.test(cmd)
        && /^\d+ 4 \* \* 1$/.test(tagScheduleWhen('weekly').cron) && /^\d+ 4 \* \* \*$/.test(tagScheduleWhen('daily').cron),
    };
  }
  // audio: '-skip_aud' only when the audio option is off (default on)
  const hasSkipAud = (options) => downloadArgs({ module: 'db', tags: 'x', options }).args.includes('-skip_aud');
  out.audioArgs = { off: hasSkipAud({ audio: false }), on: hasSkipAud({ audio: true }), default: hasSkipAud({}) };
  out.audioArgs.ok = out.audioArgs.off && !out.audioArgs.on && !out.audioArgs.default;
  // gender: 'keep' picks -> -gender, 'exclude' picks -> -gender_exclude, excluding every label is no filter
  const ga = (g) => genderArgs(g).join(' ');
  out.genderArgs = {
    keep: ga({ select: ['only_female', 'both'], op: 'keep' }), exclude: ga({ select: ['only_male'], op: 'exclude', conf: 'high' }),
    all: ga({ select: ['only_female', 'only_male', 'both', 'none', 'unknown'], op: 'exclude' }),
  };
  // the image model only when it is turned on: off, a 'model' query filters by tags
  const wasOn = settings.genderModel.enabled;
  settings.genderModel.enabled = true;
  out.genderArgs.model = ga({ select: ['only_male'], op: 'exclude', mode: 'model' });
  settings.genderModel.enabled = false;
  out.genderArgs.modelOff = ga({ select: ['only_male'], op: 'exclude', mode: 'model' });
  // -gender_verify_media: only with the model turned on and 'Tag + model' picked
  out.genderArgs.verifyOff = ga({ select: ['only_female'], mode: 'model', verifyMedia: true });
  settings.genderModel.enabled = true;
  out.genderArgs.verify = ga({ select: ['only_female'], mode: 'model', verifyMedia: true });
  out.genderArgs.verifyTags = ga({ select: ['only_female'], mode: 'tags', verifyMedia: true });
  out.genderArgs.verifyNo = ga({ select: ['only_female'], mode: 'model', verifyMedia: false });
  settings.genderModel.enabled = wasOn;
  out.genderArgs.ok = out.genderArgs.keep === '-gender only_female,both' && out.genderArgs.exclude === '-gender_exclude only_male -gender_conf high'
    && out.genderArgs.all === '' && out.genderArgs.model === `-gender_exclude only_male -gender_mode model -gender_model_url ${settings.genderModel.url}`
    && out.genderArgs.modelOff === '-gender_exclude only_male'
    && out.genderArgs.verify === `-gender only_female -gender_mode model -gender_model_url ${settings.genderModel.url} -gender_verify_media`
    && out.genderArgs.verifyOff === '-gender only_female' && out.genderArgs.verifyTags === '-gender only_female'
    && out.genderArgs.verifyNo === `-gender only_female -gender_mode model -gender_model_url ${settings.genderModel.url}`;
  // safety blacklist: its own option; the gender filter never implies it
  out.safetyArgs = {
    on: safetyArgs({ safetyBlacklist: true }).join(' '), off: safetyArgs({ safetyBlacklist: false }).join(' '), missing: safetyArgs({}).join(' '),
    genderOnly: [...genderArgs({ select: ['only_female'] }), ...safetyArgs({ gender: { select: ['only_female'] } })].join(' '),
  };
  out.safetyArgs.ok = out.safetyArgs.on === '-safety_blacklist' && out.safetyArgs.off === '' && out.safetyArgs.missing === ''
    && out.safetyArgs.genderOnly === '-gender only_female' && settings.download.safetyBlacklist !== undefined;
  // the media check box: hidden while 'Tag + model' cannot be picked, disabled with 'Chỉ dựa vào tag', saved in download.gender
  try {
    const savedGender = structuredClone(settings.download.gender);
    const ui = await js(`(() => {
      const was = { on: S.settings.genderModel.enabled, g: { ...genderState() } };
      const read = () => ({ hidden: $('#f-gender-verify-wrap').hidden, disabled: $('#f-gender-verify').disabled, checked: $('#f-gender-verify').checked });
      S.settings.genderModel.enabled = false; S.settings.download.gender = { ...was.g, mode: 'tags', verifyMedia: false }; renderGenderFilter();
      const off = read();
      S.settings.genderModel.enabled = true; renderGenderFilter();
      const tags = read();
      S.settings.download.gender = { ...was.g, mode: 'model', verifyMedia: false }; renderGenderFilter();
      const box = $('#f-gender-verify');
      box.checked = true;
      box.dispatchEvent(new Event('change'));
      const model = read();
      S.settings.genderModel.enabled = was.on; renderGenderFilter();
      return { off, tags, model };
    })()`);
    await new Promise((r) => setTimeout(r, 300)); // the renderer saves through settings:set
    ui.saved = settings.download.gender.verifyMedia === true && settings.download.gender.mode === 'model';
    settings.download.gender = savedGender;
    saveSettings();
    await js(`(() => { S.settings.download.gender = ${JSON.stringify(savedGender)}; renderGenderFilter(); })()`);
    out.genderVerifyUi = { ...ui, ok: ui.off.hidden && !ui.tags.hidden && ui.tags.disabled && !ui.model.hidden && !ui.model.disabled
      && ui.model.checked && ui.saved };
  } catch (e) {
    out.genderVerifyUi = { ok: false, error: String(e.message || e) };
  }
  // gender model server: status through main, the token never reaches the renderer, the core gets it as a secret argument
  try {
    const fakeToken = 'selftest-model-token-0123';
    const st = await js('window.lustra.genderModel.status()');
    const tok = await js(`window.lustra.genderModel.setToken(${JSON.stringify(fakeToken)})`);
    const view = JSON.stringify(await js('window.lustra.settings.get()'));
    const wasOn = settings.genderModel.enabled;
    settings.genderModel.enabled = false;
    const secretOff = (await genderSecretArgs({ mode: 'model' })).length === 0; // model off: the token is not handed out
    settings.genderModel.enabled = true;
    const secretArgs = (await genderSecretArgs({ mode: 'model' })).join(' ');
    settings.genderModel.enabled = wasOn;
    const blocked = async (m) => js(`window.lustra.rpc(${JSON.stringify(m)}, {url: 'http://127.0.0.1:1'}).then(() => 'allowed', (e) => 'blocked')`);
    const rpcBlocked = [await blocked('gender.modelStatus'), await blocked('gender.modelLoad'), await blocked('gender.modelUnload')];
    const cleared = await js('window.lustra.genderModel.clearToken()');
    // unload through main -> sidecar -> server (a no-op when nothing is loaded; load is the same path, not run: ~550 MB)
    const un = await js('window.lustra.genderModel.unload()');
    out.genderModel = {
      url: st.url, reachable: st.reachable, error: st.error, check: st.check?.code, unload: un.reachable ? un.model?.loaded === false : un.error,
      tokenSet: tok.set, rpcBlocked, cleared: !cleared.set,
      rendererHasNoToken: !view.includes(fakeToken), secretArgs: secretArgs === `-gender_model_token ${fakeToken}` && secretOff,
    };
    // a reachable server reports its hardware: the renderer warns before turning the model on without a GPU
    out.genderModel.ok = typeof st.reachable === 'boolean' && st.url === settings.genderModel.url && tok.set
      && rpcBlocked.every((x) => x === 'blocked') && (!st.reachable || (typeof st.check?.code === 'string' && un.model?.loaded === false))
      && out.genderModel.cleared && out.genderModel.rendererHasNoToken && out.genderModel.secretArgs;
  } catch (e) {
    out.genderModel = { ok: false, error: String(e.message || e) };
  }
  // library page: the renderer (file:// page) shows a downloaded image from disk; 'open' refuses non media files
  try {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lustra-selftest-lib-'));
    const img = path.join(dir, 'db_1 test.png');
    fs.copyFileSync(path.join(__dirname, 'assets', 'icons', 'full-mask-orange-64.png'), img);
    const launcher = path.join(dir, 'evil.desktop');
    fs.writeFileSync(launcher, '[Desktop Entry]\n');
    const loaded = await js(`new Promise((resolve) => { const i = new Image(); i.onload = () => resolve(i.naturalWidth); i.onerror = () => resolve(0);
      i.src = fileUrl(${JSON.stringify(img)}); })`);
    const refused = await js(`window.lustra.library.open(${JSON.stringify(launcher)})`);
    out.libraryFiles = { loaded, refused, ok: loaded === 64 && /chỉ mở được/.test(String(refused)) };
    fs.rmSync(dir, { recursive: true, force: true });
  } catch (e) {
    out.libraryFiles = { ok: false, error: String(e.message || e) };
  }
  // saved query schedule: set from the renderer, run fields owned by main survive a stale whole-list write (no run: self-test)
  try {
    const entry = { name: 'selftest-auto', module: 'db', tags: 'selftest_auto', at: Date.now() };
    const keep = settings.search.saved;
    settings.search.saved = [entry];
    const key = `${entry.module}\n${entry.tags}`;
    let r = await js(`window.lustra.saved.setAuto(${JSON.stringify(key)}, 24, 'all').then((x) => x.settings.search.saved, (e) => 'ERR ' + e.message)`);
    const on = Array.isArray(r) && r[0]?.auto?.every === 24;
    settings.search.saved[0].auto.lastRun = 123456;
    settings.search.saved[0].auto.maxId = 777;
    r = await js(`window.lustra.settings.set({ search: { saved: [{ name: 'selftest-auto', module: 'db', tags: 'selftest_auto', at: 1, auto: { every: 72 } }] } }).then((x) => x.settings.search.saved)`);
    const merged = r[0]?.auto?.every === 72 && r[0]?.auto?.lastRun === 123456 && r[0]?.auto?.maxId === 777;
    r = await js(`window.lustra.saved.setAuto(${JSON.stringify(key)}, 0, 'all').then((x) => x.settings.search.saved)`);
    const off = !r[0]?.auto;
    const bad = await js(`window.lustra.saved.setAuto(${JSON.stringify(key)}, 5, 'all').then(() => 'SET', (e) => e.message)`);
    out.savedAuto = { on, merged, off, bad, ok: on && merged && off && /chu kỳ/.test(bad) };
    settings.search.saved = keep;
  } catch (e) {
    out.savedAuto = { ok: false, error: String(e.message || e) };
  }
  // identical active job refused unless asked on purpose, 'run again' reuses the query (nothing is spawned: pump is off)
  const realPump = jobs.pump;
  const made = [];
  try {
    jobs.pump = () => {};
    const q = { module: 'db', tags: 'selftest_duplicate', path: path.join(os.tmpdir(), 'lustra-selftest-dup') };
    const first = await jobs.start(q);
    made.push(first.id);
    const dup = await jobs.start(q).then(() => 'ADDED', (e) => e.message);
    const forced = await jobs.start({ ...q, allowDuplicate: true });
    made.push(forced.id);
    const rerunDup = await jobs.rerun(first.id).then(() => 'ADDED', (e) => e.message);
    made.forEach((id) => jobs.cancel(id));
    const again = await jobs.rerun(first.id);
    made.push(again.id);
    out.duplicateJobs = { dup, forced: forced.status, rerunDup, rerun: again.tags === q.tags && again.status, hidden: !('query' in again) };
    out.duplicateJobs.ok = /^DUPLICATE/.test(dup) && forced.status === 'queued' && /^DUPLICATE/.test(rerunDup)
      && again.status === 'queued' && out.duplicateJobs.hidden;
  } catch (e) {
    out.duplicateJobs = { ok: false, error: String(e.message || e) };
  } finally {
    made.forEach((id) => { jobs.cancel(id); jobs.jobs.delete(id); });
    jobs.pump = realPump;
  }
  saveSettings();
  return out;
}

async function selfTestGenerator(js) {
  // extension generator: write-only key, a refused session (no network), no install without a ready draft and a confirmation
  const out = {};
  const realConfirm = generator.confirm;
  const waitIdle = async () => { for (let i = 0; i < 100 && generator.proc; i++) await new Promise((r) => setTimeout(r, 100)); };
  try {
    const fake = `sk-ant-selftest-${'k'.repeat(32)}`;
    out.setKey = await js(`window.lustra.gen.setKey(${JSON.stringify(fake)}).then((s) => s, (e) => 'rejected: ' + e.message)`);
    out.keyStored = encryptionAvailable ? Boolean(secrets[AI_SECRET]?.enc) : !secrets[AI_SECRET];
    out.keyNotPlain = !fs.readFileSync(settingsFile(), 'utf8').includes(fake);
    const status = JSON.stringify(await js('window.lustra.gen.status()')) + JSON.stringify(await js('window.lustra.settings.get()'));
    out.rendererHasNoKey = !status.includes(fake) && !(secrets[AI_SECRET]?.enc && status.includes(secrets[AI_SECRET].enc));
    out.refused = await js(`(async () => {
      const done = new Promise((resolve) => { const off = window.lustra.on('gen:event', (ev) => { if (ev.type === 'done') { off(); resolve(ev); } }); });
      await window.lustra.gen.start({ url: 'http://example.com/', mode: 'template', media: ['image'] });
      const ev = await Promise.race([done, new Promise((r) => setTimeout(() => r({ status: 'timeout' }), 60000))]);
      return { status: ev.status, ruleIds: ev.ruleIds, session: ev.session };
    })()`);
    await waitIdle();
    const refusedSession = out.refused.session;
    out.listed = (await listGenDrafts()).some((d) => d.session === refusedSession && d.status === 'refused');
    out.installNotReady = await js(`window.lustra.gen.install(${JSON.stringify(refusedSession)}).then(() => 'INSTALLED', (e) => e.message)`);
    out.badSession = await js('window.lustra.gen.get("../../etc").then(() => "READ", (e) => e.message)');
    await js('window.lustra.gen.clearKey()');
    out.aiWithoutKey = await js(`window.lustra.gen.start({ url: 'https://example.com/', mode: 'ai' }).then(() => 'STARTED', (e) => e.message)`);
    // domain denylist (lustra/safety_data/denied_domains.txt): refused before any request, with the Vietnamese reason
    out.denied = await js(`(async () => {
      const done = new Promise((resolve) => { const off = window.lustra.on('gen:event', (ev) => { if (ev.type === 'done') { off(); resolve(ev); } }); });
      await window.lustra.gen.start({ url: 'https://www.youtube.com/watch?v=1', mode: 'template', media: ['video'] });
      const ev = await Promise.race([done, new Promise((r) => setTimeout(() => r({ status: 'timeout' }), 60000))]);
      return { status: ev.status, ruleIds: ev.ruleIds, session: ev.session, summary: String(ev.summary || '').slice(0, 300) };
    })()`);
    await waitIdle();
    // signed remote catalog: the archive url / sha256 never come from the renderer (main reads the verified catalog)
    const realCatalogUrl = settings.advanced.catalogUrl;
    settings.advanced.catalogUrl = '';
    out.remoteBadId = await js(`window.lustra.ext.installRemote({ id: '../x' }).then(() => 'INSTALLED', (e) => e.message)`);
    out.remoteNoCatalog = await js(`window.lustra.ext.installRemote({ id: 'demo', url: 'https://evil.example/x.zip', sha256: '${'0'.repeat(64)}' })
      .then(() => 'INSTALLED', (e) => e.message)`);
    settings.advanced.catalogUrl = realCatalogUrl;
    out.remoteOk = /invalid extension id/.test(out.remoteBadId) && /catalog url must start with https/.test(out.remoteNoCatalog);
    if (!bundledCore()) {
      // a ready draft of the offline fake site of lustra/tests/test_extgen.py, then the confirmed install path of main
      const py = 'import pathlib, sys\nfrom lustra.tests.test_extgen import SITE, GenRequest, GenSession, fake_fetcher\n'
        + "r = GenSession(GenRequest(url=SITE, mode='template'), drafts_root=pathlib.Path(sys.argv[1]), fetcher=fake_fetcher()).run()\n"
        + "print('@@' + r['session'] + ' ' + r['status'] + ' ' + r['extId'])";
      const res = spawnSync(resolvePython(), ['-c', py, draftsRoot()], { cwd: REPO_ROOT, env: pythonEnv(), encoding: 'utf8', timeout: 180000 });
      const m = /@@(\S+) (\S+) (\S+)/.exec(res.stdout || '');
      out.readyDraft = m ? m[2] : `failed: ${redact(res.stderr || '').slice(-300)}`;
      if (m && m[2] === 'ready') {
        const [, session, , extId] = m;
        const asked = [];
        generator.confirm = async (msg) => { asked.push(msg); return false; };
        out.installDeclined = await js(`window.lustra.gen.install('${session}').then(() => 'INSTALLED', (e) => e.message)`);
        const before = await sidecar.call('ext.list', {});
        out.notInstalledYet = !before.some((x) => x.id === extId && x.installed && x.source !== 'catalog');
        generator.confirm = async (msg) => { asked.push(msg); return true; };
        out.installed = await js(`window.lustra.gen.install('${session}').then((r) => r && r.id, (e) => 'error: ' + e.message)`);
        out.asked = asked.length;
        out.inList = (await sidecar.call('ext.list', {})).some((x) => x.id === extId && x.installed);
        await sidecar.call('ext.uninstall', { id: extId }).catch(() => {});
        out.removed = await js(`window.lustra.gen.remove('${session}')`);
        out.removedGone = !fs.existsSync(genSessionDir(session));
      }
    }
    generator.confirm = async () => true;
    await js(`window.lustra.gen.remove(${JSON.stringify(refusedSession)})`);
    if (out.denied.session) await js(`window.lustra.gen.remove(${JSON.stringify(out.denied.session)})`);
    out.ok = out.keyNotPlain && out.rendererHasNoKey && out.keyStored && out.refused.status === 'refused'
      && (out.refused.ruleIds || []).includes('R10') && out.listed && /chỉ cài được/.test(out.installNotReady)
      && /invalid draft session/.test(out.badSession) && /Chưa có khoá/.test(out.aiWithoutKey)
      && out.denied.status === 'refused' && (out.denied.ruleIds || []).includes('R7') && /danh sách chặn/.test(out.denied.summary)
      && out.remoteOk
      && (bundledCore() ? true : out.readyDraft === 'ready' && /cancelled$/.test(out.installDeclined) && out.notInstalledYet
        && out.installed === 'fakebooru' && out.asked === 2 && out.inList && out.removed === true && out.removedGone);
  } catch (err) {
    out.error = String(err && err.stack || err);
  } finally {
    generator.confirm = realConfirm;
  }
  return out;
}

async function selfTestIndexTools(js, shot) {
  // download index tools: clean only through main (confirmation), import only from a dialog-picked folder (a dry run job runs the
  // real core command), multi-site groups (validation, forced options, a site waits for the previous one; nothing is spawned)
  const out = {};
  const blocked = async (m) => js(`window.lustra.rpc(${JSON.stringify(m)}, {}).then(() => 'allowed', (e) => 'blocked: ' + e.message)`);
  out.rpcBlocked = [await blocked('index.clean'), await blocked('index.cleanCancel')];
  const realConfirm = confirmIndexClean;
  const waitDone = `new Promise((resolve) => {
    const off = window.lustra.on('sidecar:notify', ({ method, params }) => { if (method === 'index.cleanDone') { off(); resolve(params); } });
    setTimeout(() => { off(); resolve({ timeout: true }); }, 20000);
  })`;
  try {
    let asked = 0;
    confirmIndexClean = async () => { asked += 1; return { ok: false }; };
    out.cleanDeclined = await js(`window.lustra.index.clean().then(() => 'STARTED', (e) => e.message)`);
    out.cleanBadOptions = await js(`window.lustra.index.clean('x').then(() => 'STARTED', (e) => e.message)`);
    const askedDeclined = asked;
    out.cleanDryRun = await js(`(async () => { const done = ${waitDone}; const r = await window.lustra.index.clean({ dryRun: true }); return { r, done: await done }; })()`);
    confirmIndexClean = async () => { asked += 1; return { ok: true, missingFolders: false }; };
    out.clean = await js(`(async () => { const done = ${waitDone}; const r = await window.lustra.index.clean(); return { r, done: await done }; })()`);
    out.cleanAsked = asked;
    out.cleanOk = /cancelled/.test(out.cleanDeclined) && /invalid options/.test(out.cleanBadOptions) && askedDeclined === 1 && asked === 2
      && out.cleanDryRun.done?.ok === true && out.cleanDryRun.done?.dryRun === true && out.clean.done?.ok === true
      && out.clean.done?.dryRun === false && typeof out.clean.done?.removed === 'number';
  } catch (e) {
    out.cleanOk = false;
    out.cleanError = String(e.message || e);
  } finally {
    confirmIndexClean = realConfirm;
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lustra-selftest-import-'));
  try {
    for (const name of ['db_1.jpg', 'rx_2.png', 'IMG_3.jpg', 'notes.txt']) fs.writeFileSync(path.join(dir, name), 'x');
    out.importUnpicked = await js(`window.lustra.index.importLibrary({ folder: ${JSON.stringify(dir)} }).then(() => 'STARTED', (e) => e.message)`);
    approvedImportFolders.add(dir); // what index:pickFolder does after the native dialog
    out.importBadModule = await js(`window.lustra.index.importLibrary({ folder: ${JSON.stringify(dir)}, module: 'DB!' }).then(() => 'STARTED', (e) => e.message)`);
    const job = await js(`window.lustra.index.importLibrary({ folder: ${JSON.stringify(dir)}, dryRun: true })`);
    let j = jobs.jobs.get(job.id);
    for (let i = 0; i < 120 && j && !['done', 'failed', 'cancelled'].includes(j.status); i++) {
      await new Promise((r) => setTimeout(r, 250));
      j = jobs.jobs.get(job.id);
    }
    const p = j?.progress || {};
    out.importJob = { kind: job.kind, publicHidesSecrets: !('moduleSecretArgs' in job) && !('importQuery' in job), status: j?.status,
      exit: j?.exitCode, scanned: p.scanned, totalAll: p.totalAll, unknown: p.unknown, unknownNames: p.unknownNames, error: j?.error };
    // the job card: import texts, the lists of names (renderer)
    await js(`document.querySelector('[data-page="downloads"]').click()`);
    out.importCard = await js(`(async () => {
      for (let i = 0; i < 20 && !document.querySelector('.job[data-id="${job.id}"]'); i++) await new Promise((r) => setTimeout(r, 100));
      const card = document.querySelector('.job[data-id="${job.id}"]');
      if (!card) return { card: false };
      const lists = [...card.querySelectorAll('.job-actions .btn')].find((b) => b.textContent === 'Danh sách');
      const text = card.textContent;
      if (lists && !lists.hidden) lists.click();
      await new Promise((r) => setTimeout(r, 200));
      const modal = document.querySelector('#modal-card').textContent;
      return { card: true, text, listsShown: Boolean(lists && !lists.hidden), modal };
    })()`);
    await shot('24-import-job-lists');
    await js('closeModal()');
    out.importCardOk = out.importCard.card && /Nhập thư viện/.test(out.importCard.text) && /2 file sẽ được tra/.test(out.importCard.text)
      && out.importCard.listsShown && /Không nhận ra tên \(1\)/.test(out.importCard.modal) && /IMG_3\.jpg/.test(out.importCard.modal);
    out.importOk = /folder dialog/.test(out.importUnpicked) && /invalid module/.test(out.importBadModule) && job.kind === 'import'
      && out.importJob.publicHidesSecrets && j?.status === 'done' && p.scanned === 3 && p.totalAll === 2 && p.unknown === 1 && out.importCardOk;
    jobs.jobs.delete(job.id);
  } catch (e) {
    out.importOk = false;
    out.importError = String(e.message || e);
  } finally {
    approvedImportFolders.delete(dir);
    fs.rmSync(dir, { recursive: true, force: true });
  }
  const realPump = jobs.pump;
  const made = [];
  try {
    jobs.pump = () => {};
    const q = { module: 'db', tags: 'selftest_multi', path: path.join(os.tmpdir(), 'lustra-selftest-multi'), options: { skipKnown: false } };
    const tryMulti = (mods) => js(`window.lustra.jobs.startMulti(${JSON.stringify(q)}, ${JSON.stringify(mods)}).then((r) => 'ADDED ' + r.length, (e) => e.message)`);
    out.multiRefused = [await tryMulti([]), await tryMulti(['db']), await tryMulti(['rx', 'BAD!']), await tryMulti(Array.from({ length: 9 }, (_, i) => `m${i}`))];
    const list = await js(`window.lustra.jobs.startMulti(${JSON.stringify(q)}, ['rx', 'db', 'rx'])`);
    made.push(...list.map((x) => x.id));
    const [a, b] = list.map((x) => jobs.jobs.get(x.id));
    const waitsBefore = jobs._waiting(b);
    jobs.cancel(a.id);
    const waitsAfter = jobs._waiting(b);
    const forged = await js(`window.lustra.jobs.start({ ...${JSON.stringify(q)}, tags: 'selftest_multi_forged', group: { id: 'x', index: 9 } })`);
    made.push(forged.id);
    out.multi = {
      modules: list.map((x) => x.module), sameGroup: list[0].group?.id === list[1].group?.id && list[1].group?.index === 1,
      forcedArgs: [a, b].every((x) => x.args.includes('-skip_known') && x.args.includes('-prefix')), waitsBefore, waitsAfter,
      forgedGroup: forged.group, rerunAlone: (await jobs.rerun(a.id, true)).group,
    };
    made.push(...jobs.list().filter((x) => x.tags === 'selftest_multi' && !made.includes(x.id)).map((x) => x.id));
    out.multiOk = out.multiRefused.every((x) => /module khác nhau/.test(x)) && out.multi.modules.join() === 'db,rx' && out.multi.sameGroup
      && out.multi.forcedArgs && waitsBefore === true && waitsAfter === false && forged.group === null && out.multi.rerunAlone === null;
  } catch (e) {
    out.multiOk = false;
    out.multiError = String(e.message || e);
  } finally {
    made.forEach((id) => { jobs.cancel(id); jobs.jobs.delete(id); });
    jobs.pump = realPump;
    await js(`document.querySelector('#jobs-clear').click()`).catch(() => {}); // the renderer drops the jobs main no longer has
  }
  out.ui = await selfTestIndexUi(js, shot);
  out.ok = out.rpcBlocked.every((x) => /^blocked: .*not allowed/.test(x)) && out.cleanOk && out.importOk && out.multiOk && out.ui.ok;
  return out;
}

async function selfTestIndexUi(js, shot) {
  // renderer side of the index tools: settings controls (a dry run fills the status line), the import dialog, the multi-site picker
  // and a multi-site group box in the job list (nothing is spawned: pump is off)
  const out = {};
  const pause = (ms) => new Promise((r) => setTimeout(r, ms));
  try {
    await js(`document.querySelector('[data-page="settings"]').click()`);
    await pause(300);
    out.dryRun = await js(`(async () => {
      document.querySelector('#index-clean-check').click();
      const st = document.querySelector('#index-clean-status');
      for (let i = 0; i < 60 && !(!st.hidden && /Đã kiểm/.test(st.textContent)); i++) await new Promise((r) => setTimeout(r, 250));
      st.scrollIntoView({ block: 'center' });
      return { text: st.textContent, buttons: ['#index-clean', '#index-clean-check', '#index-import'].every((s) => document.querySelector(s)) };
    })()`);
    await pause(300);
    await shot('20-index-settings');
    out.importDialog = await js(`(async () => {
      document.querySelector('#index-import').click();
      await new Promise((r) => setTimeout(r, 300));
      const m = document.querySelector('.import-modal');
      return { open: Boolean(m), modules: m ? m.querySelectorAll('select option').length : 0, startDisabled: m ? m.querySelector('.btn.primary').disabled : null };
    })()`);
    await shot('21-import-dialog');
    await js('closeModal()');
    out.multi = await js(`(async () => {
      document.querySelector('[data-page="search"]').click();
      document.querySelector('#filters').open = true;
      const pills = [...document.querySelectorAll('#f-multi-pills .pill')];
      pills[0]?.click();
      await new Promise((r) => setTimeout(r, 150));
      document.querySelector('#f-multi').scrollIntoView({ block: 'center' });
      return { visible: !document.querySelector('#f-multi').hidden, pills: pills.length, label: document.querySelector('#btn-download').textContent,
        warn: !document.querySelector('#f-multi-warn').hidden, summary: document.querySelector('#filters-sum').textContent };
    })()`);
    await pause(200);
    await shot('22-multi-site');
    out.multiOff = await js(`(async () => { document.querySelector('#f-multi-pills .pill.on')?.click(); await new Promise((r) => setTimeout(r, 50));
      return document.querySelector('#btn-download').textContent; })()`);
  } catch (e) {
    out.error = String(e.message || e);
  }
  const realPump = jobs.pump;
  let made = [];
  try {
    jobs.pump = () => {};
    const list = await js(`window.lustra.jobs.startMulti({ module: 'db', tags: 'selftest_group_ui', path: ${JSON.stringify(path.join(os.tmpdir(), 'lustra-selftest-group'))} }, ['rx'])`);
    made = list.map((x) => x.id);
    jobs.jobs.get(made[0]).progress = { stateName: 'DOWNLOADING', totalAll: 10, processed: 4, success: 4 };
    jobs.emit(jobs.jobs.get(made[0]));
    await js(`document.querySelector('[data-page="downloads"]').click()`);
    out.group = await js(`(async () => {
      const find = () => [...document.querySelectorAll('.job-group')].find((x) => x.textContent.includes('selftest_group_ui'));
      for (let i = 0; i < 20 && !find(); i++) await new Promise((r) => setTimeout(r, 100));
      await new Promise((r) => setTimeout(r, 100));
      const g = find();
      return g ? { cards: g.querySelectorAll('.job').length, head: g.querySelector('.job-group-head').textContent,
        order: [...g.querySelectorAll('.job')].map((c) => c.textContent.includes('site 1/2') ? 1 : c.textContent.includes('site 2/2') ? 2 : 0),
        waiting: g.textContent.includes('Chờ site trước tải xong') } : null;
    })()`);
    await pause(200);
    await shot('23-job-group');
  } catch (e) {
    out.groupError = String(e.message || e);
  } finally {
    made.forEach((id) => { jobs.cancel(id); });
    jobs.pump = realPump;
    await js(`document.querySelector('#jobs-clear').click()`).catch(() => {});
    await pause(200);
    out.groupRemoved = await js(`![...document.querySelectorAll('.job-group')].some((x) => x.textContent.includes('selftest_group_ui'))`).catch(() => null);
  }
  out.ok = Boolean(out.dryRun?.buttons && /Đã kiểm/.test(out.dryRun?.text) && out.importDialog?.open && out.importDialog.modules >= 3
    && out.importDialog.startDisabled === true && out.multi?.visible && out.multi.pills >= 1 && /\(2 site\)/.test(out.multi.label) && out.multi.warn
    && /\+1 site/.test(out.multi.summary) && out.multiOff === 'Tải xuống' && out.group?.cards === 2 && out.group.order.join() === '1,2'
    && out.group.waiting && /db → rx/.test(out.group.head) && out.groupRemoved === true);
  return out;
}

async function selfTestQuitGuard(dir) {
  // quit / relaunch with jobs: stand-in processes (Electron as node) for a job that honours 'cancel' and one that ignores it
  const graceful = "process.stdin.on('data', (d) => { if (String(d).includes('cancel')) process.exit(130); }); setInterval(() => {}, 1000);";
  const stubborn = "process.on('SIGTERM', () => {}); process.stdin.on('data', () => {}); setInterval(() => {}, 1000);";
  const make = (name, script) => ({
    id: `selftest-${name}-${Date.now()}`, module: 'zz', tags: name, dest: path.join(dir, 'quit'), status: 'queued', progress: {},
    createdAt: Date.now(), startedAt: 0, endedAt: 0, exitCode: null, logTail: [], label: 'selftest', secretArgs: [], args: [],
    testCommand: { cmd: process.execPath, args: ['-e', script], env: { ELECTRON_RUN_AS_NODE: '1' } },
  });
  const maxJobs = settings.download.maxConcurrentJobs;
  const realAsk = askQuit;
  const asked = [];
  const out = {};
  let ids = [];
  try {
    settings.download.maxConcurrentJobs = 2;
    ids = [make('graceful', graceful), make('stubborn', stubborn), make('queued', graceful)].map((j) => jobs._add(j).id);
    await new Promise((r) => setTimeout(r, 1000));
    out.needsConfirm = jobsNeedingConfirm().length;
    askQuit = async (message, detail) => { asked.push(`${message} ${detail}`); return false; };
    out.declined = await requestQuit(true);
    out.asked = asked;
    out.afterDecline = ids.map((id) => jobs.jobs.get(id).status);
    const t0 = Date.now();
    out.shutdown = await jobs.shutdown(1500);
    out.shutdownMs = Date.now() - t0;
    await new Promise((r) => setTimeout(r, 300)); // 'close' events
    out.final = ids.map((id) => { const j = jobs.jobs.get(id); return `${j.status}:${j.exitCode}`; });
    out.ok = out.needsConfirm === 3 && out.declined === false && asked.length === 1
      && out.afterDecline.join() === 'running,running,queued' && out.shutdown.stopped === 2 && out.shutdown.killed === 1
      && out.final.join() === 'cancelled:130,cancelled:130,cancelled:null';
  } finally {
    askQuit = realAsk;
    jobs.closing = false;
    settings.download.maxConcurrentJobs = maxJobs;
    for (const id of ids) jobs.jobs.delete(id);
  }
  return out;
}

protocol.registerSchemesAsPrivileged([
  { scheme: 'lustra-thumb', privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true } },
]);

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) { if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.focus(); }
  });
  app.whenReady().then(async () => {
    loadSettings();
    await initEncryption();
    registerThumbProtocol();
    registerIpc();
    registerIndexIpc();
    await applyProxyToSession();
    sidecar.start();
    setupMenu();
    createWindow();
    syncIcon({ startup: true }).catch(() => {}); // macOS dock, launcher / shortcuts written by an older version or another icon
    pruneThumbCache();
    if (process.env.LUSTRA_SELFTEST_DIR) mainWindow.webContents.once('did-finish-load', () => runSelfTest(process.env.LUSTRA_SELFTEST_DIR));
    sidecar.ready.then(() => maybeAutoUpdateTags()).catch(() => {});
    if (!process.env.LUSTRA_SELFTEST_DIR) refreshTagSchedule(); // a self-test never touches the user's crontab
    sidecar.ready.then(() => {
      setTimeout(() => runDueSaved().catch(() => {}), 60000); // saved queries due while the app was closed
      setInterval(() => runDueSaved().catch(() => {}), AUTO_CHECK_MS);
    }).catch(() => {});
  });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', (e) => {
    // Cmd+Q, the macOS app menu, OS logout, app.quit(): jobs still queued / running need the user's confirmation first
    if (!quitApproved && jobs.active().length) {
      e.preventDefault();
      requestQuit(false);
      return;
    }
    quitApproved = true;
    jobs.killAll();
    generator.kill(); // an unfinished generation leaves its draft folder and report behind
    sidecar.stop();
  });
}
