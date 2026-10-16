import { describe, expect, it } from 'vitest';
import { drPagesAccess, drPagesInNav, parseDrPagesMode } from './dr-pages';

const anon = { signedIn: false, isAdmin: false, isLinkAccount: false };
const user = { signedIn: true, isAdmin: false, isLinkAccount: false };
const admin = { signedIn: true, isAdmin: true, isLinkAccount: false };
const link = { signedIn: true, isAdmin: false, isLinkAccount: true };

describe('parseDrPagesMode', () => {
  it('accepts admin and all, case-insensitively', () => {
    expect(parseDrPagesMode('admin')).toBe('admin');
    expect(parseDrPagesMode(' ALL ')).toBe('all');
  });
  it('is off for anything else', () => {
    expect(parseDrPagesMode(undefined)).toBe('off');
    expect(parseDrPagesMode('')).toBe('off');
    expect(parseDrPagesMode('true')).toBe('off');
  });
});

describe('drPagesAccess', () => {
  it('404s everyone while off', () => {
    for (const v of [anon, user, admin]) expect(drPagesAccess('off', v)).toBe('not-found');
  });
  it('shows admins only in admin mode', () => {
    expect(drPagesAccess('admin', admin)).toBe('show');
    expect(drPagesAccess('admin', user)).toBe('not-found');
    expect(drPagesAccess('admin', anon)).toBe('not-found');
  });
  it('shows signed-in accounts and prompts the signed-out in all mode', () => {
    expect(drPagesAccess('all', user)).toBe('show');
    expect(drPagesAccess('all', admin)).toBe('show');
    expect(drPagesAccess('all', anon)).toBe('sign-in');
  });
  it('never shows share-link accounts', () => {
    expect(drPagesAccess('all', link)).toBe('not-found');
    expect(drPagesInNav('all', link)).toBe(false);
  });
  it('offers the nav entry exactly when the page shows', () => {
    expect(drPagesInNav('admin', admin)).toBe(true);
    expect(drPagesInNav('admin', user)).toBe(false);
    expect(drPagesInNav('all', anon)).toBe(false);
  });
});
