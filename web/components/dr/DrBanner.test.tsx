// @vitest-environment node
import React from 'react';
import { renderToString } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

let search = '';
const path = '/nirspec';
vi.mock('next/navigation', () => ({
  usePathname: () => path,
  useSearchParams: () => new URLSearchParams(search),
}));
vi.mock('next/link', () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement('a', { href, ...rest }, children),
}));
vi.mock('@/lib/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { id: 'u' }, userProfile: { can_inspect: true } }),
}));

import { DrProvider, useDrTag } from '@/lib/dr/client';
import { DrBannerContent } from './DrBanner';
import { EnterInspectionModeButton } from '@/components/spectra/inspection/EnterInspectionModeButton';

const under = (tag: string | null, el: React.ReactElement) => renderToString(<DrProvider tag={tag}>{el}</DrProvider>);

describe('useDrTag reads the provider, never the URL', () => {
  const Probe = () => <i>{String(useDrTag())}</i>;
  it('is null without a provider value even when the URL carries ?dr=', () => {
    search = 'dr=DR2026.1';
    expect(under(null, <Probe />)).toBe('<i>null</i>');
  });
  it('is the provided tag even when the URL carries none', () => {
    search = '';
    expect(under('DR2026.1', <Probe />)).toBe('<i>DR2026.1</i>');
  });
});

describe('DrBanner', () => {
  it('renders nothing on the live catalog (even with a stray ?dr= in the URL)', () => {
    search = 'view=objects&dr=DR2026.1';
    expect(under(null, <DrBannerContent />)).toBe('');
  });
  it('shows the tag, its release page and the way back to live', () => {
    search = 'view=objects&dr=DR2026.1&page=2';
    const html = under('DR2026.1', <DrBannerContent />);
    expect(html).toContain('DR2026.1');
    expect(html).toContain('href="/releases/DR2026.1"');
    expect(html).toContain('href="/nirspec?view=objects&amp;page=2"');
    expect(html).toContain('Live catalog');
  });
});

describe('mutation controls under a release', () => {
  it('offers inspection mode live and hides it under a release', () => {
    expect(under(null, <EnterInspectionModeButton targetId="J1" filterStr="view=objects" />)).toContain('/inspect?');
    expect(under('DR2026.1', <EnterInspectionModeButton targetId="J1" filterStr="view=objects" />)).toBe('');
  });
});
