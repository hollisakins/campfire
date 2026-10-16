import type { Metadata } from 'next';
import Link from 'next/link';
import { notFound } from 'next/navigation';
import { ArrowLeft, Archive, Camera, Telescope } from 'lucide-react';
import { SignInLink } from '@/components/auth/SignInLink';
import { CopyBlock } from '@/components/releases/CopyBlock';
import { ReleasePageDoc } from '@/components/releases/ReleasePageDoc';
import { formatArea, formatBytes, releaseCitation, releaseSummary } from '@/lib/dr-releases';
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

function InstrumentCard({ icon: Icon, title, stats, foot, href }: {
  icon: React.ElementType;
  title: string;
  stats: { value: string; label: string }[];
  foot?: React.ReactNode;
  /** Browse this part of the release (P6c-1). */
  href?: string;
}) {
  const head = (
    <>
      <div className="mb-4 flex items-center gap-2 text-text-primary">
        <Icon className="h-5 w-5 text-primary" />
        <h2 className="font-semibold">{title}</h2>
      </div>
      <div className="grid grid-cols-2 gap-4">
        {stats.map((s) => <Stat key={s.label} {...s} />)}
      </div>
    </>
  );
  const footer = foot ? <div className="mt-3 flex flex-wrap gap-x-3 text-sm text-text-tertiary">{foot}</div> : null;
  const cls = 'block rounded-card border border-border bg-card p-5';
  // A footer that carries its own links (the NIRCam field list) stays outside
  // the card's link — nested anchors are invalid HTML — so only the header and
  // stats link; a card with a plain footer is one link.
  const nested = Array.isArray(foot);
  if (href && !nested) {
    return <Link href={href} className={`${cls} transition-colors hover:bg-card-hover`}>{head}{footer}</Link>;
  }
  return (
    <div className={cls}>
      {href ? <Link href={href} className="block">{head}</Link> : head}
      {footer}
    </div>
  );
}

const n = (v: number | null) => (v == null ? '—' : v.toLocaleString());
const plural = (v: number | null, one: string, many: string) => (v === 1 ? one : many);

function ReleaseBody({ detail }: { detail: ReleaseDetail }) {
  const { release: r, files, complete, pageAssets } = detail;
  const browse = (href: string) => `${href}${href.includes('?') ? '&' : '?'}dr=${encodeURIComponent(r.tag)}`;
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

      {r.page_md && (
        <ReleasePageDoc tag={r.tag} markdown={r.page_md} assets={new Set(pageAssets.map((a) => a.name))} />
      )}

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
            foot={s.photometry ? `photometry: ${s.photometry.catalogs.map((c) => c.catalog).join(' · ')}` : undefined}
            href={`/nirspec?dr=${encodeURIComponent(r.tag)}`}
          />
        )}
        {s.nircam && (
          <InstrumentCard
            icon={Camera}
            title="NIRCam"
            stats={[
              { value: n(s.nircam.fields.length), label: plural(s.nircam.fields.length, 'field', 'fields') },
              { value: formatArea(s.nircam.area_arcmin2), label: 'sky area' },
              { value: n(s.nircam.mosaics), label: plural(s.nircam.mosaics, 'mosaic', 'mosaics') },
              { value: formatBytes(s.nircam.bytes), label: 'data volume' },
            ]}
            foot={s.nircam.fields.map((f) => (
              <Link key={f.field} href={browse(`/nircam/${encodeURIComponent(f.field)}`)} className="hover:underline">
                {f.field}
              </Link>
            ))}
            href={s.nircam.fields.length === 1
              ? browse(`/nircam/${encodeURIComponent(s.nircam.fields[0].field)}`)
              : browse('/nircam')}
          />
        )}
      </div>

      <Section title="Download">
        {!complete ? (
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
        <details className="mt-4 text-sm">
          <summary className="cursor-pointer text-text-secondary">API</summary>
          <div className="mt-2">
            <CopyBlock label="endpoint" text={`${SITE_URL}/api/v1/dr/${encodeURIComponent(r.tag)}/files`} />
          </div>
        </details>
      </Section>

      <Section title="Cite">
        <div className="space-y-3">
          <CopyBlock label="citation" text={cite.text} />
          <CopyBlock label="BibTeX" text={cite.bibtex} />
        </div>
      </Section>
    </div>
  );
}
