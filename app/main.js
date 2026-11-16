// Lustra desktop: Electron main process
// - spawns the Python sidecar (python -m lustra.server, JSON-RPC over stdio) for queries
// - runs downloads as separate CLI processes (LUSTRA_IPC=1 python -m lustra ...) with progress on stdout
// - owns settings (userData/settings.json), secrets (safeStorage), thumbnails cache (lustra-thumb://), appearance
'use strict';

const {
  app, BrowserWindow, ipcMain, dialog, shell, nativeImage, protocol, net, safeStorage, session, Menu,
} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const fsp = fs.promises;
const os = require('node:os');
const crypto = require('node:crypto');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const netlib = require('node:net');
const dnsp = require('node:dns').promises;

app.setName('Lustra');
if (process.env.LUSTRA_USER_DATA) app.setPath('userData', path.resolve(process.env.LUSTRA_USER_DATA));
if (process.platform === 'linux' && typeof app.setDesktopName === 'function') app.setDesktopName('lustra.desktop');

const REPO_ROOT = path.resolve(__dirname, '..');
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

const DEFAULT_SETTINGS = {
  version: 3,
  appearance: {
    theme: 'ember',
    icon: 'full-mask-orange',
    background: { mode: 'icon', image: '', blur: 4, opacity: 0.9, dim: 0.18 },
  },
  search: { module: 'rx', autocomplete: true, previewOnSearch: true, safePreview: false },
  download: {
    path: path.join(os.homedir(), 'Downloads', 'Lustra'), threads: 4, images: true, videos: true, videoFormat: '',
    gender: { select: [], conf: 'medium', strict: false },
    lowRes: false, prefix: true, appendInfo: false, reverse: false, limit: 0, parchi: false, preserveDate: false,
    subfolderPerQuery: false, dumpTags: false, dumpSources: false, dumpComments: false, dumpHashes: false, infoMode: 'run',
    warnNonEmpty: true, maxConcurrentJobs: 1,
  },
  connection: {
    proxy: '', ignoreProxy: false, proxyNoDownload: false, timeout: 0, retries: 0, headers: {}, cookies: {},
  },
  tags: { autoUpdate: false, autoUpdateDays: 7, lastAutoUpdate: {} },
  advanced: { pythonPath: '', verbose: false, catalogUrl: '' },
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
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, file);
  try { fs.chmodSync(file, 0o600); } catch { /* not supported on this filesystem */ }
}

// ---------- security helpers ----------

const RE_EXT_ID = /^[a-z][a-z0-9_-]{1,31}$/;
const RE_SHA256 = /^[0-9a-fA-F]{64}$/;
const approvedPaths = new Set(); // files picked by the user through native dialogs (renderer cannot invent paths)

function redact(text) {
  return String(text)
    .replace(/([?&](?:api_key|login|user_id)=)[^&\s"]+/g, '$1<REDACTED>')
    .replace(/(-api_key|-cookies|-headers|-proxy)(\s+|=)(\S+)/g, '$1$2<REDACTED>');
}

async function confirmMain(message, detail) {
  const r = await dialog.showMessageBox(mainWindow, {
    type: 'warning', buttons: ['Huỷ', 'Đồng ý'], defaultId: 0, cancelId: 0, noLink: true, message, detail,
  });
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

function loadSettings() {
  try {
    const raw = JSON.parse(fs.readFileSync(settingsFile(), 'utf8'));
    secrets = raw.secrets || {};
    delete raw.secrets;
    settings = deepMerge(DEFAULT_SETTINGS, raw);
  } catch {
    settings = structuredClone(DEFAULT_SETTINGS);
  }
  if (!THEMES.includes(settings.appearance.theme)) settings.appearance.theme = 'ember';
  // v1/v2 -> v3: earlier default backgrounds were too blurred, the icon outline must stay visible
  const bg = settings.appearance.background;
  const oldDefaults = [[48, 0.32, 0.55], [14, 0.9, 0.18]];
  if ((settings.version || 1) < 3 && oldDefaults.some(([b, o, d]) => bg.blur === b && bg.opacity === o && bg.dim === d)) {
    Object.assign(bg, { blur: 4, opacity: 0.9, dim: 0.18 });
  }
  settings.version = 3;
  if (!ICONS.some((i) => i.id === settings.appearance.icon)) settings.appearance.icon = ICONS[0].id;
}

function saveSettings() {
  writeJsonAtomic(settingsFile(), { ...settings, secrets });
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

function pythonEnv(extra = {}) {
  const sep = process.platform === 'win32' ? ';' : ':';
  return {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    LUSTRA_DATA_DIR: userData(),
    PYTHONPATH: [REPO_ROOT, process.env.PYTHONPATH].filter(Boolean).join(sep),
    ...extra,
  };
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
  if (!value) { delete secrets[module]; saveSettings(); return; }
  if (encryptionAvailable) secrets[module] = { enc: (await safeStorage.encryptStringAsync(value)).toString('base64') };
  else secrets[module] = { plain: value }; // no OS keyring available: stored as plain text (reported to UI)
  saveSettings();
}

async function getSecret(module) {
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
    const py = resolvePython();
    this.ready = new Promise((resolve, reject) => {
      this._resolveReady = resolve;
      this._rejectReady = reject;
    });
    this.proc = spawn(py, ['-m', 'lustra.server'], { cwd: REPO_ROOT, env: pythonEnv(), stdio: ['pipe', 'pipe', 'pipe'] });
    this.proc.on('error', (err) => {
      send('sidecar:status', { ok: false, error: `Không chạy được Python (${py}): ${err.message}` });
      this._rejectReady(err);
    });
    readline.createInterface({ input: this.proc.stdout }).on('line', (line) => this._onLine(line));
    readline.createInterface({ input: this.proc.stderr }).on('line', (line) => send('log', { source: 'sidecar', text: redact(line) }));
    this.proc.on('exit', (code) => {
      for (const { reject, timer } of this.pending.values()) { clearTimeout(timer); reject(new Error('sidecar exited')); }
      this.pending.clear();
      send('sidecar:status', { ok: false, error: `sidecar exited (${code})` });
      if (!this.stopping && this.restarts < 5) {
        this.restarts += 1;
        setTimeout(() => this.start(), 1000 * this.restarts);
      }
    });
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
      send('sidecar:status', { ok: true, info: msg.params });
      this._resolveReady(msg.params);
    } else if (msg.method === 'log') {
      send('log', { source: msg.params.scope || 'core', text: msg.params.text });
    } else if (msg.method) {
      send('sidecar:notify', { method: msg.method, params: msg.params });
    }
  }

  async call(method, params = {}, timeoutMs = 120000) {
    await this.ready;
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`timeout: ${method}`));
      }, timeoutMs);
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
  if (c.headers && Object.keys(c.headers).length) a.push('-headers', JSON.stringify(c.headers));
  if (c.cookies && Object.keys(c.cookies).length) a.push('-cookies', JSON.stringify(c.cookies));
  const key = await getSecret(module);
  if (key) a.push('-api_key', key);
  if (settings.advanced.verbose) a.push('-verbose');
  return a;
}

function genderArgs(g) {
  // content filter; enabling it also enables the safety blacklist in the core
  const sel = (g && Array.isArray(g.select) ? g.select : []).filter((x) => ['only_female', 'only_male', 'both', 'none', 'unknown'].includes(x));
  if (!sel.length) return [];
  const a = ['-gender', sel.join(',')];
  if (['low', 'medium', 'high'].includes(g.conf)) a.push('-gender_conf', g.conf);
  if (g.strict) a.push('-gender_strict');
  return a;
}

function searchArgs(q) {
  const a = [];
  if (q.dateMin) a.push('-mindate', toCliDate(q.dateMin));
  if (q.dateMax) a.push('-maxdate', toCliDate(q.dateMax));
  return a;
}

function sanitizeFolderName(s) {
  return String(s).replace(/[<>:"/\\|?*\x00-\x1f]+/g, '_').replace(/\s+/g, ' ').trim().slice(0, 60) || 'query';
}

function downloadArgs(q) {
  const d = { ...settings.download, ...(q.options || {}) };
  let dest = q.path || d.path;
  if (d.subfolderPerQuery && !q.path) dest = path.join(dest, `${q.module}_${sanitizeFolderName(q.tags)}`);
  const a = ['-path', dest];
  if (!d.images) a.push('-skip_img');
  if (!d.videos) a.push('-skip_vid');
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
  if (d.dumpTags) a.push('-dump_tags');
  if (d.dumpSources) a.push('-dump_sources');
  if (d.dumpComments) a.push('-dump_comments');
  if (d.dumpHashes) a.push('-dump_hashes');
  if (d.infoMode === 'item') a.push('-dump_per_item');
  if (d.infoMode === 'merge') a.push('-merge_lists');
  return { args: a, dest };
}

// ---------- download jobs (separate CLI processes) ----------

class Jobs {
  constructor() {
    this.jobs = new Map();
    this.seq = 0;
  }

  public(job) {
    const { proc, logTail, args, secretArgs, ...rest } = job;
    return { ...rest, logTail: logTail.slice(-40) };
  }

  emit(job) {
    send('jobs:update', this.public(job));
  }

  async start(q) {
    if (!q || !q.module || !String(q.tags || '').trim()) throw new Error('module and tags are required');
    const id = `job-${Date.now()}-${++this.seq}`;
    const { args, dest } = downloadArgs(q);
    const job = {
      id, module: q.module, tags: String(q.tags).trim(), dest, status: 'queued', progress: {}, createdAt: Date.now(),
      startedAt: 0, endedAt: 0, exitCode: null, logTail: [], label: q.label || '',
      secretArgs: await connectionArgs(q.module),
      args: ['-module', q.module, ...searchArgs(q), ...genderArgs(q.gender), ...args,
        ...String(q.tags).trim().split(/\s+/)],
    };
    this.jobs.set(id, job);
    this.emit(job);
    this.pump();
    return this.public(job);
  }

  running() {
    return [...this.jobs.values()].filter((j) => j.status === 'running').length;
  }

  pump() {
    const max = Math.max(1, settings.download.maxConcurrentJobs || 1);
    for (const job of this.jobs.values()) {
      if (this.running() >= max) break;
      if (job.status === 'queued') this._spawn(job);
    }
  }

  _spawn(job) {
    fs.mkdirSync(job.dest, { recursive: true });
    job.status = 'running';
    job.startedAt = Date.now();
    const proc = spawn(resolvePython(), ['-m', 'lustra', ...job.args], {
      cwd: REPO_ROOT, env: pythonEnv({ LUSTRA_IPC: '1', LUSTRA_SECRET_ARGS: JSON.stringify(job.secretArgs || []) }), stdio: ['pipe', 'pipe', 'pipe'],
    });
    job.proc = proc;
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
    proc.on('error', (err) => onLine(`ERROR: ${err.message}`));
    proc.on('exit', (code) => {
      job.exitCode = code;
      job.endedAt = Date.now();
      if (job.status === 'cancelling') job.status = 'cancelled';
      else job.status = code === 0 && !(job.progress.failed > 0 && !job.progress.success) ? 'done' : 'failed';
      delete job.proc;
      this.emit(job);
      this.pump();
    });
    this.emit(job);
  }

  cancel(id) {
    const job = this.jobs.get(id);
    if (!job) return false;
    if (job.status === 'queued') {
      job.status = 'cancelled';
      this.emit(job);
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

  killAll() {
    for (const j of this.jobs.values()) if (j.proc) { try { j.proc.stdin.write('cancel\n'); } catch { /* ignore */ } }
  }
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

function registerThumbProtocol() {
  protocol.handle('lustra-thumb', async (request) => {
    const u = new URL(request.url);
    const target = u.searchParams.get('u') || '';
    const referer = u.searchParams.get('r') || '';
    if (!/^https?:\/\//i.test(target)) return new Response('bad url', { status: 400 });
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
    liveTaskbarIcon: !(process.platform === 'linux' && (process.env.WAYLAND_DISPLAY || process.env.XDG_SESSION_TYPE === 'wayland')),
  };
}

function applyWindowIcon() {
  const img = nativeImage.createFromPath(iconPath(settings.appearance.icon));
  if (img.isEmpty()) return;
  if (process.platform === 'darwin') app.dock?.setIcon(img);
  else mainWindow?.setIcon(img);
}

function desktopEntryPath() {
  return path.join(os.homedir(), '.local', 'share', 'applications', 'lustra.desktop');
}

async function installLauncher() {
  if (process.platform !== 'linux') return { ok: false, message: 'Chỉ cần trên Linux' };
  const iconDest = path.join(userData(), 'icons', 'lustra.png');
  await fsp.mkdir(path.dirname(iconDest), { recursive: true });
  await fsp.copyFile(iconPath(settings.appearance.icon), iconDest);
  const exec = app.isPackaged ? `"${process.execPath}"` : `"${process.execPath}" "${__dirname}"`;
  const entry = [
    '[Desktop Entry]', 'Type=Application', 'Name=Lustra', 'Comment=Booru downloader', `Exec=${exec} %U`,
    `Icon=${iconDest}`, 'Terminal=false', 'Categories=Network;Graphics;', 'StartupWMClass=Lustra', '',
  ].join('\n');
  await fsp.mkdir(path.dirname(desktopEntryPath()), { recursive: true });
  await fsp.writeFile(desktopEntryPath(), entry);
  return { ok: true, path: desktopEntryPath() };
}

// ---------- tag lists auto update ----------

async function maybeAutoUpdateTags() {
  if (!settings.tags.autoUpdate) return;
  const days = Math.max(1, settings.tags.autoUpdateDays || 7) * 86400000;
  for (const module of ['rx', 'db']) {
    const last = settings.tags.lastAutoUpdate[module] || 0;
    if (Date.now() - last < days) continue;
    try {
      await sidecar.call('tags.update', { module, apiKey: await getSecret(module) });
      settings.tags.lastAutoUpdate[module] = Date.now();
      saveSettings();
    } catch (err) {
      send('log', { source: 'tags', text: `auto update ${module} failed: ${err.message}` });
    }
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
  handle('rpc', async (_e, method, params) => {
    // installs never go through the generic rpc channel (see ext:* handlers): they need main-side checks and confirmation
    const allowed = ['app.info', 'ext.list', 'ext.uninstall', 'ext.remoteCatalog', 'tags.autocomplete', 'tags.browse',
      'tags.validate', 'tags.status', 'files.nonEmpty'];
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
  handle('ext:installRemote', async (_e, item) => {
    const url = String(item?.url || '');
    const sha256 = String(item?.sha256 || '');
    if (!/^https:\/\//i.test(url) || !RE_SHA256.test(sha256)) throw new Error('remote extensions need an https url and a sha256');
    const ok = await confirmMain(`Cài extension "${String(item?.name || item?.id || '?').slice(0, 80)}"?`,
      `Extension là code Python sẽ chạy trên máy bạn với quyền của bạn.\nNguồn: ${url}\nChỉ cài từ nguồn bạn tin cậy.`);
    if (!ok) throw new Error('cancelled');
    return sidecar.call('ext.install', { url, sha256 });
  });
  handle('tags:update', async (_e, module) => sidecar.call('tags.update', { module, apiKey: await getSecret(module) }));
  handle('search:count', async (_e, q) => sidecar.call('search.count', {
    module: q.module, tags: q.tags, args: [...(await connectionArgs(q.module)), ...searchArgs(q)],
  }));
  handle('search:preview', async (_e, q, page) => sidecar.call('search.preview', {
    module: q.module, tags: q.tags, page: page || 0, args: [...(await connectionArgs(q.module)), ...searchArgs(q), ...genderArgs(q.gender)],
  }, 180000));

  handle('settings:get', () => ({ settings, appearance: appearanceState(), secrets: secretsStatus() }));
  handle('settings:set', async (_e, patch) => {
    if (!patch || typeof patch !== 'object') throw new Error('invalid settings patch');
    if (patch.secrets) delete patch.secrets;
    const py = patch.advanced?.pythonPath;
    if (py !== undefined && py !== settings.advanced.pythonPath) {
      // the python path is executed by the app: changing it requires a native confirmation
      const valid = py === '' || (path.isAbsolute(py) && fs.existsSync(py) && fs.statSync(py).isFile());
      if (!valid || (py && !(await confirmMain('Dùng chương trình này làm Python cho Lustra?', py)))) delete patch.advanced.pythonPath;
    }
    settings = deepMerge(settings, patch || {});
    saveSettings();
    if (patch?.connection) await applyProxyToSession();
    if (patch?.appearance) {
      if (patch.appearance.icon) applyWindowIcon();
      send('appearance:changed', appearanceState());
    }
    return { settings, appearance: appearanceState(), secrets: secretsStatus() };
  });
  handle('secrets:set', async (_e, module, value) => { await setSecret(module, String(value || '').trim()); return secretsStatus(); });

  handle('jobs:start', (_e, q) => jobs.start(q));
  handle('jobs:cancel', (_e, id) => jobs.cancel(id));
  handle('jobs:list', () => jobs.list());
  handle('jobs:clear', () => jobs.clearFinished());

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

  handle('app:relaunch', () => { app.relaunch(); app.quit(); });
  handle('app:info', () => ({
    version: app.getVersion(), electron: process.versions.electron, platform: process.platform, userData: userData(),
    python: resolvePython(), repo: REPO_ROOT, sidecar: sidecar.info, encryption: encryptionAvailable,
    launcherInstalled: fs.existsSync(desktopEntryPath()),
  }));
  handle('launcher:install', () => installLauncher());
  handle('shell:open', (_e, p) => {
    // folders only (download destinations): files could be executables or .desktop launchers
    const target = String(p || '');
    if (!path.isAbsolute(target) || !fs.existsSync(target) || !fs.statSync(target).isDirectory()) return 'not a folder';
    return shell.openPath(target);
  });
  handle('shell:external', (_e, url) => (/^https?:\/\//.test(url) ? shell.openExternal(url) : false));
}

function secretsStatus() {
  return {
    encryption: encryptionAvailable,
    modules: Object.fromEntries(Object.entries(secrets).map(([k, v]) => [k, { set: true, encrypted: Boolean(v.enc) }])),
  };
}

// ---------- window ----------

function createWindow() {
  mainWindow = new BrowserWindow({
    width: settings.window.width, height: settings.window.height, minWidth: 1080, minHeight: 680,
    title: 'Lustra', icon: iconPath(settings.appearance.icon), backgroundColor: '#0d0b0a', show: false,
    autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  Menu.setApplicationMenu(null);
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('close', () => {
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
  const report = {};
  try {
    await sidecar.ready;
    await wait(1500);
    await shot('01-search-empty');
    report.preview = await js(`(async () => {
      document.querySelector('#module-btn').click();
      [...document.querySelectorAll('#module-menu button')].find((b) => b.textContent.includes('Danbooru'))?.click();
      const inp = document.querySelector('#tag-text');
      for (const t of ${JSON.stringify((process.env.LUSTRA_SELFTEST_TAGS || 'scenery sky cloud rating:g').split(' '))}) {
        inp.value = t + ' '; inp.dispatchEvent(new Event('input', { bubbles: true }));
      }
      inp.value = 'sun'; inp.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise((r) => setTimeout(r, 900));
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
      await wait(800);
      await shot('09-downloads');
    }
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
    report.sidecar = sidecar.info;
  } catch (err) {
    report.error = String(err && err.stack || err);
  }
  await fsp.writeFile(path.join(dir, 'report.json'), JSON.stringify(report, null, 2));
  app.quit();
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
    await applyProxyToSession();
    sidecar.start();
    createWindow();
    pruneThumbCache();
    if (process.env.LUSTRA_SELFTEST_DIR) mainWindow.webContents.once('did-finish-load', () => runSelfTest(process.env.LUSTRA_SELFTEST_DIR));
    sidecar.ready.then(() => maybeAutoUpdateTags()).catch(() => {});
  });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', () => { jobs.killAll(); sidecar.stop(); });
}
