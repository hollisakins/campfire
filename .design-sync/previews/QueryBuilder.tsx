import { useState } from 'react';
import { QueryBuilder } from 'campfire-web';

const fields = [
  { id: 'redshift', label: 'Redshift', type: 'number' as const },
  { id: 'grating', label: 'Grating', type: 'multiselect' as const, options: [
    { value: 'prism', label: 'PRISM' },
    { value: 'g140m', label: 'G140M' },
    { value: 'g395m', label: 'G395M' },
  ] },
  { id: 'program', label: 'Program', type: 'select' as const, options: [
    { value: '6585', label: 'CAPERS' },
    { value: '1180', label: 'CEERS' },
  ] },
];

export function Default() {
  const [value, setValue] = useState({
    id: 'root',
    logic: 'AND' as const,
    conditions: [
      { id: 'c1', field: 'redshift', operator: 'gte' as const, value: 3 },
      { id: 'c2', field: 'grating', operator: 'in' as const, value: ['prism', 'g395m'] },
    ],
  });
  return (
    <div style={{ maxWidth: 720 }}>
      <QueryBuilder fields={fields} value={value} onChange={setValue} />
    </div>
  );
}
