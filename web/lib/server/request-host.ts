import 'server-only';

import { headers } from 'next/headers';
import { localizeUrl } from '@/lib/host-aliases';

/** The current request's Host header, or null outside a request scope. */
export async function requestHost(): Promise<string | null> {
  try {
    return (await headers()).get('host');
  } catch {
    return null;
  }
}

/** `url` localized for the current request's host (lib/host-aliases.ts). */
export async function urlForRequest(url: string): Promise<string> {
  return localizeUrl(url, await requestHost());
}
