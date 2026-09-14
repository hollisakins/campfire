'use client';

import React, { useState, useMemo, useEffect } from 'react';
import Link from 'next/link';
import { AlertTriangle, ChevronDown, ChevronUp, Download, Copy, Check, Info, KeyRound } from 'lucide-react';
import type { NircamProductRow } from '@/lib/types';
import {
  API_KEYS_PATH,
  NIRCAM_DOWNLOAD_SCRIPT_FILENAME,
  buildNircamDownloadScript,
  formatFileSize,
  transferBytes,
  type EmbeddedDownloadToken,
} from '@/lib/nircam-download-script';
import { mintNircamDownloadToken } from '@/lib/actions/download-token';

interface CurlScriptGeneratorProps {
  selectedImages: NircamProductRow[];
  className?: string;
}

// Where the script will send its API calls: this deployment. The component is
// client-only and the script is built after a click, so `window` exists by
// then; the env fallback only covers the (never-shown) server render.
function siteOrigin(): string {
  if (typeof window !== 'undefined') return window.location.origin;
  return process.env.NEXT_PUBLIC_APP_URL || 'https://campfire.hollisakins.com';
}

// The credential the open panel minted, or why it could not.
interface TokenState {
  token: EmbeddedDownloadToken | null;
  error: string | null;
  /** A share link minted with downloads off. Nothing here can work for this
   *  visitor — there is no API key to fall back on — so the panel says that
   *  instead of offering a script that would 401 on every file. */
  linkRefused: boolean;
}

/**
 * Bulk-download panel for a NIRCam product selection: a shell script the user
 * runs locally.
 *
 * The script carries no urls — each file is fetched through
 * GET /api/v1/storage/download, which mints a fresh presigned url at download
 * time, so nothing goes stale however long a whole-field download takes. See
 * lib/nircam-download-script.ts.
 *
 * Opening the panel mints a download token for the viewer (one server call;
 * lib/auth/tokens.ts) and the script embeds it, so "download and run" needs no
 * setup. A share-link visitor gets one scoped to the link, unless the link was
 * minted with downloads off. If minting fails the script still builds, in the
 * form that reads CAMPFIRE_API_KEY or prompts for it.
 */
export const CurlScriptGenerator: React.FC<CurlScriptGeneratorProps> = ({
  selectedImages,
  className = '',
}) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const [tokenState, setTokenState] = useState<TokenState | null>(null);

  // Transfer estimate: stored (gzipped) bytes when recorded, logical
  // otherwise — what the downloads actually move.
  const totalSize = useMemo(
    () => selectedImages.reduce((sum, r) => sum + transferBytes(r), 0),
    [selectedImages],
  );

  // Mint the token when the panel opens (once per opening; the selection can
  // change underneath without re-minting).
  useEffect(() => {
    if (!isExpanded) {
      setTokenState(null);
      return;
    }
    let cancelled = false;
    mintNircamDownloadToken()
      .then((res) => {
        if (cancelled) return;
        setTokenState(
          res.token && res.expiresAt
            ? {
                token: { token: res.token, expiresAt: new Date(res.expiresAt), shareLink: res.shareLink },
                error: null,
                linkRefused: false,
              }
            : { token: null, error: res.error, linkRefused: res.shareLink },
        );
      })
      .catch((err) => {
        console.error('Failed to mint NIRCam download token:', err);
        if (!cancelled) {
          setTokenState({ token: null, error: 'Failed to prepare the download script.', linkRefused: false });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [isExpanded]);

  // Only build once the panel is open and the token answer is in: a
  // whole-field selection is thousands of lines, and the generation timestamp
  // should be when the user looked.
  const script = useMemo(
    () =>
      isExpanded && tokenState && !tokenState.linkRefused
        ? buildNircamDownloadScript(selectedImages, siteOrigin(), { token: tokenState.token ?? undefined })
        : '',
    [isExpanded, tokenState, selectedImages],
  );
  const preparing = isExpanded && tokenState === null;
  // A share-link visitor, whether or not they got a credential: no account,
  // so no API key and no CLI to point them at.
  const shareLinkSession = tokenState?.linkRefused || tokenState?.token?.shareLink === true;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(script);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy script:', err);
    }
  };

  const handleDownload = () => {
    const blob = new Blob([script], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = NIRCAM_DOWNLOAD_SCRIPT_FILENAME;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  if (selectedImages.length === 0) {
    return null;
  }

  return (
    <div className={`bg-card border border-border rounded-lg ${className}`}>
      {/* Toggle header */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-card-hover transition-colors rounded-lg"
      >
        <div className="flex items-center gap-2">
          {isExpanded ? (
            <ChevronUp className="w-4 h-4 text-text-secondary" />
          ) : (
            <ChevronDown className="w-4 h-4 text-text-secondary" />
          )}
          <span className="text-sm font-medium text-text-primary">
            Bulk download {selectedImages.length} file{selectedImages.length === 1 ? '' : 's'}
          </span>
          <span className="text-sm text-text-secondary">
            ({formatFileSize(totalSize)} total)
          </span>
        </div>
      </button>

      {/* Expanded content */}
      {isExpanded && (
        <div className="border-t border-border">
          {/* What the file is: with a token, a credential for the viewer's
              downloads (say so, and how long it lasts); without one, a script
              that needs an API key, and why the token could not be minted. */}
          {!preparing && tokenState?.token && (
            <div className="px-4 pt-4">
              <div className="flex items-start gap-2 bg-background border border-border rounded-lg p-3">
                <KeyRound className="w-4 h-4 text-text-secondary mt-0.5 shrink-0" />
                {tokenState.token.shareLink ? (
                  <p className="text-sm text-text-secondary">
                    Run with <code className="font-mono text-xs">bash {NIRCAM_DOWNLOAD_SCRIPT_FILENAME}</code>;
                    re-run it to resume after a failure. It carries a download token for this
                    shared link, good until {tokenState.token.expiresAt.toLocaleDateString()} or
                    until the link is revoked.
                  </p>
                ) : (
                  <p className="text-sm text-text-secondary">
                    Run with <code className="font-mono text-xs">bash {NIRCAM_DOWNLOAD_SCRIPT_FILENAME}</code>;
                    re-run it to resume after a failure. It carries a download token for your
                    account, good until {tokenState.token.expiresAt.toLocaleDateString()}. An{' '}
                    <Link href={API_KEYS_PATH} className="text-primary hover:underline">
                      API key
                    </Link>{' '}
                    in <code className="font-mono text-xs">CAMPFIRE_API_KEY</code> overrides it.
                  </p>
                )}
              </div>
            </div>
          )}
          {!preparing && !tokenState?.token && (
            <div className="px-4 pt-4">
              <div className="flex items-start gap-2 rounded-lg p-3 bg-amber-100 dark:bg-amber-900/40 border border-amber-200 dark:border-amber-800/50">
                <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0 text-amber-800 dark:text-amber-300" />
                {tokenState?.linkRefused ? (
                  <p className="text-sm text-amber-900 dark:text-amber-200">
                    {tokenState.error} You can browse and plot everything on this page; ask whoever
                    shared the link if you need the files themselves.
                  </p>
                ) : (
                  <p className="text-sm text-amber-900 dark:text-amber-200">
                    {tokenState?.error ?? 'Could not prepare a download credential.'} The script
                    below needs an{' '}
                    <Link href={API_KEYS_PATH} className="text-primary hover:underline">
                      API key
                    </Link>{' '}
                    in <code className="font-mono text-xs">CAMPFIRE_API_KEY</code> (it prompts for
                    one otherwise).
                  </p>
                )}
              </div>
            </div>
          )}

          {/* Script preview */}
          {!tokenState?.linkRefused && (
          <div className="p-4">
            <div className="bg-gray-900 rounded-lg overflow-hidden">
              <div className="flex items-center justify-between px-4 py-2 bg-gray-800 border-b border-gray-700">
                <span className="text-sm text-gray-400 font-mono">{NIRCAM_DOWNLOAD_SCRIPT_FILENAME}</span>
                <div className="flex items-center gap-2">
                  {/* Plain buttons: the code panel is always dark, so the
                      theme-aware Button ghost variant is unreadable here. */}
                  <button
                    onClick={handleCopy}
                    disabled={preparing || !script}
                    className="inline-flex items-center rounded-md px-2.5 py-1.5 text-sm font-medium text-gray-300 hover:text-white hover:bg-gray-700 transition-colors disabled:opacity-50 disabled:pointer-events-none"
                  >
                    {copied ? (
                      <>
                        <Check className="w-4 h-4 mr-1.5" />
                        Copied
                      </>
                    ) : (
                      <>
                        <Copy className="w-4 h-4 mr-1.5" />
                        Copy
                      </>
                    )}
                  </button>
                  <button
                    onClick={handleDownload}
                    disabled={preparing || !script}
                    className="inline-flex items-center rounded-md px-2.5 py-1.5 text-sm font-medium text-gray-300 hover:text-white hover:bg-gray-700 transition-colors disabled:opacity-50 disabled:pointer-events-none"
                  >
                    <Download className="w-4 h-4 mr-1.5" />
                    Download
                  </button>
                </div>
              </div>
              <pre className="p-4 text-sm text-gray-300 font-mono overflow-x-auto max-h-96 overflow-y-auto">
                <code>{preparing ? 'Preparing download script…' : script}</code>
              </pre>
            </div>
          </div>
          )}

          {/* Programmatic access pointer. Not for a share-link visitor: the
              CLI and the Python client both want an account's API key. */}
          {!shareLinkSession && (
          <div className="px-4 pb-4">
            <div className="flex items-start gap-2 bg-background border border-border rounded-lg p-3">
              <Info className="w-4 h-4 text-text-secondary mt-0.5 shrink-0" />
              <p className="text-sm text-text-secondary">
                Regularly bulk-downloading CAMPFIRE data? See{' '}
                <Link href="/docs/api" className="text-primary hover:underline">
                  programmatic access
                </Link>{' '}
                for the CLI (<code className="font-mono text-xs">campfire pull --field</code>) and Python client.
              </p>
            </div>
          </div>
          )}
        </div>
      )}
    </div>
  );
};
