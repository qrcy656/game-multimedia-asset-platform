/* Browser-local, account-separated model choices. No API keys enter storage. */
(() => {
  'use strict';
  let user = null;
  let state = {};
  const prefix = 'fantasy-community:model-selection:v1:';
  const storageKey = () => user ? prefix + user : null;
  function read() {
    try { state = JSON.parse(localStorage.getItem(storageKey()) || '{}'); } catch (_) { /* Retain this tab's last valid choice. */ }
    if (!state || typeof state !== 'object' || Array.isArray(state)) state = {};
    return state;
  }
  function setUser(id) { user = id == null ? null : String(id); state = {}; if (user) read(); }
  function selections() { return user ? JSON.parse(JSON.stringify(read())) : {}; }
  function selection(module, role) { return selections()[module]?.[role]; }
  function set(module, role, id) {
    if (!user) throw new Error('请先登录再选择模型');
    read(); state[module] ||= {};
    if (id === undefined) delete state[module][role]; else state[module][role] = id;
    if (!Object.keys(state[module]).length) delete state[module];
    try { localStorage.setItem(storageKey(), JSON.stringify(state)); } catch (_) { /* Current tab still keeps its selection. */ }
    window.dispatchEvent(new CustomEvent('fantasy-model-selection-change', {detail:{module, role}}));
  }
  function headers() {
    return user ? {'X-Fantasy-Model-Selection': JSON.stringify(selections()), 'X-Fantasy-Model-Account': user} : {};
  }
  window.FantasyModels = Object.freeze({setUser, selections, selection, set, headers, userId: () => user, storageKey});
})();

/* Shared session protection for module pages on the same origin. */
(() => {
  'use strict';
  const module = location.pathname.split('/')[1];
  if (!['knowledge','animation','image','speech','music','model3d','narrative','effects'].includes(module)) return;
  const nativeFetch = window.fetch.bind(window);
  let session = null;
  const refreshSession = () => nativeFetch('/api/session', {credentials:'same-origin'})
    .then(async response => { session = response.ok ? await response.json() : null; window.FantasyModels.setUser(session?.user?.id); return session; });
  let loading = refreshSession();
  window.fetch = async (input, init = {}) => {
    const raw = typeof input === 'string' ? input : input.url;
    let url = new URL(raw, location.href);
    if (url.origin === location.origin && url.pathname.startsWith('/api/')) {
      url.pathname = '/' + module + url.pathname;
    }
    const method = (init.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
    const headers = new Headers(init.headers || (typeof input === 'object' && input.headers) || {});
    if (url.origin === location.origin) {
      await loading;
      for (const [name, value] of Object.entries(window.FantasyModels.headers())) headers.set(name, value);
    }
    if (url.origin === location.origin && !['GET','HEAD','OPTIONS'].includes(method)) {
      await loading;
      if (!session?.csrf) { loading = refreshSession(); await loading; }
      if (session?.csrf) headers.set('X-Community-CSRF', session.csrf);
    }
    let request = url.href;
    if (input instanceof Request) {
      request = input;
      if (url.href !== input.url) {
        const original = new Request(input, init);
        // Passing a Request as RequestInit turns its body into a streaming
        // upload, which Chromium cannot send to a local HTTP/1.1 service.
        const body = original.body ? await original.blob() : undefined;
        request = new Request(url.href, {method: original.method, headers: original.headers, body,
          mode: original.mode, credentials: original.credentials, cache: original.cache,
          redirect: original.redirect, referrer: original.referrer, referrerPolicy: original.referrerPolicy,
          integrity: original.integrity, keepalive: original.keepalive, signal: original.signal});
      }
    }
    return nativeFetch(request, {...init, headers, credentials:'same-origin'});
  };
})();
