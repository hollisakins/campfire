import { describe, expect, it } from 'vitest';
import type { DashboardSummary } from '../../../lib/actions/admin-dashboard';
import { buildDecisionItems } from '../../../lib/admin/decisions';

type SummaryOverrides = {
  [K in keyof DashboardSummary]?: Partial<DashboardSummary[K]>;
};

function summary(overrides: SummaryOverrides = {}): DashboardSummary {
  const base: DashboardSummary = {
    deployments: {
      drafts: 0,
      oldest_draft_at: null,
      deploys_7d: 0,
      unreleased_published: 0,
      missing_provenance: 0,
      distinct_crds_contexts: 0,
      latest_deploy_at: null,
    },
    review: {
      nircam_total: 0,
      nircam_pending: 0,
      nircam_done: 0,
      nircam_needs_correction: 0,
      rate_total: 0,
      rate_pending: 0,
      rate_done: 0,
      nods_total: 0,
      nods_pending: 0,
      nods_done: 0,
      published_fields_with_pending: 0,
      published_obs_with_pending: 0,
    },
    storage: {
      reclaimable_bytes: 0,
      reclaimable_count: 0,
      provisional_hashes: 0,
      pushed_undeployed_14d: 0,
      registered_7d: 0,
      bytes_added_7d: 0,
    },
    access: {
      pending_requests: 0,
      oldest_request_at: null,
      open_invites: 0,
      stale_invites: 0,
      active_share_links: 0,
      links_exposing_drafts: 0,
      active_codes: 0,
      codes_all_programs: 0,
      codes_expiring_soon: 0,
    },
    users: {
      total: 0,
      admins: 0,
      inspectors: 0,
      group_accounts: 0,
      signups_30d: 0,
      unprovisioned_30d: 0,
      recent_signups: [],
    },
    objects: { inactive: 0, stale: 0, uninspected_published: 0 },
    activity: { inspections_7d: 0, comments_7d: 0, active_inspectors_7d: 0 },
    scopes: {
      config_never_pushed: 0,
      retired_with_live_deployment: 0,
      never_deployed: 0,
      new_scopes: [],
    },
  };

  return {
    ...base,
    ...overrides,
    deployments: { ...base.deployments, ...overrides.deployments },
    review: { ...base.review, ...overrides.review },
    storage: { ...base.storage, ...overrides.storage },
    access: { ...base.access, ...overrides.access },
    users: { ...base.users, ...overrides.users },
    objects: { ...base.objects, ...overrides.objects },
    activity: { ...base.activity, ...overrides.activity },
    scopes: { ...base.scopes, ...overrides.scopes },
  };
}

describe('buildDecisionItems', () => {
  it('does not turn intentional or informational states into decisions', () => {
    const result = buildDecisionItems(summary({
      deployments: {
        drafts: 4,
        oldest_draft_at: '2026-01-01T00:00:00Z',
        unreleased_published: 2,
        distinct_crds_contexts: 3,
      },
      storage: { pushed_undeployed_14d: 42, provisional_hashes: 7 },
      access: { links_exposing_drafts: 5, codes_all_programs: 2, stale_invites: 3 },
      scopes: { config_never_pushed: 8 },
    }));

    expect(result).toEqual([]);
  });

  it('keeps publication and review state out of administrative decisions', () => {
    const result = buildDecisionItems(summary({
      review: { published_fields_with_pending: 2, published_obs_with_pending: 3 },
      access: { pending_requests: 2, oldest_request_at: '2026-08-27T00:00:00Z' },
      users: { unprovisioned_30d: 1 },
      scopes: { retired_with_live_deployment: 1 },
    }));

    expect(result.map((item) => item.id)).toEqual([
      'access-requests',
      'unprovisioned-users',
    ]);
  });
});
