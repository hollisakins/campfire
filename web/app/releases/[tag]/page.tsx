import type { Metadata } from 'next';
import Link from 'next/link';
import { notFound } from 'next/navigation';
import { ArrowLeft, Archive, Camera, Telescope, Table2 } from 'lucide-react';
import { SignInLink } from '@/components/auth/SignInLink';
import { CopyBlock } from '@/components/releases/CopyBlock';
import { formatBytes, releaseCitation, releaseSummary } from '@/lib/dr-releases';
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

  // RLS shows an unpublished tag to admins only (they review it here before
  // publishing); everyone else gets the 404.
  const detail = await loadReleaseDetail(principal, tag);
  if (!detail) notFound();
  return (
    <Shell>
      <ReleaseBody detail={detail} />
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

function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div>
      <div className="text-2xl font-bold tabular-nums text-text-primary">{value}</div>
      <div className="text-xs font-medium uppercase tracking-wide text-text-secondary">{label}</div>
    </div>
  );
}

function InstrumentCard({ icon: Icon, title, stats, foot }: {
  icon: React.ElementType;
  title: string;
  stats: { value: string; label: string }[];
  foot?: string;
}) {
  return (
    <div className="rounded-card border border-border bg-card p-5">
      <div className="mb-4 flex items-center gap-2 text-text-primary">
        <Icon className="h-5 w-5 text-primary" />
        <h2 className="font-semibold">{title}</h2>
      </div>
      <div className="grid grid-cols-2 gap-4">
        {stats.map((s) => <Stat key={s.label} {...s} />)}
      </div>
      {foot && <div className="mt-3 text-sm text-text-tertiary">{foot}</div>}
    </div>
  );
}

const n = (v: number | null) => (v == null ? '—' : v.toLocaleString());
const plural = (v: number | null, one: string, many: string) => (v === 1 ? one : many);

function ReleaseBody({ detail }: { detail: ReleaseDetail }) {
  const { release: r, programs, files, exported } = detail;
  const s = releaseSummary(r.components, r.counts);
  const cite = releaseCitation(r, SITE_URL);
  const published = r.state === 'published';

  return (
    <div className="space-y-6">
      <header>
        <div className="flex flex-wrap items-center gap-3">
          <Archive className="h-7 w-7 text-primary" />
          <h1 className="font-mono text-3xl font-bold text-text-primary">{r.tag}</h1>
          {!published && (
            <span className="rounded-full border border-warning/40 bg-warning/10 px-2.5 py-0.5 text-xs font-medium text-warning">
              Preview · not published
            </span>
          )}
        </div>
        <div className="mt-2 flex flex-wrap gap-x-4 text-sm text-text-secondary">
          {r.published_at ? (
            <span>Published {formatUpdateDate(r.published_at)}</span>
          ) : r.frozen_at ? (
            <span>Frozen {formatUpdateDate(r.frozen_at)}</span>
          ) : null}
          {r.withdrawn_at && <span>Withdrawn {formatUpdateDate(r.withdrawn_at)}</span>}
          {r.doi && <span className="font-mono">doi:{r.doi}</span>}
        </div>
        {r.notes && <p className="mt-4 text-text-secondary">{r.notes}</p>}
        {r.known_issues && (
          <p className="mt-3 text-sm text-warning">
            <span className="font-semibold">Known issues:</span> {r.known_issues}
          </p>
        )}
      </header>

      <div className="grid gap-4 sm:grid-cols-2">
        {s.nirspec && (
          <InstrumentCard
            icon={Telescope}
            title="NIRSpec"
            stats={[
              { value: n(s.nirspec.spectra), label: 'spectra' },
              { value: n(s.nirspec.objects), label: 'objects' },
              { value: n(s.nirspec.programs), label: plural(s.nirspec.programs, 'program', 'programs') },
              { value: formatBytes(s.nirspec.bytes), label: 'data volume' },
            ]}
          />
        )}
        {s.nircam && (
          <InstrumentCard
            icon={Camera}
            title="NIRCam"
            stats={[
              { value: n(s.nircam.fields.length), label: plural(s.nircam.fields.length, 'field', 'fields') },
              { value: n(s.nircam.mosaics), label: plural(s.nircam.mosaics, 'mosaic', 'mosaics') },
              { value: formatBytes(s.nircam.bytes), label: 'data volume' },
            ]}
            foot={s.nircam.fields.map((f) => f.field).join(' · ')}
          />
        )}
        {s.photometry && (
          <InstrumentCard
            icon={Table2}
            title="Photometry"
            stats={[{ value: n(s.photometry.rows), label: 'matched objects' }]}
            foot={s.photometry.catalogs.map((c) => `${c.field} · ${c.catalog}`).join(' · ')}
          />
        )}
      </div>

      {(programs.length > 0 || (s.nircam && s.nircam.fields.length > 0)) && (
        <details className="group rounded-card border border-border bg-card">
          <summary className="cursor-pointer select-none px-5 py-3 text-sm font-semibold uppercase tracking-wide text-text-tertiary">
            Breakdown
          </summary>
          <div className="space-y-6 px-5 pb-5">
            {programs.length > 0 && (
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
            )}
            {s.nircam && s.nircam.fields.length > 0 && (
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase tracking-wide text-text-tertiary">
                  <tr>
                    <th className="py-2 pr-4 font-medium">Field</th>
                    <th className="py-2 pr-4 text-right font-medium">Mosaics</th>
                    <th className="py-2 text-right font-medium">Volume</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {s.nircam.fields.map((f) => (
                    <tr key={f.field}>
                      <td className="py-2 pr-4 font-mono text-text-primary">{f.field}</td>
                      <td className="py-2 pr-4 text-right tabular-nums text-text-secondary">{n(f.mosaics)}</td>
                      <td className="py-2 text-right tabular-nums text-text-secondary">{formatBytes(f.bytes)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </details>
      )}

      <Section title="Download" aside={<span className="text-xs text-text-tertiary">schema v{r.dr_format_version}</span>}>
        {!exported ? (
          <p className="text-sm text-text-secondary">Not available yet.</p>
        ) : files.length === 0 ? (
          <p className="text-sm text-text-secondary">No files for your programs.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-text-tertiary">
                <tr>
                  <th className="py-2 pr-4 font-medium">Table</th>
                  <th className="py-2 pr-4 font-medium">Scope</th>
                  <th className="py-2 pr-4 text-right font-medium">Rows</th>
                  <th className="py-2 text-right font-medium">Size</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {files.map((f) => (
                  <tr key={`${f.stream}/${f.scope}`}>
                    <td className="py-2 pr-4 text-text-primary">{f.stream}</td>
                    <td className="py-2 pr-4 font-mono text-text-secondary">{f.scope}</td>
                    <td className="py-2 pr-4 text-right tabular-nums text-text-secondary">{f.rows?.toLocaleString() ?? '—'}</td>
                    <td className="py-2 text-right tabular-nums text-text-secondary">{formatBytes(f.size)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-4 space-y-2">
          <CopyBlock
            label="API"
            text={`curl -H "Authorization: Bearer $CAMPFIRE_API_KEY" \\\n  ${SITE_URL}/api/v1/dr/${r.tag}/files`}
          />
          <p className="text-xs text-text-tertiary">
            API keys: <Link href="/profile/api-keys" className="text-primary hover:underline">your profile</Link>.
            Python client support (<code className="font-mono">campfire pull --dr {r.tag}</code>) coming soon.
          </p>
        </div>
      </Section>

      <Section title="Cite" aside={!r.doi ? <span className="text-xs text-text-tertiary">DOI pending</span> : undefined}>
        <div className="space-y-3">
          <CopyBlock label="citation" text={cite.text} />
          <CopyBlock label="BibTeX" text={cite.bibtex} />
        </div>
      </Section>
    </div>
  );
}
