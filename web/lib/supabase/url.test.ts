import { afterEach, describe, expect, it } from 'vitest';
import { publicSupabaseCookieName, serverSupabaseUrl, supabaseCookieNameFor } from './url';

const saved = { ...process.env };
afterEach(() => {
  process.env = { ...saved };
});

describe('serverSupabaseUrl', () => {
  it('prefers the runtime server-only url', () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL = 'https://campfire-api.hollisakins.com';
    process.env.SUPABASE_URL = 'http://kong:8000';
    expect(serverSupabaseUrl()).toBe('http://kong:8000');
  });

  it('falls back to the public url', () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL = 'https://campfire-api.hollisakins.com';
    delete process.env.SUPABASE_URL;
    expect(serverSupabaseUrl()).toBe('https://campfire-api.hollisakins.com');
    process.env.SUPABASE_URL = '';
    expect(serverSupabaseUrl()).toBe('https://campfire-api.hollisakins.com');
  });
});

describe('cookie name', () => {
  it('matches the supabase-js default for the public url, whatever the transport url', () => {
    // Hosted project and self-hosted API, as the browser client derives them.
    expect(supabaseCookieNameFor('https://puyczxwyuzpnqvpachip.supabase.co')).toBe(
      'sb-puyczxwyuzpnqvpachip-auth-token',
    );
    expect(supabaseCookieNameFor('https://campfire-api.hollisakins.com')).toBe(
      'sb-campfire-api-auth-token',
    );
    // The internal url would have produced a different cookie name.
    expect(supabaseCookieNameFor('http://kong:8000')).toBe('sb-kong-auth-token');

    process.env.NEXT_PUBLIC_SUPABASE_URL = 'https://campfire-api.hollisakins.com';
    process.env.SUPABASE_URL = 'http://kong:8000';
    expect(publicSupabaseCookieName()).toBe('sb-campfire-api-auth-token');
  });

  it('is undefined (supabase-js default) when no public url is configured', () => {
    delete process.env.NEXT_PUBLIC_SUPABASE_URL;
    expect(publicSupabaseCookieName()).toBeUndefined();
  });
});
