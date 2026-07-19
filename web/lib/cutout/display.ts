// Display-cutout helper for the three PNG routes (`/api/tile-thumbnail`,
// `/api/og-image`, `/api/v1/cutout`) — epic #337, Phase 5. Renders a field's
// FitsGL pyramid to an opaque black-backed PNG, matching the legacy PNG-tile
// compositor's canvas so consumers see no visual regime change. Server-only
// (pulls in `sharp` via `./encode`).

import { renderCutout } from './index';
import { encodePng } from './encode';
import type { FieldCutoutSource } from './source';

export interface DisplayCutoutArgs {
  ra: number;
  dec: number;
  fovArcsec: number;
  outputSize: number;
}

/**
 * Render a North-up display cutout PNG from a resolved field source.
 * Throws on fetch/decode failure — callers catch and fall back to the legacy
 * PNG-tile path while both stacks coexist.
 */
export async function renderDisplayCutoutPng(
  src: FieldCutoutSource,
  args: DisplayCutoutArgs,
): Promise<Buffer> {
  // The producer's default stretch (trilogy levels solved from precomputed
  // stats + knobs upstream in `displayDefaults`) — so the cutout opens on the
  // same transfer the map does; absent ⇒ the engine default (asinh + auto).
  const display = src.display;
  const cutout = await renderCutout(src.bands, {
    center: [args.ra, args.dec],
    fovArcsec: args.fovArcsec,
    outputSize: args.outputSize,
    ...(display !== null && {
      stretch: display.stretch,
      ...(display.limits !== undefined && { limits: display.limits }),
      ...(display.trilogyK !== undefined && { trilogyK: display.trilogyK }),
    }),
  });
  // No-coverage (NaN) pixels render transparent; flatten onto black like the
  // legacy compositor's opaque canvas.
  return encodePng(cutout, { background: '#000000' });
}
