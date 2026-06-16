import { useState } from 'react';
import { RangeFilterChip } from 'campfire-web';

// Closed chip trigger showing the active range (opens an editor on click).
export function Active() {
  const [range, setRange] = useState<[number | null, number | null]>([4, 9]);
  return (
    <div style={{ paddingBottom: 240 }}>
      <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>▾ Click the chip to open the min/max range editor.</div>
      <RangeFilterChip
        label="Redshift"
        min={range[0]}
        max={range[1]}
        onChange={(min, max) => setRange([min, max])}
        minBound={0}
        maxBound={15}
        step={0.1}
        precision={2}
        quickRanges={[
          { label: 'z < 3', min: null, max: 3 },
          { label: '3–6', min: 3, max: 6 },
          { label: 'z > 6', min: 6, max: null },
        ]}
      />
    </div>
  );
}

export function Empty() {
  const [range, setRange] = useState<[number | null, number | null]>([null, null]);
  return (
    <div style={{ paddingBottom: 240 }}>
      <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>▾ Click the chip to open the min/max range editor.</div>
      <RangeFilterChip
        label="Redshift"
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
