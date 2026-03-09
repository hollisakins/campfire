import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Metadata } from 'next';
import { LogIn } from 'lucide-react';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { MetricCards } from '@/components/spectra/MetricCards';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { GratingDetails } from '@/components/spectra/GratingDetails';
import { InspectionPanel } from '@/components/spectra/InspectionPanel';
import { SpectrumPlot } from '@/components/spectra/SpectrumPlot';
import { RedshiftFitSummary } from '@/components/spectra/RedshiftFitSummary';
import { RedshiftFitPlot } from '@/components/spectra/RedshiftFitPlot';
import { ObjectNavigation } from '@/components/spectra/ObjectNavigation';
import { DownloadButtons } from '@/components/spectra/DownloadButtons';
import { CopyLinkButton } from '@/components/spectra/CopyLinkButton';
import { CoordinateDisplay } from '@/components/spectra/CoordinateDisplay';
import { ShowOnMapLink } from '@/components/map/ShowOnMapLink';
import { ReturnToMapButton } from '@/components/map/ReturnToMapButton';
import { TileCutoutWrapper } from '@/components/spectra/TileCutoutWrapper';
import { NearbyObjects } from '@/components/spectra/NearbyObjects';
import { SEDPlotViewer } from '@/components/spectra/SEDPlotViewer';
import { InspectionModeOverlay } from '@/components/spectra/inspection/InspectionModeOverlay';
import { EnterInspectionModeButton } from '@/components/spectra/inspection/EnterInspectionModeButton';
import { getSpectrumById, getObjectMetadata } from '@/lib/actions/spectra';
import { getMapLayers, getNearbyShutters } from '@/lib/actions/map';
import type { MapLayer, Shutter } from '@/lib/actions/map';
import { parseFiltersFromURL, parseSortingFromURL } from '@/lib/utils/url-params';

interface SpectrumDetailPageProps {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

export async function generateMetadata({ params }: SpectrumDetailPageProps): Promise<Metadata> {
  const { id } = await params;
  const objectId = decodeURIComponent(id);

  // Use lightweight metadata fetch (no auth required) for OG tags
  const metadata = await getObjectMetadata(objectId);

  if (!metadata) {
    return { title: 'Object Not Found - CAMPFIRE' };
  }

  const redshiftText = metadata.redshift !== null
    ? `z = ${metadata.redshift.toFixed(4)}`
    : 'z = unknown';

  const description = `${objectId} | ${redshiftText}`;

  return {
    title: `${objectId} - CAMPFIRE`,
    description,
    openGraph: {
      title: objectId,
      description: `${redshiftText} | ${metadata.program_name || metadata.field}`,
      images: [{
        url: `/api/og-image/${encodeURIComponent(objectId)}`,
        width: 300,
        height: 300,
        alt: `RGB thumbnail for ${objectId}`,
      }],
    },
    twitter: {
      card: 'summary',
      title: objectId,
      description: `${redshiftText} | ${metadata.program_name || metadata.field}`,
    },
  };
}

export default async function SpectrumDetailPage({ params, searchParams }: SpectrumDetailPageProps) {
  const { id } = await params;

  // The id is the object_id string (e.g., "cosmos_ddt_66964")
  const objectId = decodeURIComponent(id);

  // Parse filter and sort params from URL
  const searchParamsObj = await searchParams;
  const urlParams = new URLSearchParams();
  Object.entries(searchParamsObj).forEach(([key, value]) => {
    if (value) {
      urlParams.set(key, Array.isArray(value) ? value.join(',') : value);
    }
  });

  // Fetch spectrum data first (need field for subsequent queries)
  const spectrumResult = await getSpectrumById(objectId);
  const { spectrum, isAuthenticated } = spectrumResult;

  // Show login prompt if not authenticated
  if (!isAuthenticated) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'NIRSpec', href: '/spectra' },
            { label: objectId },
          ]}
          className="mb-6"
        />

        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card dark:bg-slate-800 rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary dark:text-slate-400" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Sign in to view this spectrum
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6 max-w-md">
            Access to spectrum details requires authentication. Please sign in with your
            CAMPFIRE account.
          </p>
          <Link
            href="/login"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </Link>
        </div>
      </div>
    );
  }

  // Handle not found or access denied
  if (!spectrum) {
    notFound();
  }

  // Fetch map layer and nearby shutters in parallel (need spectrum.field)
  const [mapLayersResult, shuttersResult] = await Promise.all([
    getMapLayers(spectrum.field),
    getNearbyShutters(spectrum.ra, spectrum.dec, spectrum.field),
  ]);

  // Find the RGB map layer (preferred for cutout), falling back to default
  const rgbLayer: MapLayer | null = mapLayersResult.layers.find(l => l.filter === 'rgb')
    || mapLayersResult.layers.find(l => l.is_default)
    || mapLayersResult.layers[0]
    || null;
  const nearbyShutters: Shutter[] = shuttersResult.shutters;

  // Parse filters and sorting from URL for navigation
  const filters = parseFiltersFromURL(urlParams);
  const { sortColumn, sortDirection } = parseSortingFromURL(urlParams);
  const isInspectionMode = searchParamsObj.mode === 'inspect';
  const filterStr = urlParams.toString();

  // Inspection mode: render fullscreen overlay
  if (isInspectionMode) {
    return (
      <InspectionModeOverlay
        spectrum={spectrum}
        mapLayer={rgbLayer}
        nearbyShutters={nearbyShutters}
        filterStr={filterStr}
        filters={filters}
        sortColumn={sortColumn}
        sortDirection={sortDirection}
      />
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      {/* Breadcrumbs */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-4">
          <Breadcrumbs
            items={[
              { label: 'CAMPFIRE', href: '/' },
              { label: 'NIRSpec', href: '/spectra' },
              { label: spectrum.object_id },
            ]}
          />
          {filterStr && (
            <Link
              href={`/spectra?${filterStr}`}
              className="text-sm text-primary hover:text-primary-hover flex items-center gap-1"
            >
              ← Back to Filtered List
            </Link>
          )}
          <ReturnToMapButton />
        </div>

        {/* Navigation */}
        <ObjectNavigation
          objectId={objectId}
          filters={filters}
          sortColumn={sortColumn}
          sortDirection={sortDirection}
          filterStr={filterStr}
        />
      </div>

      {/* Main Content with Tabs */}
      <Tabs defaultValue={spectrum.spectra[0]?.grating.toLowerCase() || 'prism'}>
        {/* Header and RGB Image Side-by-Side */}
        <div className="flex gap-6 items-start mb-6">
          {/* Left Column: Header and Tab Navigation (~80% width) */}
          <div className="flex-1" style={{ minHeight: '350px' }}>
            {/* Header */}
            <div className="mb-4">
              <h1 className="text-3xl font-bold font-mono text-text-primary dark:text-slate-100 mb-2">
                {spectrum.object_id}
              </h1>
              <div className="flex items-center gap-2 text-sm text-text-secondary dark:text-slate-400 mb-3">
                <span>Program:</span>
                <Link
                  href={`/spectra?programs=${spectrum.program_id}`}
                  className="inline-flex items-center hover:bg-gray-100 dark:hover:bg-slate-700 px-2 py-1 rounded transition-colors text-text-primary dark:text-slate-100"
                >
                  {spectrum.program_name || `ID ${spectrum.program_id}`}
                </Link>
                <span>·</span>
                <span>Field:</span>
                <Link
                  href={`/spectra?fields=${spectrum.field}`}
                  className="inline-flex items-center hover:bg-gray-100 dark:hover:bg-slate-700 px-2 py-1 rounded transition-colors text-text-primary dark:text-slate-100"
                >
                  {spectrum.field}
                </Link>
                {spectrum.observation && (
                  <>
                    <span>·</span>
                    <span>Observation:</span>
                    <Link
                      href={`/spectra?observations=${spectrum.observation}`}
                      className="inline-flex items-center hover:bg-gray-100 dark:hover:bg-slate-700 px-2 py-1 rounded transition-colors text-text-primary dark:text-slate-100"
                    >
                      {spectrum.observation}
                    </Link>
                  </>
                )}
              </div>
              <div className="flex items-center gap-4">
                <CoordinateDisplay ra={spectrum.ra} dec={spectrum.dec} />
                <ShowOnMapLink ra={spectrum.ra} dec={spectrum.dec} field={spectrum.field} objectId={spectrum.object_id} />
              </div>
            </div>

            {/* Metric Cards */}
            <div className="mb-4">
              <MetricCards
                maxSnr={spectrum.max_snr || null}
                redshift={spectrum.redshift}
                redshiftQuality={spectrum.redshift_quality}
                numGratings={spectrum.spectra.length}
              />
            </div>

            {/* Action Buttons */}
            <div className="flex gap-4 mb-6">
              <DownloadButtons spectra={spectrum.spectra} objectId={spectrum.object_id} />
              <CopyLinkButton objectId={spectrum.object_id} />
            </div>

            {/* Tab Navigation */}
            <TabsList>
              {spectrum.spectra.sort((a, b) => {
                const order = ['PRISM', 'G140M', 'G235M', 'G395M'];
                return order.indexOf(a.grating) - order.indexOf(b.grating);
              }).map((spec) => (
                <TabsTrigger key={spec.grating} value={spec.grating.toLowerCase()}>
                  {spec.grating}
                </TabsTrigger>
              ))}
              <TabsTrigger value="redshift">REDSHIFT</TabsTrigger>
              {spectrum.hasSedPlot && (
                <TabsTrigger value="photometry">PHOTOMETRY</TabsTrigger>
              )}
              <TabsTrigger value="inspect">INSPECT</TabsTrigger>
              <TabsTrigger value="context">CONTEXT</TabsTrigger>
            </TabsList>
          </div>

          {/* Right Column: Tile Cutout with Shutters */}
          <div className="flex-shrink-0" style={{ width: '300px' }}>
            <TileCutoutWrapper
              objectId={spectrum.object_id}
              ra={spectrum.ra}
              dec={spectrum.dec}
              field={spectrum.field}
              mapLayer={rgbLayer}
              shutters={nearbyShutters}
              size={300}
            />
          </div>
        </div>

        {/* Tab Content (100% width, below header and image) */}
        <div className="mt-6">
          {/* Spectroscopy Tabs */}
          {spectrum.spectra.map((spec) => (
            <TabsContent key={spec.grating} value={spec.grating.toLowerCase()}>
              <GratingDetails spectrum={spec} />
              <SpectrumPlot
                fitsPath={spec.fits_path}
                grating={spec.grating}
                initialRedshift={spectrum.redshift_inspected ?? spectrum.redshift_auto}
              />
            </TabsContent>
          ))}

          {/* Redshift Tab */}
          <TabsContent value="redshift">
            <div className="space-y-4">
              {/* Redshift Fit Summary Table */}
              <RedshiftFitSummary
                spectra={spectrum.spectra}
                redshift_auto={spectrum.redshift_auto}
              />

              {/* Individual Grating Fits */}
              {spectrum.spectra.map((spec, index) => (
                <details key={index} className="group" open={index === 0}>
                  <summary className="cursor-pointer list-none">
                    <div className="bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg p-4 hover:bg-gray-50 dark:hover:bg-slate-700 transition-colors">
                      <div className="flex items-center justify-between">
                        <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">
                          {spec.grating} Redshift Fit
                        </h3>
                        <svg
                          className="w-5 h-5 text-text-secondary dark:text-slate-400 transition-transform group-open:rotate-180"
                          fill="none"
                          stroke="currentColor"
                          viewBox="0 0 24 24"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth={2}
                            d="M19 9l-7 7-7-7"
                          />
                        </svg>
                      </div>
                    </div>
                  </summary>
                  <div className="mt-2">
                    <RedshiftFitPlot
                      fitsPath={spec.fits_path}
                      grating={spec.grating}
                      initialRedshift={spectrum.redshift_inspected ?? spectrum.redshift_auto}
                    />
                  </div>
                </details>
              ))}
            </div>
          </TabsContent>

          {/* Photometry Tab */}
          {spectrum.hasSedPlot && (
            <TabsContent value="photometry">
              <SEDPlotViewer objectId={spectrum.object_id} />
            </TabsContent>
          )}

          {/* Inspect Tab */}
          <TabsContent value="inspect">
            <EnterInspectionModeButton filterStr={filterStr} />
            <InspectionPanel
              objectDbId={spectrum.id}
              objectId={spectrum.object_id}
              initialData={{
                redshift_auto: spectrum.redshift_auto,
                redshift_inspected: spectrum.redshift_inspected,
                redshift_quality: spectrum.redshift_quality,
                spectral_features: spectrum.spectral_features,
                object_flags: spectrum.object_flags,
                dq_flags: spectrum.dq_flags,
                last_inspected_at: spectrum.last_inspected_at,
                last_inspected_by: spectrum.last_inspected_by,
              }}
            />
          </TabsContent>

          {/* Context Tab */}
          <TabsContent value="context">
            <NearbyObjects
              ra={spectrum.ra}
              dec={spectrum.dec}
              currentObjectId={spectrum.object_id}
            />
          </TabsContent>
        </div>
      </Tabs>
    </div>
  );
}
