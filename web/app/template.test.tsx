// @vitest-environment node
// The per-navigation shell provides the release the SERVER decided (P6c-1):
// template.tsx re-runs on each navigation and reads the stamped header.
import React from 'react';
import { renderToString } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

let drTag: string | null = null;
vi.mock('server-only', () => ({}));
vi.mock('@/lib/dr/server', () => ({ requestDrTag: async () => drTag }));
vi.mock('@/components/layout/Navigation', () => ({
  Navigation: ({ drTag }: { drTag: string | null }) => React.createElement('nav', { 'data-dr': String(drTag) }),
}));
vi.mock('@/components/dr/DrBanner', () => ({ DrBanner: () => null }));

import RootTemplate from './template';
import { useDrTag } from '@/lib/dr/client';

const Probe = () => React.createElement('i', null, String(useDrTag()));

describe('app/template.tsx', () => {
  it('provides the header tag to the client tree and the navigation', async () => {
    drTag = 'DR-seed';
    const html = renderToString(await RootTemplate({ children: <Probe /> }));
    expect(html).toContain('<nav data-dr="DR-seed"></nav>');
    expect(html).toContain('<i>DR-seed</i>');
  });
  it('provides null on a live navigation', async () => {
    drTag = null;
    const html = renderToString(await RootTemplate({ children: <Probe /> }));
    expect(html).toContain('<nav data-dr="null"></nav>');
    expect(html).toContain('<i>null</i>');
  });
});
