import Link from 'next/link';
import type { UpdateEntry } from '@/lib/updates/types';
import { formatUpdateDate } from '@/lib/updates/format';
import { CategoryChip } from './CategoryChip';

/** Compact, single-entry row used in the landing-page feed: date, category,
 *  and a title linking to the full entry's anchor on /updates. */
export function UpdateRow({ entry }: { entry: UpdateEntry }) {
  return (
    <div>
      <div className="flex items-center gap-3 mb-1">
        <time
          className="font-mono text-xs text-text-tertiary tabular-nums"
          dateTime={entry.date}
        >
          {formatUpdateDate(entry.date)}
        </time>
        <CategoryChip category={entry.category} />
      </div>

      <Link
        href={`/updates#${entry.slug}`}
        className="text-base font-semibold text-text-primary hover:text-primary transition-colors"
      >
        {entry.title}
      </Link>
    </div>
  );
}
