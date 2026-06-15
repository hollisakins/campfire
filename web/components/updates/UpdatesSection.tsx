import Link from 'next/link';
import { ArrowRight, Megaphone } from 'lucide-react';
import type { UpdateEntry } from '@/lib/updates/types';
import { UpdateRow } from './UpdateRow';

interface UpdatesSectionProps {
  entries: UpdateEntry[];
  /** Total number of updates that exist (≥ entries.length). */
  total: number;
}

/** Landing-page "Updates" feed: the most recent entries, with older rows
 *  fading out and a link to the full /updates page when more exist. */
export function UpdatesSection({ entries, total }: UpdatesSectionProps) {
  if (entries.length === 0) return null;

  const hasMore = total > entries.length;

  return (
    <section className="mb-8" aria-labelledby="updates-heading">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Megaphone className="w-5 h-5 text-primary" />
          <h2 id="updates-heading" className="text-xl font-bold text-text-primary">
            Updates
          </h2>
        </div>
        <Link
          href="/updates"
          className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
        >
          View all
          <ArrowRight className="w-4 h-4" />
        </Link>
      </div>

      <div className="divide-y divide-border border-t border-border">
        {entries.map((entry, i) => {
          // When more updates exist beyond the visible set, fade older (lower)
          // rows to hint at additional history behind the "view all" link.
          const opacity = hasMore ? Math.max(0.55, 1 - i * 0.12) : 1;
          return (
            <div key={entry.slug} style={{ opacity }}>
              <UpdateRow entry={entry} />
            </div>
          );
        })}
      </div>

      {hasMore && (
        <div className="pt-3 text-center">
          <Link href="/updates" className="text-sm text-primary hover:underline">
            View all {total} updates →
          </Link>
        </div>
      )}
    </section>
  );
}
