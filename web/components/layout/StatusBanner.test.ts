import { describe, it, expect } from 'vitest';
import { statusBannerText } from './StatusBanner';

describe('statusBannerText', () => {
  it('says nothing while all is well', () => {
    expect(statusBannerText(undefined)).toBeNull();
    expect(statusBannerText({ state: 'ok', message: null })).toBeNull();
  });
  it('names a degraded or unreachable database', () => {
    expect(statusBannerText({ state: 'degraded', message: null })).toMatch(/sign-in and saving/);
    expect(statusBannerText({ state: 'down', message: null })).toMatch(/unavailable/);
  });
  it("prefers the operator's message", () => {
    expect(statusBannerText({ state: 'degraded', message: 'Jetstream2 storage incident.' })).toBe('Jetstream2 storage incident.');
    expect(statusBannerText({ state: 'ok', message: 'Maintenance tonight.' })).toBe('Maintenance tonight.');
  });
});
