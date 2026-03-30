'use client';

import React, { useState } from 'react';
import { Link, Check } from 'lucide-react';

interface CopyLinkButtonProps {
  targetId: string;
  url?: string;  // Override the generated URL path
}

export const CopyLinkButton: React.FC<CopyLinkButtonProps> = ({ targetId, url: urlOverride }) => {
  const [copied, setCopied] = useState(false);

  const copyToClipboard = async () => {
    try {
      const url = urlOverride
        ? `${window.location.origin}${urlOverride}`
        : `${window.location.origin}/spectra/${encodeURIComponent(targetId)}`;
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  return (
    <button
      onClick={copyToClipboard}
      className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-text-primary dark:text-slate-100 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg hover:bg-gray-50 dark:hover:bg-slate-700 transition-colors"
      title="Copy link to this object"
    >
      {copied ? (
        <>
          <Check className="w-4 h-4 text-green-600 dark:text-green-400" />
          <span className="text-green-600 dark:text-green-400">Copied!</span>
        </>
      ) : (
        <>
          <Link className="w-4 h-4" />
          <span>Copy Link</span>
        </>
      )}
    </button>
  );
};
