'use client';

import { Lock, Globe, Users, Shield } from 'lucide-react';

interface ListBadgeProps {
  visibility: 'private' | 'public_read' | 'public_edit';
  isSystem?: boolean;
  size?: 'sm' | 'md';
}

const visibilityConfig = {
  private: { label: 'Private', icon: Lock, bg: 'bg-slate-100 dark:bg-slate-700', text: 'text-slate-600 dark:text-slate-300' },
  public_read: { label: 'Public', icon: Globe, bg: 'bg-blue-50 dark:bg-blue-950', text: 'text-blue-700 dark:text-blue-300' },
  public_edit: { label: 'Collaborative', icon: Users, bg: 'bg-emerald-50 dark:bg-emerald-950', text: 'text-emerald-700 dark:text-emerald-300' },
};

export function ListBadge({ visibility, isSystem, size = 'sm' }: ListBadgeProps) {
  const config = visibilityConfig[visibility];
  const Icon = config.icon;
  const sizeClasses = size === 'sm' ? 'text-[10px] px-1.5 py-0.5' : 'text-xs px-2 py-0.5';
  const iconSize = size === 'sm' ? 'w-2.5 h-2.5' : 'w-3 h-3';

  return (
    <span className="inline-flex items-center gap-1">
      <span className={`inline-flex items-center gap-1 rounded-full font-medium ${sizeClasses} ${config.bg} ${config.text}`}>
        <Icon className={iconSize} />
        {config.label}
      </span>
      {isSystem && (
        <span className={`inline-flex items-center gap-1 rounded-full font-medium ${sizeClasses} bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300`}>
          <Shield className={iconSize} />
          System
        </span>
      )}
    </span>
  );
}
