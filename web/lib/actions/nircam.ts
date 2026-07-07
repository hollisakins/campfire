'use server';

import { createClient } from '@/lib/supabase/server';
import { paginateQuery } from '@/lib/supabase/paginate';
import type { NircamImage, NircamExpmap } from '@/lib/types';

export interface NircamImagesResult {
  images: NircamImage[];
  error?: string;
  isAuthenticated: boolean;
}

export interface NircamExpmapsResult {
  expmaps: NircamExpmap[];
  error?: string;
  isAuthenticated: boolean;
}

export interface NircamFilterOptionsResult {
  fields: string[];
  tiles: string[];
  filters: string[];
  pixel_scales: string[];
  extensions: string[];
  epochs: string[];  // exposure-subset names ('' = full field)
  error?: string;
}

/**
 * Fetch all NIRCam images from the database.
 * Requires authentication but no program-based access control.
 * Returns all images for client-side filtering/sorting.
 */
export async function getNircamImages(): Promise<NircamImagesResult> {
  const supabase = await createClient();

  // Check if user is authenticated
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return {
      images: [],
      isAuthenticated: false,
    };
  }

  try {
    const { data, error } = await paginateQuery<NircamImage>(
      () => supabase
        .from('nircam_images')
        .select('*')
        .order('field', { ascending: true })
        .order('filter', { ascending: true })
        .order('tile', { ascending: true })
        .order('id', { ascending: true }),
    );

    if (error) {
      console.error('Error fetching NIRCam images:', error);
      return {
        images: [],
        error: error.message,
        isAuthenticated: true,
      };
    }

    return {
      images: data,
      isAuthenticated: true,
    };
  } catch (err) {
    console.error('Unexpected error fetching NIRCam images:', err);
    return {
      images: [],
      error: 'An unexpected error occurred',
      isAuthenticated: true,
    };
  }
}

/**
 * Fetch the per-(field, filter) exposure-coverage maps a user may see.
 *
 * Expmaps are registered in `storage_objects` (product_type `nircam_expmap`) and
 * their visibility rides the owning field deployment via RLS: a published field
 * deployment is public to everyone, drafts stay admin-only. We simply select
 * active rows and let `select_storage_objects_by_access` do the gating — no
 * bespoke access logic here, mirroring how mosaics rely on `nircam_images` RLS.
 */
export async function getNircamExpmaps(): Promise<NircamExpmapsResult> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return { expmaps: [], isAuthenticated: false };
  }

  try {
    const { data, error } = await paginateQuery<{
      field: string; filter: string | null; storage_key: string;
      size_bytes: number | null;
    }>(
      () => supabase
        .from('storage_objects')
        .select('field, filter, storage_key, size_bytes')
        .eq('product_type', 'nircam_expmap')
        .eq('status', 'active')
        .order('field', { ascending: true })
        .order('filter', { ascending: true }),
    );

    if (error) {
      console.error('Error fetching NIRCam expmaps:', error);
      return { expmaps: [], error: error.message, isAuthenticated: true };
    }

    const expmaps: NircamExpmap[] = data
      .filter((r) => r.filter)  // per-filter product; skip any unscoped row
      .map((r) => ({
        field: r.field,
        filter: r.filter as string,
        storage_key: r.storage_key,
        file_size: r.size_bytes ?? undefined,
      }));

    return { expmaps, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching NIRCam expmaps:', err);
    return { expmaps: [], error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

/**
 * Fetch unique filter options from the NIRCam images table.
 * Used to populate filter dropdowns.
 */
export async function getNircamFilterOptions(): Promise<NircamFilterOptionsResult> {
  const supabase = await createClient();

  // Check if user is authenticated
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return {
      fields: [],
      tiles: [],
      filters: [],
      pixel_scales: [],
      extensions: [],
      epochs: [],
    };
  }

  try {
    const { data: images, error } = await paginateQuery<{
      field: string; tile: string; filter: string;
      pixel_scale: string; extension: string; epoch: string | null;
    }>(
      () => supabase
        .from('nircam_images')
        .select('field, tile, filter, pixel_scale, extension, epoch')
        .order('field')
        .order('filter')
        .order('tile'),
    );

    if (error) {
      console.error('Error fetching NIRCam filter options:', error);
      return {
        fields: [],
        tiles: [],
        filters: [],
        pixel_scales: [],
        extensions: [],
        epochs: [],
        error: error.message,
      };
    }

    const fields = [...new Set(images.map(i => i.field))].sort();
    const filters = [...new Set(images.map(i => i.filter))].sort();
    const pixel_scales = [...new Set(images.map(i => i.pixel_scale))].sort();
    // Epoch '' = full-field mosaic; keep it (sorts first) so the "Full field"
    // option is always available alongside any named subset epochs.
    const epochs = [...new Set(images.map(i => i.epoch ?? ''))].sort();
    const extensions = [...new Set(images.map(i => i.extension))].sort((a, b) => {
      // Sort extensions by priority: sci > err > rms > srcmask
      const order = ['sci', 'err', 'rms', 'srcmask'];
      const aIdx = order.indexOf(a.toLowerCase());
      const bIdx = order.indexOf(b.toLowerCase());
      if (aIdx === -1 && bIdx === -1) return a.localeCompare(b);
      if (aIdx === -1) return 1;
      if (bIdx === -1) return -1;
      return aIdx - bIdx;
    });

    // Sort tiles alphanumerically (A1, A2, A10, B1, etc.)
    const tiles = [...new Set(images.map(i => i.tile))].sort((a, b) => {
      const aMatch = a.match(/^([A-Z]+)(\d+)$/);
      const bMatch = b.match(/^([A-Z]+)(\d+)$/);

      if (aMatch && bMatch) {
        const [, aLetter, aNumber] = aMatch;
        const [, bLetter, bNumber] = bMatch;

        if (aLetter !== bLetter) {
          return aLetter.localeCompare(bLetter);
        }
        return parseInt(aNumber, 10) - parseInt(bNumber, 10);
      }

      return a.localeCompare(b);
    });

    return {
      fields,
      tiles,
      filters,
      pixel_scales,
      extensions,
      epochs,
    };
  } catch (err) {
    console.error('Unexpected error fetching NIRCam filter options:', err);
    return {
      fields: [],
      tiles: [],
      filters: [],
      pixel_scales: [],
      extensions: [],
      epochs: [],
      error: 'An unexpected error occurred',
    };
  }
}
