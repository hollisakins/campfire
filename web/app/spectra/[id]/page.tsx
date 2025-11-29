import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { MetricCards } from '@/components/spectra/MetricCards';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { GratingDetails } from '@/components/spectra/GratingDetails';
import { InspectionPanel } from '@/components/spectra/InspectionPanel';
import { SpectrumPlot } from '@/components/spectra/SpectrumPlot';
import { RedshiftFitSummary } from '@/components/spectra/RedshiftFitSummary';
import { RedshiftFitPlot } from '@/components/spectra/RedshiftFitPlot';
import { Pagination } from '@/components/spectra/Pagination';
import { DownloadButtons } from '@/components/spectra/DownloadButtons';
import { CoordinateDisplay } from '@/components/spectra/CoordinateDisplay';
import { RGBImage } from '@/components/spectra/RGBImage';
import { NearbyObjects } from '@/components/spectra/NearbyObjects';
import { getSpectrumById, getAdjacentObjects } from '@/lib/actions/spectra';
import { generateRGBImageUrl } from '@/lib/r2';
import { LogIn } from 'lucide-react';
import { parseFiltersFromURL, parseSortingFromURL } from '@/lib/utils/url-params';

interface SpectrumDetailPageProps {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
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

  const filters = parseFiltersFromURL(urlParams);
  const { sortColumn, sortDirection } = parseSortingFromURL(urlParams);

  // Fetch the spectrum data
  const { spectrum, error, isAuthenticated } = await getSpectrumById(objectId);

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
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Sign in to view this spectrum
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
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

  // Get pagination info (respecting filters and sorting from URL)
  const { previous, next, currentIndex, total } = await getAdjacentObjects(
    objectId,
    filters,
    sortColumn,
    sortDirection
  );

  // Generate RGB image URL (try to load, will show fallback if not found)
  let rgbImageUrl: string | null = null;
  try {
    rgbImageUrl = await generateRGBImageUrl(spectrum.object_id);
  } catch (error) {
    console.error('Failed to generate RGB image URL:', error);
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
          {urlParams.toString() && (
            <Link
              href={`/spectra?${urlParams.toString()}`}
              className="text-sm text-primary hover:text-primary-hover flex items-center gap-1"
            >
              ← Back to Filtered List
            </Link>
          )}
        </div>

        {/* Pagination */}
        <Pagination
          current={currentIndex}
          total={total}
          prevHref={previous ? `/spectra/${encodeURIComponent(previous)}?${urlParams.toString()}` : undefined}
          nextHref={next ? `/spectra/${encodeURIComponent(next)}?${urlParams.toString()}` : undefined}
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
              <h1 className="text-3xl font-bold font-mono text-text-primary mb-2">
                {spectrum.object_id}
              </h1>
              <div className="text-sm text-text-secondary space-x-4 mb-3">
                <span>Program: {spectrum.program_name || `ID ${spectrum.program_id}`}</span>
                <span>-</span>
                <span>Field: {spectrum.field}</span>
              </div>
              <CoordinateDisplay ra={spectrum.ra} dec={spectrum.dec} />
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
              <TabsTrigger value="photometry">PHOTOMETRY</TabsTrigger>
              <TabsTrigger value="inspect">INSPECT</TabsTrigger>
              <TabsTrigger value="context">CONTEXT</TabsTrigger>
            </TabsList>
          </div>

          {/* Right Column: RGB Image */}
          <div className="flex-shrink-0" style={{ width: '300px' }}>
            <RGBImage
              objectId={spectrum.object_id}
              rgbImageUrl={rgbImageUrl}
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
                    <div className="bg-card border border-border rounded-lg p-4 hover:bg-gray-50 transition-colors">
                      <div className="flex items-center justify-between">
                        <h3 className="text-lg font-semibold text-text-primary">
                          {spec.grating} Redshift Fit
                        </h3>
                        <svg
                          className="w-5 h-5 text-text-secondary transition-transform group-open:rotate-180"
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
          <TabsContent value="photometry">
            <div className="bg-card border border-border rounded-lg p-12 text-center">
              <p className="text-text-secondary">
                Multi-wavelength photometry will appear here
              </p>
            </div>
          </TabsContent>

          {/* Inspect Tab */}
          <TabsContent value="inspect">
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
