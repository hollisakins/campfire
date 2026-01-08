'use client';

import React, { useState } from 'react';
import { Link, Check } from 'lucide-react';

interface CopyLinkButtonProps {
  objectId: string;
}

export const CopyLinkButton: React.FC<CopyLinkButtonProps> = ({ objectId }) => {
  const [copied, setCopied] = useState(false);

  const copyToClipboard = async () => {
    try {
      const url = `${window.location.origin}/spectra/${encodeURIComponent(objectId)}`;
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
      className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-text-primary bg-card border border-border rounded-lg hover:bg-gray-50 transition-colors"
      title="Copy link to this object"
    >
      {copied ? (
        <>
          <Check className="w-4 h-4 text-green-600" />
          <span className="text-green-600">Copied!</span>
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
