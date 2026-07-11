import Link from 'next/link';
import { LogIn, Scissors } from 'lucide-react';
import { getFitsglDatasets } from '@/lib/actions/map';
import { createClient } from '@/lib/supabase/server';
import { CutoutsPageContent } from './CutoutsPageContent';

export const dynamic = 'force-dynamic';

interface CutoutsPageProps {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

/**
 * Cutout service (epic #337, Phase 5): a COSMOS-cutouts-style GUI over the
 * science cutout API — pick a field + coordinates, preview a multi-band
 * figure, download the FITS. Fields come from the deployed FitsGL datasets
 * (RLS keeps draft-backed ones admin-only).
 */
export default async function CutoutsPage({ searchParams }: CutoutsPageProps) {
  const params = await searchParams;
  const str = (v: string | string[] | undefined) => (typeof v === 'string' ? v : undefined);

  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Sign in to use the cutout service
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
            Generating cutouts from the imaging requires authentication.
          </p>
          <Link
            href="/login"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-on-primary rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </Link>
        </div>
      </div>
    );
  }

  const datasets = (await getFitsglDatasets()).filter((d) => d.kind === 'field');

  if (datasets.length === 0) {
    return (
      <div className="container mx-auto px-4 py-8">
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <Scissors className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            No imaging available yet
          </h2>
          <p className="text-text-secondary max-w-md">
            The cutout service works on fields with a deployed FitsGL tile
            pyramid; none are published yet.
          </p>
        </div>
      </div>
    );
  }

  return (
    <CutoutsPageContent
      datasets={datasets}
      initial={{
        field: str(params.field),
        ra: str(params.ra),
        dec: str(params.dec),
        fov: str(params.fov),
        bands: str(params.bands),
      }}
    />
  );
}
