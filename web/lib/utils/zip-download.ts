// Shared client-side helper: fetch a set of authorized proxy URLs, bundle them
// into a single ZIP in the browser, and trigger a download.
//
// Both bulk download surfaces use this — the results-table "FITS ZIP" button
// (DownloadTableButtons) and the object-detail "Download All" button
// (DownloadButtons). Each file is fetched through the credential-free download
// Worker (which supplies CORS so the browser can read the bytes), then zipped
// with fflate at store level (FITS are already dense, so compression buys almost
// nothing and costs CPU/memory).

/** A ready-to-fetch Worker proxy link plus the name it should take in the ZIP. */
export interface ProxyFile {
  proxyUrl: string;
  filename: string;
}

export interface ZipDownloadResult {
  /** True if at least one file was fetched and a ZIP was produced/downloaded. */
  ok: boolean;
  /** Filenames that failed to fetch (present even when ok is true — a partial ZIP). */
  failed: string[];
  /** Set only when nothing could be fetched; carries the first failure's cause
   *  (e.g. "HTTP 403" for a rejected signature, "Failed to fetch" for an
   *  unreachable/blocked Worker) so the UI can show an actionable message. */
  error?: string;
}

/**
 * Fetch every file through the download proxy, ZIP them client-side, and start a
 * browser download of the single archive.
 *
 * @param files       proxy URLs + target filenames
 * @param zipFilename name for the downloaded archive
 * @param onProgress  called after each file settles (done, total)
 * @param concurrency max simultaneous fetches (default 6)
 */
export async function downloadFilesAsZip(
  files: ProxyFile[],
  zipFilename: string,
  onProgress?: (done: number, total: number) => void,
  concurrency = 6,
): Promise<ZipDownloadResult> {
  const fileData: { filename: string; data: Uint8Array }[] = [];
  const failed: string[] = [];
  let firstError: string | null = null;
  let completed = 0;
  const queue = [...files];

  async function fetchWorker() {
    while (queue.length > 0) {
      const file = queue.shift()!;
      try {
        // proxyUrl is a ready-to-fetch Worker link (?url=<presigned>&sig=<hmac>);
        // the Worker supplies CORS so the browser can read the bytes to zip them.
        const resp = await fetch(file.proxyUrl);
        if (!resp.ok) {
          // Include the proxy's own message (e.g. "Proxy misconfigured: …") so a
          // config/auth failure is legible instead of a bare status code.
          const detail = await resp.text().catch(() => '');
          throw new Error(detail ? `HTTP ${resp.status}: ${detail.slice(0, 200)}` : `HTTP ${resp.status}`);
        }
        const buf = await resp.arrayBuffer();
        fileData.push({ filename: file.filename, data: new Uint8Array(buf) });
      } catch (err) {
        failed.push(file.filename);
        if (!firstError) firstError = err instanceof Error ? err.message : String(err);
      }
      completed++;
      onProgress?.(completed, files.length);
    }
  }

  await Promise.all(Array.from({ length: Math.max(1, concurrency) }, () => fetchWorker()));

  if (fileData.length === 0) {
    // Every fetch failed — surface the first cause so a config/auth problem is
    // diagnosable instead of collapsing into a generic "download failed".
    return {
      ok: false,
      failed,
      error: `All ${files.length} file(s) failed to download${firstError ? ` (${firstError})` : ''}`,
    };
  }

  // Build ZIP in browser (lazy-load fflate to keep it out of the initial bundle).
  const { zipSync } = await import('fflate');

  // Deduplicate filenames so same-named FITS from different observations don't
  // clobber each other in the archive.
  const zipInput: Record<string, [Uint8Array, { level: 0 }]> = {};
  const seenNames = new Set<string>();
  for (const { filename, data } of fileData) {
    let name = filename;
    if (seenNames.has(name)) {
      const dot = name.lastIndexOf('.');
      const base = dot > 0 ? name.substring(0, dot) : name;
      const ext = dot > 0 ? name.substring(dot) : '';
      let counter = 2;
      while (seenNames.has(`${base}_${counter}${ext}`)) counter++;
      name = `${base}_${counter}${ext}`;
    }
    seenNames.add(name);
    zipInput[name] = [data, { level: 0 }];
  }

  const zipped = zipSync(zipInput);

  // fflate returns a fresh, exact-size Uint8Array, so its backing buffer is the
  // whole ZIP. The cast satisfies the DOM lib's ArrayBuffer-only BlobPart typing.
  const blob = new Blob([zipped.buffer as ArrayBuffer], { type: 'application/zip' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = zipFilename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);

  return { ok: true, failed };
}
