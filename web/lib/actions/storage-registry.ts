'use server';

import { requireAdmin as requireAdminIdentity } from '@/lib/auth/identity';
import {
  GetObjectCommand,
  HeadObjectCommand,
  ListObjectsV2Command,
} from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import {
  getS3ClientForBackend,
  getBucketNameForBackend,
  type DataBackend,
} from '@/lib/storage';

// ---------------------------------------------------------------------------
// Storage-registry (Axis A) admin actions — epic #210, B5.
//
// Read-only browser over `storage_objects`, the shadow index of every object in
// cloud storage. This is the "what's in the cloud" view (orthogonal to the
// draft/publish lifecycle, which lives on the Deployments page). Populated by
// every deploy — including the canonical spectrum-exposure intermediates.
// `storage_objects` RLS is admin-only; requireAdmin is defense-in-depth.
// ---------------------------------------------------------------------------

async function requireAdmin() {
  const { supabase } = await requireAdminIdentity();
  return supabase;
}

export interface StorageObjectRow {
  id: number;
  storage_key: string;
  product_type: string;
  instrument: string | null;
  observation: string | null;
  field: string | null;
  exposure_ref: string | null;
  size_bytes: number;
  content_hash: string;
  backend: string;
  status: string;
  cfpipe_version: string | null;
  created_at: string;
}

export interface StorageObjectsResult {
  objects: StorageObjectRow[];
  total: number;
  error?: string;
}

// Sort-key whitelist lives in @/lib/admin/sort-keys — a 'use server' module
// may only export async functions.

// Backed by the get_admin_storage_objects RPC: whitelisted server-side sort +
// windowed total in one scan (the previous count:'exact' ran a second full
// COUNT over the registry — its largest-table hot path).
export async function getStorageObjects(params?: {
  observation?: string;
  field?: string;
  productType?: string;
  status?: string;
  backend?: string;
  search?: string;
  sortColumn?: string;
  sortDirection?: 'asc' | 'desc';
  page?: number;      // 1-based
  pageSize?: number;
}): Promise<StorageObjectsResult> {
  try {
    const supabase = await requireAdmin();

    const { data, error } = await supabase.rpc('get_admin_storage_objects', {
      p_product_type: params?.productType ?? null,
      p_status: params?.status ?? null,
      p_field: params?.field ?? null,
      p_observation: params?.observation ?? null,
      p_backend: params?.backend ?? null,
      p_sort_column: params?.sortColumn ?? 'created_at',
      p_sort_direction: params?.sortDirection ?? 'desc',
      p_page: params?.page ?? 1,
      p_page_size: params?.pageSize ?? 50,
      p_search: params?.search ?? null,
    });

    if (error) return { objects: [], total: 0, error: error.message };
    const rows = (data ?? []) as (StorageObjectRow & { total_count: number })[];
    const total = rows[0]?.total_count ?? 0;
    return {
      objects: rows.map(({ total_count: _t, ...row }) => row as StorageObjectRow),
      total,
    };
  } catch (err) {
    return {
      objects: [],
      total: 0,
      error: err instanceof Error ? err.message : 'Failed to load storage objects',
    };
  }
}

export interface StorageFacets {
  productTypes: string[];
  statuses: string[];
  backends: string[];
  fields: string[];
  observations: string[];
  error?: string;
}

// Distinct facet values for the filter dropdowns (get_admin_storage_facets —
// grouped scans server-side, replacing the hardcoded 5-of-22 product list).
export async function getStorageFacets(): Promise<StorageFacets> {
  const empty: StorageFacets = {
    productTypes: [], statuses: [], backends: [], fields: [], observations: [],
  };
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase.rpc('get_admin_storage_facets');
    if (error) return { ...empty, error: error.message };
    const rows = (data ?? []) as { kind: string; value: string }[];
    const pick = (kind: string) => rows.filter((r) => r.kind === kind).map((r) => r.value);
    return {
      productTypes: pick('product_type'),
      statuses: pick('status'),
      backends: pick('backend'),
      fields: pick('field'),
      observations: pick('observation'),
    };
  } catch (err) {
    return { ...empty, error: err instanceof Error ? err.message : 'Failed to load facets' };
  }
}

export interface StorageBudget {
  total_bytes: number;
  cap_bytes: number;
  pct_used: number;
  registry_bytes: number;
  tile_bytes: number;
  by_product_type: Record<string, number> | null;
  by_backend: Record<string, number> | null;
  // get_storage_budget has always returned these two; the interface just
  // dropped them (2026-08 dashboard redesign exposes them).
  by_bucket: Record<string, number> | null;
  // NOTE: by_status covers ALL statuses while total_bytes counts only active
  // rows + tiles — superseded/revoked bytes sit OUTSIDE the total, so render
  // them beside the budget meter, never as segments inside it.
  by_status: Record<string, { count: number; bytes: number }> | null;
  error?: string;
}

export async function getStorageBudget(): Promise<StorageBudget | { error: string }> {
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase.rpc('get_storage_budget');
    if (error) return { error: error.message };
    return data as unknown as StorageBudget;
  } catch (err) {
    return { error: err instanceof Error ? err.message : 'Failed to load budget' };
  }
}

// ---------------------------------------------------------------------------
// Object detail (Phase 2, audit C3) — the drawer's data
// ---------------------------------------------------------------------------

export interface StorageObjectDetail {
  id: number;
  storage_key: string;
  product_type: string;
  instrument: string | null;
  observation: string | null;
  field: string | null;
  exposure_ref: string | null;
  size_bytes: number;
  content_hash: string;
  sci_dq_hash: string | null;
  wcs_hash: string | null;
  content_type: string;
  backend: string;
  bucket: string;
  status: string;
  cfpipe_version: string | null;
  deployment_id: number | null;
  uploaded_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface StorageDeployment {
  id: number;
  observation: string | null;
  field: string | null;
  status: string;
  cfpipe_version: string | null;
  jwst_version: string | null;
  crds_context: string | null;
  deployed_at: string | null;
  published_at: string | null;
  revoked_at: string | null;
}

export interface StorageDeployEvent {
  id: string;
  action: string;
  status_to: string | null;
  affected_count: number | null;
  occurred_at: string;
  actor_name: string | null;
}

export interface StorageObjectDetailResult {
  object: StorageObjectDetail | null;
  deployment: StorageDeployment | null;
  events: StorageDeployEvent[];
  error?: string;
}

// Full provenance for a single registry object plus its lifecycle history:
// the storage_objects row, the deployment that produced it, and the
// deploy_events that touched either the object or its deployment. Plain selects
// under admin RLS — no RPC / no reserved-word surface.
export async function getStorageObjectDetail(
  id: number,
): Promise<StorageObjectDetailResult> {
  try {
    const supabase = await requireAdmin();

    const { data: objectRaw, error } = await supabase
      .from('storage_objects')
      .select(
        'id, storage_key, product_type, instrument, observation, field, exposure_ref, ' +
        'size_bytes, content_hash, sci_dq_hash, wcs_hash, content_type, backend, bucket, status, ' +
        'cfpipe_version, deployment_id, uploaded_by, created_at, updated_at',
      )
      .eq('id', id)
      .single();

    if (error || !objectRaw) {
      return { object: null, deployment: null, events: [], error: error?.message ?? 'Not found' };
    }
    const object = objectRaw as unknown as StorageObjectDetail;

    let deployment: StorageDeployment | null = null;
    if (object.deployment_id != null) {
      const { data: dep } = await supabase
        .from('deployments')
        .select(
          'id, observation, field, status, cfpipe_version, jwst_version, crds_context, ' +
          'deployed_at, published_at, revoked_at',
        )
        .eq('id', object.deployment_id)
        .single();
      deployment = (dep as unknown as StorageDeployment) ?? null;
    }

    // Lifecycle history: events on this object, or on its deployment batch.
    let eventsQuery = supabase
      .from('deploy_events')
      .select('id, action, status_to, affected_count, occurred_at, actor')
      .order('occurred_at', { ascending: false })
      .limit(50);
    eventsQuery = object.deployment_id != null
      ? eventsQuery.or(`object_id.eq.${id},deployment_id.eq.${object.deployment_id}`)
      : eventsQuery.eq('object_id', id);
    const { data: rawEvents } = await eventsQuery;

    const events = (rawEvents ?? []) as unknown as {
      id: string; action: string; status_to: string | null;
      affected_count: number | null; occurred_at: string; actor: string | null;
    }[];

    // Resolve actor uuids → names in one batch (no FK to embed on).
    const actorIds = Array.from(new Set(events.map((e) => e.actor).filter(Boolean))) as string[];
    const nameById = new Map<string, string>();
    if (actorIds.length) {
      const { data: profiles } = await supabase
        .from('user_profiles')
        .select('user_id, username, full_name')
        .in('user_id', actorIds);
      for (const p of profiles ?? []) {
        nameById.set(p.user_id, p.full_name || p.username);
      }
    }

    return {
      object,
      deployment,
      events: events.map(({ actor, ...e }) => ({
        ...e,
        actor_name: actor ? nameById.get(actor) ?? null : null,
      })),
    };
  } catch (err) {
    return {
      object: null, deployment: null, events: [],
      error: err instanceof Error ? err.message : 'Failed to load object detail',
    };
  }
}

// ---------------------------------------------------------------------------
// Direct download (Phase 2, audit C1) — the headline gap
// ---------------------------------------------------------------------------

export interface PresignedDownload {
  url: string | null;
  filename: string;
  error?: string;
}

const DOWNLOAD_TTL_SECONDS = 300;

function basename(key: string): string {
  const parts = key.split('/');
  return parts[parts.length - 1] || key;
}

// Presign a GET for one or more registry objects, by id (never a client-
// supplied key — mirrors presignExposurePngs). Presigns the object's EXACT
// recorded key on its recorded backend; deliberately does NOT route through
// r2.ts's dual-read resolver (which fails open to R2 under the legacy key and
// is gated by OSN_READ_ENABLED) — an admin download wants the literal object.
// No status filter: admins may pull superseded/revoked/draft objects (RLS
// already allows them to see those rows). ResponseContentDisposition forces a
// download with the real filename cross-origin, so a plain navigation works
// without a streaming proxy.
export async function presignStorageObjectDownload(
  ids: number | number[],
): Promise<Record<number, PresignedDownload>> {
  const idList = Array.isArray(ids) ? ids : [ids];
  const out: Record<number, PresignedDownload> = {};
  if (idList.length === 0) return out;
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase
      .from('storage_objects')
      .select('id, storage_key, backend')
      .in('id', idList);
    if (error || !data) {
      for (const id of idList) out[id] = { url: null, filename: '', error: error?.message ?? 'Not found' };
      return out;
    }

    await Promise.all(data.map(async (row) => {
      const key = row.storage_key as string;
      const filename = basename(key);
      try {
        const backend: DataBackend = row.backend === 'r2' ? 'r2' : 'osn';
        const url = await getSignedUrl(
          getS3ClientForBackend(backend),
          new GetObjectCommand({
            Bucket: getBucketNameForBackend(backend),
            Key: key,
            ResponseContentDisposition: `attachment; filename="${filename}"`,
          }),
          { expiresIn: DOWNLOAD_TTL_SECONDS },
        );
        out[row.id] = { url, filename };
      } catch (err) {
        out[row.id] = {
          url: null, filename,
          error: err instanceof Error ? err.message : 'Failed to presign',
        };
      }
    }));
    return out;
  } catch (err) {
    for (const id of idList) {
      out[id] = { url: null, filename: '', error: err instanceof Error ? err.message : 'Failed' };
    }
    return out;
  }
}

// ---------------------------------------------------------------------------
// Live bucket verification (Phase 2, audit C3) — read-only HEAD
// ---------------------------------------------------------------------------

export interface HeadResult {
  present: boolean;
  contentLength: number | null;
  etag: string | null;
  lastModified: string | null;
  registrySize: number | null;
  registryHash: string | null;
  sizeMatches: boolean | null;
  error?: string;
}

// Issue a live HEAD against the bucket to confirm an object is actually there
// and compare its size to what the registry recorded — surfacing registry-vs-
// bucket drift per object. Object-absent (404) is a valid result, not an
// error. Read-only: never writes back to the registry (reconciliation is
// Phase 3 ledger work).
export async function headStorageObject(id: number): Promise<HeadResult> {
  const empty: HeadResult = {
    present: false, contentLength: null, etag: null, lastModified: null,
    registrySize: null, registryHash: null, sizeMatches: null,
  };
  try {
    const supabase = await requireAdmin();
    const { data: row, error } = await supabase
      .from('storage_objects')
      .select('storage_key, backend, size_bytes, content_hash')
      .eq('id', id)
      .single();
    if (error || !row) return { ...empty, error: error?.message ?? 'Not found' };

    const registrySize = Number(row.size_bytes);
    const registryHash = row.content_hash as string;
    const backend: DataBackend = row.backend === 'r2' ? 'r2' : 'osn';

    try {
      const head = await getS3ClientForBackend(backend).send(
        new HeadObjectCommand({
          Bucket: getBucketNameForBackend(backend),
          Key: row.storage_key as string,
        }),
      );
      const contentLength = head.ContentLength ?? null;
      return {
        present: true,
        contentLength,
        etag: head.ETag ?? null,
        lastModified: head.LastModified ? head.LastModified.toISOString() : null,
        registrySize,
        registryHash,
        sizeMatches: contentLength != null ? contentLength === registrySize : null,
      };
    } catch (err: unknown) {
      const e = err as { name?: string; $metadata?: { httpStatusCode?: number } };
      if (e?.name === 'NotFound' || e?.name === 'NoSuchKey' || e?.$metadata?.httpStatusCode === 404) {
        return { ...empty, present: false, registrySize, registryHash };
      }
      return {
        ...empty, registrySize, registryHash,
        error: err instanceof Error ? err.message : 'HEAD failed',
      };
    }
  } catch (err) {
    return { ...empty, error: err instanceof Error ? err.message : 'Failed to verify' };
  }
}

// ---------------------------------------------------------------------------
// Bucket LIST (Phase 2, audit C4) — the honest "what's actually in the bucket"
// ---------------------------------------------------------------------------

export interface BucketObject {
  key: string;
  size: number;
  etag: string | null;
  lastModified: string | null;
  registered: boolean;
}

export interface BucketListing {
  objects: BucketObject[];
  nextToken: string | null;
  error?: string;
}

// List the actual bucket under a prefix (the web tier's first LIST capability).
// Cross-marks which listed keys are registered in storage_objects, exposing the
// registry's blind corners — tiles / rgb / sed / out-of-band writes that sit in
// the bucket with no row. Admin-only; read creds already configured.
export async function listBucketObjects(
  backend: DataBackend,
  prefix: string,
  continuationToken?: string,
): Promise<BucketListing> {
  try {
    const supabase = await requireAdmin();

    const res = await getS3ClientForBackend(backend).send(
      new ListObjectsV2Command({
        Bucket: getBucketNameForBackend(backend),
        Prefix: prefix,
        MaxKeys: 100,
        ContinuationToken: continuationToken,
      }),
    );

    const contents = res.Contents ?? [];
    const keys = contents.map((c) => c.Key).filter(Boolean) as string[];

    // Which of these keys are registered (on this backend)?
    const registered = new Set<string>();
    if (keys.length) {
      const { data: rows } = await supabase
        .from('storage_objects')
        .select('storage_key')
        .eq('backend', backend)
        .in('storage_key', keys);
      for (const r of rows ?? []) registered.add(r.storage_key as string);
    }

    return {
      objects: contents.map((c) => ({
        key: c.Key as string,
        size: Number(c.Size ?? 0),
        etag: c.ETag ?? null,
        lastModified: c.LastModified ? c.LastModified.toISOString() : null,
        registered: registered.has(c.Key as string),
      })),
      nextToken: res.NextContinuationToken ?? null,
    };
  } catch (err) {
    return {
      objects: [], nextToken: null,
      error: err instanceof Error ? err.message : 'Failed to list bucket',
    };
  }
}
