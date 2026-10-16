import { releaseStateBadge } from '@/lib/dr-releases';

const TONES = {
  published: 'border-current text-success',
  pending: 'border-current text-warning',
  withdrawn: 'border-border text-text-tertiary',
  failed: 'border-current text-danger',
} as const;

export function ReleaseStateBadge({ state }: { state: string }) {
  const { label, tone } = releaseStateBadge(state);
  return (
    <span
      className={`inline-flex items-center rounded-full border bg-card px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}
    >
      {label}
    </span>
  );
}
