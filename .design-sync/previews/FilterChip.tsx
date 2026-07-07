import { useState } from 'react';
import { FilterChip } from 'campfire-web';

const note: React.CSSProperties = { fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 };

const gratings = [
  { value: 'prism', label: 'PRISM' },
  { value: 'g140m', label: 'G140M' },
  { value: 'g235m', label: 'G235M' },
  { value: 'g395m', label: 'G395M' },
];

// Renders the closed chip trigger with its active selection count (the dropdown
// opens on click — interaction-only, not shown statically).
export function MultiSelect() {
  const [selected, setSelected] = useState<(string | number)[]>(['prism', 'g395m']);
  return (
    <div style={{ paddingBottom: 220 }}>
      <div style={note}>▾ Click the chip to open the options dropdown.</div>
      <FilterChip label="Grating" options={gratings} selected={selected} onChange={setSelected} multiSelect />
    </div>
  );
}

export function Empty() {
  const [selected, setSelected] = useState<(string | number)[]>([]);
  return (
    <div style={{ paddingBottom: 220 }}>
      <div style={note}>▾ Click the chip to open the options dropdown.</div>
      <FilterChip label="Grating" options={gratings} selected={selected} onChange={setSelected} multiSelect />
    </div>
  );
}
