import { Badge } from 'campfire-web';

const row: React.CSSProperties = { display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' };

export function StatRow() {
  return (
    <div style={row}>
      <Badge value="1,284" label="Spectra" />
      <Badge value={312} label="Targets" />
      <Badge value="4.8" label="Max z" />
    </div>
  );
}

export function Compact() {
  return (
    <div style={row}>
      <Badge value={42} label="Programs" compact />
      <Badge value="7" label="Fields" compact />
    </div>
  );
}
