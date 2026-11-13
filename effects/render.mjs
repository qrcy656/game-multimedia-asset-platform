import { Worker } from 'node:worker_threads';

export const RENDER_LIMITS = Object.freeze({
  timeoutMs: null, maxJsonBytes: 12 * 1024 * 1024, maxLayers: 24, maxEmitters: 8,
  maxDuration: 15, maxFrames: 64, maxCompSide: 1024, maxCellSide: 1024,
  maxAtlasSide: 4096, maxAtlasPixels: 16 * 1024 * 1024, maxTextureSide: 2048,
  maxTexturePixels: 4 * 1024 * 1024, maxTextures: 16, maxLiveParticles: 8000
});

function renderError(message, code = 'render_invalid') {
  const error = new Error(message); error.code = code; return error;
}

// The caller has already selected a saved file from the authenticated user's library.
// No filesystem path, code, URL, worker path or unbounded limits come from the caller.
export function exportPNG(effect, options = {}) {
  return new Promise((resolve, reject) => {
    let serialized;
    try {
      serialized = JSON.stringify(effect);
      if (!serialized || Buffer.byteLength(serialized) > RENDER_LIMITS.maxJsonBytes) throw new Error();
      if (!['atlas', 'frame'].includes(options.format)) throw new Error();
      if (options.time !== undefined && (typeof options.time !== 'number' || !Number.isFinite(options.time))) throw new Error();
    } catch (_) { reject(renderError('Invalid or oversized effect export.')); return; }
    const signal = options.signal;
    let worker, finished = false;
    const onAbort = () => finish(renderError('Effect export was cancelled.', 'render_cancelled'));
    const finish = async (error, result) => {
      if (finished) return;
      finished = true;
      signal?.removeEventListener('abort', onAbort);
      if (worker) await worker.terminate();
      if (error) reject(error); else resolve(result);
    };
    if (signal?.aborted) { finish(renderError('Effect export was cancelled.', 'render_cancelled')); return; }
    try {
      worker = new Worker(new URL('./renderer-worker.mjs', import.meta.url), {
        workerData: { serialized, format: options.format, time: options.time, limits: RENDER_LIMITS },
        resourceLimits: {maxOldGenerationSizeMb: 192, maxYoungGenerationSizeMb: 32, stackSizeMb: 4}
      });
      // Rendering has no wall-clock deadline. The owner may cancel its background job.
      signal?.addEventListener('abort', onAbort, {once: true});
      worker.once('message', message => {
        if (message.ok) finish(null, {png: Buffer.from(message.png), info: message.info});
        else finish(renderError(message.error || 'Invalid effect export.'));
      });
      worker.once('error', () => finish(renderError('Effect export failed.')));
      worker.once('exit', () => { if (!finished) finish(renderError('Effect export stopped.')); });
    } catch (_) { finish(renderError('Effect exporter is unavailable.')); }
  });
}
