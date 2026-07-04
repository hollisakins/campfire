// Admin list sort-key whitelists — the single source of truth shared by each
// page's useTableUrlState (client) and the backing RPC's ORDER BY validation.
//
// These MUST live outside the 'use server' action modules: a "use server" file
// may only export async functions, so exporting these const arrays from
// deployments.ts / storage-registry.ts / nircam-exposures.ts throws at runtime
// ("A 'use server' file can only export async functions, found object").

export const DEPLOYMENT_SORT_KEYS = ['id', 'deployed_at', 'status', 'scope'] as const;

export const DEPLOY_EVENT_SORT_KEYS = ['occurred_at', 'action'] as const;

export const STORAGE_OBJECT_SORT_KEYS = [
  'created_at', 'size_bytes', 'product_type', 'storage_key', 'observation', 'field', 'status',
] as const;

export const EXPOSURE_SORT_KEYS = [
  'filename', 'field', 'filter', 'detector', 'stage', 'review_status', 'date_obs', 'updated_at',
] as const;
