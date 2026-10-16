import { describe, expect, it, vi } from 'vitest';

vi.mock('server-only', () => ({}));

import { imageSource, pageAssetHref } from './ReleasePageDoc';
import { pageAssetName } from '@/lib/server/dr-releases';

describe('page document assets', () => {
  it('maps a bare or ./ figure name to the asset route and refuses anything else', () => {
    expect(pageAssetHref('DR-t', 'nz.png')).toBe('/api/dr/page-asset?tag=DR-t&name=nz.png');
    expect(pageAssetHref('DR-t', './fig 1.png')).toBeNull();
    expect(pageAssetHref('DR-t', '../x.png')).toBeNull();
  });
  it('reads the asset name off the content-addressed key only', () => {
    expect(pageAssetName('dr/DR-t/page/0123456789abcdef/nz.png')).toBe('nz.png');
    expect(pageAssetName('dr/DR-t/page/nz.png')).toBe('');
    expect(pageAssetName('data/products/nircam/f/f_layout.png')).toBe('');
  });
});

describe('page document image sources', () => {
  const assets = new Set(['nz.png']);
  it('allows page assets and absolute https only', () => {
    expect(imageSource('DR-t', 'nz.png', assets)).toBe('/api/dr/page-asset?tag=DR-t&name=nz.png');
    expect(imageSource('DR-t', './nz.png', assets)).toBe('/api/dr/page-asset?tag=DR-t&name=nz.png');
    expect(imageSource('DR-t', 'https://example.org/fig.png', assets)).toBe('https://example.org/fig.png');
  });
  it('drops site-relative, protocol-relative, http, data and unknown sources', () => {
    for (const src of ['/api/download?path=x', '//evil.example/p.png', 'http://example.org/fig.png', 'data:image/png;base64,AAAA', 'other.png', 'javascript:alert(1)']) {
      expect(imageSource('DR-t', src, assets), src).toBeNull();
    }
  });
});

