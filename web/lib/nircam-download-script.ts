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

# Check the credential once, up front, rather than failing once per file: the
# download route without a key answers 400 to an accepted credential and 401
# to a rejected one, and never touches a file either way.
check_code=$(curl -sS -o /dev/null -w '%{http_code}' \\
  -H "Authorization: Bearer $API_KEY" "$BASE_URL/api/v1/storage/download")
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

# fetch <storage key (url-encoded)> <local path under OUT_DIR> <expected bytes, 0 if unknown>
fetch() {
  local key="$1" rel="$2" expected="$3"
  local file="$OUT_DIR/$rel" part="$OUT_DIR/$rel.part" headers="$OUT_DIR/$rel.headers"
  local attempt code rc size have want total
  n=$((n + 1))
  echo "[$n/$TOTAL] $rel"

  if [ -s "$file" ]; then
    size=$(( $(wc -c < "$file") ))
    if [ "$expected" -ne 0 ] && [ "$size" -eq "$expected" ]; then
      echo "  already downloaded, skipping"
      skipped=$((skipped + 1))
      return 0
    fi
    if [ "$expected" -eq 0 ]; then
      # The archive did not tell us this file's size, so a file on disk cannot
      # be told apart from a truncated one (an older script wrote straight to
      # this name). Ask the store: resume from where the file ends, and a
      # complete file answers 416 with the true size, nothing re-downloaded.
      echo "  exists ($size bytes, archive size unknown); verifying against the store"
      rm -f "$part"
      mv -f "$file" "$part"
    else
      # A complete download always lands via mv below, so a wrong-sized file is
      # either a partial from an older script or a product re-deployed since.
      # Fetch it again; the old file stays until the new one is complete.
      echo "  exists with $size bytes, expected $expected; downloading again"
      rm -f "$part"
    fi
  fi

  mkdir -p "$(dirname "$file")"
  attempt=1
  while :; do
    # -C - resumes the .part file. Each attempt asks the API for a fresh
    # presigned link (the 302), and curl drops the Authorization header when
    # it follows the redirect to the storage host, as the store requires.
    # -D keeps the response headers: on a 416 the store's Content-Range
    # carries the object's true size.
    code=$(curl -fL --progress-bar -C - -o "$part" -D "$headers" -w '%{http_code}' \\
      -H "Authorization: Bearer $API_KEY" \\
      "$BASE_URL/api/v1/storage/download?key=$key")
    rc=$?
    total=$(grep -i '^content-range:' "$headers" 2>/dev/null | tail -1 | sed 's|.*[*]/||' | tr -dc '0-9')
    rm -f "$headers"
    if [ "$rc" -eq 0 ] || [ "$code" = "416" ]; then
      have=$(( $(wc -c < "$part") ))
      want=$expected
      # 416: the .part already covers the whole object (a complete file being
      # verified, or a previous run that died before the rename). The size
      # the store reported is the one to check against when the archive's
      # is unknown.
      if [ "$code" = "416" ] && [ "$want" -eq 0 ]; then
        want=\${total:-0}
      fi
      if [ "$want" -ne 0 ] && [ "$have" -ne "$want" ]; then
        if [ "$code" = "416" ]; then
          # Larger than the object: a leftover from an older, larger deploy.
          # It can never resume (every offset is past the end), so start over.
          echo "  partial file has $have bytes but the object is $want; starting over"
          rm -f "$part"
          if [ "$attempt" -ge "$ATTEMPTS" ]; then
            echo "  error: giving up after $ATTEMPTS attempts; re-run to retry" >&2
            failed=$((failed + 1)); failed_files="$failed_files\\n  $rel"
            return 1
          fi
          attempt=$((attempt + 1))
          continue
        fi
        echo "  error: downloaded $have bytes but the archive lists $want; kept as $part" >&2
        failed=$((failed + 1)); failed_files="$failed_files\\n  $rel"
        return 1
      fi
      mv -f "$part" "$file"
      if [ "$code" = "416" ]; then
        echo "  already complete"
        skipped=$((skipped + 1))
      else
        downloaded=$((downloaded + 1))
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
if [ "$failed" -gt 0 ]; then
  printf "\\nFailed:%b\\n\\nRe-run this script to retry them.\\n" "$failed_files" >&2
  exit 1
fi
`;

  return out;
}
