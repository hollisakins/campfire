import { HomeContent } from '@/components/home/HomeContent';
import { UpdatesSection } from '@/components/updates/UpdatesSection';
import { getVisibleUpdates } from '@/lib/updates/visibility';

// Rendering mode is decided by getVisibleUpdates: static when no updates are
// program-gated, dynamic (per-viewer) once a gated update is published.

const LANDING_UPDATES_LIMIT = 5;

export default async function Home() {
  const all = await getVisibleUpdates();
  const entries = all.slice(0, LANDING_UPDATES_LIMIT);

  return (
    <div className="container mx-auto px-4 py-12">
      <div className="max-w-4xl mx-auto">
        <HomeContent />
        <UpdatesSection entries={entries} total={all.length} />
      </div>
    </div>
  );
}
