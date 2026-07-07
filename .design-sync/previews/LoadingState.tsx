import { LoadingState } from 'campfire-web';

export function Default() {
  return (
    <div style={{ maxWidth: 560 }}>
      <LoadingState />
    </div>
  );
}

export function WithLabel() {
  return (
    <div style={{ maxWidth: 560 }}>
      <LoadingState label="Querying spectra…" />
    </div>
  );
}
