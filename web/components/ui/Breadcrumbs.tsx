'use client';

import React from 'react';
import Link from 'next/link';
import { ChevronRight } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

interface BreadcrumbItem {
  label: string;
  href?: string;
}

interface BreadcrumbsProps {
  items: BreadcrumbItem[];
  className?: string;
}

export const Breadcrumbs: React.FC<BreadcrumbsProps> = ({ items, className = '' }) => {
  // Share-link visitors get no breadcrumbs (docs/design-public-mirror.md §7):
  // every ancestor crumb points outside the shared scope, so the trail reads as
  // an invitation to explore a site that renders empty for them.
  const { isLinkAccount } = useAuth();
  if (isLinkAccount) return null;

  return (
    <nav className={`flex items-center space-x-2 text-sm ${className}`}>
      {items.map((item, index) => {
        const isLast = index === items.length - 1;

        return (
          <React.Fragment key={index}>
            {item.href && !isLast ? (
              <Link
                href={item.href}
                className="text-text-secondary hover:text-primary transition-colors"
              >
                {item.label}
              </Link>
            ) : (
              <span className={isLast ? 'text-text-primary font-medium' : 'text-text-secondary'}>
                {item.label}
              </span>
            )}
            {!isLast && (
              <ChevronRight className="w-4 h-4 text-text-secondary dark:text-text-tertiary" />
            )}
          </React.Fragment>
        );
      })}
    </nav>
  );
};
