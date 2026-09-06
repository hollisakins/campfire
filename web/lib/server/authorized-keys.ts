// Chunked key authorization for download actions.
//
// Every bulk-download action re-derives its authorized key set server-side by
// asking the DB, under the caller's RLS session, which of the client-supplied
// keys it can see (`.in(column, keys)`). PostgREST puts that list in the
// REQUEST URL, so a large selection builds a multi-tens-of-KB URL and the
// gateway rejects the whole query — a COSMOS-wide NIRCam mosaic selection is
// ~20-70 KB against a typical ~8-16 KB limit. The failure is total and looks
// like "nothing was authorized", which silently drops every one of those files
// from the generated script (issue: NIRCam download script listed only the
// exposure maps, whose short key list was the one query that fit).
//
// So: never send an unbounded `.in()` list. Slice it, run the slices in
// bounded waves, and surface a query error instead of returning an empty set
// that a caller can't tell apart from "you may not download any of these".
// `web/lib/r2.ts` (resolveObjectBackends) does the same for its registry
// lookup.
import 'server-only';

/** Keys per `.in()` query. 50 keeps the URL well under any gateway limit even
 *  for the longest canonical NIRCam mosaic keys (~5 KB per chunk). */
export const KEY_AUTH_CHUNK = 50;

/** Chunks in flight at once. Bounds the DB fan-out for a very large selection
 *  (5,000 keys = 100 chunks) without giving up the round-trip overlap. */
const MAX_CONCURRENT_CHUNKS = 10;

type QueryResult<Row> = { data: Row[] | null; error: { message: string } | null };

/**
 * Resolve which of `keys` the DB returns, querying in chunks.
 *
 * `runQuery` receives one chunk and must return the rows visible to the caller
 * (i.e. the query must run under the caller's RLS session, never the service
 * role). `column` names the field on those rows carrying the key.
 *
 * Returns the authorized keys in input order, deduplicated. Any chunk error
 * fails the whole call — a partial authorization set would silently truncate
 * the caller's download.
 */
export async function authorizeKeysInChunks<Row extends Record<string, unknown>>(
  keys: string[],
  column: string,
  runQuery: (chunk: string[]) => PromiseLike<QueryResult<Row>>,
  chunkSize: number = KEY_AUTH_CHUNK,
): Promise<{ keys: string[]; error: string | null }> {
  const requested = [...new Set(keys)];
  if (requested.length === 0) return { keys: [], error: null };

  const chunks: string[][] = [];
  for (let i = 0; i < requested.length; i += chunkSize) {
    chunks.push(requested.slice(i, i + chunkSize));
  }

  const found = new Set<string>();
  for (let i = 0; i < chunks.length; i += MAX_CONCURRENT_CHUNKS) {
    const wave = chunks.slice(i, i + MAX_CONCURRENT_CHUNKS);
    const results = await Promise.all(wave.map((chunk) => runQuery(chunk)));
    for (const { data, error } of results) {
      if (error) return { keys: [], error: error.message };
      for (const row of data ?? []) {
        const value = row[column];
        if (typeof value === 'string') found.add(value);
      }
    }
  }

  // Input order, so the caller's key↔presigned-url pairing is deterministic.
  return { keys: requested.filter((k) => found.has(k)), error: null };
}
