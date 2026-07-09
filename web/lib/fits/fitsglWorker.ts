/**
 * FitsGL decode-worker shim for Next.js (epic #337, Phase 1 / #338).
 *
 * `@fitsgl/core` offloads the CPU-bound RICE/GZIP tile decode onto a pool of Web
 * Workers. Its built-in factory uses the Vite idiom
 * `new Worker(new URL('../worker.js', import.meta.url), { type: 'module' })`,
 * which Next's bundler doesn't wire up the same way — so we supply our own
 * `workerFactory` via `tileOptions` (see `FitsGLViewerDemo`). webpack 5 (the
 * `next build` bundler) recognises the `new Worker(new URL(<specifier>,
 * import.meta.url))` form and emits `@fitsgl/core/worker` as its own chunk; the
 * worker module self-attaches the decode message handler when it detects a
 * worker scope, so importing it as the entry is all that's required.
 *
 * The alternative — `tileOptions={{ useWorker: false }}` — runs decode inline on
 * the main thread and needs no worker chunk at all; keep it as the fallback if a
 * bundler ever chokes on the worker emission.
 */
import type { WorkerLike } from '@fitsgl/core';

/**
 * Construct one FitsGL tile-decode Web Worker.
 *
 * `WorkerLike` is the library's intentionally-minimal structural view of a worker
 * (`postMessage` / `terminate` / an `onmessage` taking `{ data }`). A real DOM
 * `Worker` is exactly what it models and is what the pool drives at runtime, but
 * it doesn't type-assign under strict variance — `Worker.onmessage` receives a
 * full `MessageEvent`, a supertype of `{ data: unknown }` — so we cast.
 */
export function makeFitsglWorker(): WorkerLike {
  const worker = new Worker(new URL('@fitsgl/core/worker', import.meta.url), {
    type: 'module',
  });
  return worker as unknown as WorkerLike;
}
