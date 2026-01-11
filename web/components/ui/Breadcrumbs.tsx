import React from 'react';
import Link from 'next/link';
import { ChevronRight } from 'lucide-react';

interface BreadcrumbItem {
  label: string;
  href?: string;
}

interface BreadcrumbsProps {
  items: BreadcrumbItem[];
  className?: string;
}

export const Breadcrumbs: React.FC<BreadcrumbsProps> = ({ items, className = '' }) => {
  return (
    <nav className={`flex items-center space-x-2 text-sm ${className}`}>
      {items.map((item, index) => {
        const isLast = index === items.length - 1;

        return (
          <React.Fragment key={index}>
            {item.href && !isLast ? (
              <Link
                href={item.href}
                className="text-text-secondary dark:text-slate-400 hover:text-primary transition-colors"
              >
                {item.label}
              </Link>
            ) : (
              <span className={isLast ? 'text-text-primary dark:text-slate-100 font-medium' : 'text-text-secondary dark:text-slate-400'}>
                {item.label}
              </span>
            )}
            {!isLast && (
              <ChevronRight className="w-4 h-4 text-text-secondary dark:text-slate-500" />
            )}
          </React.Fragment>
        );
      })}
    </nav>
  );
};
