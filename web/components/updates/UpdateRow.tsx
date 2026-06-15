import Link from 'next/link';
import { Lock } from 'lucide-react';
import type { UpdateEntry } from '@/lib/updates/types';
import { formatUpdateDate } from '@/lib/updates/format';
import { CategoryChip } from './CategoryChip';

/** Compact, single-line landing-feed row: title (linking to the entry's anchor
 *  on /updates) and category chip on the left, date pushed to the right. */
export function UpdateRow({ entry }: { entry: UpdateEntry }) {
  return (
    <div className="flex items-center gap-3">
      <Link
        href={`/updates#${entry.slug}`}
        className="min-w-0 truncate text-base font-semibold text-text-primary hover:text-primary transition-colors"
      >
        {entry.title}
      </Link>
      <span className="shrink-0">
        <CategoryChip category={entry.category} />
      </span>
      {entry.programs.length > 0 && (
        <span
          className="shrink-0"
          title={`Restricted to program ${entry.programs.join(', ')}`}
        >
          <Lock className="w-3 h-3 text-text-tertiary" />
        </span>
      )}
      <time
        className="ml-auto shrink-0 whitespace-nowrap font-mono text-xs text-text-tertiary tabular-nums"
        dateTime={entry.date}
      >
        {formatUpdateDate(entry.date)}
      </time>
    </div>
  );
}
