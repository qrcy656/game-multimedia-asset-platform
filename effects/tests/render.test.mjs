import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createCanvas, loadImage} from '@napi-rs/canvas';
import {exportPNG} from '../render.mjs';
import {mock} from 'node:test';

const effect = {
  app: 'arcadia-effects', v: 2,
  doc: {
    v: 2, id: 'fx_render_test', name: 'Render test',
    comp: {w: 128, h: 96, dur: 0.6, fps: 30},
    exp: {cols: 2, rows: 2, frames: 4, cellW: 64, cellH: 48, ss: 1},
    layers: [{id: 'lr_render_test', type: 'emitter', name: 'Sparks',
      em: {bursts: [{t: 0, n: 12}], rate: 0, speed: 80, seed: 7},
      pt: {life: 0.5, size: 10, sprite: {kind: 'shape', id: 'dot', p: {}}, gravY: 30}}]
  }, tex: {}
};
function dimensions(png) {
  assert.deepEqual([...png.subarray(0, 8)], [137,80,78,71,13,10,26,10]);
  return [png.readUInt32BE(16), png.readUInt32BE(20)];
}
const frame = await exportPNG(effect, {format: 'frame', time: 0.2});
assert.deepEqual(dimensions(frame.png), [128, 96]);
assert.equal(frame.info.frames, 1);
const image = await loadImage(frame.png), canvas = createCanvas(128, 96), context = canvas.getContext('2d');
context.drawImage(image, 0, 0);
const pixels = context.getImageData(0, 0, 128, 96).data;
assert(pixels.some((n, i) => i % 4 === 3 && n > 0), 'frame contains visible pixels');
const second = await exportPNG(effect, {format: 'frame', time: 0.2});
assert.equal(createHash('sha256').update(frame.png).digest('hex'), createHash('sha256').update(second.png).digest('hex'));
const atlas = await exportPNG(effect, {format: 'atlas'});
assert.deepEqual(dimensions(atlas.png), [128, 96]);
assert.equal(atlas.info.frames, 4);
assert.equal(atlas.info.frameWidth, 64);
assert(atlas.info.fps > 0);

// Explicitly embedded PNGs decode; no network or filesystem texture source is accepted.
const textureCanvas = createCanvas(16, 16);
textureCanvas.getContext('2d').fillRect(0, 0, 16, 16);
const textured = structuredClone(effect);
textured.doc.textures = [{id: 'tex_fixture', name: 'fixture'}];
textured.tex.tex_fixture = textureCanvas.toDataURL('image/png');
textured.doc.layers[0].pt.sprite = {kind: 'tex', texId: 'tex_fixture'};
assert((await exportPNG(textured, {format: 'frame', time: 0.2})).png.length > 100);

for (const mutate of [
  fx => fx.doc.comp.w = 99999,
  fx => fx.doc.comp.dur = 1000,
  fx => fx.doc.exp.frames = 1000,
  fx => {fx.doc.exp.cols = 64; fx.doc.exp.cellW = 128;},
  fx => fx.doc.layers = Array.from({length: 25}, () => structuredClone(fx.doc.layers[0])),
  fx => fx.doc.layers[0].start = -100,
  fx => {fx.doc.textures = [{id: 'remote'}]; fx.tex.remote = 'http://127.0.0.1/internal';},
  fx => {fx.doc.textures = [{id: 'remote'}]; fx.tex.remote = 'file:///etc/passwd';},
  fx => {fx.doc.textures = [{id: 'remote'}]; fx.tex.remote = 'data:image/svg+xml;base64,PHN2Zz4=';},
  fx => {fx.doc.layers[0].constructor = {};}
]) {
  const invalid = structuredClone(effect); mutate(invalid);
  await assert.rejects(exportPNG(invalid, {format: 'atlas'}), {code: 'render_invalid'});
}
await assert.rejects(exportPNG(effect, {format: 'javascript'}), {code: 'render_invalid'});
await assert.rejects(exportPNG(effect, {format: 'frame', time: -1}), {code: 'render_invalid'});
// Advancing wall-clock timers past the former deadline must not kill the renderer.
mock.timers.enable({apis: ['setTimeout']});
try {
  const pending = exportPNG(effect, {format: 'atlas'});
  mock.timers.tick(60000);
  assert.equal((await pending).info.frames, 4);
} finally { mock.timers.reset(); }
const cancelled = new AbortController();
cancelled.abort();
await assert.rejects(exportPNG(effect, {format: 'frame', time: 0.2, signal: cancelled.signal}), {code: 'render_cancelled'});
const running = new AbortController();
const interrupted = exportPNG(effect, {format: 'atlas', signal: running.signal});
running.abort();
await assert.rejects(interrupted, {code: 'render_cancelled'});
console.log('Render checks passed: deterministic visible frame, atlas metadata, embedded PNG, resource bounds, and blocked URL/SVG textures.');
