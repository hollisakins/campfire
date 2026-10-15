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

import { DrBannerContent } from './DrBanner';
import { EnterInspectionModeButton } from '@/components/spectra/inspection/EnterInspectionModeButton';

vi.mock('@/lib/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { id: 'u' }, userProfile: { can_inspect: true } }),
}));

describe('DrBanner', () => {
  it('renders nothing on the live catalog', () => {
    search = 'view=objects';
    expect(renderToString(<DrBannerContent />)).toBe('');
  });
  it('shows the tag, its release page and the way back to live', () => {
    search = 'view=objects&dr=DR2026.1&page=2';
    const html = renderToString(<DrBannerContent />);
    expect(html).toContain('DR2026.1');
    expect(html).toContain('href="/releases/DR2026.1"');
    expect(html).toContain('href="/nirspec?view=objects&amp;page=2"');
    expect(html).toContain('Live catalog');
  });
});

describe('mutation controls under a release', () => {
  it('offers inspection mode live and hides it under a release', () => {
    search = '';
    expect(renderToString(<EnterInspectionModeButton targetId="J1" filterStr="view=objects" />)).toContain('/inspect?');
    search = 'dr=DR2026.1';
    expect(renderToString(<EnterInspectionModeButton targetId="J1" filterStr="view=objects" />)).toBe('');
  });
});
