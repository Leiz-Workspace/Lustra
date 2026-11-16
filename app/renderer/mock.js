// Browser-only mock of the preload bridge: lets the UI run in a plain browser (design work, screenshots, UI tests)
// with neutral placeholder thumbnails. Not loaded behaviour in Electron: preload defines window.lustra first.
(function () {
  if (window.lustra) return;
  const listeners = {};
  const emit = (ch, data) => (listeners[ch] || []).forEach((cb) => cb(data));
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  const ICONS = [
    { id: 'full-mask-orange', name: 'Full mask · Orange' }, { id: 'exposed-jaw-orange', name: 'Exposed jaw · Orange' },
    { id: 'full-mask-monochrome', name: 'Full mask · Mono' }, { id: 'exposed-jaw-monochrome', name: 'Exposed jaw · Mono' },
  ];
  const settings = {
    appearance: { theme: 'ember', icon: 'full-mask-orange', background: { mode: 'icon', image: '', blur: 4, opacity: 0.9, dim: 0.18 } },
    search: { module: 'db', autocomplete: true, safePreview: false },
    download: { path: '/home/user/Downloads/Lustra', threads: 4, images: true, videos: true, videoFormat: '', lowRes: false, prefix: true,
      gender: { select: [], conf: 'medium', strict: false },
      appendInfo: false, reverse: false, limit: 0, parchi: false, preserveDate: false, subfolderPerQuery: false, dumpTags: false,
      dumpSources: false, dumpComments: false, dumpHashes: false, infoMode: 'run', warnNonEmpty: false, maxConcurrentJobs: 1 },
    connection: { proxy: '', ignoreProxy: false, proxyNoDownload: false, timeout: 0, retries: 0, headers: {}, cookies: {} },
    tags: { autoUpdate: false, autoUpdateDays: 7 },
    advanced: { pythonPath: '', verbose: false, catalogUrl: '' },
  };
  const iconUrl = (id) => `../assets/icons/${id}.png`;
  const appearance = () => ({
    ...settings.appearance, iconUrl: iconUrl(settings.appearance.icon),
    backgroundUrl: settings.appearance.background.mode === 'image' && settings.appearance.background.image ? settings.appearance.background.image : iconUrl(settings.appearance.icon),
    icons: ICONS.map((i) => ({ ...i, url: iconUrl(i.id) })), themes: ['ember', 'graphite', 'paper', 'oled'], liveTaskbarIcon: false,
  });
  const modules = [
    { id: 'danbooru', name: 'Danbooru', abbr: 'db', version: '1.0.0', source: 'builtin', default: true, installed: true, media: ['image', 'video'], capabilities: ['gender_tags', 'parent_child', 'preview'],
      site: 'https://danbooru.donmai.us/', description: 'Danbooru JSON API. Anonymous search is limited to 2 tags: extra tags are checked locally.', auth: { apiKey: true, apiKeyFormat: 'API_KEY,LOGIN' } },
    { id: 'rule34', name: 'Rule34', abbr: 'rx', version: '2.0.0', source: 'builtin', default: true, installed: true, media: ['image', 'video'], capabilities: ['gender_tags', 'parent_child', 'preview'],
      site: 'https://rule34.xxx/', description: 'Gelbooru-based DAPI (XML). API key + user id recommended.', auth: { apiKey: true, apiKeyFormat: 'KEY,USER_ID' } },
    { id: 'e621', name: 'e621', abbr: 'en', version: '1.0.0', source: 'catalog', default: false, installed: false, media: ['image', 'video'], site: 'https://e621.net/', description: 'e621 JSON API (2 requests/second).', auth: {} },
    { id: 'paheal', name: 'Rule34 Paheal', abbr: 'rp', version: '1.0.0', source: 'catalog', default: false, installed: false, media: ['image', 'video'], site: 'https://rule34.paheal.net/', description: 'Shimmie danbooru-compatible API. Max 3 tags.', auth: {} },
    { id: 'xbooru', name: 'Xbooru', abbr: 'xb', version: '1.0.0', source: 'catalog', default: false, installed: false, media: ['image', 'video'], site: 'https://xbooru.com/', description: 'Gelbooru-based DAPI (XML).', auth: {} },
  ];
  const TAGS = [
    ['scenery', 412000, 'general'], ['sky', 380000, 'general'], ['cloud', 300000, 'general'], ['cat_ears', 143800, 'general'], ['cat', 98000, 'general'],
    ['castle', 22000, 'general'], ['cherry_blossoms', 61000, 'general'], ['city', 52000, 'general'], ['coffee', 9000, 'general'],
    ['smile', 2500000, 'general'], ['solo', 5200000, 'general'], ['sunset', 41000, 'general'], ['star_(sky)', 33000, 'general'],
    ['original', 1600000, 'copyright'], ['touhou', 900000, 'copyright'], ['hatsune_miku', 120000, 'character'], ['highres', 8400000, 'metadata'],
    ['absurdres', 2100000, 'metadata'], ['blue_sky', 120000, 'general'], ['bird', 70000, 'general'], ['book', 150000, 'general'],
    ['building', 90000, 'general'], ['ocean', 110000, 'general'], ['outdoors', 1300000, 'general'], ['mountain', 42000, 'general'],
  ];
  const palette = ['#ff6a1a', '#6fa8ff', '#4fd18b', '#b892ff', '#ffb547', '#ff5468', '#46c3c9'];
  const thumb = (id) => {
    const c1 = palette[id % palette.length]; const c2 = palette[(id * 3 + 1) % palette.length];
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/></linearGradient></defs><rect width="300" height="300" fill="url(#g)"/><circle cx="${80 + (id % 140)}" cy="${90 + (id % 90)}" r="${30 + (id % 40)}" fill="rgba(255,255,255,.35)"/><text x="16" y="282" font-family="monospace" font-size="20" fill="rgba(0,0,0,.55)">#${id}</text></svg>`;
    return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
  };
  const jobs = new Map();
  window.lustra = {
    __mock: true,
    rpc: async (method, p) => {
      await delay(60);
      if (method === 'app.info') return { name: 'Lustra', version: '2.0 r1', python: '3.13' };
      if (method === 'ext.list') return modules;
      if (method === 'ext.install') { const m = modules.find((x) => x.id === p.id); if (m) m.installed = true; return m; }
      if (method === 'ext.uninstall') { const m = modules.find((x) => x.id === p.id); if (m) m.installed = false; return { ok: true }; }
      if (method === 'ext.remoteCatalog') return [];
      if (method === 'tags.autocomplete') {
        const neg = p.prefix.startsWith('-'); const base = p.prefix.replace(/^-/, '');
        return TAGS.filter((t) => t[0].startsWith(base)).sort((a, b) => b[1] - a[1]).slice(0, p.limit || 12)
          .map(([tag, posts, type]) => ({ tag, posts, type, insert: `${neg ? '-' : ''}${tag}` }));
      }
      if (method === 'tags.browse') {
        let rows = TAGS.filter((t) => (p.prefix === '#' ? !/^[a-z]/.test(t[0]) : t[0].startsWith(p.prefix)) && t[1] >= (p.minPosts || 0)
          && (!p.types?.length || p.types.includes(t[2])));
        rows = p.sort === 'posts' ? rows.sort((a, b) => b[1] - a[1]) : rows.sort((a, b) => a[0].localeCompare(b[0]));
        return { total: rows.length, items: rows.slice(p.offset, p.offset + p.limit).map(([tag, posts, type]) => ({ tag, posts, type })) };
      }
      if (method === 'tags.validate') return { valid: !/[\s]{2,}|\(\)/.test(p.tags), tags: p.tags.split(' ') };
      if (method === 'tags.status') return [{ module: 'db', path: '2tags/db_tags.json', size: 21e6, mtime: Date.now() / 1000 }, { module: 'rx', path: '2tags/rx_tags.json', size: 24.6e6, mtime: Date.now() / 1000 - 86400 * 56 }];
      if (method === 'files.nonEmpty') return { exists: true, nonEmpty: false };
      throw new Error(`mock: ${method}`);
    },
    tags: { update: async (m) => { setTimeout(() => emit('sidecar:notify', { method: 'tags.updateDone', params: { module: m, written: true, message: 'written 1234 tags (mock)' } }), 800); return { job: `tags-${m}` }; } },
    search: {
      count: async () => { await delay(300); return { count: 143815, requiredTags: [] }; },
      preview: async (q, page) => {
        await delay(400);
        const terms = q.tags.split(' ').filter((t) => !t.startsWith('-') && !t.includes(':'));
        const required = q.module === 'db' && terms.length > 2 ? terms.slice(2) : [];
        const sel = (q.gender && q.gender.select) || [];
        const labels = ['only_female', 'only_male', 'both', 'none', 'unknown'];
        const confs = ['high', 'medium', 'low'];
        const genderStats = {};
        const items = Array.from({ length: 48 }, (_, i) => {
          const id = 9000000 - page * 48 - i * 7;
          const tags = ['scenery', 'sky', 'outdoors', ...terms.slice(0, 2), i % 3 ? 'cloud' : 'sunset'];
          if (i % 4 === 0) tags.push(...required);
          const excludedBy = required.length > 0 && i % 4 !== 0 ? ['required'] : [];
          let gender;
          if (sel.length) {
            const label = labels[i % 5];
            gender = { label, conf: label === 'unknown' ? null : confs[i % 3], flags: [], evidence: ['mock'], source: 'tags', effective: label };
            genderStats[label] = (genderStats[label] || 0) + 1;
            if (!sel.includes(label)) excludedBy.push('gender');
          }
          return { id: String(id), preview: thumb(id), sample: thumb(id), isVideo: i % 9 === 4, ext: i % 9 === 4 ? 'mp4' : 'jpg', score: String((id % 97)),
            width: '1920', height: '1080', date: '16-11-2026', rating: 'g', tags, gender, excludedBy, excluded: excludedBy.length > 0 };
        });
        return { page, pageSize: 48, items, tasks: [q.tags], requiredTags: required, site: 'https://example.invalid/',
          genderStats, blockedCount: sel.length ? 2 : 0, genderEnabled: sel.length > 0 };
      },
    },
    settings: {
      get: async () => ({ settings, appearance: appearance(), secrets: { encryption: true, modules: {} } }),
      set: async (patch) => {
        const merge = (a, b) => { for (const [k, v] of Object.entries(b)) { if (v && typeof v === 'object' && !Array.isArray(v)) { a[k] = a[k] || {}; merge(a[k], v); } else a[k] = v; } };
        merge(settings, patch);
        if (patch.appearance) emit('appearance:changed', appearance());
        return { settings, appearance: appearance(), secrets: { encryption: true, modules: {} } };
      },
    },
    secrets: { set: async () => ({ encryption: true, modules: {} }) },
    jobs: {
      start: async (q) => {
        const id = `job-${Date.now()}`;
        const job = { id, module: q.module, tags: q.tags, label: q.label || '', dest: q.path, status: 'running', createdAt: Date.now(), progress: { stateName: 'SEARCHING' }, logTail: ['mock job started'] };
        jobs.set(id, job);
        let n = 0;
        const t = setInterval(() => {
          n += 4;
          job.progress = n < 12 ? { stateName: 'SCANNING_PAGES1', total: 96 } : { stateName: 'DOWNLOADING', totalAll: 96, processed: Math.min(96, n), success: Math.min(96, n) };
          if (n >= 96) { job.status = 'done'; job.progress.stateName = 'DONE'; clearInterval(t); }
          emit('jobs:update', { ...job });
        }, 300);
        return { ...job };
      },
      cancel: async (id) => { const j = jobs.get(id); if (j) { j.status = 'cancelled'; emit('jobs:update', { ...j }); } return true; },
      list: async () => [...jobs.values()],
      clear: async () => { for (const [k, j] of jobs) if (j.status !== 'running') jobs.delete(k); return [...jobs.values()]; },
    },
    dialogs: { folder: async () => '', image: async () => '', zip: async () => '' },
    ext: {
      installZip: async () => { throw new Error('mock'); },
      installCatalog: async (id) => { const m = modules.find((x) => x.id === id); if (m) m.installed = true; return { ...m, restartRequired: false }; },
      installRemote: async () => { throw new Error('mock'); },
    },
    appearance: { setBackgroundImage: async () => appearance() },
    app: {
      info: async () => ({ version: '2.0.1', electron: 'mock', platform: 'browser', userData: '~/.config/Lustra', python: '.venv/bin/python', repo: '.', sidecar: { name: 'Lustra', version: '2.0 r1', python: '3.13' } }),
      relaunch: async () => location.reload(), installLauncher: async () => ({ ok: false, message: 'mock' }), openPath: async () => '', openExternal: async () => false,
    },
    thumbUrl: (url) => url,
    on: (ch, cb) => { (listeners[ch] = listeners[ch] || []).push(cb); return () => {}; },
  };
  setTimeout(() => emit('sidecar:status', { ok: true, info: { version: '2.0 r1 (mock)' } }), 300);
}());
