import { DR_PARAM } from '@/lib/dr/tag';

/**
 * fetch() a same-origin GET route and return its JSON, throwing on a non-2xx
 * status with the route's `error` message when it sent one.
 *
 * Read-only data goes through GET route handlers, not server actions (perf
 * T2-C, #506; decision D-C): Next serializes server-action POSTs per client,
 * so a read fired from a mount effect queued behind every other action on
 * the page. GETs run in parallel, abort with their TanStack query (`signal`)
 * and can carry Cache-Control. Mutations stay actions.
 *
 * `dr`: the release the page is browsing (P6c-1). It is appended as `?dr=`
 * so middleware stamps the route's request with the release header; the
 * caller's query key must carry it too (see the hooks under lib/hooks).
 */
export async function fetchJson<T>(url: string, init: { signal?: AbortSignal; dr?: string | null } = {}): Promise<T> {
  const res = await fetch(withDrParam(url, init.dr), { signal: init.signal });
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body?.error === 'string' && body.error) message = body.error;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(message);
  }
  return res.json() as Promise<T>;
}

/** `url` with `dr=<tag>` appended (no-op without a tag). */
export function withDrParam(url: string, dr: string | null | undefined): string {
  if (!dr) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}${DR_PARAM}=${encodeURIComponent(dr)}`;
}
