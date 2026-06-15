import Link from 'next/link';
import type { Metadata } from 'next';
import { Megaphone } from 'lucide-react';
import { getAllUpdates } from '@/lib/updates/loader';
import { CURRENT_VERSIONS } from '@/lib/updates/versions';
import { formatUpdateDate } from '@/lib/updates/format';
import { CategoryChip } from '@/components/updates/CategoryChip';
import { MarkdownRenderer } from '@/components/docs';

// Read from the filesystem at build time; render statically.
export const dynamic = 'force-static';

export const metadata: Metadata = {
  title: 'Updates — CAMPFIRE',
  description:
    'New observations, pipeline re-reductions, and CLI / client changes for the CAMPFIRE archive.',
};

export default function UpdatesPage() {
  const updates = getAllUpdates();

  return (
    <div className="container mx-auto px-4 py-12">
      <div className="max-w-3xl mx-auto">
        {/* Header */}
        <div className="flex items-center gap-2 mb-2">
          <Megaphone className="w-7 h-7 text-primary" />
          <h1 className="text-3xl font-bold text-text-primary">Updates</h1>
        </div>
        <p className="text-text-secondary mb-8">
          Announcements of new observations, pipeline improvements, and CLI /
          Python client changes.
        </p>

        {/* Current versions */}
        <div className="rounded-card border border-border bg-card p-5 mb-10">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-text-tertiary mb-3">
            Current versions
          </h2>
          <dl className="grid grid-cols-1 sm:grid-cols-3 gap-4 text-sm">
            <div>
              <dt className="text-text-secondary">Pipeline</dt>
              <dd className="font-mono text-text-primary">{CURRENT_VERSIONS.pipeline}</dd>
            </div>
            <div>
              <dt className="text-text-secondary">CLI / Python client</dt>
              <dd className="font-mono text-text-primary">{CURRENT_VERSIONS.client}</dd>
            </div>
            <div>
              <dt className="text-text-secondary">Data release</dt>
              <dd className="text-text-primary">{CURRENT_VERSIONS.dataRelease}</dd>
            </div>
          </dl>
        </div>

        {/* Entries */}
        {updates.length === 0 ? (
          <p className="text-text-secondary">No updates yet.</p>
        ) : (
          <div className="space-y-12">
            {updates.map((entry) => (
              <article key={entry.slug} id={entry.slug} className="scroll-mt-24">
                <div className="flex items-center gap-3 mb-2">
                  <time
                    className="font-mono text-xs text-text-tertiary tabular-nums"
                    dateTime={entry.date}
                  >
                    {formatUpdateDate(entry.date)}
                  </time>
                  <CategoryChip category={entry.category} />
                  <a
                    href={`#${entry.slug}`}
                    className="ml-auto text-text-tertiary hover:text-primary"
                    aria-label="Link to this update"
                  >
                    #
                  </a>
                </div>

                <h2 className="text-xl font-bold text-text-primary mb-3">
                  {entry.title}
                </h2>

                <div className="[&_p]:leading-6 [&_li]:leading-6">
                  <MarkdownRenderer content={entry.body} />
                </div>

                {entry.links.length > 0 && (
                  <div className="mt-4 flex flex-wrap gap-x-5 gap-y-1">
                    {entry.links.map((link) => (
                      <Link
                        key={link.href}
                        href={link.href}
                        className="text-sm text-primary hover:underline"
                      >
                        {link.label} →
                      </Link>
                    ))}
                  </div>
                )}
              </article>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
