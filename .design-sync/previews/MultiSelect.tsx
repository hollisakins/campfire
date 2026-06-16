import { useState } from 'react';
import { MultiSelect } from 'campfire-web';

const programs = [
  { id: '1180', name: 'CEERS' },
  { id: '6585', name: 'CAPERS' },
  { id: '1210', name: 'JADES Deep' },
  { id: '2750', name: 'UNCOVER' },
  { id: '3215', name: 'JADES Origins' },
];

export function Default() {
  const [selected, setSelected] = useState<string[]>(['6585', '1210']);
  return (
    <div style={{ maxWidth: 360 }}>
      <MultiSelect label="Programs" options={programs} selected={selected} onChange={setSelected} />
    </div>
  );
}
