'use server';

import { linkMayDownload } from '@/lib/auth/access-context';
import { getRequestPrincipal } from '@/lib/auth/identity';
import { generateDownloadToken } from '@/lib/auth/tokens';

export interface DownloadTokenResult {
  /** null when no credential could be minted; `error` then says why. */
  token: string | null;
  expiresAt: string | null;
  /** True when the caller is a share-link visitor, minted or refused. The
   *  panel's advice differs for them: they have no account, so no API key to
   *  fall back on, and a refusal is final rather than something to fix. */
  shareLink: boolean;
  error: string | null;
}

/**
 * Mint the download token a generated NIRCam bulk-download script embeds
 * (lib/auth/tokens.ts explains the credential; lib/nircam-download-script.ts
 * the script). Minted for the cookie session that asked, so the token names
 * exactly the viewer building the script.
 *
 * Share links get one too, so a shared field can be bulk-downloaded like any
 * other — but only while the link is live and permits downloads, and the token
 * expires with the link. What it can then reach is the link's own scope,
 * re-resolved on every request by GET /api/v1/storage/download. A link minted
 * with allow_download off gets no token, which is the whole point of the flag.
 */
export async function mintNircamDownloadToken(): Promise<DownloadTokenResult> {
  const refuse = (error: string, shareLink = false): DownloadTokenResult => ({
    token: null, expiresAt: null, shareLink, error,
  });
  try {
    const principal = await getRequestPrincipal();
    if (!principal) return refuse('Not authenticated');

    const isLink = principal.access.isLinkAccount;
    if (isLink && !linkMayDownload(principal.access)) {
      return refuse('This shared link does not permit file downloads.', true);
    }

    // A link with an expiry mints a credential that dies with it, so the
    // script's "valid until" is the truth rather than an optimistic 30 days.
    const linkExpiry = principal.access.linkScope?.expiresAt;
    const { token, expiresAt } = await generateDownloadToken(principal.user.id, {
      notAfter: linkExpiry ? new Date(linkExpiry) : null,
    });
    return { token, expiresAt: expiresAt.toISOString(), shareLink: isLink, error: null };
  } catch (err) {
    console.error('Error minting NIRCam download token:', err);
    return refuse('Failed to prepare the download script.');
  }
}
