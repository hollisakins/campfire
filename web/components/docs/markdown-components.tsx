// The docs' react-markdown element map, shared by the server renderer
// (MarkdownServer — the /docs route, prerendered) and the client renderer
// (MarkdownRenderer — pages that assemble markdown at runtime). No React
// hooks here: everything interactive is a client component (CodeBlock's copy
// button, DocImage's lightbox), which a server tree may reference freely.

import React from 'react';
import type { Components } from 'react-markdown';
import { CodeBlock } from './CodeBlock';
import { DocImage } from './DocImage';
import { headingToId } from '@/lib/docs/toc';

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

function anchoredHeading(Tag: 'h2' | 'h3' | 'h4', className: string) {
  const Heading = ({ children }: { children?: React.ReactNode }) => {
    const text = extractTextFromChildren(children);
    const id = headingToId(text);
    return (
      <Tag id={id} className={`${className} scroll-mt-4 group`}>
        <a href={`#${id}`} className="no-underline hover:underline">
          {children}
        </a>
        <span className="opacity-0 group-hover:opacity-100 ml-2 text-text-secondary">#</span>
      </Tag>
    );
  };
  Heading.displayName = `Anchored${Tag.toUpperCase()}`;
  return Heading;
}

export const markdownComponents: Components = {
  // Headings with anchor links
  h1: ({ children }) => (
    <h1 className="text-3xl font-bold text-text-primary mt-8 mb-4 first:mt-0">
      {children}
    </h1>
  ),
  h2: anchoredHeading('h2', 'text-2xl font-semibold text-text-primary mt-8 mb-4'),
  h3: anchoredHeading('h3', 'text-xl font-semibold text-text-primary mt-6 mb-3'),
  h4: anchoredHeading('h4', 'text-lg font-semibold text-text-primary mt-4 mb-2'),

  // Paragraphs
  p: ({ children }) => (
    <p className="text-text-primary leading-7 mb-4">
      {children}
    </p>
  ),

  // Lists
  ul: ({ children }) => (
    <ul className="list-disc list-outside ml-6 mb-4 space-y-2 text-text-primary">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="list-decimal list-outside ml-6 mb-4 space-y-2 text-text-primary">
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
    <blockquote className="border-l-4 border-primary pl-4 italic text-text-secondary mb-4">
      {children}
    </blockquote>
  ),

  // Tables
  table: ({ children }) => (
    <div className="overflow-x-auto mb-4">
      <table className="min-w-full border border-border rounded-lg overflow-hidden">
        {children}
      </table>
    </div>
  ),
  thead: ({ children }) => (
    <thead className="bg-card">{children}</thead>
  ),
  tbody: ({ children }) => (
    <tbody className="divide-y divide-border">{children}</tbody>
  ),
  tr: ({ children }) => (
    <tr className="hover:bg-card-hover">{children}</tr>
  ),
  th: ({ children }) => (
    <th className="px-4 py-2 text-left font-semibold text-text-primary">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="px-4 py-2 text-text-primary">{children}</td>
  ),

  // Horizontal rule
  hr: () => (
    <hr className="border-border my-8" />
  ),

  // Strong and emphasis
  strong: ({ children }) => (
    <strong className="font-semibold text-text-primary">{children}</strong>
  ),
  em: ({ children }) => (
    <em className="italic">{children}</em>
  ),

  // Images (for screenshots) - clickable to open lightbox
  img: ({ src, alt }) => (
    <DocImage src={typeof src === 'string' ? src : undefined} alt={alt} />
  ),
};
