import type { Metadata } from 'next';
import Link from 'next/link';
import { Archive } from 'lucide-react';
import type { RequestPrincipal } from '@/lib/auth/identity';
import { SignInLink } from '@/components/auth/SignInLink';
import { ReleaseStateBadge } from '@/components/releases/ReleaseStateBadge';
import { parseReleaseComponents, releaseCount } from '@/lib/dr-releases';
import { listReleases } from '@/lib/server/dr-releases';
import { formatUpdateDate } from '@/lib/updates/format';
import { releasePagesPrincipal } from './gate';

export const dynamic = 'force-dynamic';

export const metadata: Metadata = {
  title: 'Data releases — CAMPFIRE',
  description: 'Frozen, citable snapshots of the CAMPFIRE catalog and its products.',
};

export default async function ReleasesPage() {
  const principal = await releasePagesPrincipal();

  return (
    <div className="container mx-auto px-4 py-12">
      <div className="max-w-3xl mx-auto">
        <div className="flex items-center gap-2 mb-2">
          <Archive className="w-7 h-7 text-primary" />
          <h1 className="text-3xl font-bold text-text-primary">Data releases</h1>
        </div>
        <p className="text-text-secondary mb-8">
          A data release is a frozen snapshot of the catalog: every published spectrum, object,
          line fit and photometry match at one moment, with the exact files archived by content
          hash. The live catalog keeps changing; a release never does, so it is what a paper
          should cite.
        </p>

        {principal ? <ReleaseList principal={principal} /> : <SignInPrompt />}
      </div>
    </div>
  );
}

function SignInPrompt() {
  return (
    <div className="rounded-card border border-border bg-card p-5 text-text-secondary">
      <SignInLink className="text-primary hover:underline">Sign in</SignInLink> to see the data
      releases.
    </div>
  );
}

async function ReleaseList({ principal }: { principal: RequestPrincipal }) {
  const releases = await listReleases(principal.supabase);
  const isAdmin = principal.access.isAdmin;

  if (releases.length === 0) {
    return (
      <div className="rounded-card border border-border bg-card p-5 text-text-secondary">
        No data releases yet.
        {isAdmin && (
          <span className="block mt-2 text-xs text-text-tertiary">
            Admin: freeze one with <code className="font-mono">campfire dr freeze --tag &lt;TAG&gt; --nirspec</code>.
          </span>
        )}
      </div>
    );
  }

  return (
    <>
      {isAdmin && releases.some((r) => r.state !== 'published') && (
        <p className="mb-4 text-xs text-text-tertiary">
          Unpublished releases are listed because you are an admin; readers see published ones only.
        </p>
      )}
      <ul className="space-y-4">
        {releases.map((r) => {
          const spectra = releaseCount(r.counts, 'spectra');
          const objects = releaseCount(r.counts, 'objects');
          const when = r.published_at ?? r.frozen_at;
          return (
            <li key={r.tag}>
              <Link
                href={`/releases/${encodeURIComponent(r.tag)}`}
                className="block rounded-card border border-border bg-card p-5 transition-colors hover:bg-card-hover"
              >
                <div className="flex flex-wrap items-center gap-3">
                  <h2 className="font-mono text-lg font-semibold text-text-primary">{r.tag}</h2>
                  <ReleaseStateBadge state={r.state} />
                  {when && (
                    <span className="ml-auto text-sm text-text-tertiary">
                      {r.published_at ? 'Published' : 'Frozen'} {formatUpdateDate(when)}
                    </span>
                  )}
                </div>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-text-secondary">
                  {spectra != null && <span>{spectra.toLocaleString()} spectra</span>}
                  {objects != null && <span>{objects.toLocaleString()} objects</span>}
                  <span>{parseReleaseComponents(r.components).map((c) => c.label).join(' · ')}</span>
                  {r.doi && <span className="font-mono">doi:{r.doi}</span>}
                </div>
                {r.notes && <p className="mt-2 text-sm text-text-tertiary line-clamp-2">{r.notes}</p>}
              </Link>
            </li>
          );
        })}
      </ul>
    </>
  );
}
