import type { Metadata } from 'next';
import Link from 'next/link';
import { notFound } from 'next/navigation';
import { ArrowLeft, Archive } from 'lucide-react';
import { SignInLink } from '@/components/auth/SignInLink';
import { Badge } from '@/components/ui/Badge';
import { CopyBlock } from '@/components/releases/CopyBlock';
import { ReleaseStateBadge } from '@/components/releases/ReleaseStateBadge';
import { formatBytes, parseReleaseComponents, releaseCitation, releaseCount } from '@/lib/dr-releases';
import { loadReleaseDetail, type ReleaseDetail } from '@/lib/server/dr-releases';
import { formatUpdateDate } from '@/lib/updates/format';
import { releasePagesPrincipal } from '../gate';

export const dynamic = 'force-dynamic';

const TAG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$/;
const SITE_URL = process.env.NEXT_PUBLIC_APP_URL || 'https://campfire.hollisakins.com';

type Params = { params: Promise<{ tag: string }> };

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { tag } = await params;
  return { title: `${decodeURIComponent(tag)} — CAMPFIRE data release` };
}

export default async function ReleasePage({ params }: Params) {
  const principal = await releasePagesPrincipal();
  const tag = decodeURIComponent((await params).tag);
  if (!TAG_RE.test(tag)) notFound();

  if (!principal) {
    return (
      <Shell>
        <div className="rounded-card border border-border bg-card p-5 text-text-secondary">
          <SignInLink className="text-primary hover:underline">Sign in</SignInLink> to see this
          data release.
        </div>
      </Shell>
    );
  }

  const detail = await loadReleaseDetail(principal, tag);
  if (!detail) notFound();
  return (
    <Shell>
      <ReleaseBody detail={detail} isAdmin={principal.access.isAdmin} />
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="container mx-auto px-4 py-12">
      <div className="max-w-4xl mx-auto">
        <Link href="/releases" className="mb-6 inline-flex items-center gap-1 text-sm text-text-secondary hover:text-text-primary">
          <ArrowLeft className="h-4 w-4" /> Data releases
        </Link>
        {children}
      </div>
    </div>
  );
}

function Section({ title, children, aside }: { title: string; children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <section className="rounded-card border border-border bg-card p-5">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-text-tertiary">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function ReleaseBody({ detail, isAdmin }: { detail: ReleaseDetail; isAdmin: boolean }) {
  const { release: r, programs, files, exported, complete } = detail;
  const counts = r.counts;
  const stats: [string, number | null][] = [
    ['Spectra', releaseCount(counts, 'spectra')],
    ['Objects', releaseCount(counts, 'objects')],
    ['Line fits', releaseCount(counts, 'line_fits')],
    ['Photometry', releaseCount(counts, 'photometry')],
    ['Files', releaseCount(counts, 'manifest')],
  ];
  const cite = releaseCitation(r, SITE_URL);
  const components = parseReleaseComponents(r.components);
  const published = r.state === 'published';

  return (
    <div className="space-y-6">
      <header>
        <div className="flex flex-wrap items-center gap-3">
          <Archive className="h-7 w-7 text-primary" />
          <h1 className="font-mono text-3xl font-bold text-text-primary">{r.tag}</h1>
          <ReleaseStateBadge state={r.state} />
        </div>
        <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm text-text-secondary">
          {r.frozen_at && <Fact label="Frozen" value={formatUpdateDate(r.frozen_at)} />}
          {r.published_at && <Fact label="Published" value={formatUpdateDate(r.published_at)} />}
          {r.withdrawn_at && <Fact label="Withdrawn" value={formatUpdateDate(r.withdrawn_at)} />}
          <Fact label="Format" value={`v${r.dr_format_version}`} />
          {r.doi && <Fact label="DOI" value={r.doi} mono />}
        </dl>
        {!published && isAdmin && (
          <p className="mt-3 rounded-lg border border-border bg-surface-2 px-3 py-2 text-xs text-text-secondary">
            Not published: only admins can see this release. It goes public with{' '}
            <code className="font-mono">campfire dr publish {r.tag}</code> once it is ready.
          </p>
        )}
        {r.notes && <p className="mt-4 text-text-secondary">{r.notes}</p>}
        {r.known_issues && (
          <p className="mt-3 text-sm text-warning">
            <span className="font-semibold">Known issues:</span> {r.known_issues}
          </p>
        )}
      </header>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {stats.map(([label, value]) => (
          <Badge key={label} label={label} value={value == null ? '—' : value.toLocaleString()} compact />
        ))}
      </div>

      <Section title="Contents">
        <ul className="flex flex-wrap gap-2">
          {components.map((c) => (
            <li key={c.raw} className="rounded-full border border-border px-3 py-1 text-sm text-text-secondary">
              {c.label}
            </li>
          ))}
        </ul>
        <p className="mt-3 text-xs text-text-tertiary">
          The published spectra, objects, line fits and photometry as they stood at the freeze;
          drafts and revoked spectra are not included. Every file is copied once into the
          content-addressed archive, so the bytes behind a release outlive later re-reductions.
        </p>
      </Section>

      <Section title="By program" aside={<span className="text-xs text-text-tertiary">programs you can access</span>}>
        {programs.length === 0 ? (
          <p className="text-sm text-text-secondary">No spectra from programs you can access.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-text-tertiary">
                <tr>
                  <th className="py-2 pr-4 font-medium">Program</th>
                  <th className="py-2 pr-4 text-right font-medium">Spectra</th>
                  <th className="py-2 text-right font-medium">Objects</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {programs.map((p) => (
                  <tr key={p.slug}>
                    <td className="py-2 pr-4">
                      <span className="font-mono text-text-primary">{p.slug}</span>
                      {p.name && p.name !== p.slug && <span className="ml-2 text-text-tertiary">{p.name}</span>}
                    </td>
                    <td className="py-2 pr-4 text-right tabular-nums text-text-secondary">{p.spectra.toLocaleString()}</td>
                    <td className="py-2 text-right tabular-nums text-text-secondary">{p.objects.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-xs text-text-tertiary">
              An object observed by several programs counts under each.
            </p>
          </div>
        )}
      </Section>

      <Section
        title="Export files"
        aside={exported ? (
          <span className={`text-xs ${complete ? 'text-success' : 'text-warning'}`}>
            {complete ? 'complete' : 'partially built'}
          </span>
        ) : undefined}
      >
        {!exported ? (
          <p className="text-sm text-text-secondary">
            Not exported yet. The per-program tables (gzip JSONL, format v{r.dr_format_version})
            appear here once <code className="font-mono">campfire dr export</code> has run.
          </p>
        ) : files.length === 0 ? (
          <p className="text-sm text-text-secondary">No export files for programs you can access.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-text-tertiary">
                <tr>
                  <th className="py-2 pr-4 font-medium">Stream</th>
                  <th className="py-2 pr-4 font-medium">Scope</th>
                  <th className="py-2 pr-4 text-right font-medium">Rows</th>
                  <th className="py-2 pr-4 text-right font-medium">Size</th>
                  <th className="py-2 font-medium">sha256</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {files.map((f) => (
                  <tr key={`${f.stream}/${f.scope}`}>
                    <td className="py-2 pr-4 text-text-primary">{f.stream}</td>
                    <td className="py-2 pr-4 font-mono text-text-secondary">{f.scope}</td>
                    <td className="py-2 pr-4 text-right tabular-nums text-text-secondary">{f.rows?.toLocaleString() ?? '—'}</td>
                    <td className="py-2 pr-4 text-right tabular-nums text-text-secondary">{formatBytes(f.size)}</td>
                    <td className="py-2 font-mono text-xs text-text-tertiary" title={f.sha256 ?? undefined}>
                      {f.sha256 ? f.sha256.slice(0, 12) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="Get the data">
        <div className="space-y-3 text-sm text-text-secondary">
          <p>
            The files endpoint lists every export file you may read, with a one-hour download url
            and its sha256, size and row count. Authenticate with an API key from{' '}
            <Link href="/profile/api-keys" className="text-primary hover:underline">your profile</Link>.
          </p>
          <CopyBlock
            label="files request"
            text={`curl -H "Authorization: Bearer $CAMPFIRE_API_KEY" \\\n  ${SITE_URL}/api/v1/dr/${r.tag}/files`}
          />
          <p className="text-xs text-text-tertiary">
            Coming next: <code className="font-mono">campfire pull --dr {r.tag}</code> and{' '}
            <code className="font-mono">Campfire(dr=&quot;{r.tag}&quot;)</code> in the Python client, which
            merge each stream into one table and fetch the archived FITS by hash.
          </p>
        </div>
      </Section>

      <Section title="Cite this release">
        <div className="space-y-3">
          <CopyBlock label="citation" text={cite.text} />
          <CopyBlock label="BibTeX" text={cite.bibtex} />
          {!r.doi && (
            <p className="text-xs text-text-tertiary">
              No DOI yet: the release will be deposited with a DOI before it is announced. Until then,
              cite the tag and link.
            </p>
          )}
        </div>
      </Section>
    </div>
  );
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-1.5">
      <dt className="text-text-tertiary">{label}</dt>
      <dd className={mono ? 'font-mono' : undefined}>{value}</dd>
    </div>
  );
}
