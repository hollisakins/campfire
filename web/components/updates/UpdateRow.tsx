import Link from 'next/link';
import { ArrowUpRight } from 'lucide-react';
import type { UpdateEntry } from '@/lib/updates/types';
import { formatUpdateDate } from '@/lib/updates/format';
import { CategoryChip } from './CategoryChip';

/** Compact, single-entry row used in the landing-page feed. */
export function UpdateRow({ entry }: { entry: UpdateEntry }) {
  return (
    <div className="py-4">
      <div className="flex items-center gap-3 mb-1">
        <time
          className="font-mono text-xs text-text-tertiary tabular-nums"
          dateTime={entry.date}
        >
          {formatUpdateDate(entry.date)}
        </time>
        <CategoryChip category={entry.category} />
      </div>

      <h3 className="text-base font-semibold text-text-primary">{entry.title}</h3>

      {entry.summary && (
        <p className="mt-1 text-sm text-text-secondary line-clamp-2">
          {entry.summary}
        </p>
      )}

      {entry.links.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
          {entry.links.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="inline-flex items-center gap-0.5 text-sm text-primary hover:underline"
            >
              {link.label}
              <ArrowUpRight className="w-3 h-3" />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
