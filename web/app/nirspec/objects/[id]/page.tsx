import Link from 'next/link';
import { preload } from 'react-dom';
import { HydrationBoundary } from '@tanstack/react-query';
import { SignInLink } from '@/components/auth/SignInLink';
import { notFound } from 'next/navigation';
import { Metadata } from 'next';
import { LogIn } from 'lucide-react';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { ReturnToMapButton } from '@/components/map/ReturnToMapButton';
import { ObjectNavigation } from '@/components/spectra/ObjectNavigation';
import { UnifiedObjectPage } from '@/components/spectra/UnifiedObjectPage';
import { PlotlyPreload } from '@/components/plot/PlotlyPreload';
import { loadObjectHeader, loadObjectMetadata, loadObjectPhotometry } from '@/lib/server/objects';
import { dehydrateSidecarUrls, resolveSpectrumSidecars } from '@/lib/server/spectrum-sidecars';
import { spectrum1dSources } from '@/lib/spectrum-sidecars';
import { getAssetVersions, assetVersionFor } from '@/lib/asset-version';
import { parseSortingFromURL } from '@/lib/utils/url-params';

interface ObjectDetailPageProps {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

/**
 * How many member spectra get a 1-D preload hint from the HTML. The
 * comparison plot fetches every visible spectrum regardless; the cap keeps a
 * many-spectra object from queueing its whole set ahead of the page's own
 * scripts.
 */
const PRELOAD_1D_MAX = 8;

export async function generateMetadata({ params }: ObjectDetailPageProps): Promise<Metadata> {
  const { id } = await params;
  const objectId = decodeURIComponent(id);

  // Shares the page body's row lookup (request-memoized) for a signed-in
  // viewer; a service-role read for everyone else.
  const metadata = await loadObjectMetadata(objectId);

  if (!metadata) {
    return { title: 'Object Not Found - CAMPFIRE' };
  }

  const redshiftText = metadata.redshift !== null
    ? `z = ${Number(metadata.redshift).toFixed(4)}`
    : 'z = unknown';

  return {
    title: `${objectId} - CAMPFIRE`,
    description: `${objectId} | ${redshiftText} | ${metadata.field}`,
    openGraph: {
      title: objectId,
      description: `${redshiftText} | ${metadata.field}`,
      images: [{
        // kind + asset version: see /api/og-image and lib/asset-version.ts (#497)
        url: `/api/og-image/${encodeURIComponent(objectId)}?kind=object&v=${assetVersionFor(await getAssetVersions(), metadata.field)}`,
        width: 300,
        height: 300,
        alt: `RGB thumbnail for ${objectId}`,
      }],
    },
    twitter: {
      card: 'summary',
      title: objectId,
      description: `${redshiftText} | ${metadata.field}`,
    },
  };
}

/**
 * The object page (perf T2-E, #510). The HTML is rendered from the header
 * alone — the object row and its member targets/spectra — and carries what
 * the first plotted trace needs: preload hints for the Plotly chunks and for
 * each member spectrum's 1-D payload, plus the resolved sidecar urls seeded
 * into the client query cache. Photometry streams in behind a Suspense
 * boundary; comments, nearby objects and the SED's P(z) fetch when they
 * scroll into view; the 2-D S/N arrays load only for expanded spectrum
 * cards, after their 1-D trace.
 */
export default async function ObjectDetailPage({ params, searchParams }: ObjectDetailPageProps) {
  const { id } = await params;
  const objectId = decodeURIComponent(id);

  // Preserve filter/sort params for back navigation
  const searchParamsObj = await searchParams;
  const urlParams = new URLSearchParams();
  urlParams.set('view', 'objects');
  Object.entries(searchParamsObj).forEach(([key, value]) => {
    if (value && key !== 'view' && key !== 'tab' && key !== 'grating') {
      urlParams.set(key, Array.isArray(value) ? value.join(',') : value);
    }
  });
  const backHref = `/nirspec?${urlParams.toString()}`;

  // Sorting (for the navigation cache key) and the raw parameter string, which
  // the navigation links carry and GET /api/objects/adjacent parses itself.
  const { sortColumn, sortDirection } = parseSortingFromURL(urlParams, 'objects');
  const filterStr = urlParams.toString();

  const { object, isAuthenticated } = await loadObjectHeader(objectId);

  if (!isAuthenticated) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Objects', href: backHref },
            { label: objectId },
          ]}
          className="mb-6"
        />

        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Sign in to view this object
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
            Access to object details requires authentication.
          </p>
          <SignInLink
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-on-primary rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </SignInLink>
        </div>
      </div>
    );
  }

  if (!object) {
    notFound();
  }

  // Photometry is not awaited: the promise crosses to the client tree and
  // settles behind a Suspense boundary in UnifiedObjectPage. It never
  // rejects (loadObjectPhotometry answers null on any failure).
  const photometry = object.has_photometry ? loadObjectPhotometry(object.id) : Promise.resolve(null);

  // Sidecar urls for every member spectrum: one registry resolution here,
  // seeded into the client cache below, so no /api/spectrum/sidecars round
  // trip stands between hydration and the first 1-D fetch — and the 1-D
  // payloads themselves are preloaded from the HTML, so the browser has
  // them in flight before any script runs. `crossOrigin` matches fetch()'s
  // default mode and credentials (cors, same-origin) so the browser pairs
  // the preload with the client's fetch of the same url; an as=fetch preload
  // without it is a no-cors request nothing matches.
  const fitsPaths = object.member_targets.flatMap(m => m.spectra.map(s => s.fits_path));
  const sidecars = await resolveSpectrumSidecars(fitsPaths);
  for (const fitsPath of fitsPaths.slice(0, PRELOAD_1D_MAX)) {
    const { front, route } = spectrum1dSources(sidecars.get(fitsPath), fitsPath);
    preload(front ?? route, { as: 'fetch', crossOrigin: 'anonymous' });
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <PlotlyPreload />

      {/* Breadcrumbs + Navigation */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-4">
          <Breadcrumbs
            items={[
              { label: 'CAMPFIRE', href: '/' },
              { label: 'Objects', href: backHref },
              { label: object.object_id },
            ]}
          />
          <Link
            href={backHref}
            className="text-sm text-primary hover:text-primary-hover flex items-center gap-1"
          >
            &larr; Back to List
          </Link>
          <ReturnToMapButton />
        </div>
        <ObjectNavigation
          targetId={objectId}
          sortColumn={sortColumn}
          sortDirection={sortDirection}
          filterStr={filterStr}
        />
      </div>

      {/* Header + cutout + sidebar + panel (all client-managed). The
          hydration boundary hands the resolved sidecar urls to every
          spectrum query in the tree. */}
      <HydrationBoundary state={dehydrateSidecarUrls(sidecars)}>
        <UnifiedObjectPage object={object} photometry={photometry} />
      </HydrationBoundary>
    </div>
  );
}
