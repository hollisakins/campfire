import { useState } from 'react';
import { CoordinateSearchChip } from 'campfire-web';

type CoordValue = { ra: number; dec: number; radius: number; radius_unit: 'degrees' | 'arcmin' | 'arcsec' };

// Closed chip trigger; with a value set it shows the active cone-search summary.
export function Active() {
  const [value, setValue] = useState<CoordValue | null>({
    ra: 150.1191, dec: 2.2058, radius: 1, radius_unit: 'arcmin',
  });
  return (
    <div style={{ paddingBottom: 320 }}>
      <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>▾ Click the chip to open the coordinate + radius cone-search editor.</div>
      <CoordinateSearchChip value={value} onChange={setValue} />
    </div>
  );
}

export function Empty() {
  const [value, setValue] = useState<CoordValue | null>(null);
  return (
    <div style={{ paddingBottom: 320 }}>
      <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>▾ Click the chip to open the coordinate + radius cone-search editor.</div>
      <CoordinateSearchChip value={value} onChange={setValue} />
    </div>
  );
}
