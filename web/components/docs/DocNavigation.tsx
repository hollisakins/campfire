'use client';

import React from 'react';
import Link from 'next/link';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import type { DocPage } from '@/lib/docs/config';

interface DocNavigationProps {
  prev?: DocPage;
  next?: DocPage;
}

export default function DocNavigation({ prev, next }: DocNavigationProps) {
  if (!prev && !next) {
    return null;
  }

  return (
    <nav className="flex justify-between items-center mt-12 pt-8 border-t border-border dark:border-slate-700">
      {prev ? (
        <Link
          href={`/docs/${prev.slug}`}
          className="group flex items-center gap-2 text-text-secondary dark:text-slate-400 hover:text-primary transition-colors"
        >
          <ChevronLeft className="w-4 h-4 group-hover:-translate-x-1 transition-transform" />
          <div className="text-right">
            <div className="text-xs uppercase tracking-wide mb-0.5">Previous</div>
            <div className="font-medium text-text-primary dark:text-slate-200 group-hover:text-primary">
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
          className="group flex items-center gap-2 text-text-secondary dark:text-slate-400 hover:text-primary transition-colors text-right"
        >
          <div>
            <div className="text-xs uppercase tracking-wide mb-0.5">Next</div>
            <div className="font-medium text-text-primary dark:text-slate-200 group-hover:text-primary">
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
