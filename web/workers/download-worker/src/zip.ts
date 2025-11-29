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

  try {
    // Create ZIP instance
    const zip = new Zip((err, data, final) => {
      if (err) {
        console.error('ZIP error:', err);
        writer.abort(err);
        return;
      }

      // Write chunk to stream
      writer.write(data);

      // Close stream when ZIP is finalized
      if (final) {
        writer.close();
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

        // Write file data
        zipFile.push(uint8Array, true); // true = final chunk
      } catch (fileError) {
        console.error(`Error processing file ${file.key}:`, fileError);
        // Continue with next file instead of failing entire download
        continue;
      }
    }

    // Finalize ZIP
    zip.end();
  } catch (error) {
    console.error('ZIP streaming error:', error);
    await writer.abort(error);
    throw error;
  }
}
