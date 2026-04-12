import { redirect, notFound } from 'next/navigation';
import { Metadata } from 'next';
import { createServiceClient } from '@/lib/supabase/server';
import { getTargetMetadata } from '@/lib/actions/spectra';

interface TargetRedirectPageProps {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

export async function generateMetadata({ params }: TargetRedirectPageProps): Promise<Metadata> {
  const { id } = await params;
  const targetId = decodeURIComponent(id);

  const metadata = await getTargetMetadata(targetId);

  if (!metadata) {
    return { title: 'Target Not Found - CAMPFIRE' };
  }

  const redshiftText = metadata.redshift !== null
    ? `z = ${metadata.redshift.toFixed(4)}`
    : 'z = unknown';

  return {
    title: `${targetId} - CAMPFIRE`,
    description: `${targetId} | ${redshiftText}`,
    openGraph: {
      title: targetId,
      description: `${redshiftText} | ${metadata.program_name || metadata.field}`,
      images: [{
        url: `/api/og-image/${encodeURIComponent(targetId)}`,
        width: 300,
        height: 300,
        alt: `RGB thumbnail for ${targetId}`,
      }],
    },
    twitter: {
      card: 'summary',
      title: targetId,
      description: `${redshiftText} | ${metadata.program_name || metadata.field}`,
    },
  };
}

export default async function TargetRedirectPage({ params, searchParams }: TargetRedirectPageProps) {
  const { id } = await params;
  const targetId = decodeURIComponent(id);
  const searchParamsObj = await searchParams;

  // Look up the target's parent object using service role (no auth needed for redirect)
  const supabase = createServiceClient();

  const { data, error } = await supabase
    .from('targets')
    .select('object_id, objects!inner(object_id)')
    .eq('target_id', targetId)
    .single();

  if (error || !data) {
    notFound();
  }

  // Build redirect URL
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const objectId = (data.objects as any).object_id as string;

  // Preserve relevant search params (filter/sort state)
  const redirectParams = new URLSearchParams();
  redirectParams.set('tab', targetId);
  // Forward grating param if present
  const grating = typeof searchParamsObj.grating === 'string' ? searchParamsObj.grating : null;
  if (grating) {
    redirectParams.set('grating', grating);
  }
  // Forward filter/sort params
  Object.entries(searchParamsObj).forEach(([key, value]) => {
    if (value && key !== 'from_object' && key !== 'grating' && key !== 'tab') {
      redirectParams.set(key, Array.isArray(value) ? value.join(',') : value);
    }
  });

  redirect(`/nirspec/objects/${encodeURIComponent(objectId)}?${redirectParams.toString()}`);
}
