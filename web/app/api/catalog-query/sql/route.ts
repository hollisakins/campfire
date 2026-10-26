import { NextRequest } from 'next/server';
import { getRequestPrincipal } from '@/lib/auth/identity';
import { parseTreeParam } from '@/lib/api-helpers';
import { treeRejection } from '@/lib/server/catalog-query';

export interface CatalogQuerySqlResponse {
  /** The WHERE clause catalog_tree_sql compiles for the tree, without the access scope. */
  sql: string;
  root: 'objects' | 'spectra' | 'targets';
}

const NO_STORE = { 'Cache-Control': 'private, no-store' };

/**
 * GET /api/catalog-query/sql?q=<tree>&root=objects|spectra
 *
 * The SQL a predicate tree compiles to, for the Advanced modal's "Copy as
 * SQL": the same `catalog_tree_sql` the queries run, without the scope
 * clause (the caller's own RLS applies when the text is run). Compiling
 * touches no data, but the function is the catalogue's, so a session is
 * required like every other read.
 */
export async function GET(request: NextRequest) {
  const principal = await getRequestPrincipal();
  if (!principal) return Response.json({ error: 'Unauthorized' }, { status: 401 });

  const sp = request.nextUrl.searchParams;
  const root = sp.get('root') ?? 'objects';
  if (root !== 'objects' && root !== 'spectra' && root !== 'targets') {
    return Response.json({ error: 'root must be objects, spectra or targets' }, { status: 400 });
  }
  const tree = parseTreeParam(sp);
  if (tree.error) return Response.json({ error: tree.error }, { status: 400 });
  if (!tree.tree) return Response.json({ error: 'q is required' }, { status: 400 });

  const { data, error } = await principal.supabase.rpc('catalog_tree_sql', { p_tree: tree.tree, p_root: root });
  const rejected = treeRejection(error);
  if (rejected) return Response.json({ error: rejected }, { status: 400 });
  if (error) {
    console.error('catalog-query sql error:', error);
    return Response.json({ error: 'Failed to compile the query' }, { status: 500 });
  }
  const body: CatalogQuerySqlResponse = { sql: String(data ?? ''), root };
  return Response.json(body, { headers: NO_STORE });
}
