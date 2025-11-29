'use client';

import React, { useState } from 'react';
import Image from 'next/image';

interface RGBImageProps {
  objectId: string;
  rgbImageUrl: string | null;
  size?: number;
}

export const RGBImage: React.FC<RGBImageProps> = ({
  objectId,
  rgbImageUrl,
  size = 350
}) => {
  const [imageError, setImageError] = useState(false);

  // Show placeholder if no URL provided or if image failed to load
  if (!rgbImageUrl || imageError) {
    return (
      <div
        className="bg-gray-200 rounded-lg flex items-center justify-center"
        style={{ width: size, height: size }}
      >
        <p className="text-gray-500 text-sm text-center px-4">
          No RGB image available
        </p>
      </div>
    );
  }

  return (
    <div
      className="relative rounded-lg overflow-hidden border border-gray-300"
      style={{ width: size, height: size }}
    >
      <Image
        src={rgbImageUrl}
        alt={`RGB image for ${objectId}`}
        fill
        className="object-cover"
        onError={() => setImageError(true)}
        unoptimized // For R2 signed URLs
      />
    </div>
  );
};
