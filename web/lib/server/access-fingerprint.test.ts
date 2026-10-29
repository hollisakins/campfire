import { describe, it, expect, vi } from 'vitest';

vi.mock('server-only', () => ({}));

import { accessFingerprint } from './access-fingerprint';

describe('accessFingerprint', () => {
  it('depends on the program set and the admin flag only, in no order', () => {
    const a = accessFingerprint({ isAdmin: false, accessibleSlugs: ['pb', 'pa', 'pa'] });
    expect(a).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(accessFingerprint({ isAdmin: false, accessibleSlugs: ['pa', 'pb'] })).toBe(a);
    expect(accessFingerprint({ isAdmin: false, accessibleSlugs: ['pa'] })).not.toBe(a);
    expect(accessFingerprint({ isAdmin: true, accessibleSlugs: ['pa', 'pb'] })).not.toBe(a);
  });
});
