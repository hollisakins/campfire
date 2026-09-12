import type { DashboardSummary } from '../actions/admin-dashboard';
import { ageDays } from './format';

export type DecisionTone = 'action';

export interface DecisionItem {
  id: string;
  tone: DecisionTone;
  label: string;
  detail: string;
  count: number;
  href: string;
}

export function buildDecisionItems(summary: DashboardSummary): DecisionItem[] {
  const items: DecisionItem[] = [];

  if (summary.access.pending_requests > 0) {
    const age = summary.access.oldest_request_at
      ? ` Oldest request: ${ageDays(summary.access.oldest_request_at)}d.`
      : '';
    items.push({
      id: 'access-requests',
      tone: 'action',
      label: 'Inspection access is waiting for approval',
      detail: `A person is waiting on an admin decision.${age}`,
      count: summary.access.pending_requests,
      href: '/admin/inspection-requests',
    });
  }

  if (summary.users.unprovisioned_30d > 0) {
    items.push({
      id: 'unprovisioned-users',
      tone: 'action',
      label: 'Recent users have no program access',
      detail: 'Confirm whether these accounts still need to be provisioned.',
      count: summary.users.unprovisioned_30d,
      href: '/admin/users',
    });
  }

  return items;
}
