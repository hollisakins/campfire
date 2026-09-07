// The NIRCam bulk-download shell script (components/nircam/CurlScriptGenerator).
//
// Pure text generation, kept out of the component so the script's contract
// can be tested: one `fetch` line per product, every download going through
// GET /api/v1/storage/download (a fresh presigned url per file, minted at
// download time, so the script never expires), complete files skipped and
// partial ones resumed, so a failed run is simply re-run.
//
// The script carries a download token (lib/auth/tokens.ts): a credential that
// names the user and can only download what they may download, for 30 days.
// It is what lets "download the script and run it" work without an API key,
// and why the file must not be shared. CAMPFIRE_API_KEY in the environment
// overrides it (an API key works after the token expires). The products come
// from the field page's RLS-scoped listing, and the route re-authorizes every
// key under the credential's own scope when the script actually runs.

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

  const authNote = token
    ? `# Authentication: the script asks the CAMPFIRE API for each file's download
# link at the moment it fetches that file, so the links never go stale.
#
# THIS FILE CONTAINS A CREDENTIAL — do not share it. DOWNLOAD_TOKEN below
# lets whoever holds it download the CAMPFIRE products your account can,
# and nothing else, until ${token.expiresAt.toISOString().slice(0, 10)}.
# After that, regenerate the script from the field page — or set
# CAMPFIRE_API_KEY to an API key from ${base}${API_KEYS_PATH}, which
# takes precedence over the embedded token whenever it is set.`
    : `# Authentication: the script asks the CAMPFIRE API for each file's download
# link at the moment it fetches that file, so nothing in this script expires.
# The API needs a key: create one at ${base}${API_KEYS_PATH}
# and export it before running (the script prompts for it otherwise):
#
#   export CAMPFIRE_API_KEY=sk_...
#   bash ${NIRCAM_DOWNLOAD_SCRIPT_FILENAME}`;

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

  const rejectedHint = token
    ? `The embedded token expires ${token.expiresAt.toISOString().slice(0, 10)}: regenerate the script from the field page, or set CAMPFIRE_API_KEY.`
    : `Check it at $BASE_URL${API_KEYS_PATH}`;

  let out = `#!/bin/bash
# CAMPFIRE NIRCam Data Download Script
# Generated: ${now.toISOString()}
# Files: ${rows.length}
# Total size: ${formatFileSize(totalBytes)}
#
# Resumable: if a run fails or is interrupted, just run the script again.
# Files that already exist with the right size are skipped and partial
# downloads (*.part) resume where they stopped.
#
${authNote}
#
# Prefer a Python tool? The campfire CLI's \`campfire pull --field <field>\`
# downloads the same products — see ${base}/docs/api/cli

set -u

BASE_URL=${shellQuote(base)}
OUT_DIR="\${CAMPFIRE_DOWNLOAD_DIR:-nircam_data}"
ATTEMPTS=5
TOTAL=${rows.length}

${credential}

# Every API call goes through here: the bearer rides in curl's config read
# from stdin (-K -), never on the command line, where any other user on the
# host could read it out of the process list (ps, /proc/<pid>/cmdline) for as
# long as a transfer runs — and this script is written for shared clusters.
auth_curl() {
  printf 'header = "Authorization: Bearer %s"\\n' "$API_KEY" | curl -K - "$@"
}

# Check the credential once, up front, rather than failing once per file: the
# download route without a key answers 400 to an accepted credential and 401
# to a rejected one, and never touches a file either way.
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


# probe_total <key> <offset>: the object's size according to the store, read
# from the Content-Range of a one-byte range request at <offset> (a 206 and a
# 416 both carry it). Empty when the store could not be asked.
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
    # The listing gave no size, or a different one (an older script wrote
    # straight to this name and may have been cut short; or the product was
    # re-deployed since this script was generated). The store is the truth:
    # ask it how big the object is, without downloading anything.
    total=$(probe_total "$key" "$size")
    if [ -n "$total" ] && [ "$size" -eq "$total" ]; then
      echo "  already downloaded (size verified against the store), skipping"
      if [ "$expected" -ne 0 ]; then
        stale=$((stale + 1)); stale_files="$stale_files\\n  $rel (listing: $expected bytes, store: $total)"
      fi
      skipped=$((skipped + 1))
      return 0
    fi
    # Never resume a mismatched final file: if it is an older version of the
    # product, appending the new one's tail would corrupt it. Fetch it again;
    # the old file stays until the new one is complete. Any .part on disk is
    # this script's own in-progress download of the current object (it is
    # never seeded from the final file), so the loop below resumes it rather
    # than starting over — a slow link makes net progress across runs.
    echo "  exists with $size bytes but the object is \${total:-of unknown size}; downloading again"
  fi

  mkdir -p "$(dirname "$file")"
  attempt=1
  while :; do
    # -C - resumes the .part file. Each attempt asks the API for a fresh
    # presigned link (the 302), and curl drops the Authorization header when
    # it follows the redirect to the storage host, as the store requires.
    # -D keeps the response headers: on a 416 the store's Content-Range
    # carries the object's true size.
    code=$(auth_curl -fL --progress-bar -C - -o "$part" -D "$headers" -w '%{http_code}' \\
      "$BASE_URL/api/v1/storage/download?key=$key")
    rc=$?
    total=$(grep -i '^content-range:' "$headers" 2>/dev/null | tail -1 | sed 's|.*/||' | tr -dc '0-9')
    rm -f "$headers"
    if [ "$code" = "416" ]; then
      # The .part already reaches the end of the object: a previous run died
      # between the download finishing and the rename — or it is a leftover
      # from an older, larger deploy, which can never resume (every offset is
      # past the end). The Content-Range total tells the two apart.
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
      # curl checked the body against Content-Length, so these are the whole
      # object as the store has it. A listing that says otherwise is stale
      # (re-deployed since this script was generated): worth a note, not a
      # failure.
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
  printf "\\nNote: %s file(s) differ in size from this script's listing — the product\\nmay have been re-deployed since the script was generated. The files on disk\\nare what the archive serves now; regenerate the script to refresh the listing.%b\\n" "$stale" "$stale_files"
fi
if [ "$failed" -gt 0 ]; then
  printf "\\nFailed:%b\\n\\nRe-run this script to retry them.\\n" "$failed_files" >&2
  exit 1
fi
`;

  return out;
}
