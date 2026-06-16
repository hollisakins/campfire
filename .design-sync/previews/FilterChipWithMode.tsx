import { useState } from 'react';
import { FilterChipWithMode } from 'campfire-web';

const features = [
  { value: 'lya', label: 'Lyα' },
  { value: 'oiii', label: '[O III]' },
  { value: 'ha', label: 'Hα' },
  { value: 'civ', label: 'C IV' },
];

// Closed chip trigger showing label, mode, and selection count.
export function Default() {
  const [selected, setSelected] = useState<(string | number)[]>(['oiii', 'ha']);
  const [mode, setMode] = useState<'any' | 'all' | 'none'>('all');
  return (
    <div style={{ paddingBottom: 260 }}>
      <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>▾ Click the chip to open the options + Any/All/None mode dropdown.</div>
      <FilterChipWithMode
        label="Emission lines"
        options={features}
        selected={selected}
        onChange={setSelected}
        mode={mode}
        onModeChange={setMode}
      />
    </div>
  );
}
