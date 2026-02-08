'use client';

import React from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Maximize2 } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

interface EnterInspectionModeButtonProps {
  filterStr: string;
}

export const EnterInspectionModeButton: React.FC<EnterInspectionModeButtonProps> = ({ filterStr }) => {
  const { user, userProfile } = useAuth();
  const pathname = usePathname();
  const canInspect = user && userProfile?.can_comment;

  if (!canInspect) return null;

  const params = new URLSearchParams(filterStr);
  params.set('mode', 'inspect');
  const href = `${pathname}?${params.toString()}`;

  return (
    <div className="mb-4">
      <Link
        href={href}
        className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg bg-primary hover:bg-primary-hover text-white transition-colors"
      >
        <Maximize2 className="w-4 h-4" />
        Enter Inspection Mode
      </Link>
      <span className="ml-3 text-xs text-text-secondary dark:text-slate-400">
        Fullscreen keyboard-driven inspection
      </span>
    </div>
  );
};
