'use server';

import { getRequestPrincipal } from '@/lib/auth/identity';
import { generateDownloadToken } from '@/lib/auth/tokens';

/**
 * Mint the download token a generated NIRCam bulk-download script embeds
 * (lib/auth/tokens.ts explains the credential; lib/nircam-download-script.ts
 * the script). Minted for the cookie session that asked, so the token names
 * exactly the viewer building the script. Link accounts get none: the API is
 * closed to them (see authenticateApiRequest), and a script they generated
 * would fail at run time anyway — better to say so on the page.
 */
export async function mintNircamDownloadToken(): Promise<
  { token: string; expiresAt: string; error: null } | { token: null; expiresAt: null; error: string }
> {
  try {
    const principal = await getRequestPrincipal();
    if (!principal) return { token: null, expiresAt: null, error: 'Not authenticated' };
    if (principal.access.isLinkAccount) {
      return {
        token: null,
        expiresAt: null,
        error: 'Bulk download is not available on a shared link; sign in with an account.',
      };
    }
    const { token, expiresAt } = await generateDownloadToken(principal.user.id);
    return { token, expiresAt: expiresAt.toISOString(), error: null };
  } catch (err) {
    console.error('Error minting NIRCam download token:', err);
    return { token: null, expiresAt: null, error: 'Failed to prepare the download script.' };
  }
}
