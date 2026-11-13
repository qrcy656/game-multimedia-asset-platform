import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('./integration.js', import.meta.url), 'utf8');
const catalogSource = fs.readFileSync(new URL('../vendor/js/ui/catalog.js', import.meta.url), 'utf8');
const appSource = fs.readFileSync(new URL('../vendor/js/ui/app.js', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function browser(owner, raw = new Map(), fetcher = async () => new Response('{}'), apiBase = '') {
  const events = {};
  const window = {
    FantasyEffectsSession: { user: {id: owner, name: owner}, csrf: 'csrf-' + owner, storage_key: owner, home_url: '/', logout_url: '/logout', api_base: apiBase },
    location: {href: 'http://localhost:18082' + apiBase + '/', origin: 'http://localhost:18082'},
    localStorage: {
      get length() { return raw.size; }, key: i => [...raw.keys()][i] ?? null,
      getItem: key => raw.get(key) ?? null, setItem: (key, value) => raw.set(key, String(value)), removeItem: key => raw.delete(key)
    },
    fetch: fetcher, addEventListener: (type, fn) => { events[type] = fn; }, setInterval() {},
  };
  class BrowserRequest extends Request {
    constructor(input, init) { super(typeof input === 'string' ? new URL(input, window.location.href) : input, init); }
  }
  const context = vm.createContext({window, document: {readyState: 'loading', getElementById: () => null, addEventListener() {}},
    URL, Request: BrowserRequest, Headers, console, setTimeout, clearTimeout});
  vm.runInContext(source, context);
  return {window, context, raw, effects: window.FantasyEffects, events};
}

// Both accounts share one origin. Enumeration/clear must remain scoped, including texture GC.
{
  const raw = new Map([['afx.workspace', 'legacy-shared-data']]);
  const a = browser('alice', raw), b = browser('bob', raw);
  a.effects.storage.setItem('afx.workspace', 'alice-draft');
  a.effects.storage['afx.tex.one'] = 'alice-texture';
  b.effects.storage.setItem('afx.workspace', 'bob-draft');
  assert.equal(a.effects.storage.getItem('afx.workspace'), 'alice-draft');
  assert.equal(b.effects.storage.getItem('afx.tex.one'), null);
  assert.deepEqual(Object.keys(a.effects.storage), ['afx.workspace', 'afx.tex.one']);
  assert.equal(a.effects.storage.length, 2);
  a.window.FantasyEffectsSession.user.id = 'bob'; // Immutable captured namespace, not the mutable bootstrap object.
  a.effects.storage.setItem('afx.workspace', 'still-alice');
  assert.equal(b.effects.storage.getItem('afx.workspace'), 'bob-draft');
  a.effects.storage.clear();
  assert.equal(raw.get('afx.workspace'), 'legacy-shared-data');
  assert.equal(b.effects.storage.length, 1);
}
// Owner and CSRF override caller-supplied headers. Cross-origin traffic receives neither.
{
  const seen = [];
  const b = browser('alice', undefined, async (...args) => { seen.push(args); return new Response('{}'); });
  await b.window.fetch('api/list');
  await b.window.fetch('/api/save', {method: 'POST', headers: {'X-Effects-Owner': 'bob'}, body: '{}'});
  await b.window.fetch('https://example.org/api/list');
  assert.equal(seen[0][0].headers.get('X-Effects-Owner'), 'alice');
  assert.equal(seen[0][0].headers.get('X-Effects-CSRF'), null);
  assert.equal(seen[1][0].headers.get('X-Effects-Owner'), 'alice');
  assert.equal(seen[1][0].headers.get('X-Effects-CSRF'), 'csrf-alice');
  assert.equal(seen[1][0].credentials, 'same-origin');
  assert.equal(typeof seen[2][0], 'string');
}
// Failure and session-change paths never turn into a successful save or cross-account read.
{
  const seen = [];
  const b = browser('alice', undefined, async request => {seen.push(request); return new Response('{}');}, '/effects');
  await b.window.fetch('api/list');
  await b.window.fetch('/api/session');
  await b.window.fetch('/effects/api/save', {method:'POST', body:'{"name":"prefixed"}'});
  assert.deepEqual(seen.map(request => new URL(request.url).pathname), ['/effects/api/list','/effects/api/session','/effects/api/save']);
  assert.ok(seen.every(request => request.headers.get('X-Effects-Owner') === 'alice'));
  assert.equal(seen[2].headers.get('X-Effects-CSRF'), 'csrf-alice');
  assert.equal(await seen[2].text(), '{"name":"prefixed"}');
}
for (const status of [401, 403]) {
  let calls = 0, flushes = 0;
  const b = browser('alice', undefined, async () => { calls++; return new Response('{"code":"invalid_csrf"}', {status}); });
  b.window.AFX = {flushAutosave() {flushes++;}, state: {playing: true}};
  await assert.rejects(b.window.fetch('/api/list'));
  assert.equal(b.effects.isActive(), false);
  assert.equal(b.window.AFX.state.playing, false);
  await assert.rejects(b.window.fetch('/api/save', {method: 'POST'}));
  assert.equal(calls, 1);
  assert.equal(flushes, 1);
}
{
  const b = browser('alice', undefined, async () => new Response('{"code":"content_read_only","error":"Read-only shared content"}', {status: 403}));
  await assert.rejects(b.window.fetch('/api/save', {method: 'POST'}), /Read-only/);
  assert.equal(b.effects.isActive(), true, 'content permissions never lock an otherwise valid session');
}
{
  const b = browser('alice', undefined, async () => new Response(JSON.stringify({user: {id: 'bob'}})));
  assert.equal(await b.effects.checkSession(), false);
  assert.equal(b.effects.isActive(), false);
}
{
  const b = browser('alice', undefined, async () => {throw new TypeError('offline');});
  b.effects.storage.setItem('afx.workspace', 'kept');
  await assert.rejects(b.window.fetch('/api/save', {method: 'POST'}));
  assert.equal(b.effects.isActive(), true);
  assert.equal(b.effects.storage.getItem('afx.workspace'), 'kept');
}
// A response whose body is read after a lock must not reach editor callbacks.
{
  const b = browser('alice');
  const response = await b.window.fetch('/api/list');
  b.effects.lock();
  await assert.rejects(response.json());
}
// Real patched catalog rejects HTTP 500 and success-shaped errors; it never saves offline.
for (const [status, payload] of [[500, {error: 'disk full'}], [200, {ok: false}], [200, {ok: true, file: 'x.json'}]]) {
  const b = browser('alice', undefined, async request => new Response(JSON.stringify(request.url.endsWith('/api/list') ? [] : payload), {status}));
  let saved, refresh = 0;
  b.window.AFX = {Dom: {h() {}}, t: x => x, state: {doc: {name: 'draft'}},
    Model: {effectFile: doc => ({doc})}, Engine: {renderThumb() {throw Error('no canvas in unit test');}}, setStatus() {}};
  b.context.fetch = b.window.fetch;
  vm.runInContext(catalogSource, b.context);
  b.window.AFX.Catalog.refresh = () => refresh++;
  b.window.AFX.Catalog.saveCurrent(ok => {saved = ok;});
  await tick(); await tick();
  assert.equal(saved, status === 200 && payload.ok === true);
  assert.equal(refresh, saved ? 1 : 0);
  assert.equal(b.effects.storage.getItem('afx.library'), null);
}
// Exercise the actual app save callback: failed or stale responses must preserve dirty.
for (const outcome of ['failed', 'changed', 'unchanged']) {
  const b = browser('alice');
  let callback, cleared = false, flushed = 0;
  b.window.AFX = {t: x => x, Model: {stripJson: doc => JSON.stringify(doc)},
    Catalog: {saveCurrent: done => callback = done}};
  vm.runInContext(appSource, b.context);
  const afx = b.window.AFX;
  afx.state.doc = {id: 'test', name: 'before', layers: []}; afx.state.dirty = true;
  afx.wsSyncSaved = () => cleared = true;
  afx.emit = () => {};
  afx.flushAutosave = () => flushed++;
  afx.saveCurrent();
  if (outcome === 'changed') afx.state.doc.name = 'edited during save';
  callback(outcome !== 'failed');
  assert.equal(afx.state.dirty, outcome !== 'unchanged');
  assert.equal(cleared, outcome === 'unchanged');
  assert.equal(flushed, outcome === 'failed' ? 0 : 1);
}
console.log('Frontend integration: account storage, owner/CSRF, lock, offline draft, failed save, and pending-edit checks passed.');
