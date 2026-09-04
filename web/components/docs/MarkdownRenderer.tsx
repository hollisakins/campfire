'use client';

import React, { useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { markdownComponents } from './markdown-components';
import { extractHeadings, type TOCItem } from '@/lib/docs/toc';

export type { TOCItem } from '@/lib/docs/toc';

interface MarkdownRendererProps {
  content: string;
  onTOCChange?: (toc: TOCItem[]) => void;
}

/**
 * Client-side markdown renderer for pages that pick their content at
 * runtime (program detail pages). The static /docs route uses
 * MarkdownServer instead, so the parser stays out of that route's bundle.
 */
export default function MarkdownRenderer({ content, onTOCChange }: MarkdownRendererProps) {
  useEffect(() => {
    if (onTOCChange) {
      onTOCChange(extractHeadings(content));
    }
  }, [content, onTOCChange]);

  return (
    <div className="prose-campfire">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={markdownComponents}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
