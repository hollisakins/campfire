import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Archive } from 'lucide-react';
import { getRequestPrincipal } from '@/lib/auth/identity';
import { ReleaseStateBadge } from '@/components/releases/ReleaseStateBadge';
import { drPagesMode } from '@/lib/dr-pages';
import { releaseCount, releaseNextStep } from '@/lib/dr-releases';
import { listReleases } from '@/lib/server/dr-releases';
import { formatUpdateDate } from '@/lib/updates/format';

export const dynamic = 'force-dynamic';

// The admin side of data releases (P6a): every state, with the next CLI
// step. The public /releases pages show published releases only, so this is
// where an unpublished rehearsal is found. Server component under the
// client admin layout; it re-checks admin itself.
export default async function AdminReleasesPage() {
  const principal = await getRequestPrincipal();
  if (!principal?.access.isAdmin) notFound();

  const releases = await listReleases(principal.supabase);
  const pagesOn = drPagesMode() !== 'off';

  return (
    <div>
      <div className="mb-6 flex items-center gap-2">
        <Archive className="h-6 w-6 text-primary" />
        <h1 className="text-2xl font-semibold text-text-primary">Data Releases</h1>
      </div>

      {releases.length === 0 ? (
        <div className="rounded-card border border-border bg-card p-5 text-sm text-text-secondary">
          No releases. Freeze one with{' '}
          <code className="font-mono">campfire dr freeze --tag &lt;TAG&gt; --nirspec --field &lt;FIELD&gt;</code>.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-card border border-border bg-card">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-text-tertiary">
              <tr>
                <th className="px-4 py-3 font-medium">Tag</th>
                <th className="px-4 py-3 font-medium">State</th>
                <th className="px-4 py-3 font-medium">Frozen</th>
                <th className="px-4 py-3 font-medium">Published</th>
                <th className="px-4 py-3 text-right font-medium">Spectra</th>
                <th className="px-4 py-3 text-right font-medium">Objects</th>
                <th className="px-4 py-3 text-right font-medium">Files</th>
                <th className="px-4 py-3 font-medium">Next step</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {releases.map((r) => {
                const next = releaseNextStep(r.state, r.tag);
                return (
                  <tr key={r.tag}>
                    <td className="px-4 py-3 font-mono text-text-primary">
                      {pagesOn ? (
                        <Link href={`/releases/${encodeURIComponent(r.tag)}`} className="hover:underline">{r.tag}</Link>
                      ) : r.tag}
                    </td>
                    <td className="px-4 py-3"><ReleaseStateBadge state={r.state} /></td>
                    <td className="px-4 py-3 text-text-secondary">{r.frozen_at ? formatUpdateDate(r.frozen_at) : '—'}</td>
                    <td className="px-4 py-3 text-text-secondary">{r.published_at ? formatUpdateDate(r.published_at) : '—'}</td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-secondary">{releaseCount(r.counts, 'spectra')?.toLocaleString() ?? '—'}</td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-secondary">{releaseCount(r.counts, 'objects')?.toLocaleString() ?? '—'}</td>
                    <td className="px-4 py-3 text-right tabular-nums text-text-secondary">{releaseCount(r.counts, 'manifest')?.toLocaleString() ?? '—'}</td>
                    <td className="px-4 py-3">
                      {next ? <code className="font-mono text-xs text-text-secondary">{next}</code> : <span className="text-text-tertiary">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {!pagesOn && (
        <p className="mt-4 text-xs text-text-tertiary">
          The public pages are off (<code className="font-mono">NEXT_PUBLIC_DR_PAGES</code> unset).
        </p>
      )}
    </div>
  );
}
