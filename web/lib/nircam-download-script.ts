// The NIRCam bulk-download shell script (components/nircam/CurlScriptGenerator).
//
// Kept out of the component so the script's contract can be tested: one
// `fetch` line per product, every download going through
// GET /api/v1/storage/download, which mints a fresh presigned url per file at
// download time — so the script itself never goes stale. Complete files are
// skipped and partial ones resumed, so a failed run is simply re-run.
//
// The script embeds a download token (lib/auth/tokens.ts) scoped to the
// viewer, so it runs without an API key; CAMPFIRE_API_KEY overrides it. The
// route re-authorizes every key under that scope when the script runs.

import type { NircamProductRow } from '@/lib/types';
import { isCompressedKey } from '@/lib/layout';

export const NIRCAM_DOWNLOAD_SCRIPT_FILENAME = 'download_nircam_data.sh';

/** Path on the site where a user mints the `sk_` key the script needs. */
export const API_KEYS_PATH = '/profile/api-keys';

/** Bytes the script moves for a product: the stored (gzipped) size when the
 * registry recorded one, the logical size otherwise. Zero when unknown. */
export function transferBytes(row: NircamProductRow): number {
  return row.file_size_stored ?? row.file_size ?? 0;
}

export function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

/** Bytes the script should find on disk once a product is complete, or 0
 * when it cannot know. A gzipped mosaic is downloaded as-is (`.fits.gz`), so
 * only the registry's stored size describes it; the page attaches that
 * fail-open (lib/actions/nircam.ts attachStoredSizes), and a row that came
 * through without it must not be checked against the logical size — the
 * script would re-download it on every run and never accept the result. */
export function expectedBytes(row: NircamProductRow): number {
  if (row.file_size_stored != null) return row.file_size_stored;
  if (isCompressedKey(row.file_path)) return 0;
  return row.file_size ?? 0;
}

/** Local filename for a product: the basename of its storage key (a gzipped
 * mosaic keeps its `.fits.gz`). */
export function localFilename(row: NircamProductRow): string {
  return row.file_path.split('/').pop() || row.file_path;
}

/** POSIX single-quote a string for the shell. Storage keys and field names
 * are plain `[A-Za-z0-9_./-]`, but quote defensively anyway. */
export function shellQuote(s: string): string {
  return `'${s.replace(/'/g, `'\\''`)}'`;
}

/** The download-route url for a storage key, relative to `origin`. */
export function downloadRouteUrl(origin: string, key: string): string {
  return `${origin}/api/v1/storage/download?key=${encodeURIComponent(key)}`;
}

/** The credential a script embeds (lib/actions/download-token.ts mints it). */
export interface EmbeddedDownloadToken {
  token: string;
  expiresAt: Date;
  /** Minted for a share link rather than an account. The credential works the
   *  same way; only the advice changes — a link visitor has no account, so no
   *  API key to fall back on, and the script dies with the link. */
  shareLink?: boolean;
}

export interface BuildScriptOptions {
  /** Omit to build a script that relies on CAMPFIRE_API_KEY / a prompt. */
  token?: EmbeddedDownloadToken;
  now?: Date;
}

/**
 * Build the script for `rows` (the user's selection), downloading from the
 * CAMPFIRE deployment at `origin` (e.g. `https://campfire.hollisakins.com`).
 * Empty when there is nothing to download.
 */
export function buildNircamDownloadScript(
  rows: NircamProductRow[],
  origin: string,
  opts: BuildScriptOptions = {},
): string {
  if (rows.length === 0) return '';

  const now = opts.now ?? new Date();
  const base = origin.replace(/\/+$/, '');
  const totalBytes = rows.reduce((sum, r) => sum + transferBytes(r), 0);
  const fields = [...new Set(rows.map((r) => r.field))];
  const token = opts.token;

  const authNote = token?.shareLink
    ? `# Scoped to the shared link this script came from, DOWNLOAD_TOKEN below
# authorizes the downloads until ${token.expiresAt.toISOString().slice(0, 10)}, or until the link is revoked.
# Regenerate the script from the same page if it stops working.`
    : token
    ? `# DOWNLOAD_TOKEN below authorizes the downloads, and works until ${token.expiresAt.toISOString().slice(0, 10)}.
# After that, regenerate the script from the field page, or set CAMPFIRE_API_KEY
# (it takes precedence whenever set) to a key from
# ${base}${API_KEYS_PATH}`
    : `# Needs a CAMPFIRE API key — create one at ${base}${API_KEYS_PATH}
# and export it before running (the script prompts for it otherwise):
#
#   export CAMPFIRE_API_KEY=sk_...`;

  const credential = token
    ? `DOWNLOAD_TOKEN=${shellQuote(token.token)}
API_KEY="\${CAMPFIRE_API_KEY:-$DOWNLOAD_TOKEN}"`
    : `API_KEY="\${CAMPFIRE_API_KEY:-}"
if [ -z "$API_KEY" ] && [ -t 0 ]; then
  read -rsp "CAMPFIRE API key (sk_...): " API_KEY
  echo
fi
if [ -z "$API_KEY" ]; then
  echo "error: no API key. Create one at $BASE_URL${API_KEYS_PATH} and run:" >&2
  echo "  CAMPFIRE_API_KEY=sk_... bash $0" >&2
  exit 1
fi`;

  const rejectedHint = token?.shareLink
    ? `The shared link may have expired or been revoked, and the embedded token expires ${token.expiresAt.toISOString().slice(0, 10)}: open the shared page again to check.`
    : token
      ? `The embedded token expires ${token.expiresAt.toISOString().slice(0, 10)}: regenerate the script from the field page, or set CAMPFIRE_API_KEY.`
      : `Check it at $BASE_URL${API_KEYS_PATH}`;

  let out = `#!/bin/bash
# CAMPFIRE NIRCam data download
# Generated: ${now.toISOString()}
# Files: ${rows.length}
# Total size: ${formatFileSize(totalBytes)}
#
# Run: bash ${NIRCAM_DOWNLOAD_SCRIPT_FILENAME}
# Re-run to resume: finished files are skipped, partial ones (*.part) continue
# where they stopped.
#
${authNote}
#
# The campfire CLI downloads the same products with
# \`campfire pull --field <field>\` — see ${base}/docs/api/cli

set -u

BASE_URL=${shellQuote(base)}
OUT_DIR="\${CAMPFIRE_DOWNLOAD_DIR:-nircam_data}"
ATTEMPTS=5
TOTAL=${rows.length}

${credential}

# Every API call goes through here. The bearer rides in a curl config on
# stdin (-K -) rather than the command line, which is readable by other users
# on a shared host.
auth_curl() {
  printf 'header = "Authorization: Bearer %s"\\n' "$API_KEY" | curl -K - "$@"
}

# Check the credential once, up front: with no key= the download route answers
# 400 to an accepted credential and 401 to a rejected one, touching no files.
check_code=$(auth_curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/storage/download")
if [ "$check_code" = "401" ]; then
  echo "error: the API rejected this credential (HTTP 401). ${rejectedHint}" >&2
  exit 1
fi
if [ "$check_code" = "000" ]; then
  echo "error: could not reach $BASE_URL" >&2
  exit 1
fi

n=0
downloaded=0
skipped=0
failed=0
failed_files=""
stale=0
stale_files=""


# probe_total <key> <offset>: the object's size per the store, from the
# Content-Range of a one-byte range request (206 and 416 both carry it).
# Empty when the store could not be asked.
probe_total() {
  local hdr="$OUT_DIR/.probe.$$" total
  auth_curl -sSL -o /dev/null -D "$hdr" -r "$2-$2" \\
    "$BASE_URL/api/v1/storage/download?key=$1" >/dev/null 2>&1
  total=$(grep -i '^content-range:' "$hdr" 2>/dev/null | tail -1 | sed 's|.*/||' | tr -dc '0-9')
  rm -f "$hdr"
  printf '%s' "$total"
}

# fetch <storage key (url-encoded)> <local path under OUT_DIR> <expected bytes, 0 if unknown>
fetch() {
  local key="$1" rel="$2" expected="$3"
  local file="$OUT_DIR/$rel" part="$OUT_DIR/$rel.part" headers="$OUT_DIR/$rel.headers"
  local attempt code rc size have total
  n=$((n + 1))
  echo "[$n/$TOTAL] $rel"

  if [ -s "$file" ]; then
    size=$(( $(wc -c < "$file") ))
    if [ "$expected" -ne 0 ] && [ "$size" -eq "$expected" ]; then
      echo "  already downloaded, skipping"
      skipped=$((skipped + 1))
      return 0
    fi
    # No size in the listing, or a different one (a truncated older download,
    # or a re-deploy since this script was generated). Ask the store.
    total=$(probe_total "$key" "$size")
    if [ -n "$total" ] && [ "$size" -eq "$total" ]; then
      echo "  already downloaded (size verified against the store), skipping"
      if [ "$expected" -ne 0 ]; then
        stale=$((stale + 1)); stale_files="$stale_files\\n  $rel (listing: $expected bytes, store: $total)"
      fi
      skipped=$((skipped + 1))
      return 0
    fi
    # Never resume a mismatched final file: appending a new version's tail to
    # an old one would corrupt it. Re-fetch, keeping the old file until the new
    # one lands. Any .part is this script's own download of the current object,
    # so the loop below resumes it and a slow link still makes progress.
    echo "  exists with $size bytes but the object is \${total:-of unknown size}; downloading again"
  fi

  mkdir -p "$(dirname "$file")"
  attempt=1
  while :; do
    # -C - resumes the .part. Each attempt gets a fresh presigned link (the
    # 302); curl drops the Authorization header on the redirect, as the store
    # requires. -D keeps the headers for the 416 Content-Range below.
    code=$(auth_curl -fL --progress-bar -C - -o "$part" -D "$headers" -w '%{http_code}' \\
      "$BASE_URL/api/v1/storage/download?key=$key")
    rc=$?
    total=$(grep -i '^content-range:' "$headers" 2>/dev/null | tail -1 | sed 's|.*/||' | tr -dc '0-9')
    rm -f "$headers"
    if [ "$code" = "416" ]; then
      # The .part reaches the end of the object: either a run that died before
      # the rename, or a leftover from a larger older deploy that can never
      # resume. The Content-Range total tells them apart.
      have=$(( $(wc -c < "$part") ))
      if [ -n "$total" ] && [ "$have" -ne "$total" ]; then
        echo "  partial file has $have bytes but the object is $total; starting over"
        rm -f "$part"
        if [ "$attempt" -ge "$ATTEMPTS" ]; then
          echo "  error: giving up after $ATTEMPTS attempts; re-run to retry" >&2
          failed=$((failed + 1)); failed_files="$failed_files\\n  $rel"
          return 1
        fi
        attempt=$((attempt + 1))
        continue
      fi
      mv -f "$part" "$file"
      echo "  already complete"
      skipped=$((skipped + 1))
      return 0
    fi
    if [ "$rc" -eq 0 ]; then
      # curl checked the body against Content-Length, so this is the whole
      # object. A listing that says otherwise is stale: a note, not a failure.
      have=$(( $(wc -c < "$part") ))
      mv -f "$part" "$file"
      downloaded=$((downloaded + 1))
      if [ "$expected" -ne 0 ] && [ "$have" -ne "$expected" ]; then
        echo "  note: the listing said $expected bytes, the store served $have"
        stale=$((stale + 1)); stale_files="$stale_files\\n  $rel (listing: $expected bytes, store: $have)"
      fi
      return 0
    fi
    case "$code" in
      401)
        echo "error: the API rejected the credential mid-run (HTTP 401); stopping. ${rejectedHint}" >&2
        exit 1 ;;
      403|404)
        echo "  error: not available for your account (HTTP $code), skipping" >&2
        failed=$((failed + 1)); failed_files="$failed_files\\n  $rel"
        return 1 ;;
    esac
    if [ "$attempt" -ge "$ATTEMPTS" ]; then
      echo "  error: giving up after $ATTEMPTS attempts (curl exit $rc, HTTP $code); re-run to resume" >&2
      failed=$((failed + 1)); failed_files="$failed_files\\n  $rel"
      return 1
    fi
    echo "  transfer interrupted (curl exit $rc, HTTP $code); retrying in $((attempt * 5))s"
    sleep $((attempt * 5))
    attempt=$((attempt + 1))
  done
}

echo "============================="
echo "CAMPFIRE NIRCam Data Download"
echo "============================="
echo ""
echo "$TOTAL files (${formatFileSize(totalBytes)} total) -> $OUT_DIR/"
echo ""

`;

  for (const field of fields) {
    const fieldRows = rows.filter((r) => r.field === field);
    const fieldBytes = fieldRows.reduce((sum, r) => sum + transferBytes(r), 0);
    out += `# Field: ${field.toUpperCase()} (${fieldRows.length} files, ${formatFileSize(fieldBytes)})\n`;
    for (const row of fieldRows) {
      const rel = `${field}/${localFilename(row)}`;
      out += `fetch ${shellQuote(encodeURIComponent(row.file_path))} ${shellQuote(rel)} ${expectedBytes(row)}\n`;
    }
    out += '\n';
  }

  out += `echo ""
echo "Done: $downloaded downloaded, $skipped already present, $failed failed"
echo "Files saved in: $OUT_DIR/"
if [ "$stale" -gt 0 ]; then
  printf "\\nNote: %s file(s) differ in size from this script's listing, so they were\\nlikely re-deployed since it was generated. The files on disk are what the\\narchive serves now; regenerate the script to refresh the listing.%b\\n" "$stale" "$stale_files"
fi
if [ "$failed" -gt 0 ]; then
  printf "\\nFailed:%b\\n\\nRe-run this script to retry them.\\n" "$failed_files" >&2
  exit 1
fi
`;

  return out;
}
