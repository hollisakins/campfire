import { useState } from 'react';
import { InlineMultiFilter } from 'campfire-web';

const gratings = [
  { value: 'prism', label: 'PRISM' },
  { value: 'g140m', label: 'G140M' },
  { value: 'g235m', label: 'G235M' },
  { value: 'g395m', label: 'G395M' },
];

export function Default() {
  const [selected, setSelected] = useState<(string | number)[]>(['prism', 'g235m']);
  const [mode, setMode] = useState<'any' | 'all' | 'none'>('any');
  return (
    <div style={{ maxWidth: 360 }}>
      <InlineMultiFilter
        label="Grating"
        options={gratings}
        selected={selected}
        onChange={setSelected}
        mode={mode}
        onModeChange={setMode}
      />
    </div>
  );
}
