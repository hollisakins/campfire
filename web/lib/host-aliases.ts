/**
 * Alias hostnames: the portal served whole under a second domain.
 *
 * Production lives on campfire-jwst.org; the pre-move hollisakins.com names
 * keep serving the same container (ops/Caddyfile). Some networks block one
 * domain and not the other (a firewall category for newly registered
 * domains, a reputation flag on the old one), so a visitor on an alias host
 * must never be sent to a primary host: every absolute url the browser
 * follows (the Supabase API, the download Worker, map tiles, cutout redirects,
 * auth verify links) is rewritten to the alias of its host.
 *
 * `NEXT_PUBLIC_HOST_ALIASES` is a comma-separated list of `alias=primary`
 * hostname pairs, e.g.
 * `campfire.hollisakins.com=campfire-jwst.org,campfire-api.hollisakins.com=api.campfire-jwst.org`.
 * It is inlined at build time (a GitHub variable, like the other
 * NEXT_PUBLIC_* values), so the browser and the server read the same map.
 * Unset, nothing is rewritten.
 *
 * Importable from both sides; the server reads the request's Host header
 * (lib/server/request-host.ts), the browser `window.location`.
 */

/** primary hostname → alias hostname */
export type HostAliasMap = ReadonlyMap<string, string>;

export function parseHostAliases(raw: string | undefined): Map<string, string> {
  const map = new Map<string, string>();
  for (const pair of (raw ?? '').split(',')) {
    const [alias, primary] = pair.split('=').map((s) => s.trim().toLowerCase());
    if (alias && primary) map.set(primary, alias);
  }
  return map;
}

const ALIASES: HostAliasMap = parseHostAliases(process.env.NEXT_PUBLIC_HOST_ALIASES);

function hostnameOf(host: string): string {
  // A Host header may carry a port; aliases never do.
  return host.trim().toLowerCase().replace(/:\d+$/, '');
}

/** Whether `host` (a Host header or hostname) is one of the alias hostnames. */
export function isAliasHost(host: string | null | undefined, aliases: HostAliasMap = ALIASES): boolean {
  if (!host) return false;
  const h = hostnameOf(host);
  for (const alias of aliases.values()) if (alias === h) return true;
  return false;
}

/**
 * `url` as a visitor on `requestHost` must see it: on an alias host, a url
 * whose host is a primary hostname moves to that host's alias (path, query
 * and scheme kept); on any other host, or for a url that is relative,
 * unparseable or on a host without an alias, `url` comes back unchanged.
 */
export function localizeUrl(
  url: string,
  requestHost: string | null | undefined,
  aliases: HostAliasMap = ALIASES,
): string {
  if (!url || !isAliasHost(requestHost, aliases)) return url;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return url;
  }
  const alias = aliases.get(parsed.hostname.toLowerCase());
  if (!alias) return url;
  parsed.hostname = alias;
  // `URL` normalizes a bare origin to `origin/`; keep the caller's form so a
  // base url still concatenates as `${base}/path`.
  const out = parsed.toString();
  return !url.endsWith('/') && out.endsWith('/') && parsed.pathname === '/' && !parsed.search && !parsed.hash
    ? out.slice(0, -1)
    : out;
}

/** `localizeUrl` for the current page (browser only; unchanged on the server). */
export function browserUrl(url: string): string {
  if (typeof window === 'undefined') return url;
  return localizeUrl(url, window.location.host);
}
