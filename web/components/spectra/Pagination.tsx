import React from 'react';
import Link from 'next/link';
import { ChevronLeft, ChevronRight } from 'lucide-react';

interface PaginationProps {
  current: number;
  total: number;
  prevHref?: string;
  nextHref?: string;
  className?: string;
}

export const Pagination: React.FC<PaginationProps> = ({
  current,
  total,
  prevHref,
  nextHref,
  className = '',
}) => {
  return (
    <div className={`flex items-center space-x-4 ${className}`}>
      {prevHref ? (
        <Link
          href={prevHref}
          className="p-2 rounded-lg hover:bg-card transition-colors text-text-primary"
          aria-label="Previous"
        >
          <ChevronLeft className="w-5 h-5" />
        </Link>
      ) : (
        <div className="p-2 text-text-secondary opacity-50">
          <ChevronLeft className="w-5 h-5" />
        </div>
      )}

      <span className="text-sm font-medium text-text-primary">
        {current > 0 && total > 0 ? `${current} of ${total}` : '? of ?'}
      </span>

      {nextHref ? (
        <Link
          href={nextHref}
          className="p-2 rounded-lg hover:bg-card transition-colors text-text-primary"
          aria-label="Next"
        >
          <ChevronRight className="w-5 h-5" />
        </Link>
      ) : (
        <div className="p-2 text-text-secondary opacity-50">
          <ChevronRight className="w-5 h-5" />
        </div>
      )}
    </div>
  );
};
