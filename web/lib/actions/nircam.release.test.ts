// /imaging field cards under a data release (P6c-2): a card's layout is the
// release's ARCHIVED layout member of that field, decided on the manifest
// row, never the RPC's layout_key (the live key while the member is pending).
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('server-only', () => ({}));

let drTag: string | null = null;
vi.mock('@/lib/dr/server', () => ({ requestDrTag: async () => drTag }));

const rpcRows = [
  { field: 'a2744', layout_key: 'data/products/nircam/a2744/a2744_layout.png' },
  { field: 'cosmos', layout_key: 'data/products/nircam/cosmos/cosmos_layout.png' },
];
const supabase = { rpc: async () => ({ data: rpcRows, error: null }) };
vi.mock('@/lib/auth/identity', () => ({ getRequestIdentity: async () => ({ user: { id: 'u1' }, supabase }) }));
vi.mock('@/lib/supabase/server', () => ({ createClient: async () => supabase }));

const liveSigned = vi.fn(async (keys: string[]) => keys.map((k) => `https://live.example/${k}`));
vi.mock('@/lib/r2', () => ({ generateDownloadUrls: (keys: string[]) => liveSigned(keys) }));

let listed: { components: string[]; productTypes?: string[] } | null = null;
const ARCH = `archive/${'a'.repeat(64)}/products/nircam/a2744/a2744_layout.png`;
const ARCH_ACS = `archive/${'b'.repeat(64)}/products/acswfc/a2744/a2744_layout.png`;
const ARCH_WFC3 = `archive/${'c'.repeat(64)}/products/wfc3ir/hstonly/hstonly_layout.png`;
// only a2744's layouts are archived; cosmos's is pending (not returned).
// The ACS/WFC one is listed FIRST: the card must still take NIRCam's.
let members = [
  { storageKey: 'data/products/acswfc/a2744/a2744_layout.png', component: 'acswfc/a2744', productType: 'acswfc_layout', archiveKey: ARCH_ACS },
  { storageKey: 'data/products/nircam/a2744/a2744_layout.png', component: 'nircam/a2744', productType: 'nircam_layout', archiveKey: ARCH },
];
vi.mock('@/lib/server/dr-files', () => ({
  listReleaseMembers: async () => [],
  listReleaseMembersIn: async (_sb: unknown, _tag: string, opts: { components: string[]; productTypes?: string[] }) => {
    listed = opts;
    return members;
  },
  releaseDownloadUrls: async (byKey: Map<string, { archiveKey: string } | null>) =>
    new Map([...byKey].map(([k, m]) => [k, m ? `https://osn.example/${m.archiveKey}?sig=1` : null])),
}));

import { getNircamFields } from './nircam';

beforeEach(() => {
  drTag = null;
  listed = null;
  liveSigned.mockClear();
});

describe('getNircamFields', () => {
  it('presigns the live layout keys for the live catalog', async () => {
    const { fields } = await getNircamFields();
    expect(fields.map((f) => f.layout_url)).toEqual([
      'https://live.example/data/products/nircam/a2744/a2744_layout.png',
      'https://live.example/data/products/nircam/cosmos/cosmos_layout.png',
    ]);
  });
  it('under a release uses the archived layout members and nothing else', async () => {
    drTag = 'DR-t';
    const { fields } = await getNircamFields();
    // every imaging instrument's component of each field, and its layout kind
    expect(listed).toEqual({
      components: ['nircam/a2744', 'acswfc/a2744', 'wfc3ir/a2744', 'nircam/cosmos', 'acswfc/cosmos', 'wfc3ir/cosmos'],
      productTypes: ['nircam_layout', 'acswfc_layout', 'wfc3ir_layout'],
    });
    // NIRCam's layout when the release holds it, whatever order the members come in
    expect(fields.find((f) => f.field === 'a2744')?.layout_url).toBe(`https://osn.example/${ARCH}?sig=1`);
    expect(fields.find((f) => f.field === 'cosmos')?.layout_url).toBeNull();
    expect(liveSigned).not.toHaveBeenCalled();
  });
  it('under a release a field without NIRCam shows the layout of the instrument it has', async () => {
    drTag = 'DR-t';
    const saved = members;
    const savedRows = [...rpcRows];
    rpcRows.splice(0, rpcRows.length, { field: 'hstonly', layout_key: 'data/products/wfc3ir/hstonly/hstonly_layout.png' });
    members = [{ storageKey: 'data/products/wfc3ir/hstonly/hstonly_layout.png', component: 'wfc3ir/hstonly', productType: 'wfc3ir_layout', archiveKey: ARCH_WFC3 }];
    try {
      const { fields } = await getNircamFields();
      expect(fields[0].layout_url).toBe(`https://osn.example/${ARCH_WFC3}?sig=1`);
    } finally {
      members = saved;
      rpcRows.splice(0, rpcRows.length, ...savedRows);
    }
  });
});
