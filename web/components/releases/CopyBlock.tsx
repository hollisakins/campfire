'use client';

import { useState } from 'react';
import { Check, Copy } from 'lucide-react';

/** A monospace block with a copy button (citations, CLI snippets). */
export function CopyBlock({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard refused (insecure context); the text stays selectable
    }
  };

  return (
    <div className="relative rounded-lg border border-border bg-background">
      <button
        type="button"
        onClick={copy}
        aria-label={`Copy ${label}`}
        className="absolute right-2 top-2 inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-1 text-xs text-text-secondary hover:text-text-primary"
      >
        {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
        {copied ? 'Copied' : 'Copy'}
      </button>
      <pre className="overflow-x-auto whitespace-pre-wrap break-words p-3 pr-20 font-mono text-xs text-text-primary">
        {text}
      </pre>
    </div>
  );
}
