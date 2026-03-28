// Cloudflare R2 client setup
// This is a placeholder for future integration with R2 storage

import { S3Client, GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';

// Placeholder credentials (will be replaced with environment variables)
const R2_ACCOUNT_ID = process.env.R2_ACCOUNT_ID || 'placeholder';
const R2_ACCESS_KEY_ID = process.env.R2_ACCESS_KEY_ID || 'placeholder';
const R2_SECRET_ACCESS_KEY = process.env.R2_SECRET_ACCESS_KEY || 'placeholder';
const R2_BUCKET_NAME = process.env.R2_BUCKET_NAME || 'campfire-fits';

// Create R2 client (S3-compatible)
export const r2Client = new S3Client({
  region: 'auto',
  endpoint: `https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com`,
  credentials: {
    accessKeyId: R2_ACCESS_KEY_ID,
    secretAccessKey: R2_SECRET_ACCESS_KEY,
  },
});

/**
 * Generate a signed URL for downloading a FITS file from R2
 * @param fitsPath - Path to the FITS file in R2 (e.g., "v1.0/object_id/PRISM.fits")
 * @param expiresIn - URL expiration time in seconds (default: 1 hour)
 * @returns Signed URL for downloading the file
 */
export async function generateDownloadUrl(
  fitsPath: string,
  expiresIn: number = 3600
): Promise<string> {
  const command = new GetObjectCommand({
    Bucket: R2_BUCKET_NAME,
    Key: fitsPath,
  });

  try {
    const signedUrl = await getSignedUrl(r2Client, command, { expiresIn });
    return signedUrl;
  } catch (error) {
    console.error('Error generating signed URL:', error);
    // For now, return a placeholder URL
    return `#download-placeholder-${fitsPath}`;
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

