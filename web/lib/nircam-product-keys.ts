/**
 * The mosaic↔thumbnail key contract (NIRCam data products table).
 *
 * A mosaic's thumbnail is registered as `<base>_thumb.png` beside it, where
 * `<base>` is the mosaic file_path minus its `_<ext>.fits[.gz]` suffix.
 * Both sides reduce to that shared base: the server action keys its presigned
 * thumbnail map with `thumbnailBase(storage_key)`, and the table looks a row
 * up via `mosaicBase(row)`. Keep the two in lockstep — production mosaics are
 * gzipped FITS (`_sci.fits.gz`), so the matcher accepts both endings.
 */

/** file_path minus `_<ext>.fits[.gz]`, or null if the suffix doesn't match. */
export function mosaicBase(row: { file_path: string; extension: string }): string | null {
  for (const suffix of [`_${row.extension}.fits.gz`, `_${row.extension}.fits`]) {
    if (row.file_path.endsWith(suffix)) {
      return row.file_path.slice(0, -suffix.length);
    }
  }
  return null;
}

/** Registered thumbnail storage key minus `_thumb.png`. */
export function thumbnailBase(storageKey: string): string {
  return storageKey.replace(/_thumb\.png$/, '');
}
