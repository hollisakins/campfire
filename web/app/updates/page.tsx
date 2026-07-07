import type { Metadata } from 'next';
import { Lock, Megaphone } from 'lucide-react';
import { getVisibleUpdates } from '@/lib/updates/visibility';
import { STATUS_BADGES, DATA_RELEASE, GITHUB_REPO_URL } from '@/lib/updates/versions';
import { formatUpdateDate } from '@/lib/updates/format';
import { CategoryChip } from '@/components/updates/CategoryChip';
import { MarkdownRenderer } from '@/components/docs';

// Rendering mode is decided by getVisibleUpdates: static when no updates are
// program-gated, dynamic (per-viewer) once a gated update is published.

export const metadata: Metadata = {
  title: 'Updates — CAMPFIRE',
  description:
    'New observations, pipeline re-reductions, and CLI / client changes for the CAMPFIRE archive.',
};

export default async function UpdatesPage() {
  const updates = await getVisibleUpdates();

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

        {/* Current versions — badges read live from GitHub (see lib/updates/versions). */}
        <div className="rounded-card border border-border bg-card p-5 mb-10">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-text-tertiary">
              Current versions
            </h2>
            <a
              href={GITHUB_REPO_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="shrink-0 text-xs text-primary hover:underline"
            >
              View on GitHub →
            </a>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {STATUS_BADGES.map((badge) => (
              <a
                key={badge.key}
                href={badge.href}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex"
                title={badge.alt}
              >
                {/* eslint-disable-next-line @next/next/no-img-element -- external shields.io badge, not a Next-optimizable asset */}
                <img src={badge.src} alt={badge.alt} className="h-5" />
              </a>
            ))}
            {DATA_RELEASE && (
              <span className="inline-flex items-center rounded-full border border-border bg-card px-2 py-0.5 text-xs font-medium text-text-secondary">
                Data release: {DATA_RELEASE}
              </span>
            )}
          </div>
          <p className="mt-3 text-xs text-text-tertiary">
            These packages are in active development — badges track the latest
            state on GitHub.
          </p>
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
                  {entry.programs.length > 0 && (
                    <span
                      className="inline-flex items-center gap-1 text-xs text-text-tertiary"
                      title={`Restricted to program ${entry.programs.join(', ')}`}
                    >
                      <Lock className="w-3 h-3" />
                      {entry.programs.join(', ')}
                    </span>
                  )}
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
              </article>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
