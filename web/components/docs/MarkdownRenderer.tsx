'use client';

import React, { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import type { Components } from 'react-markdown';
import { CodeBlock } from './CodeBlock';

export interface TOCItem {
  id: string;
  text: string;
  level: number;
}

interface MarkdownRendererProps {
  content: string;
  onTOCChange?: (toc: TOCItem[]) => void;
}

// Extract headings from markdown for TOC
function extractHeadings(markdown: string): TOCItem[] {
  const headingRegex = /^(#{2,4})\s+(.+)$/gm;
  const headings: TOCItem[] = [];
  let match;

  while ((match = headingRegex.exec(markdown)) !== null) {
    const level = match[1].length;
    const text = match[2].trim();
    const id = text
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, '')
      .replace(/\s+/g, '-');

    headings.push({ id, text, level });
  }

  return headings;
}

// Generate id from heading text
function headingToId(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .replace(/\s+/g, '-');
}

// Recursively extract text content from React children
function extractTextFromChildren(children: React.ReactNode): string {
  if (typeof children === 'string') return children;
  if (typeof children === 'number') return String(children);
  if (!children) return '';

  if (Array.isArray(children)) {
    return children.map(extractTextFromChildren).join('');
  }

  if (React.isValidElement(children)) {
    const props = children.props as { children?: React.ReactNode };
    return extractTextFromChildren(props.children);
  }

  return '';
}

export default function MarkdownRenderer({ content, onTOCChange }: MarkdownRendererProps) {
  const [lightbox, setLightbox] = useState<{ src: string; alt: string } | null>(null);

  useEffect(() => {
    if (onTOCChange) {
      const headings = extractHeadings(content);
      onTOCChange(headings);
    }
  }, [content, onTOCChange]);

  // ESC key handler for lightbox
  useEffect(() => {
    if (!lightbox) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setLightbox(null);
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [lightbox]);

  const components: Components = {
    // Headings with anchor links
    h1: ({ children }) => (
      <h1 className="text-3xl font-bold text-text-primary dark:text-slate-100 mt-8 mb-4 first:mt-0">
        {children}
      </h1>
    ),
    h2: ({ children }) => {
      const text = extractTextFromChildren(children);
      const id = headingToId(text);
      return (
        <h2 id={id} className="text-2xl font-semibold text-text-primary dark:text-slate-100 mt-8 mb-4 scroll-mt-4 group">
          <a href={`#${id}`} className="no-underline hover:underline">
            {children}
          </a>
          <span className="opacity-0 group-hover:opacity-100 ml-2 text-text-secondary">#</span>
        </h2>
      );
    },
    h3: ({ children }) => {
      const text = extractTextFromChildren(children);
      const id = headingToId(text);
      return (
        <h3 id={id} className="text-xl font-semibold text-text-primary dark:text-slate-100 mt-6 mb-3 scroll-mt-4 group">
          <a href={`#${id}`} className="no-underline hover:underline">
            {children}
          </a>
          <span className="opacity-0 group-hover:opacity-100 ml-2 text-text-secondary">#</span>
        </h3>
      );
    },
    h4: ({ children }) => {
      const text = extractTextFromChildren(children);
      const id = headingToId(text);
      return (
        <h4 id={id} className="text-lg font-semibold text-text-primary dark:text-slate-100 mt-4 mb-2 scroll-mt-4 group">
          <a href={`#${id}`} className="no-underline hover:underline">
            {children}
          </a>
          <span className="opacity-0 group-hover:opacity-100 ml-2 text-text-secondary">#</span>
        </h4>
      );
    },

    // Paragraphs
    p: ({ children }) => (
      <p className="text-text-primary dark:text-slate-300 leading-7 mb-4">
        {children}
      </p>
    ),

    // Lists
    ul: ({ children }) => (
      <ul className="list-disc list-outside ml-6 mb-4 space-y-2 text-text-primary dark:text-slate-300">
        {children}
      </ul>
    ),
    ol: ({ children }) => (
      <ol className="list-decimal list-outside ml-6 mb-4 space-y-2 text-text-primary dark:text-slate-300">
        {children}
      </ol>
    ),
    li: ({ children }) => (
      <li className="leading-7">{children}</li>
    ),

    // Links
    a: ({ href, children }) => (
      <a
        href={href}
        className="text-primary hover:underline"
        target={href?.startsWith('http') ? '_blank' : undefined}
        rel={href?.startsWith('http') ? 'noopener noreferrer' : undefined}
      >
        {children}
      </a>
    ),

    // Code
    code: ({ className, children }) => {
      const isInline = !className;
      if (isInline) {
        return (
          <code className="bg-[var(--code-bg)] px-1.5 py-0.5 rounded text-sm font-mono text-[var(--code-text)]">
            {children}
          </code>
        );
      }
      return (
        <code className={className}>
          {children}
        </code>
      );
    },
    pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,

    // Blockquotes
    blockquote: ({ children }) => (
      <blockquote className="border-l-4 border-primary pl-4 italic text-text-secondary dark:text-slate-400 mb-4">
        {children}
      </blockquote>
    ),

    // Tables
    table: ({ children }) => (
      <div className="overflow-x-auto mb-4">
        <table className="min-w-full border border-border dark:border-slate-700 rounded-lg overflow-hidden">
          {children}
        </table>
      </div>
    ),
    thead: ({ children }) => (
      <thead className="bg-card dark:bg-slate-800">{children}</thead>
    ),
    tbody: ({ children }) => (
      <tbody className="divide-y divide-border dark:divide-slate-700">{children}</tbody>
    ),
    tr: ({ children }) => (
      <tr className="hover:bg-card-hover dark:hover:bg-slate-800/50">{children}</tr>
    ),
    th: ({ children }) => (
      <th className="px-4 py-2 text-left font-semibold text-text-primary dark:text-slate-200">
        {children}
      </th>
    ),
    td: ({ children }) => (
      <td className="px-4 py-2 text-text-primary dark:text-slate-300">{children}</td>
    ),

    // Horizontal rule
    hr: () => (
      <hr className="border-border dark:border-slate-700 my-8" />
    ),

    // Strong and emphasis
    strong: ({ children }) => (
      <strong className="font-semibold text-text-primary dark:text-slate-100">{children}</strong>
    ),
    em: ({ children }) => (
      <em className="italic">{children}</em>
    ),

    // Images (for screenshots) - clickable to open lightbox
    img: ({ src, alt }) => (
      <figure className="my-6">
        <img
          src={typeof src === 'string' ? src : undefined}
          alt={alt || ''}
          className="rounded-lg border border-border dark:border-slate-700 shadow-sm max-w-full cursor-pointer hover:opacity-90 transition-opacity"
          onClick={() => typeof src === 'string' && setLightbox({ src, alt: alt || '' })}
        />
        {alt && (
          <figcaption className="mt-2 text-center text-sm text-text-secondary dark:text-slate-400 italic">
            {alt}
          </figcaption>
        )}
      </figure>
    ),
  };

  return (
    <>
      <div className="prose-campfire">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[rehypeHighlight]}
          components={components}
        >
          {content}
        </ReactMarkdown>
      </div>

      {/* Image Lightbox */}
      {lightbox && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Image preview"
          className="fixed inset-0 z-50 flex items-center justify-center p-4 animate-fade-in"
          onClick={() => setLightbox(null)}
        >
          {/* Overlay */}
          <div className="absolute inset-0 bg-black/80 backdrop-blur-sm" />

          {/* Content */}
          <div
            className="relative z-10 flex flex-col items-center animate-zoom-in"
            onClick={(e) => e.stopPropagation()}
          >
            <img
              src={lightbox.src}
              alt={lightbox.alt}
              className="max-w-[90vw] max-h-[85vh] object-contain rounded-lg shadow-2xl"
            />
            {lightbox.alt && (
              <p className="mt-4 text-white/90 text-center text-sm max-w-2xl">
                {lightbox.alt}
              </p>
            )}
            <button
              onClick={() => setLightbox(null)}
              className="absolute -top-2 -right-2 w-8 h-8 flex items-center justify-center rounded-full bg-white/10 hover:bg-white/20 text-white transition-colors"
              aria-label="Close preview"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      )}
    </>
  );
}
