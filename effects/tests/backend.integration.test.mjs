// Real HTTP -> account storage -> upstream validator/renderer, without mocks.
// Run in the built image with these tests mounted at /app/tests (network none).
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHmac, createHash } from 'node:crypto';
import { createCanvas, loadImage } from '@napi-rs/canvas';
import { createEffectsServer } from '../server.mjs';

test('real HTTP machine workflow validates, saves, reads, and exports an isolated deterministic effect', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'fantasy-effects-http-'));
  const secret = 'isolated-integration-test-signing-secret-32-characters';
  const id = '33333333-3333-4333-8333-333333333333';
  const otherId = '44444444-4444-4444-8444-444444444444';
  const publicOrigin = 'https://effects.integration.test';
  const server = createEffectsServer({ secret, publicOrigin, homeUrl: 'https://portal.integration.test', dataDir: directory,
    vendorDir: fileURLToPath(new URL('../vendor/', import.meta.url)), frontendDir: fileURLToPath(new URL('../frontend/', import.meta.url)) });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    await fs.rm(directory, { recursive: true, force: true });
  });
  const base = 'http://127.0.0.1:' + server.address().port;
  function signed(user) {
    const now = Math.floor(Date.now() / 1000);
    const encoded = [{ alg: 'HS256', typ: 'JWT' }, { iss: 'fantasy-main', aud: 'fantasy-effects', sub: user, role: 'user', name: 'Integration AI', iat: now, exp: now + 60 }]
      .map(value => Buffer.from(JSON.stringify(value)).toString('base64url')).join('.');
    return encoded + '.' + createHmac('sha256', secret).update(encoded).digest('base64url');
  }
  const request = (url, body, user = id) => fetch(base + url, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { Authorization: 'Bearer ' + signed(user), 'X-Effects-Owner': user, ...(body === undefined ? {} : {
      Origin: publicOrigin, 'Content-Type': 'application/json',
      'X-Effects-CSRF': createHmac('sha256', secret).update('fantasy-effects\0csrf\0' + user).digest('hex'),
    }) }, body: body === undefined ? undefined : JSON.stringify(body),
  });
  async function ok(response) { assert.equal(response.status, 200, await response.clone().text()); return response; }
  const guide = await (await ok(await request('/api/guide'))).json();
  assert.ok(guide.format.includes('arcadia-effects'));
  assert.ok(guide.guide.length > 100);
  assert.equal(guide.limits.render.maxFrames, 64);
  assert.equal(guide.limits.render.timeoutMs, null);
  const effect = { app: 'arcadia-effects', v: 2, doc: {
    v: 2, id: 'fx_http_fixture', name: 'HTTP integration sparks',
    comp: { w: 128, h: 96, dur: 0.6, fps: 30 },
    exp: { cols: 2, rows: 2, frames: 4, cellW: 64, cellH: 48, ss: 1 },
    layers: [{ id: 'lr_http_fixture', type: 'emitter', name: 'Sparks',
      em: { bursts: [{ t: 0, n: 12 }], rate: 0, speed: 80, seed: 7 },
      pt: { life: 0.5, size: 10, sprite: { kind: 'shape', id: 'dot', p: {} }, gravY: 30 } }],
  }, tex: {} };
  const validation = await (await ok(await request('/api/validate', { data: effect }))).json();
  assert.equal(validation.valid, true, JSON.stringify(validation));
  const saved = await (await ok(await request('/api/save', { name: 'AI隔离测试', data: effect, create_only: true }))).json();
  const read = await ok(await request('/api/effect?f=' + encodeURIComponent(saved.file)));
  const raw = await read.text();
  assert.equal(read.headers.get('etag'), '"' + saved.revision + '"');
  assert.equal(createHash('sha256').update(raw).digest('hex'), saved.revision);
  assert.deepEqual(JSON.parse(raw), effect);
  const first = await ok(await request('/api/export', { file: saved.file, format: 'frame', time: 0.2 }));
  assert.equal(first.headers.get('content-type'), 'image/png');
  assert.equal(first.headers.get('x-effects-width'), '128');
  assert.equal(first.headers.get('x-effects-height'), '96');
  assert.equal(first.headers.get('x-effects-frames'), '1');
  const png = Buffer.from(await first.arrayBuffer());
  const image = await loadImage(png), canvas = createCanvas(128, 96), context = canvas.getContext('2d');
  context.drawImage(image, 0, 0);
  assert.ok(context.getImageData(0, 0, 128, 96).data.some((channel, index) => index % 4 === 3 && channel > 0), 'exported frame has visible particles');
  const second = await ok(await request('/api/export', { file: saved.file, format: 'frame', time: 0.2 }));
  assert.deepEqual(Buffer.from(await second.arrayBuffer()), png, 'same seed and frame time produce identical PNG');
  const atlas = await ok(await request('/api/export', { file: saved.file, format: 'atlas' }));
  assert.equal(atlas.headers.get('x-effects-frames'), '4');
  assert.ok(Number(atlas.headers.get('x-effects-fps')) > 0);
  const atlasPng = Buffer.from(await atlas.arrayBuffer());
  assert.equal(atlasPng.readUInt32BE(16), 128);
  assert.equal(atlasPng.readUInt32BE(20), 96);
  const accepted = await request('/api/exports', { file: saved.file, format: 'frame', time: 0.2 });
  assert.equal(accepted.status, 202, await accepted.clone().text());
  const { job } = await accepted.json();
  assert.equal(job.status, 'running');
  assert.equal(job.time, 0.2);
  const route = '/api/exports/' + job.id;
  let status;
  for (let attempts = 0; attempts < 250; attempts++) {
    status = (await (await ok(await request(route))).json()).job;
    if (status.status !== 'running') break;
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  assert.equal(status.status, 'done', JSON.stringify(status));
  assert.equal((await request(route, undefined, otherId)).status, 200);
  assert.equal((await request(route + '/result', undefined, otherId)).status, 200);
  assert.equal((await request(route + '/release', {}, otherId)).status, 403);
  const asyncResult = await ok(await request(route + '/result'));
  assert.equal(asyncResult.headers.get('x-effects-width'), '128');
  assert.equal(asyncResult.headers.get('x-effects-frames'), '1');
  assert.deepEqual(Buffer.from(await asyncResult.arrayBuffer()), png, 'async and inline exports use the same renderer');
  await ok(await request(route + '/release', {}));
  assert.equal((await request(route)).status, 404);
  assert.equal((await request('/api/export', { file: saved.file, format: 'frame' }, otherId)).status, 404);
  const shared = await (await request('/api/list', undefined, otherId)).json();
  assert.equal(shared.length, 1);
  assert.equal(shared[0].editable, false);
  await ok(await request('/api/export', { file: saved.file, format: 'frame', owner_key: shared[0].owner_key }, otherId));
  const invalid = structuredClone(effect);
  invalid.doc.comp.w = 99999;
  await ok(await request('/api/save', { name: 'oversized', data: invalid }));
  assert.equal((await request('/api/export', { file: 'oversized.json', format: 'atlas' })).status, 400);
  const library = path.join(directory, 'users', createHash('sha256').update(id).digest('hex'), 'library');
  assert.deepEqual((await fs.readdir(library)).sort(), ['.owner.json', 'AI隔离测试.json', 'oversized.json'].sort(), 'PNG results are not persisted');
  for (const file of [saved.file, 'oversized.json']) await ok(await request('/api/delete', { file }));
  assert.deepEqual(await (await request('/api/list')).json(), []);
});
