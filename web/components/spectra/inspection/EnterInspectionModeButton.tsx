'use client';

import React from 'react';
import Link from 'next/link';
import { Maximize2 } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

interface EnterInspectionModeButtonProps {
  targetId: string;
  filterStr: string;
}

export const EnterInspectionModeButton: React.FC<EnterInspectionModeButtonProps> = ({ targetId, filterStr }) => {
  const { user, userProfile } = useAuth();
  const canInspect = user && userProfile?.can_comment;

  if (!canInspect) return null;

  const params = new URLSearchParams(filterStr);
  params.set('start', targetId);
  const href = `/inspect?${params.toString()}`;

  return (
    <div className="mb-4">
      <Link
        href={href}
        className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg bg-primary hover:bg-primary-hover text-white transition-colors"
        title="Streamlined fullscreen view for rapid quality inspection. Auto-filters to uninspected objects and supports keyboard shortcuts for efficient review."
      >
        <Maximize2 className="w-4 h-4" />
        Enter Inspection Mode
      </Link>
    </div>
  );
};
