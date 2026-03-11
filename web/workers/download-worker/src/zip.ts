/**
 * ZIP streaming using fflate
 * Fetches files from R2 and streams them into a ZIP archive
 */

import { Zip, ZipPassThrough } from 'fflate';
import type { DownloadFile } from './auth';

/**
 * Stream multiple files from R2 into a ZIP archive
 */
export async function streamZip(
  files: DownloadFile[],
  writable: WritableStream,
  bucket: R2Bucket
): Promise<void> {
  const writer = writable.getWriter();

  // Collect chunks from fflate's synchronous callback, then flush async
  // fflate's callback data may be a view into a reusable buffer, so we
  // must copy each chunk before the callback returns.
  const chunks: Uint8Array[] = [];
  let zipError: Error | null = null;
  let finalized = false;

  try {
    // Create ZIP instance
    const zip = new Zip((err, data, final) => {
      if (err) {
        zipError = err instanceof Error ? err : new Error(String(err));
        return;
      }

      // Copy data — fflate may reuse the underlying buffer
      chunks.push(new Uint8Array(data));

      if (final) {
        finalized = true;
      }
    });

    // Process each file
    for (const file of files) {
      try {
        // Fetch from R2
        const object = await bucket.get(file.key);

        if (!object) {
          console.warn(`File not found in R2: ${file.key}`);
          continue;
        }

        // Get file data
        const arrayBuffer = await object.arrayBuffer();
        const uint8Array = new Uint8Array(arrayBuffer);

        // Create ZIP entry (no compression for FITS files - they're already compressed)
        const zipFile = new ZipPassThrough(file.filename);
        zip.add(zipFile);

        // Write file data (triggers synchronous callback)
        zipFile.push(uint8Array, true);

        if (zipError) throw zipError;

        // Flush accumulated chunks to the stream
        for (const chunk of chunks) {
          await writer.write(chunk);
        }
        chunks.length = 0;
      } catch (fileError) {
        if (fileError === zipError) throw fileError;
        console.error(`Error processing file ${file.key}:`, fileError);
        continue;
      }
    }

    // Finalize ZIP (triggers final callback with central directory)
    zip.end();

    if (zipError) throw zipError;

    // Flush remaining chunks (central directory records)
    for (const chunk of chunks) {
      await writer.write(chunk);
    }

    await writer.close();
  } catch (error) {
    console.error('ZIP streaming error:', error);
    await writer.abort(error);
    throw error;
  }
}
