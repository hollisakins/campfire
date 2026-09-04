'use client';

import React from 'react';
import Link from 'next/link';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import type { DocPage } from '@/lib/docs/config';

// Only the link fields: DocPage carries a Lucide `icon` component, which a
// server page cannot hand to this client component (functions don't cross
// the RSC boundary), so callers pass { slug, title }.
type DocLink = Pick<DocPage, 'slug' | 'title'>;

interface DocNavigationProps {
  prev?: DocLink;
  next?: DocLink;
}

export default function DocNavigation({ prev, next }: DocNavigationProps) {
  if (!prev && !next) {
    return null;
  }

  return (
    <nav className="flex justify-between items-center mt-12 pt-8 border-t border-border">
      {prev ? (
        <Link
          href={`/docs/${prev.slug}`}
          className="group flex items-center gap-2 text-text-secondary hover:text-primary transition-colors"
        >
          <ChevronLeft className="w-4 h-4 group-hover:-translate-x-1 transition-transform" />
          <div className="text-right">
            <div className="text-xs uppercase tracking-wide mb-0.5">Previous</div>
            <div className="font-medium text-text-primary group-hover:text-primary">
              {prev.title}
            </div>
          </div>
        </Link>
      ) : (
        <div />
      )}
      {next ? (
        <Link
          href={`/docs/${next.slug}`}
          className="group flex items-center gap-2 text-text-secondary hover:text-primary transition-colors text-right"
        >
          <div>
            <div className="text-xs uppercase tracking-wide mb-0.5">Next</div>
            <div className="font-medium text-text-primary group-hover:text-primary">
              {next.title}
            </div>
          </div>
          <ChevronRight className="w-4 h-4 group-hover:translate-x-1 transition-transform" />
        </Link>
      ) : (
        <div />
      )}
    </nav>
  );
}
