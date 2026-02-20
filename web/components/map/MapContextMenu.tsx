'use client';

import React, { useEffect, useRef, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { formatCoordinates } from '@/lib/utils/coordinates';

interface MapContextMenuProps {
  coords: { ra: number; dec: number };
  position: { x: number; y: number };
  onClose: () => void;
}

export function MapContextMenu({ coords, position, onClose }: MapContextMenuProps) {
  const router = useRouter();
  const menuRef = useRef<HTMLDivElement>(null);
  const [copiedItem, setCopiedItem] = useState<string | null>(null);
  const [menuPos, setMenuPos] = useState(position);

  const formatted = formatCoordinates(coords.ra, coords.dec);

  // Adjust position to stay within viewport
  useEffect(() => {
    const menu = menuRef.current;
    if (!menu) return;

    const rect = menu.getBoundingClientRect();
    const parent = menu.parentElement?.getBoundingClientRect();
    if (!parent) return;

    let { x, y } = position;

    if (x + rect.width > parent.width) {
      x = Math.max(0, x - rect.width);
    }
    if (y + rect.height > parent.height) {
      y = Math.max(0, y - rect.height);
    }

    setMenuPos({ x, y });
  }, [position]);

  // Close on Escape
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose]);

  // Close on click outside
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose();
      }
    }
    // Use timeout so the opening right-click doesn't immediately close us
    const id = setTimeout(() => {
      window.addEventListener('mousedown', handleClick);
    }, 0);
    return () => {
      clearTimeout(id);
      window.removeEventListener('mousedown', handleClick);
    };
  }, [onClose]);

  const copyToClipboard = useCallback(async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedItem(label);
      setTimeout(() => {
        setCopiedItem(null);
        onClose();
      }, 600);
    } catch {
      onClose();
    }
  }, [onClose]);

  const handleSearchSpectra = useCallback(() => {
    const params = new URLSearchParams({
      coord_ra: coords.ra.toFixed(6),
      coord_dec: coords.dec.toFixed(6),
      coord_radius: '5',
      coord_unit: 'arcsec',
    });
    router.push(`/spectra?${params.toString()}`);
    onClose();
  }, [coords, router, onClose]);

  return (
    <div
      ref={menuRef}
      className="absolute z-[1001] min-w-[220px] bg-black/85 backdrop-blur-sm text-white rounded-lg shadow-xl border border-white/10 py-1 text-sm select-none"
      style={{ left: menuPos.x, top: menuPos.y }}
    >
      {/* Coordinate copy group */}
      <MenuItem
        label={copiedItem === 'decimal' ? 'Copied!' : 'Copy coordinates (decimal)'}
        detail={formatted.decimal.combined}
        onClick={() => copyToClipboard(formatted.decimal.combined, 'decimal')}
      />
      <MenuItem
        label={copiedItem === 'sexagesimal' ? 'Copied!' : 'Copy coordinates (sexagesimal)'}
        detail={formatted.sexagesimal.combined}
        onClick={() => copyToClipboard(formatted.sexagesimal.combined, 'sexagesimal')}
      />

      <div className="my-1 border-t border-white/10" />

      {/* Link copy */}
      <MenuItem
        label={copiedItem === 'link' ? 'Copied!' : 'Copy link to this view'}
        onClick={() => copyToClipboard(window.location.href, 'link')}
      />

      <div className="my-1 border-t border-white/10" />

      {/* Navigate */}
      <MenuItem
        label="Search spectra near here"
        onClick={handleSearchSpectra}
      />
    </div>
  );
}

function MenuItem({
  label,
  detail,
  onClick,
}: {
  label: string;
  detail?: string;
  onClick: () => void;
}) {
  return (
    <button
      className="w-full text-left px-3 py-1.5 hover:bg-white/15 transition-colors cursor-pointer flex flex-col gap-0.5"
      onClick={onClick}
    >
      <span>{label}</span>
      {detail && (
        <span className="text-xs text-gray-400 font-mono">{detail}</span>
      )}
    </button>
  );
}
