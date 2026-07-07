import { useState } from 'react';
import { ColumnVisibilityDropdown } from 'campfire-web';

const columns = [
  { id: 'target_id', label: 'Target ID', alwaysVisible: true },
  { id: 'ra', label: 'RA', defaultVisible: true },
  { id: 'dec', label: 'Dec', defaultVisible: true },
  { id: 'redshift', label: 'Redshift', defaultVisible: true },
  { id: 'grating', label: 'Grating', defaultVisible: true },
  { id: 'program', label: 'Program', defaultVisible: false },
];

// Renders the closed "Columns" trigger button (the checklist opens on click).
export function Default() {
  const [visibility, setVisibility] = useState<Record<string, boolean>>({
    target_id: true, ra: true, dec: true, redshift: true, grating: true, program: false,
  });
  return (
    <div style={{ paddingBottom: 280 }}>
      <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>▾ Click to open the column checklist.</div>
      <ColumnVisibilityDropdown columns={columns} visibility={visibility} onChange={setVisibility} />
    </div>
  );
}
