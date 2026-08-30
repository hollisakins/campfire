// ---------------------------------------------------------------------------
// ATTENTION_RULES — the dashboard's attention rail as one auditable table.
//
// Every threshold that decides whether something "needs the admin" lives here,
// not scattered through JSX. Each rule derives a count from the dashboard
// summary (plus the storage budget); a count of 0 means the rule is not
// firing and renders nothing. Severity is a fixed property of the rule, and
// the rail sorts severity-first. The STAGE tag indexes each rule into the
// lifecycle the panels below the rail follow (deploy → review → publish →
// storage → access → config).
//
// Thresholds are honest guesses at an unmeasured workflow — tune them here.
// ---------------------------------------------------------------------------

import type { DashboardSummary } from '@/lib/actions/admin-dashboard';
import type { StorageBudget } from '@/lib/actions/storage-registry';
import { ageDays, fmtBytes } from './format';

export type Severity = 'act' | 'soon' | 'info';
export type Stage = 'DEPLOY' | 'REVIEW' | 'PUBLISH' | 'STORAGE' | 'ACCESS' | 'CONFIG';

export interface AttentionInput {
  summary: DashboardSummary;
  budget: StorageBudget | null;
}

export interface AttentionRule {
  id: string;
  severity: Severity;
  stage: Stage;
  /** Deep link; '#id' scrolls to a dashboard panel. */
  href: string;
  /** Row label (count renders separately). */
  label: string;
  /** 0 = not firing. */
  value: (input: AttentionInput) => number;
  /** Optional context clause rendered after the label, muted. */
  detail?: (input: AttentionInput) => string | undefined;
}

const DRAFT_STALE_DAYS = 3;
const STORAGE_CRIT_PCT = 90;
const STORAGE_WARN_PCT = 80;
const RECLAIMABLE_MIN_BYTES = 500 * 1024 ** 3; // 500 GB

export const ATTENTION_RULES: AttentionRule[] = [
  {
    id: 'published-uninspected',
    severity: 'act',
    stage: 'PUBLISH',
    href: '#review-board',
    label: 'published scopes with uninspected exposures',
    value: ({ summary }) =>
      summary.review.published_fields_with_pending + summary.review.published_obs_with_pending,
    detail: ({ summary }) => {
      const f = summary.review.published_fields_with_pending;
      const o = summary.review.published_obs_with_pending;
      return [f > 0 ? `${f} field${f === 1 ? '' : 's'}` : null, o > 0 ? `${o} obs` : null]
        .filter(Boolean).join(' · ') || undefined;
    },
  },
  {
    id: 'stale-drafts',
    severity: 'act',
    stage: 'PUBLISH',
    href: '/admin/deployments?status=draft',
    label: `draft deployments older than ${DRAFT_STALE_DAYS}d`,
    value: ({ summary }) =>
      ageDays(summary.deployments.oldest_draft_at) > DRAFT_STALE_DAYS
        ? summary.deployments.drafts : 0,
    detail: ({ summary }) => `oldest ${ageDays(summary.deployments.oldest_draft_at)}d`,
  },
  {
    id: 'storage-crit',
    severity: 'act',
    stage: 'STORAGE',
    href: '/admin/intermediate-products',
    label: `storage at ≥${STORAGE_CRIT_PCT}% of cap`,
    value: ({ budget }) => (budget && budget.pct_used >= STORAGE_CRIT_PCT ? 1 : 0),
    detail: ({ budget }) =>
      budget ? `${fmtBytes(budget.total_bytes)} of ${fmtBytes(budget.cap_bytes)}` : undefined,
  },
  {
    id: 'links-exposing-drafts',
    severity: 'act',
    stage: 'ACCESS',
    href: '/admin/share-links',
    label: 'active share links exposing drafts',
    value: ({ summary }) => summary.access.links_exposing_drafts,
  },
  {
    id: 'nircam-pending',
    severity: 'soon',
    stage: 'REVIEW',
    href: '/admin/nircam?review=pending',
    label: 'NIRCam exposures pending review',
    value: ({ summary }) => summary.review.nircam_pending,
  },
  {
    id: 'nircam-correction',
    severity: 'soon',
    stage: 'REVIEW',
    href: '/admin/nircam?correction=needed',
    label: 'NIRCam exposures flagged needs-correction',
    value: ({ summary }) => summary.review.nircam_needs_correction,
  },
  {
    id: 'rate-pending',
    severity: 'soon',
    stage: 'REVIEW',
    href: '/admin/nirspec/rate?review=pending',
    label: 'NIRSpec rate exposures pending review',
    value: ({ summary }) => summary.review.rate_pending,
  },
  {
    id: 'nods-pending',
    severity: 'soon',
    stage: 'REVIEW',
    href: '/admin/nirspec/nods',
    label: 'NIRSpec nod exposures pending review',
    value: ({ summary }) => summary.review.nods_pending,
  },
  {
    id: 'pending-requests',
    severity: 'soon',
    stage: 'ACCESS',
    href: '/admin/inspection-requests',
    label: 'pending inspection-access requests',
    value: ({ summary }) => summary.access.pending_requests,
    detail: ({ summary }) =>
      summary.access.oldest_request_at
        ? `oldest ${ageDays(summary.access.oldest_request_at)}d`
        : undefined,
  },
  {
    id: 'unprovisioned-signups',
    severity: 'soon',
    stage: 'ACCESS',
    href: '/admin/users',
    label: 'recent signups with no program access',
    value: ({ summary }) => summary.users.unprovisioned_30d,
  },
  {
    id: 'unreleased-published',
    severity: 'soon',
    stage: 'DEPLOY',
    href: '/admin/deployments',
    label: 'published deployments on non-release cfpipe versions',
    value: ({ summary }) => summary.deployments.unreleased_published,
  },
  {
    id: 'codes-all-programs',
    severity: 'soon',
    stage: 'ACCESS',
    href: '/admin/codes',
    label: 'active codes granting all programs',
    value: ({ summary }) => summary.access.codes_all_programs,
  },
  {
    id: 'codes-expiring',
    severity: 'info',
    stage: 'ACCESS',
    href: '/admin/codes',
    label: 'access codes near expiry or exhaustion',
    value: ({ summary }) => summary.access.codes_expiring_soon,
  },
  {
    id: 'stale-invites',
    severity: 'info',
    stage: 'ACCESS',
    href: '/admin/users',
    label: 'invites unaccepted for over 14d',
    value: ({ summary }) => summary.access.stale_invites,
  },
  {
    id: 'storage-warn',
    severity: 'info',
    stage: 'STORAGE',
    href: '/admin/intermediate-products',
    label: `storage at ≥${STORAGE_WARN_PCT}% of cap`,
    value: ({ budget }) =>
      budget && budget.pct_used >= STORAGE_WARN_PCT && budget.pct_used < STORAGE_CRIT_PCT
        ? 1 : 0,
    detail: ({ budget }) => (budget ? `${budget.pct_used}%` : undefined),
  },
  {
    id: 'reclaimable',
    severity: 'info',
    stage: 'STORAGE',
    href: '/admin/intermediate-products?status=superseded',
    label: 'reclaimable bytes in superseded/revoked objects',
    value: ({ summary }) =>
      summary.storage.reclaimable_bytes > RECLAIMABLE_MIN_BYTES
        ? summary.storage.reclaimable_count : 0,
    detail: ({ summary }) => fmtBytes(summary.storage.reclaimable_bytes),
  },
  {
    id: 'pushed-undeployed',
    severity: 'info',
    stage: 'DEPLOY',
    href: '/admin/intermediate-products',
    label: 'objects pushed in 14d with no deployment attached',
    value: ({ summary }) => summary.storage.pushed_undeployed_14d,
  },
  {
    id: 'provisional-hashes',
    severity: 'info',
    stage: 'STORAGE',
    href: '/admin/intermediate-products',
    label: 'objects on provisional etag hashes',
    value: ({ summary }) => summary.storage.provisional_hashes,
  },
  {
    id: 'objects-stale',
    severity: 'info',
    stage: 'REVIEW',
    href: '#review-board',
    label: 'objects stale since inspection',
    value: ({ summary }) => summary.objects.stale,
  },
  {
    id: 'objects-inactive',
    severity: 'info',
    stage: 'REVIEW',
    href: '#review-board',
    label: 'objects deactivated by reconciliation',
    value: ({ summary }) => summary.objects.inactive,
  },
  {
    id: 'config-never-pushed',
    severity: 'info',
    stage: 'CONFIG',
    href: '#scopes-panel',
    label: 'scopes never pushed through the config plane',
    value: ({ summary }) => summary.scopes.config_never_pushed,
  },
  {
    id: 'retired-live',
    severity: 'info',
    stage: 'CONFIG',
    href: '#scopes-panel',
    label: 'retired scopes with a live deployment',
    value: ({ summary }) => summary.scopes.retired_with_live_deployment,
  },
  {
    id: 'missing-provenance',
    severity: 'info',
    stage: 'DEPLOY',
    href: '/admin/deployments',
    label: 'published deployments missing provenance',
    value: ({ summary }) => summary.deployments.missing_provenance,
  },
  {
    id: 'crds-drift',
    severity: 'info',
    stage: 'DEPLOY',
    href: '/admin/deployments',
    label: 'CRDS contexts live across published deployments',
    value: ({ summary }) =>
      summary.deployments.distinct_crds_contexts > 1
        ? summary.deployments.distinct_crds_contexts : 0,
  },
];

export interface FiringRule {
  rule: AttentionRule;
  count: number;
  detail?: string;
}

const SEVERITY_ORDER: Record<Severity, number> = { act: 0, soon: 1, info: 2 };

export function evaluateAttention(input: AttentionInput): {
  firing: FiringRule[];
  checked: number;
} {
  const firing: FiringRule[] = [];
  for (const rule of ATTENTION_RULES) {
    const count = rule.value(input);
    if (count > 0) firing.push({ rule, count, detail: rule.detail?.(input) });
  }
  firing.sort(
    (a, b) =>
      SEVERITY_ORDER[a.rule.severity] - SEVERITY_ORDER[b.rule.severity] ||
      b.count - a.count,
  );
  return { firing, checked: ATTENTION_RULES.length };
}
