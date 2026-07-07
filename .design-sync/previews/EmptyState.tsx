import { EmptyState, Button } from 'campfire-web';
import { Search, Telescope } from 'lucide-react';

export function NoResults() {
  return (
    <div style={{ maxWidth: 560 }}>
      <EmptyState
        icon={Search}
        title="No spectra match your filters"
        description="Try widening the redshift range or clearing the grating filter to see more results."
        action={<Button variant="secondary" size="sm">Clear filters</Button>}
      />
    </div>
  );
}

export function Minimal() {
  return (
    <div style={{ maxWidth: 560 }}>
      <EmptyState
        icon={Telescope}
        title="No observations yet"
        description="Observations will appear here once a program is reduced and deployed."
      />
    </div>
  );
}
