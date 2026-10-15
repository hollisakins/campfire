import 'server-only';

import { headers } from 'next/headers';
import { DR_HEADER, parseDrTag } from './tag';

/**
 * The release this request browses, from the header middleware stamps for a
 * `?dr=` URL (never from the client directly: middleware strips an incoming
 * `x-campfire-dr`). Null for the live catalog, and in any context without
 * request headers.
 */
export async function requestDrTag(): Promise<string | null> {
  try {
    const h = await headers();
    return parseDrTag(h.get(DR_HEADER));
  } catch {
    return null;
  }
}
