import { useState } from 'react';
import { ChipSelect } from 'campfire-web';

const gratings = [
  { value: 'prism', label: 'PRISM', color: '#c63f0c' },
  { value: 'g140m', label: 'G140M', color: '#1d4ed8' },
  { value: 'g235m', label: 'G235M', color: '#4d7c0f' },
  { value: 'g395m', label: 'G395M', color: '#b45309' },
];

export function Selectable() {
  const [selected, setSelected] = useState<(string | number)[]>(['prism', 'g395m']);
  return (
    <div style={{ maxWidth: 460 }}>
      <ChipSelect options={gratings} selected={selected} onChange={setSelected} />
    </div>
  );
}

export function Disabled() {
  return (
    <div style={{ maxWidth: 460 }}>
      <ChipSelect options={gratings} selected={['g140m']} onChange={() => {}} disabled />
    </div>
  );
}
