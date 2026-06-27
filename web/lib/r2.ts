// Storage client helpers for the `data` bucket (FITS, RGB, SED, …).
//
// Endpoint/region/addressing-style are resolved per-purpose by the storage
// backend factory (`./storage`), so this layer is backend-agnostic
// (R2 today, OSN later). The `data` client is built lazily on first use.

import { GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { getS3Client, getBucketName } from './storage';

/** S3 client for the `data` storage backend (lazy, cached). */
export function getDataClient() {
  return getS3Client('data');
}

/**
 * Generate a signed URL for downloading a file from the data store.
 * @param fitsPath - Object key (e.g., "spectra/obs_name/file.fits")
 * @param expiresIn - URL expiration time in seconds (default: 1 hour)
 * @returns Signed URL for downloading the file
 * @throws if storage is misconfigured or signing fails — surfaced loudly so a
 *   cutover misconfig is diagnosable rather than silently masked.
 */
export async function generateDownloadUrl(
  fitsPath: string,
  expiresIn: number = 3600
): Promise<string> {
  const command = new GetObjectCommand({
    Bucket: getBucketName('data'),
    Key: fitsPath,
  });

  try {
    return await getSignedUrl(getDataClient(), command, { expiresIn });
  } catch (error) {
    console.error(`Failed to sign download URL for "${fitsPath}":`, error);
    throw new Error(`Failed to generate download URL for ${fitsPath}`);
  }
}

/**
 * Generate multiple download URLs for an object's spectra
 * @param fitsPaths - Array of FITS file paths
 * @returns Array of signed URLs
 */
export async function generateMultipleDownloadUrls(
  fitsPaths: string[]
): Promise<string[]> {
  return Promise.all(fitsPaths.map(path => generateDownloadUrl(path)));
}

/**
 * Extract observation name from target_id
 * Pattern: {observation}_{number} -> {observation}
 * Example: ember_uds_p4_1018 -> ember_uds_p4
 * @param targetId - Full target ID
 * @returns Observation name (everything before the last underscore and number)
 */
export function extractObservationName(targetId: string): string {
  // Split by underscore and find the last part
  const parts = targetId.split('_');

  // Check if the last part is a number
  const lastPart = parts[parts.length - 1];
  if (/^\d+$/.test(lastPart)) {
    // If it's a number, remove it and join the rest
    return parts.slice(0, -1).join('_');
  }

  // If not a number pattern, return the full target_id as observation name
  return targetId;
}

/**
 * Generate RGB image path in R2 for an object
 * Pattern: rgb/{observation}/{object_id}_rgb.png
 * Example: rgb/ember_uds_p4/ember_uds_p4_1018_rgb.png
 * @param targetId - Full target ID
 * @returns R2 path to RGB image
 */
export function generateRGBImagePath(targetId: string): string {
  const observation = extractObservationName(targetId);
  return `rgb/${observation}/${targetId}_rgb.png`;
}

/**
 * Generate a signed URL for an RGB image from R2
 * @param targetId - Full target ID
 * @param expiresIn - URL expiration time in seconds (default: 1 hour)
 * @returns Signed URL for the RGB image
 */
export async function generateRGBImageUrl(
  targetId: string,
  expiresIn: number = 3600
): Promise<string> {
  const rgbPath = generateRGBImagePath(targetId);
  return generateDownloadUrl(rgbPath, expiresIn);
}

/**
 * Generate SED plot path in R2 for an object
 * Pattern: sed/{observation}/{object_id}_sed.pdf
 * Example: sed/ember_uds_p4/ember_uds_p4_1018_sed.pdf
 * @param targetId - Full target ID
 * @returns R2 path to SED plot PDF
 */
export function generateSEDPlotPath(targetId: string): string {
  const observation = extractObservationName(targetId);
  return `sed/${observation}/${targetId}_sed.pdf`;
}

