'use client';

import React from 'react';
import Link from 'next/link';
import { Map } from 'lucide-react';

interface ShowOnMapLinkProps {
  ra: number;
  dec: number;
  field: string;
  objectId: string;
}

export function ShowOnMapLink({ ra, dec, field, objectId }: ShowOnMapLinkProps) {
  const mapUrl = `/map?field=${encodeURIComponent(field)}&ra=${ra}&dec=${dec}&zoom=8&highlight=${encodeURIComponent(objectId)}`;

  return (
    <Link
      href={mapUrl}
      className="inline-flex items-center gap-1 text-sm text-primary hover:text-primary-hover transition-colors"
      title="Show this object on the image map"
    >
      <Map className="w-4 h-4" />
      <span>Show on Map</span>
    </Link>
  );
}
