import 'server-only';

import { getRequestIdentity } from '@/lib/auth/identity';
import { requestDrTag } from './server';

/**
 * Make a client-rendered catalog section answer `?dr=` the way the server
 * does (P6c-1): the list pages fetch their data from the client, so without
 * this the HTML of `/nirspec?dr=DR-nope` would be a 200 and only its reads
 * would fail. Called from the section layouts; it resolves identity, which
 * confirms the release with the database (404 for a tag the hook refuses)
 * and applies the viewer gate (404 for a viewer the pages flag does not
 * admit). A no-op while browsing live.
 */
export async function assertReleaseViewer(): Promise<void> {
  const tag = await requestDrTag();
  if (!tag) return;
  await getRequestIdentity();
}
