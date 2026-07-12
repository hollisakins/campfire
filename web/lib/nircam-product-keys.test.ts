import { describe, it, expect } from 'vitest';
import { mosaicBase, thumbnailBase } from './nircam-product-keys';

// Pins the thumbnail↔mosaic key contract. Production mosaics are gzipped
// (`_sci.fits.gz`) — the original matcher only stripped `_sci.fits`, so no
// EGS thumbnail ever matched and the table rendered none.
describe('mosaicBase', () => {
  it('strips _<ext>.fits.gz (production naming)', () => {
    expect(
      mosaicBase({
        file_path: 'data/products/nircam/egs/f444w/mosaic_nircam_f444w_egs_30mas_ceers_sci.fits.gz',
        extension: 'sci',
      }),
    ).toBe('data/products/nircam/egs/f444w/mosaic_nircam_f444w_egs_30mas_ceers');
  });

  it('strips _<ext>.fits (ungzipped)', () => {
    expect(
      mosaicBase({
        file_path: 'data/products/nircam/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_A1_sci.fits',
        extension: 'sci',
      }),
    ).toBe('data/products/nircam/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_A1');
  });

  it('uses the row extension, not just any suffix', () => {
    expect(
      mosaicBase({
        file_path: 'data/products/nircam/egs/f444w/mosaic_nircam_f444w_egs_30mas_NE_wht.fits.gz',
        extension: 'wht',
      }),
    ).toBe('data/products/nircam/egs/f444w/mosaic_nircam_f444w_egs_30mas_NE');
  });

  it('returns null when the path does not end with the extension suffix', () => {
    expect(
      mosaicBase({
        file_path: 'data/products/nircam/egs/f444w/expmap_egs_f444w.fits',
        extension: 'sci',
      }),
    ).toBeNull();
  });
});

describe('the two sides of the contract meet', () => {
  it('a registered thumbnail key and its sci mosaic reduce to the same base', () => {
    // Real EGS keys from the first production deploy of the redesign.
    const thumbKey =
      'data/products/nircam/egs/f444w/mosaic_nircam_f444w_egs_30mas_SW_thumb.png';
    const mosaicRow = {
      file_path: 'data/products/nircam/egs/f444w/mosaic_nircam_f444w_egs_30mas_SW_sci.fits.gz',
      extension: 'sci',
    };
    expect(mosaicBase(mosaicRow)).toBe(thumbnailBase(thumbKey));
  });
});
