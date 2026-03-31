import { redirect } from 'next/navigation';

/**
 * Redirect from old /spectra/[id] URLs to /nirspec/targets/[id].
 * Preserves query params so shared links continue to work.
 */
export default async function SpectraRedirect({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { id } = await params;
  const sp = await searchParams;
  const qs = new URLSearchParams();
  Object.entries(sp).forEach(([key, value]) => {
    if (value) qs.set(key, Array.isArray(value) ? value.join(',') : value);
  });
  const query = qs.toString();
  redirect(`/nirspec/targets/${id}${query ? `?${query}` : ''}`);
}
