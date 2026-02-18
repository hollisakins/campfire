'use client';

import { useState, useEffect } from 'react';
import Link from 'next/link';

const STORAGE_KEY = 'campfire-map-return-url';

export function ReturnToMapButton() {
  const [mapUrl, setMapUrl] = useState<string | null>(null);

  useEffect(() => {
    setMapUrl(sessionStorage.getItem(STORAGE_KEY));
  }, []);

  if (!mapUrl) return null;

  return (
    <Link
      href={mapUrl}
      onClick={() => sessionStorage.removeItem(STORAGE_KEY)}
      className="text-sm text-primary hover:text-primary-hover flex items-center gap-1"
      title="Return to your previous map view"
    >
      ← Return to Map
    </Link>
  );
}
