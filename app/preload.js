// Lustra desktop: preload bridge (contextIsolation). The renderer never sees API keys or spawns processes itself.
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const invoke = (channel, ...args) => ipcRenderer.invoke(channel, ...args);

contextBridge.exposeInMainWorld('lustra', {
  rpc: (method, params) => invoke('rpc', method, params || {}),
  sidecarStatus: () => invoke('sidecar:getStatus'),
  tags: {
    update: (module) => invoke('tags:update', module),
    scheduleStatus: () => invoke('tags:scheduleStatus'),
    schedule: (options) => invoke('tags:schedule', options), // {enabled, frequency: 'daily' | 'weekly'}: modules 'auto' (main.js)
  },
  search: {
    count: (query) => invoke('search:count', query),
    preview: (query, page) => invoke('search:preview', query, page),
    related: (query) => invoke('search:related', query),
  },
  settings: {
    get: () => invoke('settings:get'),
    set: (patch) => invoke('settings:set', patch),
  },
  secrets: {
    set: (module, value) => invoke('secrets:set', module, value), // empty value: no change
    clear: (module) => invoke('secrets:clear', module),
  },
  jobs: {
    start: (query) => invoke('jobs:start', query),
    cancel: (id) => invoke('jobs:cancel', id),
    list: () => invoke('jobs:list'),
    clear: () => invoke('jobs:clear'),
    rerun: (id, allowDuplicate) => invoke('jobs:rerun', id, allowDuplicate === true),
    // one query on several sites: one job per module, one after another, -skip_known forced (main builds the group)
    startMulti: (query, modules) => invoke('jobs:startMulti', query, modules),
  },
  dialogs: {
    folder: (current) => invoke('dialog:folder', current),
    image: () => invoke('dialog:image'),
    zip: () => invoke('dialog:zip'),
  },
  ext: {
    installZip: (file, sha256) => invoke('ext:installZip', file, sha256),
    installCatalog: (id) => invoke('ext:installCatalog', id),
    installRemote: (item) => invoke('ext:installRemote', item),
  },
  gen: {
    // extension generator: creates drafts; install is a separate call that main confirms natively. The API key is write-only
    status: () => invoke('gen:status'),
    start: (opts) => invoke('gen:start', opts),
    cancel: () => invoke('gen:cancel'),
    list: () => invoke('gen:list'),
    get: (session) => invoke('gen:get', session),
    open: (session) => invoke('gen:open', session),
    exportZip: (session) => invoke('gen:export', session),
    install: (session) => invoke('gen:install', session),
    remove: (session) => invoke('gen:remove', session),
    setKey: (value) => invoke('gen:setKey', value),
    clearKey: () => invoke('gen:clearKey'),
  },
  genderModel: {
    status: () => invoke('gmodel:status'),
    load: () => invoke('gmodel:load'), // turn on: load the model on the server now
    unload: () => invoke('gmodel:unload'), // turn off: free its memory on the server
    setToken: (value) => invoke('gmodel:setToken', value),
    clearToken: () => invoke('gmodel:clearToken'),
  },
  saved: {
    // saved query schedule: every = hours (0 = off), from 'now' (only posts newer than today) or 'all'
    setAuto: (key, every, from) => invoke('saved:auto', key, every, from),
  },
  library: {
    // files of the download index: open with the default app (media extensions only) or show in the file manager
    open: (file) => invoke('library:open', file),
    reveal: (file) => invoke('library:reveal', file),
  },
  index: {
    // download index maintenance. clean: rows whose file is gone (main confirms natively; {dryRun: true} only counts).
    // Library import: the folder comes from pickImportFolder (native dialog), the import runs as a job
    clean: (opts) => invoke('index:clean', opts),
    cleanCancel: () => invoke('index:cleanCancel'),
    pickImportFolder: () => invoke('index:pickFolder'),
    importLibrary: (opts) => invoke('index:import', opts),
  },
  appearance: {
    setBackgroundImage: (file) => invoke('appearance:setBackgroundImage', file),
  },
  app: {
    info: () => invoke('app:info'),
    relaunch: () => invoke('app:relaunch'),
    installLauncher: () => invoke('launcher:install'),
    openPath: (p) => invoke('shell:open', p),
    openExternal: (url) => invoke('shell:external', url),
  },
  thumbUrl: (url, referer) => `lustra-thumb://t/?u=${encodeURIComponent(url)}&r=${encodeURIComponent(referer || '')}`,
  mediaUrl: (url, referer) => `lustra-thumb://m/?u=${encodeURIComponent(url)}&r=${encodeURIComponent(referer || '')}`,
  on: (channel, cb) => {
    const allowed = ['log', 'sidecar:status', 'sidecar:notify', 'jobs:update', 'jobs:log', 'appearance:changed', 'gen:event', 'nav:page'];
    if (!allowed.includes(channel)) throw new Error(`channel not allowed: ${channel}`);
    const handler = (_event, data) => cb(data);
    ipcRenderer.on(channel, handler);
    return () => ipcRenderer.removeListener(channel, handler);
  },
});
