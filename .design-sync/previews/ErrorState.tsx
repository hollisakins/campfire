import { ErrorState } from 'campfire-web';

export function Default() {
  return (
    <div style={{ maxWidth: 560 }}>
      <ErrorState message="Failed to load spectra. The reduction service returned a 503 — please retry." />
    </div>
  );
}
