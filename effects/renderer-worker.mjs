import { parentPort, workerData } from 'node:worker_threads';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';
import { createCanvas, loadImage } from '@napi-rs/canvas';

const {limits: LIMIT, format, time} = workerData;
const fail = message => { throw new Error(message); };
function range(value, min, max, label, integer = false) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max || (integer && !Number.isInteger(value))) {
    fail(label + ' is outside the server export limit.');
  }
}
function inspectTree(value, depth = 0, count = {n: 0}) {
  if (depth > 32 || ++count.n > 120000) fail('Effect structure is too large.');
  if (typeof value === 'number' && (!Number.isFinite(value) || Math.abs(value) > 1e6)) fail('Invalid effect number.');
  if (!value || typeof value !== 'object') return;
  if (Array.isArray(value) && value.length > 4096) fail('Effect array is too large.');
  for (const key of Object.keys(value)) {
    if (['__proto__', 'prototype', 'constructor'].includes(key)) fail('Invalid effect key.');
    inspectTree(value[key], depth + 1, count);
  }
}
function pngBytes(payload) {
  if (typeof payload !== 'string' || !/^data:image\/png;base64,[A-Za-z0-9+/]*={0,2}$/.test(payload)) fail('Textures must be embedded PNG data.');
  const bytes = Buffer.from(payload.slice(payload.indexOf(',') + 1), 'base64');
  if (bytes.length < 33 || bytes.length > 6 * 1024 * 1024 || !bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) || bytes.toString('ascii',12,16) !== 'IHDR') fail('Invalid PNG texture.');
  const width = bytes.readUInt32BE(16), height = bytes.readUInt32BE(20);
  range(width, 1, LIMIT.maxTextureSide, 'Texture width', true);
  range(height, 1, LIMIT.maxTextureSide, 'Texture height', true);
  if (width * height > LIMIT.maxTexturePixels) fail('Texture pixel limit exceeded.');
  return bytes;
}

// Bound native canvas allocations, which are outside the worker's V8 heap limit.
let allocatedPixels = 0, canvasCount = 0;
function boundedCanvas() {
  if (++canvasCount > 2048) fail('Canvas count limit exceeded.');
  const canvas = createCanvas(1, 1);
  let width = 1, height = 1;
  allocatedPixels++;
  for (const dimension of ['width', 'height']) {
    let proto = canvas, descriptor;
    while (proto && !descriptor) { descriptor = Object.getOwnPropertyDescriptor(proto, dimension); proto = Object.getPrototypeOf(proto); }
    if (!descriptor || !descriptor.set) fail('Unsupported canvas implementation.');
    Object.defineProperty(canvas, dimension, {
      configurable: false, enumerable: true,
      get() { return dimension === 'width' ? width : height; },
      set(value) {
        range(value, 1, LIMIT.maxAtlasSide, 'Canvas size', true);
        const pixels = dimension === 'width' ? value * height : width * value;
        const nextTotal = allocatedPixels - width * height + pixels;
        if (pixels > LIMIT.maxAtlasPixels || nextTotal > 48 * 1024 * 1024) fail('Canvas memory limit exceeded.');
        descriptor.set.call(canvas, value);
        allocatedPixels = nextTotal;
        if (dimension === 'width') width = value; else height = value;
      }
    });
  }
  return canvas;
}

async function render() {
  const effect = JSON.parse(workerData.serialized);
  inspectTree(effect);
  if (!effect || effect.app !== 'arcadia-effects' || !effect.doc || !Array.isArray(effect.doc.layers)) fail('Expected an Arcadia effect file.');
  if (effect.doc.layers.length > LIMIT.maxLayers) fail('Too many effect layers.');
  if (effect.doc.layers.some(layer => !layer || !['emitter','sprite','atlas','postfx','force'].includes(layer.type))) fail('Unknown layer type.');
  if (effect.doc.layers.filter(layer => layer.type === 'emitter').length > LIMIT.maxEmitters) fail('Too many particle emitters.');
  const context = vm.createContext({
    console: {warn() {}, error() {}, log() {}},
    document: { querySelector() { return null; }, createElement(tag) { if (tag !== 'canvas') fail('Unsupported rendering element.'); return boundedCanvas(); } }
  }, {codeGeneration: {strings: false, wasm: false}});
  context.window = context;
  // This list and its paths are fixed. User effect data is never evaluated as JavaScript.
  for (const name of ['utils', 'track', 'curve', 'path', 'sprites', 'model', 'sim', 'engine', 'atlas']) {
    const vendorRoot = process.env.VENDOR_DIR || fileURLToPath(new URL('./vendor/', import.meta.url));
    const source = fs.readFileSync(path.join(vendorRoot, 'js', 'core', name + '.js'), 'utf8');
    vm.runInContext(source, context, {filename: 'core/' + name + '.js', timeout: 2000});
  }
  const AFX = context.AFX;
  AFX.t = key => key;
  const doc = AFX.Model.migrate(effect.doc);
  range(doc.comp.w, 8, LIMIT.maxCompSide, 'Composition width', true);
  range(doc.comp.h, 8, LIMIT.maxCompSide, 'Composition height', true);
  range(doc.comp.dur, 0.01, LIMIT.maxDuration, 'Duration');
  range(doc.comp.fps, 1, 120, 'FPS');
  for (const layer of doc.layers) {
    range(layer.start, -5, LIMIT.maxDuration, 'Layer start');
    range(layer.end, layer.start, LIMIT.maxDuration, 'Layer end');
    if (layer.em) {
      if (!Array.isArray(layer.em.bursts) || layer.em.bursts.length > 256) fail('Too many emission bursts.');
      if (layer.em.path && layer.em.path.nodes && layer.em.path.nodes.length > 128) fail('Too many path nodes.');
      if (layer.em.sub && layer.em.sub.on) {
        range(layer.em.sub.maxGen, 1, 4, 'Sub-emitter generations', true);
        range(layer.em.sub.budget, 0, 2000, 'Sub-emitter particle budget', true);
      }
    }
  }
  if (!Array.isArray(doc.textures) || doc.textures.length > LIMIT.maxTextures) fail('Too many textures.');
  // loadImage receives decoded bytes only: no HTTP, file URL or filesystem reads.
  const ids = new Set();
  let texturePixels = 0;
  for (const meta of doc.textures) {
    if (!meta || typeof meta.id !== 'string' || ids.has(meta.id)) fail('Invalid texture identifier.');
    ids.add(meta.id);
    const bytes = pngBytes(effect.tex && effect.tex[meta.id]);
    texturePixels += bytes.readUInt32BE(16) * bytes.readUInt32BE(20);
    if (texturePixels > 16 * 1024 * 1024) fail('Texture memory limit exceeded.');
    const img = await loadImage(bytes);
    AFX.Sprites.texs.set(meta.id, {id: meta.id, name: meta.name, img, dataURL: effect.tex[meta.id], sheet: meta.sheet || null});
  }
  // Enforce an aggregate live-particle budget across all simulation instances.
  const simulations = new Set();
  const originalStep = AFX.Sim.prototype.step;
  AFX.Sim.prototype.step = function () {
    simulations.add(this);
    const result = originalStep.apply(this, arguments);
    let total = 0;
    for (const simulation of simulations) total += simulation.parts.length;
    if (total > LIMIT.maxLiveParticles) fail('Live particle budget exceeded.');
    return result;
  };
  let canvas, info;
  if (format === 'atlas') {
    const exp = doc.exp;
    range(exp.cols, 1, 64, 'Atlas columns', true);
    range(exp.rows, 1, 64, 'Atlas rows', true);
    range(exp.frames, 1, LIMIT.maxFrames, 'Atlas frames', true);
    if (exp.frames > exp.cols * exp.rows) fail('Atlas frames exceed the cell count.');
    range(exp.t0, 0, doc.comp.dur, 'Atlas start');
    if (exp.t1 !== -1) range(exp.t1, exp.t0, doc.comp.dur, 'Atlas end');
    const cell = AFX.Atlas.cellSize(doc);
    range(cell.w, 8, LIMIT.maxCellSide, 'Atlas cell width', true);
    range(cell.h, 8, LIMIT.maxCellSide, 'Atlas cell height', true);
    const width = exp.cols * cell.w, height = exp.rows * cell.h;
    if (width > LIMIT.maxAtlasSide || height > LIMIT.maxAtlasSide || width * height > LIMIT.maxAtlasPixels) fail('Atlas dimensions exceed the export limit.');
    const result = AFX.Atlas.build(doc);
    canvas = result.canvas;
    info = {...result.info, format, width, height};
  } else {
    const sample = time === undefined ? doc.comp.dur * 0.35 : time;
    range(sample, 0, doc.comp.dur, 'Frame time');
    canvas = boundedCanvas(); canvas.width = doc.comp.w; canvas.height = doc.comp.h;
    new AFX.Engine().render(doc, sample, canvas.getContext('2d'));
    info = {format, width: canvas.width, height: canvas.height, frames: 1, fps: doc.comp.fps, time: sample};
  }
  const png = canvas.toBuffer('image/png');
  return {ok: true, png: new Uint8Array(png), info};
}

try { parentPort.postMessage(await render()); }
catch (error) { parentPort.postMessage({ok: false, error: error && error.message ? String(error.message).slice(0, 180) : 'Invalid effect export.'}); }
