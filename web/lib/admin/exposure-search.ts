// Wildcard filename search for the admin NIRCam exposure list. The operator
// types glob-style patterns ("jw01727001*" = every exposure of that
// program+obs), which are translated here — in exactly one place — into the
// ILIKE pattern the get_admin_exposures / get_admin_exposure_neighbors RPCs
// and the PNG-manifest route all apply to `filename`, so the list, prev/next
// nav, and the pre-download manifest always agree on what matches.
//
// Semantics: `*` matches any run of characters, `?` exactly one; matching is
// case-insensitive. A term with no wildcard is a substring match (wrapped in
// %...%); a term with wildcards is anchored ("jw01727001*" matches from the
// start of the filename). Literal ILIKE metacharacters in the input are
// escaped, so a stray `%` or `_` in a pasted filename never widens the match.

/** Raw search text → ILIKE pattern for `filename`, or null when empty/unset. */
export function filenameSearchPattern(raw: string | null | undefined): string | null {
  const term = raw?.trim();
  if (!term) return null;
  const hasWildcard = term.includes('*') || term.includes('?');
  const pattern = term
    .replace(/\\/g, '\\\\')
    .replace(/%/g, '\\%')
    .replace(/_/g, '\\_')
    .replace(/\*/g, '%')
    .replace(/\?/g, '_');
  return hasWildcard ? pattern : `%${pattern}%`;
}
