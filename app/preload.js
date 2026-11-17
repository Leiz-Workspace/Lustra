// Lustra desktop: preload bridge (contextIsolation). The renderer never sees API keys or spawns processes itself.
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const invoke = (channel, ...args) => ipcRenderer.invoke(channel, ...args);

contextBridge.exposeInMainWorld('lustra', {
  rpc: (method, params) => invoke('rpc', method, params || {}),
  sidecarStatus: () => invoke('sidecar:getStatus'),
  tags: {
    update: (module) => invoke('tags:update', module),
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
  on: (channel, cb) => {
    const allowed = ['log', 'sidecar:status', 'sidecar:notify', 'jobs:update', 'jobs:log', 'appearance:changed', 'gen:event'];
    if (!allowed.includes(channel)) throw new Error(`channel not allowed: ${channel}`);
    const handler = (_event, data) => cb(data);
    ipcRenderer.on(channel, handler);
    return () => ipcRenderer.removeListener(channel, handler);
  },
});
