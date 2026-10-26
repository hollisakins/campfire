import { describe, expect, it } from 'vitest';
import { isAliasHost, localizeUrl, parseHostAliases } from './host-aliases';

const aliases = parseHostAliases(
  'campfire.hollisakins.com=campfire-jwst.org, campfire-api.hollisakins.com=api.campfire-jwst.org,' +
    'campfire-download.hollisakins.com=download.campfire-jwst.org,campfire-tiles.hollisakins.com=tiles.campfire-jwst.org,',
);

describe('parseHostAliases', () => {
  it('maps primary to alias and skips malformed pairs', () => {
    expect(aliases.get('api.campfire-jwst.org')).toBe('campfire-api.hollisakins.com');
    expect(aliases.size).toBe(4);
    expect(parseHostAliases('nope,=x,y=').size).toBe(0);
    expect(parseHostAliases(undefined).size).toBe(0);
  });
});

describe('isAliasHost', () => {
  it('recognizes alias hosts with or without a port, case-insensitively', () => {
    expect(isAliasHost('campfire.hollisakins.com', aliases)).toBe(true);
    expect(isAliasHost('Campfire.Hollisakins.com:443', aliases)).toBe(true);
    expect(isAliasHost('campfire-jwst.org', aliases)).toBe(false);
    expect(isAliasHost(null, aliases)).toBe(false);
  });
});

describe('localizeUrl', () => {
  const alias = 'campfire.hollisakins.com';

  it('rewrites primary hosts for a visitor on an alias host', () => {
    expect(localizeUrl('https://api.campfire-jwst.org', alias, aliases)).toBe('https://campfire-api.hollisakins.com');
    expect(localizeUrl('https://campfire-jwst.org/', alias, aliases)).toBe('https://campfire.hollisakins.com/');
    expect(localizeUrl('https://download.campfire-jwst.org/o/a%2Fb.json?h=1&u=https%3A%2F%2Fx', alias, aliases)).toBe(
      'https://campfire-download.hollisakins.com/o/a%2Fb.json?h=1&u=https%3A%2F%2Fx',
    );
    expect(localizeUrl('https://tiles.campfire-jwst.org/cosmos/f444w', alias, aliases)).toBe(
      'https://campfire-tiles.hollisakins.com/cosmos/f444w',
    );
  });

  it('leaves everything else alone', () => {
    expect(localizeUrl('https://api.campfire-jwst.org', 'campfire-jwst.org', aliases)).toBe('https://api.campfire-jwst.org');
    expect(localizeUrl('https://api.campfire-jwst.org', null, aliases)).toBe('https://api.campfire-jwst.org');
    expect(localizeUrl('https://uaz1.osn.mghpcc.org/b/k', alias, aliases)).toBe('https://uaz1.osn.mghpcc.org/b/k');
    expect(localizeUrl('/api/spectrum/sidecars', alias, aliases)).toBe('/api/spectrum/sidecars');
    expect(localizeUrl('', alias, aliases)).toBe('');
  });

  it('is a no-op with no aliases configured', () => {
    expect(localizeUrl('https://api.campfire-jwst.org', alias, new Map())).toBe('https://api.campfire-jwst.org');
  });
});
