import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Metadata } from 'next';
import { LogIn } from 'lucide-react';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { MetricCards } from '@/components/spectra/MetricCards';
import { DownloadButtons } from '@/components/spectra/DownloadButtons';
import { CopyLinkButton } from '@/components/spectra/CopyLinkButton';
import { CoordinateDisplay } from '@/components/spectra/CoordinateDisplay';
import { ShowOnMapLink } from '@/components/map/ShowOnMapLink';
import { NearbyObjects } from '@/components/spectra/NearbyObjects';
import { ObjectDetailClient } from '@/components/spectra/ObjectDetailClient';
import { getObjectById, getObjectMetadata } from '@/lib/actions/spectra';
import type { Spectrum } from '@/lib/types';

interface ObjectDetailPageProps {
  params: Promise<{ id: string }>;
}

export async function generateMetadata({ params }: ObjectDetailPageProps): Promise<Metadata> {
  const { id } = await params;
  const objectId = decodeURIComponent(id);

  const metadata = await getObjectMetadata(objectId);

  if (!metadata) {
    return { title: 'Object Not Found - CAMPFIRE' };
  }

  const redshiftText = metadata.best_redshift !== null
    ? `z = ${metadata.best_redshift.toFixed(4)}`
    : 'z = unknown';

  return {
    title: `${objectId} - CAMPFIRE`,
    description: `${objectId} | ${redshiftText} | ${metadata.field}`,
    openGraph: {
      title: objectId,
      description: `${redshiftText} | ${metadata.field}`,
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
      description: `${redshiftText} | ${metadata.field}`,
    },
  };
}

export default async function ObjectDetailPage({ params }: ObjectDetailPageProps) {
  const { id } = await params;
  const objectId = decodeURIComponent(id);

  const { object, isAuthenticated } = await getObjectById(objectId);

  if (!isAuthenticated) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Objects', href: '/spectra?view=objects' },
            { label: objectId },
          ]}
          className="mb-6"
        />

        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card dark:bg-slate-800 rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary dark:text-slate-400" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Sign in to view this object
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6 max-w-md">
            Access to object details requires authentication.
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

  if (!object) {
    notFound();
  }

  // Flatten all member spectra for download button
  const allSpectra: Spectrum[] = object.member_targets.flatMap(m => m.spectra);

  return (
    <div className="container mx-auto px-4 py-8">
      {/* Breadcrumbs */}
      <div className="flex items-center gap-4 mb-6">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Objects', href: '/spectra?view=objects' },
            { label: object.object_id },
          ]}
        />
        <Link
          href="/spectra?view=objects"
          className="text-sm text-primary hover:text-primary-hover flex items-center gap-1"
        >
          ← Back to Objects List
        </Link>
      </div>

      {/* Header + Selectors + Table + Viewer */}
      <ObjectDetailClient
        object={object}
        headerContent={
          <>
            <h1 className="text-3xl font-bold font-mono text-text-primary dark:text-slate-100 mb-2">
              {object.object_id}
            </h1>
            <div className="flex items-center gap-2 text-sm text-text-secondary dark:text-slate-400 mb-3">
              <span>Field:</span>
              <Link
                href={`/spectra?view=objects&fields=${object.field}`}
                className="inline-flex items-center hover:bg-gray-100 dark:hover:bg-slate-700 px-2 py-1 rounded transition-colors text-text-primary dark:text-slate-100"
              >
                {object.field}
              </Link>
              <span>·</span>
              <span>{object.n_targets} targets</span>
              <span>·</span>
              <span>{object.n_spectra} spectra</span>
            </div>
            <div className="flex items-center gap-4 mb-4">
              <CoordinateDisplay ra={object.ra} dec={object.dec} />
              <ShowOnMapLink ra={object.ra} dec={object.dec} field={object.field} objectId={object.object_id} />
            </div>

            <div className="mb-4">
              <MetricCards
                maxSnr={object.max_snr}
                redshift={object.best_redshift}
                redshiftQuality={object.best_redshift_quality}
                numGratings={object.gratings.length}
              />
            </div>

            <div className="flex gap-4">
              <DownloadButtons spectra={allSpectra} targetId={object.object_id} />
              <CopyLinkButton
                targetId={object.object_id}
                url={`/spectra/objects/${encodeURIComponent(object.object_id)}`}
              />
            </div>
          </>
        }
      />

      {/* Nearby objects */}
      <div className="mt-8">
        <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100 mb-3">
          Nearby Objects
        </h2>
        <NearbyObjects
          ra={object.ra}
          dec={object.dec}
          currentTargetId={object.object_id}
          excludeTargetIds={object.member_targets.map(m => m.target_id)}
        />
      </div>
    </div>
  );
}
