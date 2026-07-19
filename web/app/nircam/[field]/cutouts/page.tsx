import { SignInLink } from '@/components/auth/SignInLink';
import { LogIn } from 'lucide-react';
import { getFitsglDatasets } from '@/lib/actions/map';
import { getNircamFields } from '@/lib/actions/nircam';
import { createClient } from '@/lib/supabase/server';
import { CutoutsContent } from './CutoutsContent';

export const dynamic = 'force-dynamic';

interface CutoutsPageProps {
  params: Promise<{ field: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

/**
 * Field-scoped cutout tool (epic #337, Phase 5) — a COSMOS-cutouts-style GUI
 * over the science cutout API, living under /nircam/[field] so it's found by
 * browsing the field's data. Availability follows the field's deployed FitsGL
 * dataset (RLS keeps draft-backed ones admin-only).
 */
export default async function NircamFieldCutoutsPage({ params, searchParams }: CutoutsPageProps) {
  const { field: rawField } = await params;
  const field = decodeURIComponent(rawField);
  const sp = await searchParams;
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
            Sign in to make cutouts
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
            Generating cutouts from the imaging requires authentication.
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

  // The field's cutout source + the field list for the switcher dropdown.
  const [datasets, fieldsResult] = await Promise.all([
    getFitsglDatasets(field),
    getNircamFields(),
  ]);
  const dataset = datasets.find((d) => d.kind === 'field') ?? null;

  return (
    <CutoutsContent
      field={field}
      dataset={dataset}
      allFields={fieldsResult.fields}
      initial={{
        ra: str(sp.ra),
        dec: str(sp.dec),
        fov: str(sp.fov),
        bands: str(sp.bands),
      }}
    />
  );
}
