// Community Arcadia-Effects adapter. All identity comes from the Fantasy portal.
// No upstream server, public library directory, or client-selected storage root.
import http from 'node:http';
import fs from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash, createHmac, randomUUID, timingSafeEqual } from 'node:crypto';
import { spawn } from 'node:child_process';
import os from 'node:os';

const MAX_BYTES = 80 * 1024 * 1024;
const EXPORT_JOB_TTL = 60 * 60 * 1000;
const EXPORT_JOBS_PER_ACCOUNT = 2;
const EXPORT_JOBS_TOTAL = 8;
const EXPORT_TERMINAL_RECORDS = 128;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const CSP = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; worker-src 'self' blob:; media-src 'self' data: blob:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };

class HttpError extends Error {
  constructor(status, message, code) { super(message); this.status = status; this.code = code; }
}
const fail = (status, message, code) => { throw new HttpError(status, message, code); };
const isObject = value => !!value && typeof value === 'object' && !Array.isArray(value);
const digest = value => createHash('sha256').update(value).digest('hex');
const safeEqual = (a, b) => typeof a === 'string' && typeof b === 'string' && Buffer.byteLength(a) === Buffer.byteLength(b) && timingSafeEqual(Buffer.from(a), Buffer.from(b));

export function configuration(options = {}) {
  const secret = options.secret ?? process.env.EFFECTS_IDENTITY_SECRET ?? '';
  if (typeof secret !== 'string' || secret.trim().length < 32) throw new Error('EFFECTS_IDENTITY_SECRET must contain at least 32 characters.');
  const publicOrigin = options.publicOrigin ?? process.env.PUBLIC_ORIGIN;
  const homeUrl = options.homeUrl ?? process.env.HOME_URL;
  const basePath = options.basePath ?? process.env.EFFECTS_BASE_PATH ?? '';
  if (typeof basePath !== 'string' || !/^(?:\/[A-Za-z0-9_-]+)*$/.test(basePath)) throw new Error('Invalid EFFECTS_BASE_PATH.');
  for (const value of [publicOrigin, homeUrl]) {
    let url;
    try { url = new URL(value); } catch { throw new Error('PUBLIC_ORIGIN and HOME_URL must be absolute HTTP(S) origins.'); }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('PUBLIC_ORIGIN and HOME_URL must be absolute HTTP(S) origins.');
  }
  const maxBytes = options.maxBytes ?? MAX_BYTES;
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1024 || maxBytes > MAX_BYTES) throw new Error('Invalid maximum request size.');
  return Object.freeze({
    secret, publicOrigin: new URL(publicOrigin).origin, homeUrl: new URL(homeUrl).origin, basePath,
    dataDir: path.resolve(options.dataDir ?? process.env.DATA_DIR ?? '/data'),
    vendorDir: path.resolve(options.vendorDir ?? process.env.VENDOR_DIR ?? '/app/vendor'),
    frontendDir: path.resolve(options.frontendDir ?? process.env.FRONTEND_DIR ?? '/app/frontend'),
    maxBytes,
  });
}

function decodePart(value) {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) fail(401, 'Authentication required.');
  const buffer = Buffer.from(value, 'base64url');
  if (buffer.toString('base64url') !== value) fail(401, 'Authentication required.');
  try { return JSON.parse(buffer.toString('utf8')); } catch { fail(401, 'Authentication required.'); }
}

export function authenticate(authorization, config, now = Math.floor(Date.now() / 1000)) {
  if (typeof authorization !== 'string' || authorization.length > 8192 || !authorization.startsWith('Bearer ')) fail(401, 'Authentication required.');
  const parts = authorization.slice(7).split('.');
  if (parts.length !== 3) fail(401, 'Authentication required.');
  const header = decodePart(parts[0]);
  if (!isObject(header) || header.alg !== 'HS256' || (header.typ && header.typ !== 'JWT') || header.crit || header.b64 === false) fail(401, 'Authentication required.');
  const signature = createHmac('sha256', config.secret).update(parts[0] + '.' + parts[1]).digest('base64url');
  if (!safeEqual(signature, parts[2])) fail(401, 'Authentication required.');
  const claims = decodePart(parts[1]);
  if (!isObject(claims) || claims.iss !== 'fantasy-main' || claims.aud !== 'fantasy-effects' ||
      typeof claims.sub !== 'string' || !UUID.test(claims.sub) || !['user', 'admin'].includes(claims.role) ||
      !Number.isSafeInteger(claims.iat) || !Number.isSafeInteger(claims.exp) || claims.iat > now + 5 ||
      claims.exp <= now || claims.exp <= claims.iat || claims.exp - claims.iat > 120 ||
      (claims.nbf !== undefined && (!Number.isSafeInteger(claims.nbf) || claims.nbf > now + 5))) fail(401, 'Authentication required.');
  if (claims.name !== undefined && (typeof claims.name !== 'string' || claims.name.length > 256)) fail(401, 'Authentication required.');
  return Object.freeze({ id: claims.sub, name: claims.name || 'User', admin: claims.role === 'admin' });
}

export function sessionFor(user, config) {
  return {
    user,
    csrf: createHmac('sha256', config.secret).update('fantasy-effects\0csrf\0' + user.id).digest('hex'),
    home_url: config.homeUrl + '/', logout_url: config.homeUrl + '/logout',
    api_base: config.basePath,
    storage_key: 'fantasy.effects.' + digest(user.id) + '.',
    owner_key: digest(user.id),
  };
}

function checkOwner(req, user, required) {
  const owner = req.headers['x-effects-owner'];
  if ((required || owner !== undefined) && owner !== user.id) fail(403, 'Account changed. Reload the workbench.', 'owner_changed');
}

function checkWrite(req, user, config) {
  if (req.headers.origin !== config.publicOrigin) fail(403, 'Invalid request origin.', 'invalid_origin');
  if (!safeEqual(req.headers['x-effects-csrf'], sessionFor(user, config).csrf)) fail(403, 'Invalid request token.', 'invalid_csrf');
}

function headers(res) {
  res.setHeader('Cache-Control', 'no-store, private, max-age=0');
  res.setHeader('Pragma', 'no-cache');
  res.setHeader('Expires', '0');
  res.setHeader('Vary', 'Authorization, X-Effects-Owner');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('Content-Security-Policy', CSP);
  res.setHeader('X-Frame-Options', 'DENY');
}

function send(res, status, body, contentType = 'application/json; charset=utf-8', head = false) {
  const bytes = Buffer.isBuffer(body) ? body : Buffer.from(typeof body === 'string' ? body : JSON.stringify(body));
  res.writeHead(status, { 'Content-Type': contentType, 'Content-Length': bytes.length });
  res.end(head ? undefined : bytes);
}

function checkedPNG(result) {
  const png = Buffer.from(result.png);
  if (png.length > MAX_BYTES || !png.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) fail(500, 'PNG export failed.');
  return { png, info: result.info };
}

function sendPNG(res, result, format) {
  for (const [name, key] of [['Width', 'width'], ['Height', 'height'], ['Frames', 'frames'], ['Fps', 'fps']]) {
    const value = result.info?.[key];
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) res.setHeader('X-Effects-' + name, String(value));
  }
  res.setHeader('Content-Disposition', 'attachment; filename="effect-' + format + '.png"');
  return send(res, 200, result.png, 'image/png');
}

async function bodyJson(req, maxBytes) {
  if (!/^application\/json(?:\s*;|$)/i.test(req.headers['content-type'] || '')) fail(415, 'Send an application/json request.');
  const declaredLength = Number(req.headers['content-length']);
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) { req.resume(); fail(413, 'Effect exceeds the size limit.'); }
  const raw = await new Promise((resolve, reject) => {
    const chunks = [];
    let length = 0, exceeded = false;
    req.on('data', chunk => {
      length += chunk.length;
      if (length <= maxBytes) chunks.push(chunk);
      else if (!exceeded) {
        exceeded = true;
        chunks.length = 0;
        reject(new HttpError(413, 'Effect exceeds the size limit.'));
      }
    });
    req.on('end', () => { if (!exceeded) resolve(Buffer.concat(chunks)); });
    req.on('error', () => reject(new HttpError(400, 'Incomplete request.')));
    req.on('aborted', () => reject(new HttpError(400, 'Incomplete request.')));
  });
  try { return JSON.parse(raw.toString('utf8')); }
  catch { fail(400, 'Invalid JSON request.'); }
}

function safeFile(file) {
  if (typeof file !== 'string' || !file.endsWith('.json') || file.length <= 5 || Buffer.byteLength(file) > 244 ||
      file.trim() !== file || file.startsWith('.') || /[\\/\x00-\x1f\x7f:*?"<>|]/u.test(file) || file.includes('..')) fail(400, 'Invalid effect filename.');
  return file;
}

function fileForName(name) {
  if (typeof name !== 'string' || name.trim().length === 0) fail(400, 'An effect name is required.');
  return safeFile(name.trim() + '.json');
}

function validImage(value) {
  return typeof value === 'string' && /^data:image\/(?:png|jpeg|webp|gif|bmp);base64,[A-Za-z0-9+/]*={0,2}$/.test(value);
}

export function validateEffect(data) {
  if (!isObject(data) || data.app !== 'arcadia-effects' || !isObject(data.doc) ||
      (data.v !== undefined && ![1, 2].includes(data.v))) fail(400, 'Invalid Arcadia effect document.');
  const doc = data.doc;
  if ((doc.name !== undefined && (typeof doc.name !== 'string' || doc.name.length > 512)) ||
      (doc.id !== undefined && (typeof doc.id !== 'string' || doc.id.length > 256)) ||
      (doc.comp !== undefined && !isObject(doc.comp)) ||
      (doc.layers !== undefined && (!Array.isArray(doc.layers) || doc.layers.some(layer => !isObject(layer) || (layer.type !== undefined && !['emitter', 'sprite', 'atlas', 'postfx', 'force'].includes(layer.type))))) ||
      (doc.textures !== undefined && (!Array.isArray(doc.textures) || doc.textures.some(texture => !isObject(texture))))) fail(400, 'Invalid Arcadia effect document.');
  if (data.tex !== undefined && (!isObject(data.tex) || Object.values(data.tex).some(value => !validImage(value)))) fail(400, 'Invalid embedded texture.');
  if (data.thumb != null && (!validImage(data.thumb) || Buffer.byteLength(data.thumb) > 2 * 1024 * 1024)) fail(400, 'Invalid effect thumbnail.');
  // Sparse upstream documents remain supported. Reject prototype-mutating keys
  // before the browser's default-field hydration receives imported JSON.
  const pending = [[data, 0]];
  let count = 0;
  while (pending.length) {
    const [value, depth] = pending.pop();
    if (++count > 200000 || depth > 64) fail(400, 'Effect document is too complex.');
    if (typeof value === 'number' && !Number.isFinite(value)) fail(400, 'Effect numbers must be finite.');
    if (value && typeof value === 'object') {
      for (const [key, child] of Object.entries(value)) {
        if (['__proto__', 'constructor', 'prototype'].includes(key)) fail(400, 'Invalid effect property.');
        pending.push([child, depth + 1]);
      }
    }
  }
  return data;
}

async function checkedDirectory(directory) {
  const stat = await fs.lstat(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink() || await fs.realpath(directory) !== directory) fail(409, 'Storage is unavailable.');
}

async function userLibrary(config, user) {
  await fs.mkdir(config.dataDir, { recursive: true, mode: 0o700 });
  await checkedDirectory(config.dataDir);
  let directory = config.dataDir;
  for (const part of ['users', digest(user.id), 'library']) {
    directory = path.join(directory, part);
    try { await fs.mkdir(directory, { mode: 0o700 }); } catch (error) { if (error.code !== 'EEXIST') throw error; }
    await checkedDirectory(directory);
  }
  await atomicWrite(directory, '.owner.json', Buffer.from(JSON.stringify({ name: user.name })));
  return directory;
}

function libraryKey(user, selected) {
  if (selected === undefined || selected === null || selected === '') return digest(user.id);
  if (typeof selected !== 'string' || !/^[a-f0-9]{64}$/.test(selected)) fail(400, 'Invalid content owner.');
  return selected;
}

async function contentLibrary(config, user, selected, write = false) {
  const key = libraryKey(user, selected);
  const own = key === digest(user.id);
  if (write && !own && !user.admin) fail(403, 'Shared effects are read-only. Save a copy to your own library.', 'content_read_only');
  if (own) return { key, directory: await userLibrary(config, user) };
  const directory = path.join(config.dataDir, 'users', key, 'library');
  await checkedDirectory(directory);
  return { key, directory };
}

async function libraries(config, user) {
  await userLibrary(config, user);
  const root = path.join(config.dataDir, 'users');
  const result = [];
  for (const entry of await fs.readdir(root, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.isSymbolicLink() || !/^[a-f0-9]{64}$/.test(entry.name)) continue;
    const directory = path.join(root, entry.name, 'library');
    try {
      await checkedDirectory(directory);
      let name = '协作者 ' + entry.name.slice(0, 8);
      try { const value = JSON.parse((await readRegular(path.join(directory, '.owner.json'), 4096)).data); if (typeof value.name === 'string') name = value.name; } catch {}
      result.push({ key: entry.name, directory, name });
    } catch {}
  }
  return result;
}

async function readRegular(filename, maxBytes) {
  // realpath also rejects symlinks in intermediate directories, O_NOFOLLOW
  // prevents switching the final component to a link between check and open.
  if (await fs.realpath(filename) !== filename) fail(404, 'Not found.');
  const handle = await fs.open(filename, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
  try {
    const stat = await handle.stat();
    if (!stat.isFile()) fail(404, 'Not found.');
    if (stat.size > maxBytes) fail(413, 'Effect exceeds the size limit.');
    return { data: await handle.readFile(), stat };
  } finally { await handle.close(); }
}

async function existingRegular(filename) {
  try {
    const stat = await fs.lstat(filename);
    if (!stat.isFile() || stat.isSymbolicLink()) fail(409, 'Effect file is unavailable.');
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
}

async function atomicWrite(directory, file, bytes, { createOnly = false, expectedSha256 } = {}) {
  await checkedDirectory(directory);
  const target = path.join(directory, file);
  await existingRegular(target);
  if (expectedSha256 !== undefined) {
    let current;
    try { current = await readRegular(target, MAX_BYTES); }
    catch (error) { if (error.code === 'ENOENT') fail(409, 'Effect changed. Read it again before saving.', 'revision_conflict'); throw error; }
    if (digest(current.data) !== expectedSha256) fail(409, 'Effect changed. Read it again before saving.', 'revision_conflict');
  }
  const temporary = path.join(directory, '.pending-' + randomUUID());
  let handle;
  try {
    handle = await fs.open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW || 0), 0o600);
    await handle.writeFile(bytes);
    await handle.sync();
    await handle.close();
    handle = null;
    await checkedDirectory(directory);
    await existingRegular(target);
    if (createOnly) {
      // Hard-link creation is an atomic no-replace operation on this volume.
      try { await fs.link(temporary, target); }
      catch (error) { if (error.code === 'EEXIST') fail(409, 'An effect with this name already exists.', 'revision_conflict'); throw error; }
    } else {
      await fs.rename(temporary, target);
    }
  } finally {
    if (handle) await handle.close().catch(() => {});
    await fs.unlink(temporary).catch(() => {});
  }
}

async function runUpstreamValidation(data, config) {
  const script = path.join(config.vendorDir, 'tools', 'validate.mjs');
  await readRegular(script, 1024 * 1024);
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'fantasy-effects-validate-'));
  try {
    const input = path.join(directory, 'effect.json');
    await fs.writeFile(input, JSON.stringify(data), { mode: 0o600, flag: 'wx' });
    return await new Promise((resolve, reject) => {
      // Run only the bundled trusted validator, never code supplied in a document.
      const child = spawn(process.execPath, ['--max-old-space-size=192', script, input], {
        stdio: ['ignore', 'pipe', 'ignore'], cwd: config.vendorDir,
        env: { PATH: process.env.PATH || '', LANG: 'C.UTF-8' },
      });
      let output = '', size = 0, timeout = false, overflow = false;
      const timer = setTimeout(() => { timeout = true; child.kill('SIGKILL'); }, 10000);
      child.stdout.on('data', chunk => {
        size += chunk.length;
        if (size > 128 * 1024) { overflow = true; child.kill('SIGKILL'); }
        else output += chunk.toString('utf8');
      });
      child.once('error', () => { clearTimeout(timer); reject(new HttpError(503, 'Validation is unavailable.')); });
      child.once('close', code => {
        clearTimeout(timer);
        if (timeout) return reject(new HttpError(504, 'Validation exceeded the time limit.', 'validation_timeout'));
        if (overflow) return reject(new HttpError(400, 'Validation produced too many diagnostics.'));
        if (code !== 0 && code !== 1) return reject(new HttpError(400, 'Effect could not be validated.'));
        const result = { valid: code === 0, errors: [], warnings: [], infos: [] };
        for (const line of output.split(/\r?\n/)) {
          const match = /^  \[(error|warn|sim)\] (.*)$/.exec(line);
          if (!match) continue;
          const message = match[2].replaceAll(directory, '[temporary]').replaceAll(config.vendorDir, '[editor]').replaceAll(config.dataDir, '[storage]').slice(0, 1000);
          const key = { error: 'errors', warn: 'warnings', sim: 'infos' }[match[1]];
          if (result[key].length < 100) result[key].push(message);
        }
        if (!result.valid && !result.errors.length) result.errors.push('Effect validation failed.');
        resolve(result);
      });
    });
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
}

function staticTarget(pathname, config) {
  if (pathname === '/' || pathname === '/index.html') return path.join(config.vendorDir, 'index.html');
  if (pathname === '/integration.js' || pathname === '/integration.css') return path.join(config.frontendDir, pathname.slice(1));
  if (/^\/(?:js|css)\/[A-Za-z0-9_./-]+$/.test(pathname) && (pathname.endsWith('.js') || pathname.endsWith('.css'))) return path.join(config.vendorDir, pathname.slice(1));
  return null;
}

function requestUrl(req) {
  let url, decoded;
  try {
    // Inspect before URL normalization removes traversal segments.
    const raw = req.url.split('?')[0];
    decoded = decodeURIComponent(raw);
    if (!decoded.startsWith('/') || decoded.startsWith('//') || /[\\\x00-\x1f\x7f]/.test(decoded) || decoded.split('/').some(segment => segment === '.' || segment === '..')) fail(400, 'Invalid request path.');
    url = new URL(req.url, 'http://effects.invalid');
  } catch (error) { if (error instanceof HttpError) throw error; fail(400, 'Invalid request path.'); }
  return { pathname: decoded, searchParams: url.searchParams };
}

export function createEffectsServer(options = {}) {
  const config = configuration(options);
  const writes = new Map();
  const exportJobs = new Map();
  const jobNow = options.now || Date.now;
  let computationBusy = false;
  const serializeWrite = async (key, action) => {
    const previous = writes.get(key) || Promise.resolve();
    const current = previous.catch(() => {}).then(action);
    writes.set(key, current);
    try { return await current; }
    finally { if (writes.get(key) === current) writes.delete(key); }
  };
  const pruneExportJobs = () => {
    const now = jobNow();
    for (const [id, job] of exportJobs) {
      // A running render has no expiry. Retention begins only after it finishes.
      if (job.finishedAt !== undefined && now - job.finishedAt >= EXPORT_JOB_TTL) exportJobs.delete(id);
    }
    // Failed/cancelled jobs hold only status metadata, not PNG result slots.
    // Keep their recent history bounded without evicting running or done jobs.
    const terminal = [...exportJobs.values()].filter(job => job.status === 'failed' || job.status === 'cancelled');
    for (const job of terminal.slice(0, Math.max(0, terminal.length - EXPORT_TERMINAL_RECORDS))) exportJobs.delete(job.id);
  };
  const publicJob = job => ({
    id: job.id, status: job.status, file: job.file, format: job.format,
    time: job.time ?? null,
    created_at: new Date(job.createdAt).toISOString(),
    ...(job.finishedAt !== undefined ? { finished_at: new Date(job.finishedAt).toISOString() } : {}),
    ...(job.error ? { error: job.error } : {}),
  });
  const prepareExport = async (req, user) => {
    const body = await bodyJson(req, 4096);
    if (!isObject(body) || !['frame', 'atlas'].includes(body.format) ||
        (body.time !== undefined && (!Number.isFinite(body.time) || body.time < 0 || body.time > 600))) fail(400, 'Invalid export settings.');
    const file = safeFile(body.file);
    const { directory } = await contentLibrary(config, user, body.owner_key);
    const { data } = await readRegular(path.join(directory, file), config.maxBytes);
    let effect;
    try { effect = validateEffect(JSON.parse(data.toString('utf8'))); } catch { fail(422, 'Stored effect is invalid.'); }
    return { file, effect, format: body.format, ...(body.time !== undefined ? { time: body.time } : {}) };
  };
  const runExportJob = async (job, effect) => {
    try {
      const render = options.render || (await import('./render.mjs')).exportPNG;
      const result = await render(effect, { format: job.format,
        ...(job.time !== undefined ? { time: job.time } : {}), signal: job.controller.signal });
      if (job.status === 'running') {
        job.result = checkedPNG(result);
        job.status = 'done';
      }
    } catch (error) {
      if (job.status === 'running') {
        if (error?.code === 'render_cancelled') job.status = 'cancelled';
        else {
          job.status = 'failed';
          job.error = 'Effect cannot be exported with the current settings.';
        }
      }
    } finally {
      job.finishedAt = jobNow();
      job.controller = null;
      computationBusy = false;
      pruneExportJobs();
    }
  };
  const server = http.createServer(async (req, res) => {
    headers(res);
    const head = req.method === 'HEAD';
    try {
      const url = requestUrl(req);
      if (url.pathname === '/health' && ['GET', 'HEAD'].includes(req.method)) return send(res, 200, { ok: true }, undefined, head);
      const user = authenticate(req.headers.authorization, config);
      if (req.method === 'POST') checkWrite(req, user, config);
      if (url.pathname.startsWith('/api/')) checkOwner(req, user, url.pathname !== '/api/session');
      pruneExportJobs();
      if (url.pathname === '/api/session' && req.method === 'GET') return send(res, 200, sessionFor(user, config));
      if (url.pathname === '/api/guide' && req.method === 'GET') {
        const { data } = await readRegular(path.join(config.vendorDir, 'docs', 'FORMAT.md'), 1024 * 1024);
        const { data: guide } = await readRegular(path.join(config.frontendDir, 'AI_GUIDE.md'), 1024 * 1024);
        const { RENDER_LIMITS } = await import('./render.mjs');
        return send(res, 200, {
          format: data.toString('utf8'), upstream: 'https://github.com/xaidan777/Arcadia-Effects',
          guide: guide.toString('utf8'),
          usage: 'Create {app:"arcadia-effects",v:2,doc:{...},tex:{...}} effect JSON; validate before saving. List returns all shared effects with owner_key and editable. Read/export another author using owner_key; only its author or an administrator may update/delete it. Omit owner_key to save your own copy with create_only:true. Read ETag SHA256 for expected_sha256 when editing. Export format frame or atlas; frame optionally accepts time in seconds. No model is invoked.',
          limits: { max_request_bytes: config.maxBytes, validation_timeout_seconds: 10, concurrent_computations: 1, render: RENDER_LIMITS,
            export_jobs: { retention_seconds: EXPORT_JOB_TTL / 1000, max_per_account: EXPORT_JOBS_PER_ACCOUNT,
              max_total: EXPORT_JOBS_TOTAL, max_terminal_records: EXPORT_TERMINAL_RECORDS } },
        });
      }
      if (url.pathname === '/fantasy-session.js' && ['GET', 'HEAD'].includes(req.method)) {
        checkOwner(req, user, false);
        const bootstrap = JSON.stringify(sessionFor(user, config)).replace(/[<>&\u2028\u2029]/g, char => '\\u' + char.charCodeAt(0).toString(16).padStart(4, '0'));
        return send(res, 200, 'window.FantasyEffectsSession = ' + bootstrap + ';\n', MIME['.js'], head);
      }
      if (url.pathname === '/api/list' && req.method === 'GET') {
        const result = [];
        for (const library of await libraries(config, user)) {
        const { directory } = library;
        const entries = await fs.readdir(directory, { withFileTypes: true });
        for (const entry of entries) {
          if (!entry.isFile() || entry.name.startsWith('.') || !entry.name.endsWith('.json')) continue;
          try {
            safeFile(entry.name);
            const { data, stat } = await readRegular(path.join(directory, entry.name), config.maxBytes);
            const parsed = validateEffect(JSON.parse(data.toString('utf8')));
            const own = library.key === digest(user.id);
            result.push({ file: entry.name, name: parsed.doc.name || entry.name.slice(0, -5), id: parsed.doc.id || null, thumb: parsed.thumb || null, mtime: stat.mtimeMs,
              owner_key: library.key, owner_name: library.name, is_own: own, editable: own || user.admin, revision: digest(data) });
          } catch { /* A damaged file must not expose paths or break the catalog. */ }
        }
        }
        result.sort((a, b) => b.mtime - a.mtime);
        return send(res, 200, result);
      }
      if (url.pathname === '/api/effect' && req.method === 'GET') {
        const file = safeFile(url.searchParams.get('f'));
        const { directory } = await contentLibrary(config, user, url.searchParams.get('owner_key'));
        const { data } = await readRegular(path.join(directory, file), config.maxBytes);
        try { validateEffect(JSON.parse(data.toString('utf8'))); } catch { fail(422, 'Stored effect is invalid.'); }
        res.setHeader('ETag', '"' + digest(data) + '"');
        return send(res, 200, data);
      }
      if (url.pathname === '/api/validate' && req.method === 'POST') {
        const body = await bodyJson(req, config.maxBytes);
        if (!isObject(body)) fail(400, 'Invalid validation request.');
        const data = validateEffect(body.data);
        if (computationBusy) fail(429, 'Another effect is being processed. Try again shortly.', 'effects_busy');
        computationBusy = true;
        try {
          const result = await (options.validate || runUpstreamValidation)(data, config);
          return send(res, 200, result);
        } finally { computationBusy = false; }
      }
      if (url.pathname === '/api/exports' && req.method === 'POST') {
        const prepared = await prepareExport(req, user);
        if (computationBusy) fail(429, 'Another effect is being processed. Try again shortly.', 'effects_busy');
        const resultJobs = [...exportJobs.values()].filter(job => job.status === 'running' || job.status === 'done');
        const ownedJobs = resultJobs.filter(job => job.owner === user.id).length;
        // Reserve capacity before rendering; no completed, unexpired result is evicted.
        if (ownedJobs >= EXPORT_JOBS_PER_ACCOUNT || resultJobs.length >= EXPORT_JOBS_TOTAL) {
          fail(429, 'Export result storage is full. Download and release a completed export before starting another.', 'effects_exports_full');
        }
        const job = { id: randomUUID(), owner: user.id, status: 'running', file: prepared.file,
          format: prepared.format, ...(prepared.time !== undefined ? { time: prepared.time } : {}),
          createdAt: jobNow(), controller: new AbortController() };
        computationBusy = true;
        exportJobs.set(job.id, job);
        const accepted = publicJob(job);
        void runExportJob(job, prepared.effect);
        return send(res, 202, { job: accepted });
      }
      const exportRoute = /^\/api\/exports\/([0-9a-f-]+)(?:\/(result|cancel|release))?$/.exec(url.pathname);
      if (exportRoute) {
        const [, id, action] = exportRoute;
        const job = UUID.test(id) ? exportJobs.get(id) : null;
        if (!job) fail(404, 'Not found.');
        if (req.method !== 'GET' && job.owner !== user.id && !user.admin) fail(403, 'Only the author or an administrator may change this export.', 'content_read_only');
        if (!action && req.method === 'GET') return send(res, 200, { job: publicJob(job) });
        if (action === 'result' && req.method === 'GET') {
          if (job.status !== 'done') fail(409, 'Export result is not ready.', 'export_not_ready');
          return sendPNG(res, job.result, job.format);
        }
        if (action === 'cancel' && req.method === 'POST') {
          req.resume();
          if (job.status === 'running') {
            job.status = 'cancelled';
            job.controller.abort();
          }
          return send(res, 200, { job: publicJob(job) });
        }
        if (action === 'release' && req.method === 'POST') {
          req.resume();
          if (job.status === 'running') fail(409, 'Cancel the running export before releasing it.', 'export_running');
          exportJobs.delete(id);
          return send(res, 200, { ok: true });
        }
      }
      if (url.pathname === '/api/export' && req.method === 'POST') {
        const prepared = await prepareExport(req, user);
        if (computationBusy) fail(429, 'Another effect is being processed. Try again shortly.', 'effects_busy');
        computationBusy = true;
        try {
          let render = options.render;
          if (!render) {
            try { render = (await import('./render.mjs')).exportPNG; }
            catch { fail(503, 'PNG export is unavailable.'); }
          }
          let result;
          try { result = await render(prepared.effect, { format: prepared.format, ...(prepared.time !== undefined ? { time: prepared.time } : {}) }); }
          catch (error) {
            if (error.code === 'render_timeout') fail(504, 'Export exceeded the time limit.', 'render_timeout');
            fail(400, 'Effect cannot be exported with the current settings.', 'render_invalid');
          }
          return sendPNG(res, checkedPNG(result), prepared.format);
        } finally { computationBusy = false; }
      }
      if (url.pathname === '/api/save' && req.method === 'POST') {
        const body = await bodyJson(req, config.maxBytes);
        if (!isObject(body)) fail(400, 'Invalid save request.');
        const file = fileForName(body.name);
        if (body.create_only !== undefined && typeof body.create_only !== 'boolean') fail(400, 'Invalid create_only value.');
        if (body.expected_sha256 !== undefined && (typeof body.expected_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(body.expected_sha256))) fail(400, 'Invalid expected revision.');
        if (body.create_only && body.expected_sha256 !== undefined) fail(400, 'Choose creation or revision checking, not both.');
        const data = validateEffect(body.data);
        if (body.thumb != null) data.thumb = body.thumb;
        validateEffect(data);
        const bytes = Buffer.from(JSON.stringify(data));
        if (bytes.length > config.maxBytes) fail(413, 'Effect exceeds the size limit.');
        const { directory, key } = await contentLibrary(config, user, body.owner_key, true);
        await serializeWrite(key + '/' + file, () => atomicWrite(directory, file, bytes, { createOnly: body.create_only, expectedSha256: body.expected_sha256 }));
        return send(res, 200, { ok: true, file, owner_key: key, revision: digest(bytes) });
      }
      if (url.pathname === '/api/delete' && req.method === 'POST') {
        const body = await bodyJson(req, 4096);
        const file = safeFile(body?.file);
        const { directory, key } = await contentLibrary(config, user, body.owner_key, true);
        const target = path.join(directory, file);
        await serializeWrite(key + '/' + file, async () => {
          await existingRegular(target);
          await fs.unlink(target).catch(error => { if (error.code !== 'ENOENT') throw error; });
        });
        return send(res, 200, { ok: true });
      }
      const target = staticTarget(url.pathname, config);
      if (target && ['GET', 'HEAD'].includes(req.method)) {
        const { data } = await readRegular(target, config.maxBytes);
        return send(res, 200, data, MIME[path.extname(target)], head);
      }
      fail(404, 'Not found.');
    } catch (error) {
      req.resume();
      const status = error instanceof HttpError ? error.status : ['ENOENT', 'ELOOP', 'ENOTDIR'].includes(error.code) ? 404 : 500;
      const message = error instanceof HttpError ? error.message : status === 404 ? 'Not found.' : 'Unable to complete the request.';
      if (!res.headersSent) send(res, status, { error: message, ...(error.code && error instanceof HttpError ? { code: error.code } : {}) }, undefined, head);
      else res.end();
    }
  });
  server.requestTimeout = 120000;
  server.headersTimeout = 15000;
  server.maxHeadersCount = 64;
  server.on('close', () => {
    for (const job of exportJobs.values()) job.controller?.abort();
  });
  return server;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  const port = Number(process.env.PORT || 8080);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid PORT.');
  const server = createEffectsServer();
  server.listen(port, '0.0.0.0', () => console.log('Fantasy Effects workbench is ready.'));
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close(() => process.exit(0)));
}
