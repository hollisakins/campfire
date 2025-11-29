// Cloudflare R2 client setup
// This is a placeholder for future integration with R2 storage

import { S3Client, GetObjectCommand, HeadObjectCommand } from '@aws-sdk/client-s3';
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
 * Extract observation name from object_id
 * Pattern: {observation}_{number} -> {observation}
 * Example: ember_uds_p4_1018 -> ember_uds_p4
 * @param objectId - Full object ID
 * @returns Observation name (everything before the last underscore and number)
 */
export function extractObservationName(objectId: string): string {
  // Split by underscore and find the last part
  const parts = objectId.split('_');

  // Check if the last part is a number
  const lastPart = parts[parts.length - 1];
  if (/^\d+$/.test(lastPart)) {
    // If it's a number, remove it and join the rest
    return parts.slice(0, -1).join('_');
  }

  // If not a number pattern, return the full object_id as observation name
  return objectId;
}

/**
 * Generate RGB image path in R2 for an object
 * Pattern: rgb/{observation}/{object_id}_rgb.png
 * Example: rgb/ember_uds_p4/ember_uds_p4_1018_rgb.png
 * @param objectId - Full object ID
 * @returns R2 path to RGB image
 */
export function generateRGBImagePath(objectId: string): string {
  const observation = extractObservationName(objectId);
  return `rgb/${observation}/${objectId}_rgb.png`;
}

/**
 * Generate a signed URL for an RGB image from R2
 * @param objectId - Full object ID
 * @param expiresIn - URL expiration time in seconds (default: 1 hour)
 * @returns Signed URL for the RGB image
 */
export async function generateRGBImageUrl(
  objectId: string,
  expiresIn: number = 3600
): Promise<string> {
  const rgbPath = generateRGBImagePath(objectId);
  return generateDownloadUrl(rgbPath, expiresIn);
}

/**
 * Generate SED plot path in R2 for an object
 * Pattern: sed/{observation}/{object_id}_sed.pdf
 * Example: sed/ember_uds_p4/ember_uds_p4_1018_sed.pdf
 * @param objectId - Full object ID
 * @returns R2 path to SED plot PDF
 */
export function generateSEDPlotPath(objectId: string): string {
  const observation = extractObservationName(objectId);
  return `sed/${observation}/${objectId}_sed.pdf`;
}

/**
 * Check if a file exists in R2
 * @param filePath - Path to the file in R2
 * @returns Promise<boolean> - true if file exists, false otherwise
 */
export async function fileExists(filePath: string): Promise<boolean> {
  try {
    const command = new HeadObjectCommand({
      Bucket: R2_BUCKET_NAME,
      Key: filePath,
    });

    await r2Client.send(command);
    return true;
  } catch (error) {
    // HeadObject throws an error if file doesn't exist
    return false;
  }
}

/**
 * Check if a SED plot exists for an object
 * @param objectId - Full object ID
 * @returns Promise<boolean> - true if SED plot exists, false otherwise
 */
export async function sedPlotExists(objectId: string): Promise<boolean> {
  const sedPath = generateSEDPlotPath(objectId);
  return fileExists(sedPath);
}
