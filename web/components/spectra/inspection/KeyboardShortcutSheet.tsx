'use client';

import React, { useEffect, useRef } from 'react';

interface KeyboardShortcutSheetProps {
  onClose: () => void;
}

const SHORTCUTS = [
  { key: '1 2 3 4', action: 'Set redshift quality' },
  { key: '→ or N', action: 'Save & next object' },
  { key: '← or P', action: 'Save & previous object' },
  { key: 'S', action: 'Save without advancing' },
  { key: 'Z', action: 'Focus redshift input' },
  { key: 'G', action: 'Cycle grating' },
  { key: 'Escape', action: 'Exit inspection mode' },
  { key: '?', action: 'Toggle this help' },
];

export const KeyboardShortcutSheet: React.FC<KeyboardShortcutSheetProps> = ({ onClose }) => {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-[210] flex items-center justify-center bg-black/40">
      <div
        ref={panelRef}
        className="bg-background dark:bg-card border border-border dark:border-border-strong rounded-lg shadow-xl p-5 w-80"
      >
        <h3 className="text-sm font-semibold text-text-primary mb-3">
          Keyboard Shortcuts
        </h3>
        <table className="w-full text-sm">
          <tbody>
            {SHORTCUTS.map((s) => (
              <tr key={s.key} className="border-b border-border/50 last:border-0">
                <td className="py-1.5 pr-3">
                  <kbd className="font-mono text-xs px-1.5 py-0.5 rounded bg-card dark:bg-card-hover border border-border dark:border-border-strong text-text-primary">
                    {s.key}
                  </kbd>
                </td>
                <td className="py-1.5 text-text-secondary">
                  {s.action}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
