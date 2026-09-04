/**
 * fetch() a same-origin GET route and return its JSON, throwing on a non-2xx
 * status with the route's `error` message when it sent one.
 *
 * Read-only data goes through GET route handlers, not server actions (perf
 * T2-C, #506; decision D-C): Next serializes server-action POSTs per client,
 * so a read fired from a mount effect queued behind every other action on
 * the page. GETs run in parallel, abort with their TanStack query (`signal`)
 * and can carry Cache-Control. Mutations stay actions.
 */
export async function fetchJson<T>(url: string, init: { signal?: AbortSignal } = {}): Promise<T> {
  const res = await fetch(url, { signal: init.signal });
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
