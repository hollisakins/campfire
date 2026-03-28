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

  // Reset error state when URL or object changes
  useEffect(() => {
    console.log('[RGBImage] Props updated - targetId:', targetId, 'URL:', rgbImageUrl?.substring(0, 80) + '...');
    setImageError(false);
  }, [rgbImageUrl, targetId]);

  // Show placeholder if no URL provided or if image failed to load
  if (!rgbImageUrl || imageError) {
    return (
      <div
        className="bg-gray-200 dark:bg-slate-700 rounded-lg flex items-center justify-center"
        style={{ width: size, height: size }}
      >
        <p className="text-gray-500 dark:text-slate-400 text-sm text-center px-4">
          No RGB image available
        </p>
      </div>
    );
  }

  return (
    <div
      className="relative rounded-lg overflow-hidden border border-gray-300 dark:border-slate-600"
      style={{ width: size, height: size }}
    >
      <Image
        src={rgbImageUrl}
        alt={`RGB image for ${targetId}`}
        fill
        className="object-cover"
        onError={() => {
          console.log('[RGBImage] Image load error for:', targetId);
          setImageError(true);
        }}
        onLoad={() => {
          console.log('[RGBImage] Image loaded successfully for:', targetId);
        }}
        unoptimized // For R2 signed URLs
      />
    </div>
  );
};
