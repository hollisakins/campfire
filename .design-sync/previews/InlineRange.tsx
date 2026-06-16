import { useState } from 'react';
import { InlineRange } from 'campfire-web';

export function Default() {
  const [range, setRange] = useState<[number | null, number | null]>([3, 7]);
  return (
    <div style={{ maxWidth: 360 }}>
      <InlineRange
        label="Redshift"
        description="Spectroscopic redshift range"
        min={range[0]}
        max={range[1]}
        onChange={(min, max) => setRange([min, max])}
        minBound={0}
        maxBound={15}
        step={0.1}
        precision={2}
      />
    </div>
  );
}
