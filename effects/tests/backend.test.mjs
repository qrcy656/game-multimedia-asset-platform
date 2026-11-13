import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { createHmac, createHash } from 'node:crypto';
import { configuration, createEffectsServer } from '../server.mjs';

const SECRET = 'test-only-secret-with-more-than-32-characters';
const ORIGIN = 'https://effects.example.test';
const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
const PNG = 'data:image/png;base64,iVBORw0KGgo=';
const hash = value => createHash('sha256').update(value).digest('hex');
const csrf = id => createHmac('sha256', SECRET).update('fantasy-effects\0csrf\0' + id).digest('hex');
const effect = (name = '火焰', id = 'fx_test') => ({ app: 'arcadia-effects', v: 2, doc: { v: 2, id, name, comp: { w: 64, h: 64, dur: 1, fps: 30 }, layers: [], textures: [] }, tex: {} });
const token = (id = A, overrides = {}, header = { alg: 'HS256', typ: 'JWT' }, secret = SECRET) => {
  const now = Math.floor(Date.now() / 1000);
  const payload = { iss: 'fantasy-main', aud: 'fantasy-effects', sub: id, name: '测试账号', role: 'user', iat: now, exp: now + 60, ...overrides };
  const value = [header, payload].map(value => Buffer.from(JSON.stringify(value)).toString('base64url')).join('.');
  return value + '.' + createHmac('sha256', secret).update(value).digest('base64url');
};

async function fixture(t, options = {}) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'fantasy-effects-test-'));
  const config = { secret: SECRET, publicOrigin: ORIGIN, homeUrl: 'https://portal.example.test', dataDir: path.join(directory, 'data'), vendorDir: path.join(directory, 'vendor'), frontendDir: path.join(directory, 'frontend'), maxBytes: 4096, ...options };
  await fs.mkdir(path.join(config.vendorDir, 'js', 'core'), { recursive: true });
  await fs.mkdir(path.join(config.vendorDir, 'css'), { recursive: true });
  await fs.mkdir(path.join(config.vendorDir, 'docs'), { recursive: true });
  await fs.mkdir(path.join(config.vendorDir, 'tools'), { recursive: true });
  await fs.mkdir(config.frontendDir, { recursive: true });
  for (const [file, body] of [
    [path.join(config.vendorDir, 'index.html'), '<html>Editor</html>'],
    [path.join(config.vendorDir, 'js', 'core', 'model.js'), 'window.model = {};'],
    [path.join(config.vendorDir, 'css', 'app.css'), 'body{}'],
    [path.join(config.vendorDir, 'server.mjs'), 'PRIVATE SERVER SOURCE'],
    [path.join(config.vendorDir, 'SECRET.txt'), 'PRIVATE SOURCE'],
    [path.join(config.vendorDir, 'docs', 'FORMAT.md'), '# Arcadia Effects\nFormat fixture'],
    [path.join(config.vendorDir, 'tools', 'validate.mjs'), "import fs from 'node:fs'; const d=JSON.parse(fs.readFileSync(process.argv[2],'utf8')); if(d.doc.name==='invalid'){console.log('  [error] layers[0].opacity: expected number');process.exit(1);} console.log('  [warn] layers: empty effect'); console.log('  [sim] validation complete');"],
    [path.join(config.frontendDir, 'integration.js'), 'window.integration = true;'],
    [path.join(config.frontendDir, 'integration.css'), 'header{}'],
    [path.join(config.frontendDir, 'AI_GUIDE.md'), '# Agent authoring guide'],
  ]) await fs.writeFile(file, body);
  const server = createEffectsServer(config);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    await fs.rm(directory, { recursive: true, force: true });
  });
  const request = async (url, { id = A, method = 'GET', body, auth = token(id), headers = {}, ...rest } = {}) => {
    const values = { ...(auth ? { Authorization: 'Bearer ' + auth } : {}), 'X-Effects-Owner': id, ...(method === 'POST' ? { Origin: ORIGIN, 'X-Effects-CSRF': csrf(id), 'Content-Type': 'application/json' } : {}), ...headers };
    for (const key of Object.keys(values)) if (values[key] === undefined) delete values[key];
    return fetch(base + url, { method, headers: values, body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body), ...rest });
  };
  return { ...config, directory, base, server, request, library: id => path.join(config.dataDir, 'users', hash(id), 'library') };
}

async function responseStatus(response, expected) {
  assert.equal(response.status, expected, await response.clone().text());
  return response;
}
async function save(f, name = '火焰', id = A, data = effect(name)) {
  return f.request('/api/save', { method: 'POST', id, body: { name, data, thumb: PNG } });
}

test('configuration requires a real secret and absolute origin URLs', () => {
  const base = { publicOrigin: ORIGIN, homeUrl: 'https://portal.example.test' };
  for (const secret of ['', 'short', ' '.repeat(40)]) assert.throws(() => configuration({ ...base, secret }));
  assert.throws(() => configuration({ ...base, secret: SECRET, publicOrigin: ORIGIN + '/other' }));
  assert.throws(() => configuration({ ...base, secret: SECRET, homeUrl: 'javascript:alert(1)' }));
  assert.equal(configuration({ ...base, secret: SECRET }).publicOrigin, ORIGIN);
});

test('only health is anonymous; all static assets and APIs require authentication', async t => {
  const f = await fixture(t);
  await responseStatus(await f.request('/health', { auth: null }), 200);
  for (const url of ['/', '/index.html', '/js/core/model.js', '/integration.js', '/api/session', '/api/list', '/fantasy-session.js']) {
    await responseStatus(await f.request(url, { auth: null }), 401);
  }
  await responseStatus(await f.request('/api/list'), 200);
});

test('rejects forged, expired, wrongly scoped, malformed and overlong JWTs', async t => {
  const f = await fixture(t);
  const now = Math.floor(Date.now() / 1000);
  const invalid = [
    token(A, {}, undefined, 'wrong-signing-key'), token(A, { exp: now - 1, iat: now - 61 }),
    token(A, { aud: 'fantasy-images' }), token(A, { aud: ['fantasy-effects'] }), token(A, { iss: 'other' }),
    token('../outside'), token(A, { role: 'owner' }), token(A, { iat: now + 40, exp: now + 100 }),
    token(A, { iat: now, exp: now + 3600 }), token(A, { iat: 'today' }), token(A, { exp: null }),
    token(A, { nbf: now + 40 }), token(A, {}, { alg: 'none' }), token(A, {}, { alg: 'HS512' }),
    token(A, {}, { alg: 'HS256', crit: ['unsupported'] }), 'bad.token.value', 'a'.repeat(8200),
  ];
  for (const auth of invalid) await responseStatus(await f.request('/api/list', { auth }), 401);
});

test('bootstrap is account scoped, escaped, no-store, and never includes credentials', async t => {
  const f = await fixture(t);
  const auth = token(A, { name: '</script><img src=x>' });
  const sessionResponse = await f.request('/api/session', { auth, headers: { 'X-Effects-Owner': undefined } });
  const session = await sessionResponse.json();
  assert.equal(session.user.id, A);
  assert.equal(session.user.admin, false);
  assert.equal(session.csrf, csrf(A));
  assert.equal(session.home_url, 'https://portal.example.test/');
  assert.equal(session.logout_url, 'https://portal.example.test/logout');
  const bootstrap = await f.request('/fantasy-session.js', { auth });
  const js = await bootstrap.text();
  assert.match(js, /^window\.FantasyEffectsSession = /);
  assert.ok(!js.includes(SECRET) && !js.includes(auth) && !js.includes('</script>'));
  assert.match(bootstrap.headers.get('cache-control'), /no-store/);
  assert.match(bootstrap.headers.get('content-security-policy'), /script-src 'self';/);
  assert.ok(!bootstrap.headers.get('content-security-policy').includes('unsafe-eval'));
  const b = await (await f.request('/api/session', { id: B, auth: token(B, { role: 'admin' }) })).json();
  assert.equal(b.user.admin, true);
  assert.notEqual(b.csrf, session.csrf);
  assert.notEqual(b.storage_key, session.storage_key);
});

test('owner header binds API reads and detects stale tabs including session heartbeat', async t => {
  const f = await fixture(t);
  for (const url of ['/api/list', '/api/effect?f=missing.json', '/api/session', '/fantasy-session.js']) {
    const response = await f.request(url, { headers: { 'X-Effects-Owner': B } });
    await responseStatus(response, 403);
    assert.equal((await response.json()).code, 'owner_changed');
  }
  await responseStatus(await f.request('/api/list', { headers: { 'X-Effects-Owner': undefined } }), 403);
  await responseStatus(await f.request('/api/session', { headers: { 'X-Effects-Owner': undefined } }), 200);
});

test('every mutation requires the correct origin, owner and account-bound CSRF', async t => {
  const f = await fixture(t);
  for (const headers of [
    { Origin: undefined }, { Origin: 'https://evil.test' }, { Origin: 'null' },
    { 'X-Effects-CSRF': undefined }, { 'X-Effects-CSRF': 'forged' }, { 'X-Effects-CSRF': csrf(B) },
    { 'X-Effects-Owner': undefined }, { 'X-Effects-Owner': B },
  ]) {
    await responseStatus(await f.request('/api/save', { method: 'POST', headers, body: { name: 'valid', data: effect() } }), 403);
    await responseStatus(await f.request('/api/delete', { method: 'POST', headers, body: { file: 'valid.json' } }), 403);
  }
  assert.deepEqual(await (await f.request('/api/list')).json(), []);
});

test('shared CRUD keeps author writes isolated and permits administrator management', async t => {
  const f = await fixture(t);
  await responseStatus(await save(f), 200);
  const listA = await (await f.request('/api/list', { auth: token(A, { name: 'Another device' }) })).json();
  assert.equal(listA.length, 1);
  assert.equal(listA[0].file, '火焰.json');
  assert.equal(listA[0].name, '火焰');
  assert.equal(listA[0].id, 'fx_test');
  assert.equal(listA[0].thumb, PNG);
  assert.ok(listA[0].mtime > 0);
  const shared = await (await f.request('/api/list', { id: B })).json();
  assert.equal(shared.length, 1);
  assert.equal(shared[0].editable, false);
  assert.equal(shared[0].owner_key, hash(A));
  await responseStatus(await f.request('/api/effect?f=' + encodeURIComponent('火焰.json') + '&owner_key=' + hash(A), { id: B }), 200);
  await responseStatus(await f.request('/api/delete', { method: 'POST', id: B, body: { file: '火焰.json', owner_key: hash(A) } }), 403);
  await responseStatus(await f.request('/api/save', { method: 'POST', id: B, body: { name: '火焰', owner_key: hash(A), data: effect('not allowed') } }), 403);
  await responseStatus(await f.request('/api/effect?f=' + encodeURIComponent('火焰.json'), { id: B, auth: token(B, { role: 'admin' }) }), 404);
  await responseStatus(await f.request('/api/delete', { method: 'POST', id: B, body: { file: '火焰.json' } }), 200);
  assert.equal((await (await f.request('/api/list')).json()).length, 1);
  await responseStatus(await save(f, '火焰', B, effect('B version', 'fx_b')), 200);
  const aDoc = await (await f.request('/api/effect?f=' + encodeURIComponent('火焰.json'))).json();
  const bDoc = await (await f.request('/api/effect?f=' + encodeURIComponent('火焰.json'), { id: B })).json();
  assert.equal(aDoc.doc.id, 'fx_test');
  assert.equal(bDoc.doc.id, 'fx_b');
  assert.deepEqual(aDoc, { ...effect(), thumb: PNG });
  await responseStatus(await f.request('/api/delete', { method: 'POST', body: { file: '火焰.json' } }), 200);
  assert.equal((await (await f.request('/api/list')).json()).length, 1);
  assert.equal((await (await f.request('/api/list', { id: B })).json()).length, 1);
  await responseStatus(await f.request('/api/save', { method: 'POST', auth: token(A, { role: 'admin' }), body: { name: '火焰', owner_key: hash(B), data: effect('admin update') } }), 200);
  assert.equal((await (await f.request('/api/effect?f=' + encodeURIComponent('火焰.json'), { id: B })).json()).doc.name, 'admin update');
  await responseStatus(await f.request('/api/delete', { method: 'POST', auth: token(A, { role: 'admin' }), body: { file: '火焰.json', owner_key: hash(B) } }), 200);
  assert.equal((await (await f.request('/api/list')).json()).length, 0);
});

test('invalid names, schema, JSON and oversized writes never replace saved data', async t => {
  const f = await fixture(t);
  await responseStatus(await save(f, 'kept'), 200);
  const original = await fs.readFile(path.join(f.library(A), 'kept.json'), 'utf8');
  for (const name of ['../kept', '/tmp/kept', 'nested/kept', 'nested\\kept', '.hidden', 'a\0b', 'a'.repeat(245), '界'.repeat(81)]) {
    await responseStatus(await save(f, name), 400);
  }
  for (const data of [{}, { app: 'other', doc: {} }, { app: 'arcadia-effects', doc: [] }, { ...effect(), tex: { x: 'https://evil.test/a.png' } }, { ...effect(), doc: { layers: [{ type: 'code' }] } }, JSON.parse('{"app":"arcadia-effects","doc":{"__proto__":{"polluted":true}}}')]) {
    await responseStatus(await f.request('/api/save', { method: 'POST', body: { name: 'kept', data } }), 400);
  }
  await responseStatus(await f.request('/api/save', { method: 'POST', body: '{broken' }), 400);
  await responseStatus(await f.request('/api/save', { method: 'POST', body: '{}', headers: { 'Content-Type': 'text/plain' } }), 415);
  await responseStatus(await f.request('/api/save', { method: 'POST', body: { name: 'kept', data: effect(), padding: 'x'.repeat(5000) } }), 413);
  await responseStatus(await f.request('/api/save', { method: 'POST', body: { name: 'kept', data: effect(), thumb: 'javascript:alert(1)' } }), 400);
  assert.equal(await fs.readFile(path.join(f.library(A), 'kept.json'), 'utf8'), original);
  assert.deepEqual((await fs.readdir(f.library(A))).filter(name => name.startsWith('.pending-')), []);
  await responseStatus(await f.request('/api/save', { method: 'POST', body: { name: 'sparse', data: { app: 'arcadia-effects', v: 1, doc: { name: 'Old sparse', layers: [] } } } }), 200);
});

test('filename traversal cannot escape a user library', async t => {
  const f = await fixture(t);
  for (const file of ['../secret.json', '..\\secret.json', '/secret.json', 'a/b.json', '.hidden.json', 'a\0b.json']) {
    await responseStatus(await f.request('/api/effect?f=' + encodeURIComponent(file)), 400);
    await responseStatus(await f.request('/api/delete', { method: 'POST', body: { file } }), 400);
  }
});

test('static whitelist denies source, library, docs, snapshots, symlinks and traversal', async t => {
  const f = await fixture(t);
  for (const url of ['/', '/index.html', '/js/core/model.js', '/css/app.css', '/integration.js', '/integration.css']) await responseStatus(await f.request(url), 200);
  for (const url of ['/server.mjs', '/SECRET.txt', '/library/test.json', '/docs/FORMAT.md', '/tools/validate.mjs', '/.snap/x.png', '/api/snap', '/frontend/integration.js']) await responseStatus(await f.request(url), 404);
  await fs.symlink(path.join(f.vendorDir, 'SECRET.txt'), path.join(f.vendorDir, 'js', 'leak.js'));
  await responseStatus(await f.request('/js/leak.js'), 404);
  for (const rawPath of ['/js/../server.mjs', '/js/%2e%2e/server.mjs', '/js/%2e%2e%2fserver.mjs', '/js/%5c..%5cserver.mjs', '/js/%00evil.js', '//server.mjs']) {
    const result = await new Promise((resolve, reject) => {
      const req = http.request(f.base, { path: rawPath, headers: { Authorization: 'Bearer ' + token() } }, res => {
        const chunks = [];
        res.on('data', chunk => chunks.push(chunk));
        res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString() }));
      });
      req.on('error', reject);
      req.end();
    });
    assert.equal(result.status, 400, rawPath);
    assert.ok(!result.body.includes(f.directory) && !result.body.includes('PRIVATE'));
  }
});

test('symlink files and directories cannot read or alter another user or external file', async t => {
  const f = await fixture(t);
  await responseStatus(await save(f, 'original', A), 200);
  await responseStatus(await save(f, 'other', B), 200);
  const target = path.join(f.library(B), 'other.json');
  const before = await fs.readFile(target, 'utf8');
  await fs.symlink(target, path.join(f.library(A), 'link.json'));
  await responseStatus(await f.request('/api/effect?f=link.json'), 404);
  await responseStatus(await save(f, 'link'), 409);
  await responseStatus(await f.request('/api/delete', { method: 'POST', body: { file: 'link.json' } }), 409);
  assert.equal(await fs.readFile(target, 'utf8'), before);
  assert.equal((await (await f.request('/api/list')).json()).length, 2);
  const holder = path.dirname(f.library(A));
  await fs.rename(f.library(A), path.join(holder, 'original-library'));
  await fs.symlink(f.library(B), f.library(A), 'dir');
  await responseStatus(await f.request('/api/list'), 409);
  await responseStatus(await save(f, 'must-not-exist'), 409);
  await assert.rejects(fs.stat(path.join(f.library(B), 'must-not-exist.json')));
});

test('failed filesystem replacement is atomic and does not leave temporary artifacts', async t => {
  const f = await fixture(t);
  await responseStatus(await save(f, 'kept'), 200);
  await fs.mkdir(path.join(f.library(A), 'blocked.json'));
  await responseStatus(await save(f, 'blocked'), 409);
  const kept = await (await f.request('/api/effect?f=kept.json')).json();
  assert.equal(kept.doc.name, 'kept');
  assert.deepEqual((await fs.readdir(f.library(A))).filter(name => name.startsWith('.pending-')), []);
  const response = await f.request('/api/effect?f=blocked.json');
  assert.equal(response.status, 404);
  assert.ok(!(await response.text()).includes(f.directory));
});

test('chunked oversized bodies receive a bounded error without saving a file', async t => {
  const f = await fixture(t);
  const result = await new Promise((resolve, reject) => {
    const req = http.request(f.base + '/api/save', { method: 'POST', headers: { Authorization: 'Bearer ' + token(), 'X-Effects-Owner': A, 'X-Effects-CSRF': csrf(A), Origin: ORIGIN, 'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked' } }, res => {
      res.resume();
      res.on('end', () => resolve(res.statusCode));
    });
    req.on('error', reject);
    req.write('{"padding":"' + 'x'.repeat(5000));
    req.end('"}');
  });
  assert.equal(result, 413);
  assert.deepEqual(await (await f.request('/api/list')).json(), []);
});

test('machine creation and revision checking prevent overwrites and races', async t => {
  const f = await fixture(t);
  const creation = () => f.request('/api/save', { method: 'POST', body: { name: 'atomic', data: effect('atomic'), create_only: true } });
  const concurrent = await Promise.all([creation(), creation()]);
  assert.deepEqual(concurrent.map(response => response.status).sort(), [200, 409]);
  const conflict = await concurrent.find(response => response.status === 409).json();
  assert.equal(conflict.code, 'revision_conflict');
  const firstRead = await f.request('/api/effect?f=atomic.json');
  const firstBytes = await firstRead.text();
  const revision = hash(firstBytes);
  assert.equal(firstRead.headers.get('etag'), '"' + revision + '"');
  const edit = data => f.request('/api/save', { method: 'POST', body: { name: 'atomic', data, expected_sha256: revision } });
  const editing = await Promise.all([edit(effect('changed first')), edit(effect('changed second'))]);
  assert.deepEqual(editing.map(response => response.status).sort(), [200, 409]);
  const readAfter = await f.request('/api/effect?f=atomic.json');
  const afterBytes = await readAfter.text();
  const changed = await editing.find(response => response.status === 200).json();
  assert.equal(changed.revision, hash(afterBytes));
  assert.notEqual(changed.revision, revision);
  await responseStatus(await f.request('/api/save', { method: 'POST', body: { name: 'missing', data: effect(), expected_sha256: revision } }), 409);
  for (const options of [{ create_only: 'true' }, { expected_sha256: 'wrong' }, { expected_sha256: 'x'.repeat(10000) }, { create_only: true, expected_sha256: revision }]) {
    const expected = JSON.stringify(options).length > 4096 ? 413 : 400;
    await responseStatus(await f.request('/api/save', { method: 'POST', body: { name: 'atomic', data: effect(), ...options } }), expected);
  }
  assert.equal(await (await f.request('/api/effect?f=atomic.json')).text(), afterBytes);
});

test('guide is authenticated and validation executes a trusted child without storing an effect', async t => {
  const f = await fixture(t);
  const guide = await (await f.request('/api/guide')).json();
  assert.match(guide.format, /Arcadia Effects/);
  assert.equal(guide.limits.max_request_bytes, 4096);
  assert.match(guide.guide, /Agent authoring/);
  assert.equal(guide.limits.render.maxFrames, 64);
  await responseStatus(await f.request('/api/guide', { headers: { 'X-Effects-Owner': B } }), 403);
  const valid = await f.request('/api/validate', { method: 'POST', body: { data: effect() } });
  await responseStatus(valid, 200);
  assert.deepEqual(await valid.json(), { valid: true, errors: [], warnings: ['layers: empty effect'], infos: ['validation complete'] });
  const invalid = await (await f.request('/api/validate', { method: 'POST', body: { data: effect('invalid') } })).json();
  assert.equal(invalid.valid, false);
  assert.equal(invalid.errors[0], 'layers[0].opacity: expected number');
  assert.ok(!JSON.stringify(invalid).includes(f.directory));
  await responseStatus(await f.request('/api/validate', { method: 'POST', body: { data: {} } }), 400);
  await responseStatus(await f.request('/api/validate', { method: 'POST', body: { data: effect() }, headers: { 'X-Effects-CSRF': undefined } }), 403);
  assert.deepEqual(await (await f.request('/api/list')).json(), []);
});

test('PNG export reads only the current account and returns bounded metadata without saving output', async t => {
  let received;
  const png = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 1, 2, 3]);
  const f = await fixture(t, { render: async (data, options) => {
    received = { data, options };
    return { png, info: { width: 64, height: 64, frames: 1, fps: 30 } };
  } });
  await responseStatus(await save(f, 'export-me'), 200);
  const response = await f.request('/api/export', { method: 'POST', body: { file: 'export-me.json', format: 'frame', time: 0.5 } });
  await responseStatus(response, 200);
  assert.equal(response.headers.get('content-type'), 'image/png');
  assert.equal(response.headers.get('x-effects-width'), '64');
  assert.equal(response.headers.get('x-effects-height'), '64');
  assert.equal(response.headers.get('x-effects-frames'), '1');
  assert.equal(response.headers.get('x-effects-fps'), '30');
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), png);
  assert.equal(received.data.doc.name, 'export-me');
  assert.deepEqual(received.options, { format: 'frame', time: 0.5 });
  for (const body of [{ file: '../export-me.json', format: 'frame' }, { file: 'export-me.json', format: 'exe' }, { file: 'export-me.json', format: 'frame', time: -1 }, { file: 'export-me.json', format: 'frame', time: 601 }]) {
    await responseStatus(await f.request('/api/export', { method: 'POST', body }), 400);
  }
  await responseStatus(await f.request('/api/export', { method: 'POST', id: B, body: { file: 'export-me.json', format: 'atlas' } }), 404);
  await responseStatus(await f.request('/api/export', { method: 'POST', headers: { Origin: 'https://evil.test' }, body: { file: 'export-me.json', format: 'frame' } }), 403);
  assert.deepEqual((await fs.readdir(f.library(A))).filter(name => !name.startsWith('.')), ['export-me.json']);
});

test('expensive validation/export requests are serialized and failure releases capacity', async t => {
  let unblock;
  let started;
  const began = new Promise(resolve => { started = resolve; });
  const f = await fixture(t, { render: async () => {
    started();
    await new Promise(resolve => { unblock = resolve; });
    throw Object.assign(new Error('sensitive internal path /data/users/private'), { code: 'render_timeout' });
  } });
  await responseStatus(await save(f, 'busy'), 200);
  const first = f.request('/api/export', { method: 'POST', body: { file: 'busy.json', format: 'atlas' } });
  await began;
  for (const [url, body] of [['/api/export', { file: 'busy.json', format: 'frame' }], ['/api/validate', { data: effect() }]]) {
    const busy = await f.request(url, { method: 'POST', body });
    await responseStatus(busy, 429);
    assert.equal((await busy.json()).code, 'effects_busy');
  }
  unblock();
  const failed = await first;
  await responseStatus(failed, 504);
  assert.ok(!(await failed.text()).includes('/data/users'));
  await responseStatus(await f.request('/api/validate', { method: 'POST', body: { data: effect() } }), 200);
});

const exportFixturePNG = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 0]);
const exportFixtureResult = () => ({ png: exportFixturePNG, info: { width: 128, height: 96, frames: 4, fps: 16 } });

test('async exports return before rendering, isolate account access, and serve a completed PNG snapshot', async t => {
  let finish, received;
  const f = await fixture(t, { render: (data, settings) => {
    received = { data, settings };
    return new Promise(resolve => { finish = resolve; });
  } });
  await responseStatus(await save(f, 'async'), 200);
  const started = await responseStatus(await f.request('/api/exports', { method: 'POST', body: { file: 'async.json', format: 'atlas' } }), 202);
  const { job } = await started.json();
  assert.match(job.id, /^[0-9a-f-]{36}$/);
  assert.equal(job.status, 'running');
  assert.equal(job.file, 'async.json');
  assert.equal(job.format, 'atlas');
  assert.equal(job.time, null);
  assert.ok(Number.isFinite(Date.parse(job.created_at)));
  assert.equal(received.data.doc.name, 'async');
  assert.equal(received.settings.signal.aborted, false);
  const route = '/api/exports/' + job.id;
  assert.equal((await (await f.request(route)).json()).job.status, 'running');
  await responseStatus(await f.request(route + '/result'), 409);
  await responseStatus(await f.request('/health', { auth: null }), 200);
  for (const suffix of ['', '/result']) {
    await responseStatus(await f.request(route + suffix, { auth: null }), 401);
    await responseStatus(await f.request(route + suffix, { id: B, auth: token(B, { role: 'admin' }) }), suffix ? 409 : 200);
    await responseStatus(await f.request(route + suffix, { headers: { 'X-Effects-Owner': B } }), 403);
  }
  for (const action of ['/cancel', '/release']) {
    await responseStatus(await f.request(route + action, { method: 'POST', id: B, body: {} }), 403);
    await responseStatus(await f.request(route + action, { method: 'POST', auth: null, body: {} }), 401);
    await responseStatus(await f.request(route + action, { method: 'POST', headers: { 'X-Effects-CSRF': 'invalid' }, body: {} }), 403);
  }
  await responseStatus(await f.request(route + '/release', { method: 'POST', body: {} }), 409);
  for (const [url, body] of [
    ['/api/exports', { file: 'async.json', format: 'frame' }],
    ['/api/export', { file: 'async.json', format: 'frame' }],
    ['/api/validate', { data: effect() }],
  ]) await responseStatus(await f.request(url, { method: 'POST', body }), 429);
  await responseStatus(await save(f, 'async', A, effect('edited after start')), 200);
  assert.equal(received.data.doc.name, 'async', 'render uses the saved version captured at job creation');
  finish(exportFixtureResult());
  assert.equal((await (await f.request(route)).json()).job.status, 'done');
  const result = await responseStatus(await f.request(route + '/result'), 200);
  assert.equal(result.headers.get('content-type'), 'image/png');
  assert.equal(result.headers.get('x-effects-width'), '128');
  assert.equal(result.headers.get('x-effects-height'), '96');
  assert.equal(result.headers.get('x-effects-frames'), '4');
  assert.equal(result.headers.get('x-effects-fps'), '16');
  assert.deepEqual(Buffer.from(await result.arrayBuffer()), exportFixturePNG);
  await responseStatus(await f.request('/api/validate', { method: 'POST', body: { data: effect() } }), 200);
  assert.deepEqual((await fs.readdir(f.library(A))).filter(name => !name.startsWith('.')), ['async.json']);
});

test('async export cancellation aborts its worker and frees shared computation capacity', async t => {
  let signal;
  const f = await fixture(t, { render: (data, settings) => new Promise((resolve, reject) => {
    signal = settings.signal;
    signal.addEventListener('abort', () => reject(Object.assign(new Error('private renderer detail'), { code: 'render_cancelled' })), { once: true });
  }) });
  await responseStatus(await save(f, 'cancel-me'), 200);
  const { job } = await (await f.request('/api/exports', { method: 'POST', body: { file: 'cancel-me.json', format: 'frame', time: 0.3 } })).json();
  const route = '/api/exports/' + job.id;
  assert.equal(job.time, 0.3);
  const cancelled = await responseStatus(await f.request(route + '/cancel', { method: 'POST', body: {} }), 200);
  assert.equal((await cancelled.json()).job.status, 'cancelled');
  assert.equal(signal.aborted, true);
  assert.equal((await (await f.request(route)).json()).job.status, 'cancelled');
  await responseStatus(await f.request(route + '/result'), 409);
  await responseStatus(await f.request(route + '/cancel', { method: 'POST', body: {} }), 200);
  await responseStatus(await f.request('/api/validate', { method: 'POST', body: { data: effect() } }), 200);
  await responseStatus(await f.request(route + '/release', { method: 'POST', body: {} }), 200);
  await responseStatus(await f.request(route), 404);
});

test('failed async exports sanitize errors, reject invalid PNGs, and allow later requests', async t => {
  let calls = 0;
  const f = await fixture(t, { render: async () => {
    if (++calls === 1) throw new Error('/data/users/private/secret');
    return { png: Buffer.from('not a PNG'), info: {} };
  } });
  await responseStatus(await save(f, 'failed'), 200);
  for (let i = 0; i < 2; i++) {
    const accepted = await responseStatus(await f.request('/api/exports', { method: 'POST', body: { file: 'failed.json', format: 'atlas' } }), 202);
    const { job } = await accepted.json();
    const status = await responseStatus(await f.request('/api/exports/' + job.id), 200);
    const value = (await status.json()).job;
    assert.equal(value.status, 'failed');
    assert.match(value.error, /cannot be exported/);
    assert.ok(!JSON.stringify(value).includes('/data/users'));
    await responseStatus(await f.request('/api/exports/' + job.id + '/result'), 409);
  }
  await responseStatus(await f.request('/api/validate', { method: 'POST', body: { data: effect() } }), 200);
});

test('async export retention starts after completion and running jobs have no expiry', async t => {
  let now = Date.now(), finish;
  const f = await fixture(t, { now: () => now, render: () => new Promise(resolve => { finish = resolve; }) });
  await responseStatus(await save(f, 'long-render'), 200);
  const { job } = await (await f.request('/api/exports', { method: 'POST', body: { file: 'long-render.json', format: 'atlas' } })).json();
  const route = '/api/exports/' + job.id;
  now += 4 * 60 * 60 * 1000;
  assert.equal((await (await f.request(route)).json()).job.status, 'running');
  finish(exportFixtureResult());
  const completed = (await (await f.request(route)).json()).job;
  assert.equal(completed.status, 'done');
  assert.equal(Date.parse(completed.finished_at), now);
  now += 60 * 60 * 1000 - 1;
  await responseStatus(await f.request(route + '/result'), 200);
  now += 1;
  await responseStatus(await f.request(route), 404);
  await responseStatus(await f.request(route + '/result'), 404);
});

test('repeated failed and cancelled exports retain status without consuming PNG result capacity', async t => {
  const f = await fixture(t, { render: (data, settings) => {
    if (data.doc.name === 'fail-again') return Promise.reject(new Error('render failed'));
    if (data.doc.name === 'cancel-again') return new Promise((resolve, reject) => {
      settings.signal.addEventListener('abort', () => reject(Object.assign(new Error('cancelled'), { code: 'render_cancelled' })), { once: true });
    });
    return Promise.resolve(exportFixtureResult());
  } });
  for (const name of ['fail-again', 'cancel-again', 'succeed']) await responseStatus(await save(f, name), 200);
  const terminal = [];
  for (const name of ['fail-again', 'cancel-again']) {
    for (let i = 0; i < 4; i++) {
      const accepted = await responseStatus(await f.request('/api/exports', { method: 'POST', body: { file: name + '.json', format: 'atlas' } }), 202);
      const { job } = await accepted.json();
      const route = '/api/exports/' + job.id;
      if (name === 'cancel-again') await responseStatus(await f.request(route + '/cancel', { method: 'POST', body: {} }), 200);
      const status = name === 'fail-again' ? 'failed' : 'cancelled';
      assert.equal((await (await f.request(route)).json()).job.status, status);
      terminal.push({ route, status });
    }
  }
  for (let i = 0; i < 2; i++) {
    const accepted = await responseStatus(await f.request('/api/exports', { method: 'POST', body: { file: 'succeed.json', format: 'frame' } }), 202);
    const { job } = await accepted.json();
    assert.equal((await (await f.request('/api/exports/' + job.id)).json()).job.status, 'done');
  }
  await responseStatus(await f.request('/api/exports', { method: 'POST', body: { file: 'succeed.json', format: 'frame' } }), 429);
  for (const { route, status } of terminal) assert.equal((await (await f.request(route)).json()).job.status, status);
});

test('lightweight terminal history is FIFO bounded without evicting active or completed PNG jobs', async t => {
  const f = await fixture(t, { render: (data, settings) => {
    if (data.doc.name === 'keep-result') return Promise.resolve(exportFixtureResult());
    if (data.doc.name === 'keep-running') return new Promise((resolve, reject) => {
      settings.signal.addEventListener('abort', () => reject(Object.assign(new Error('cancelled'), { code: 'render_cancelled' })), { once: true });
    });
    return Promise.reject(new Error('failed metadata only'));
  } });
  for (const name of ['keep-result', 'keep-running', 'fail-many']) await responseStatus(await save(f, name), 200);
  const { job: completed } = await (await f.request('/api/exports', { method: 'POST', body: { file: 'keep-result.json', format: 'frame' } })).json();
  assert.equal((await (await f.request('/api/exports/' + completed.id)).json()).job.status, 'done');
  const guide = await (await f.request('/api/guide')).json();
  assert.equal(guide.limits.export_jobs.max_terminal_records, 128);
  const failed = [];
  for (let i = 0; i < 131; i++) {
    const response = await responseStatus(await f.request('/api/exports', { method: 'POST', body: { file: 'fail-many.json', format: 'frame' } }), 202);
    const { job } = await response.json();
    assert.equal((await (await f.request('/api/exports/' + job.id)).json()).job.status, 'failed');
    failed.push(job.id);
  }
  const accepted = await responseStatus(await f.request('/api/exports', { method: 'POST', body: { file: 'keep-running.json', format: 'atlas' } }), 202);
  const { job: running } = await accepted.json();
  let retained = 0;
  for (const [index, id] of failed.entries()) {
    const response = await f.request('/api/exports/' + id);
    await responseStatus(response, index < 3 ? 404 : 200);
    if (response.status === 200) retained++;
  }
  assert.equal(retained, 128);
  await responseStatus(await f.request('/api/exports/' + completed.id + '/result'), 200);
  assert.equal((await (await f.request('/api/exports/' + running.id)).json()).job.status, 'running');
  await responseStatus(await f.request('/api/exports/' + running.id + '/cancel', { method: 'POST', body: {} }), 200);
  await responseStatus(await f.request('/api/exports/' + failed[3]), 404, 'the next terminal record evicts only the oldest terminal history');
  await responseStatus(await f.request('/api/exports/' + completed.id + '/result'), 200);
});

test('completed result capacity is bounded per account and globally, and owner release restores it', async t => {
  const f = await fixture(t, { render: async () => exportFixtureResult() });
  const accounts = [A, B, '33333333-3333-4333-8333-333333333333', '44444444-4444-4444-8444-444444444444', '55555555-5555-4555-8555-555555555555'];
  const jobs = [];
  for (const id of accounts) await responseStatus(await save(f, 'capacity', id), 200);
  for (const id of accounts.slice(0, 4)) {
    for (let i = 0; i < 2; i++) {
      const accepted = await responseStatus(await f.request('/api/exports', { method: 'POST', id, body: { file: 'capacity.json', format: 'frame' } }), 202);
      const { job } = await accepted.json();
      assert.equal((await (await f.request('/api/exports/' + job.id, { id })).json()).job.status, 'done');
      jobs.push({ id, job });
    }
    const full = await responseStatus(await f.request('/api/exports', { method: 'POST', id, body: { file: 'capacity.json', format: 'frame' } }), 429);
    assert.equal((await full.json()).code, 'effects_exports_full');
  }
  await responseStatus(await f.request('/api/exports', { method: 'POST', id: accounts[4], body: { file: 'capacity.json', format: 'frame' } }), 429);
  const first = '/api/exports/' + jobs[0].job.id;
  await responseStatus(await f.request(first + '/release', { method: 'POST', id: B, body: {} }), 403);
  await responseStatus(await f.request(first + '/result'), 200);
  await responseStatus(await f.request(first + '/release', { method: 'POST', body: {} }), 200);
  await responseStatus(await f.request(first), 404);
  await responseStatus(await f.request('/api/exports', { method: 'POST', body: { file: 'capacity.json', format: 'frame' } }), 202);
  assert.deepEqual((await fs.readdir(f.library(A))).filter(name => !name.startsWith('.')), ['capacity.json']);
});

test('bundled upstream validator hydrates sparse files and catches invalid particle tracks', async t => {
  const vendor = new URL('../vendor/', import.meta.url);
  try { await fs.access(new URL('tools/validate.mjs', vendor)); }
  catch (error) { if (error.code === 'ENOENT') { t.skip('Run with the bundled vendor directory for semantic validation.'); return; } throw error; }
  const f = await fixture(t);
  await fs.cp(new URL('js/core/', vendor), path.join(f.vendorDir, 'js', 'core'), { recursive: true });
  await fs.copyFile(new URL('tools/validate.mjs', vendor), path.join(f.vendorDir, 'tools', 'validate.mjs'));
  const sparse = { app: 'arcadia-effects', v: 2, doc: { name: 'Sparse machine effect', layers: [] } };
  const valid = await f.request('/api/validate', { method: 'POST', body: { data: sparse } });
  await responseStatus(valid, 200);
  assert.equal((await valid.json()).valid, true);
  const invalid = { ...effect(), doc: { ...effect().doc, layers: [{ type: 'emitter', opacity: 'invalid-track' }] } };
  const report = await (await f.request('/api/validate', { method: 'POST', body: { data: invalid } })).json();
  assert.equal(report.valid, false);
  assert.ok(report.errors.some(message => message.includes('opacity')));
  assert.ok(!JSON.stringify(report).includes(f.directory));
  assert.deepEqual(await (await f.request('/api/list')).json(), []);
});
