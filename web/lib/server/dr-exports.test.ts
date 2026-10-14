// Data-release exports (P4b), reader side: which export files a viewer may
// fetch (the files are built by `campfire dr export`, tested in Python).
import { describe, it, expect, vi } from 'vitest';

vi.mock('server-only', () => ({}));

import { fileVisible, openComponentScopes, releaseVisible } from './dr-exports';

describe('visibility', () => {
  const open = openComponentScopes(['nirspec', 'nircam/cosmos', 'photometry/cosmos/COSMOS v1.2']);

  it('program files need the program; the release components are open on the manifest', () => {
    const held = new Set(['pub']);
    expect(fileVisible('spectra', 'pub', held, open)).toBe(true);
    expect(fileVisible('spectra', 'priv', held, open)).toBe(false);
    expect(fileVisible('manifest', 'nircam/cosmos', held, open)).toBe(true);
    expect(fileVisible('manifest', 'photometry/cosmos/COSMOS v1.2', held, open)).toBe(true);
    expect([...open]).not.toContain('nirspec');
  });

  it('a component-shaped name is not open on other streams or outside the release (Codex on #600)', () => {
    const held = new Set(['pub']);
    // a private program slugged like a component: its program streams stay gated
    for (const stream of ['objects', 'spectra', 'line_fits', 'photometry']) {
      expect(fileVisible(stream, 'nircam/cosmos', held, open)).toBe(false);
    }
    // a component the release did not freeze is not open either
    expect(fileVisible('manifest', 'nircam/egs', held, open)).toBe(false);
    expect(fileVisible('manifest', 'nircam/egs', new Set(['nircam/egs']), open)).toBe(true);
    expect(openComponentScopes(null).size).toBe(0);
    expect(openComponentScopes([1, 'nircam/x']).has('nircam/x')).toBe(true);
  });

  it('readers see published releases only; admins every state', () => {
    expect(releaseVisible({ state: 'published' }, false)).toBe(true);
    expect(releaseVisible({ state: 'frozen' }, false)).toBe(false);
    expect(releaseVisible({ state: 'withdrawn' }, false)).toBe(false);
    expect(releaseVisible({ state: 'frozen' }, true)).toBe(true);
  });
});
