'use client';

import React, { useState } from 'react';
import { Pin, PinOff } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';
import { usePreferences } from '@/lib/contexts/PreferencesContext';
import { MAX_PINNED_OBJECTS, type PinnedObject } from '@/lib/types';

interface PinButtonProps {
  pin: Omit<PinnedObject, 'pinned_at'>;
}

/**
 * Pin toggle rendered in the leading column of the NIRSpec table.
 * Hidden until the row is hovered (via the `group/row` class on the <tr>),
 * except when the object is already pinned — then it stays visible so pinned
 * rows are recognizable at a glance. Subscribes to PreferencesContext
 * directly, so it re-renders on pin changes even inside memoized rows.
 *
 * Hidden entirely for group accounts: PATCH /api/profile rejects them, so
 * pins would apply optimistically but silently vanish on reload.
 */
export const PinButton: React.FC<PinButtonProps> = ({ pin }) => {
  const { userProfile } = useAuth();
  const { pinnedObjects, pinObject, unpinObject } = usePreferences();
  const [hovered, setHovered] = useState(false);

  if (userProfile?.is_group_account) return null;

  const isPinned = pinnedObjects.some(p => p.target_id === pin.target_id);
  const atCap = !isPinned && pinnedObjects.length >= MAX_PINNED_OBJECTS;

  const title = isPinned
    ? 'Unpin object'
    : atCap
      ? `Pin limit reached (${MAX_PINNED_OBJECTS}) — unpin an object first`
      : 'Pin object';

  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        if (isPinned) {
          unpinObject(pin.target_id);
        } else if (!atCap) {
          pinObject(pin);
        }
      }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      disabled={atCap}
      title={title}
      aria-label={title}
      aria-pressed={isPinned}
      className={`p-1 rounded-md transition-all duration-150 active:scale-90 ${
        isPinned
          ? 'text-primary opacity-100 hover:bg-surface-2'
          : atCap
            ? 'opacity-0 group-hover/row:opacity-40 text-text-tertiary cursor-not-allowed'
            : 'opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100 text-text-tertiary hover:text-primary hover:bg-surface-2'
      }`}
    >
      {isPinned && hovered ? (
        <PinOff className="w-4 h-4" />
      ) : (
        <Pin className={`w-4 h-4 ${isPinned ? 'fill-current' : ''}`} />
      )}
    </button>
  );
};
