'use client';

import React, { useState, ReactNode, ReactElement, isValidElement } from 'react';

interface CodeBlockProps {
  children: ReactNode;
}

// Recursively extract text content from React children
function extractTextContent(node: ReactNode): string {
  if (typeof node === 'string') return node;
  if (typeof node === 'number') return String(node);
  if (!node) return '';

  if (Array.isArray(node)) {
    return node.map(extractTextContent).join('');
  }

  if (isValidElement(node)) {
    const props = node.props as { children?: ReactNode };
    return extractTextContent(props.children);
  }

  return '';
}

// Extract language from className (e.g., "hljs language-python" -> "python")
function extractLanguage(className: string): string {
  const match = className.match(/language-(\w+)/);
  return match ? match[1] : '';
}

export function CodeBlock({ children }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  // Get the code element (first child of pre)
  const codeElement = React.Children.toArray(children)[0] as ReactElement<{ className?: string }>;
  const className = codeElement?.props?.className || '';
  const language = extractLanguage(className);

  // Extract text content for copying
  const codeText = extractTextContent(children);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(codeText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  return (
    <div className="relative mb-4 rounded-lg border border-border dark:border-slate-700 overflow-hidden">
      {/* Header with language label and copy button */}
      <div className="flex justify-between items-center px-3 py-1.5 bg-slate-100 dark:bg-slate-800/50 border-b border-border dark:border-slate-700">
        <span className="text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wide">
          {language || 'code'}
        </span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1 text-xs text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
          aria-label="Copy code"
        >
          {copied ? (
            <>
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
              <span>Copied!</span>
            </>
          ) : (
            <>
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
              </svg>
              <span>Copy</span>
            </>
          )}
        </button>
      </div>

      {/* Code content */}
      <pre className="bg-[var(--code-bg)] text-[var(--code-text)] p-3 overflow-x-auto text-sm">
        {children}
      </pre>
    </div>
  );
}
