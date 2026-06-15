import { HomeContent } from '@/components/home/HomeContent';
import { UpdatesSection } from '@/components/updates/UpdatesSection';
import { getRecentUpdates } from '@/lib/updates/loader';

// Updates are read from the filesystem at build time; render statically.
export const dynamic = 'force-static';

const LANDING_UPDATES_LIMIT = 5;

export default function Home() {
  const { entries, total } = getRecentUpdates(LANDING_UPDATES_LIMIT);

  return (
    <div className="container mx-auto px-4 py-12">
      <div className="max-w-4xl mx-auto">
        <HomeContent />
        <UpdatesSection entries={entries} total={total} />
      </div>
    </div>
  );
}
