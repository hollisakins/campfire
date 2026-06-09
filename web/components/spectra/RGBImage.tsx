'use client';

import React, { useState, useEffect } from 'react';
import Image from 'next/image';

interface RGBImageProps {
  targetId: string;
  rgbImageUrl: string | null;
  size?: number;
}

export const RGBImage: React.FC<RGBImageProps> = ({
  targetId,
  rgbImageUrl,
  size = 350
}) => {
  const [imageError, setImageError] = useState(false);

  useEffect(() => {
    setImageError(false);
  }, [rgbImageUrl, targetId]);

  // Show placeholder if no URL provided or if image failed to load
  if (!rgbImageUrl || imageError) {
    return (
      <div
        className="bg-surface-2 rounded-lg flex items-center justify-center"
        style={{ width: size, height: size }}
      >
        <p className="text-text-secondary text-sm text-center px-4">
          No RGB image available
        </p>
      </div>
    );
  }

  return (
    <div
      className="relative rounded-lg overflow-hidden border border-border-strong"
      style={{ width: size, height: size }}
    >
      <Image
        src={rgbImageUrl}
        alt={`RGB image for ${targetId}`}
        fill
        className="object-cover"
        onError={() => setImageError(true)}
        unoptimized // For R2 signed URLs
      />
    </div>
  );
};
