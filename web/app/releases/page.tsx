import type { Metadata } from 'next';
import Link from 'next/link';
import { Archive } from 'lucide-react';
import type { RequestPrincipal } from '@/lib/auth/identity';
import { SignInLink } from '@/components/auth/SignInLink';
import { formatBytes, releaseSummary } from '@/lib/dr-releases';
import { listReleases } from '@/lib/server/dr-releases';
import { formatUpdateDate } from '@/lib/updates/format';
import { releasePagesPrincipal } from './gate';

export const dynamic = 'force-dynamic';

export const metadata: Metadata = {
  title: 'Data releases — CAMPFIRE',
  description: 'Stable, citable archives of the CAMPFIRE database.',
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
        <p className="text-text-secondary mb-8">Stable, citable archives of the full CAMPFIRE database.</p>

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
  // Published only, for admins too: the admin view is /admin/releases.
  const releases = await listReleases(principal.supabase, { publishedOnly: true });

  if (releases.length === 0) {
    return (
      <div className="rounded-card border border-border bg-card p-5 text-text-secondary">
        No data releases yet.
      </div>
    );
  }

  return (
    <ul className="space-y-4">
      {releases.map((r) => {
        const s = releaseSummary(r.components, r.counts);
        const parts: string[] = [];
        if (s.nirspec) {
          parts.push(`NIRSpec · ${s.nirspec.spectra?.toLocaleString() ?? '—'} spectra`);
        }
        if (s.nircam) {
          const n = s.nircam.fields.length;
          parts.push(`NIRCam · ${n} ${n === 1 ? 'field' : 'fields'}`);
        }
        if (s.photometry) parts.push('Photometry');
        const bytes = (s.nirspec?.bytes ?? 0) + (s.nircam?.bytes ?? 0);
        return (
          <li key={r.tag}>
            <Link
              href={`/releases/${encodeURIComponent(r.tag)}`}
              className="block rounded-card border border-border bg-card p-5 transition-colors hover:bg-card-hover"
            >
              <div className="flex flex-wrap items-baseline gap-3">
                <h2 className="font-mono text-lg font-semibold text-text-primary">{r.tag}</h2>
                {r.published_at && (
                  <span className="ml-auto text-sm text-text-tertiary">{formatUpdateDate(r.published_at)}</span>
                )}
              </div>
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-text-secondary">
                {parts.map((p) => <span key={p}>{p}</span>)}
                {bytes > 0 && <span>{formatBytes(bytes)}</span>}
                {r.doi && <span className="font-mono">doi:{r.doi}</span>}
              </div>
              {r.notes && <p className="mt-2 text-sm text-text-tertiary line-clamp-2">{r.notes}</p>}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
