import React from 'react';
import type { LucideIcon } from 'lucide-react';

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  icon: Icon,
  title,
  description,
  action,
  className = '',
}) => (
  <div
    className={`text-center py-16 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg ${className}`}
  >
    {Icon && (
      <Icon className="w-12 h-12 text-text-secondary dark:text-slate-400 mx-auto mb-4" />
    )}
    <p className="text-text-primary dark:text-slate-100 font-medium">{title}</p>
    {description && (
      <p className="text-text-secondary dark:text-slate-400 mt-2 text-sm max-w-md mx-auto">
        {description}
      </p>
    )}
    {action && <div className="mt-4">{action}</div>}
  </div>
);
