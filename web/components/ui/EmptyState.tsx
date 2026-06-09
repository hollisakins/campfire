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
    className={`text-center py-16 bg-card border border-border rounded-lg ${className}`}
  >
    {Icon && (
      <Icon className="w-12 h-12 text-text-secondary mx-auto mb-4" />
    )}
    <p className="text-text-primary font-medium">{title}</p>
    {description && (
      <p className="text-text-secondary mt-2 text-sm max-w-md mx-auto">
        {description}
      </p>
    )}
    {action && <div className="mt-4">{action}</div>}
  </div>
);
