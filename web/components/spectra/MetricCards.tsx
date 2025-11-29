import React from 'react';
import { Badge } from '@/components/ui/Badge';
import { QUALITY_LABELS } from '@/lib/types';

interface MetricCardsProps {
  maxSnr: number | null;
  redshift: number | null;
  redshiftQuality: number;
  numGratings: number;
}

export const MetricCards: React.FC<MetricCardsProps> = ({
  maxSnr,
  redshift,
  redshiftQuality,
  numGratings,
}) => {
  const qualityLabel = QUALITY_LABELS.find(q => q.value === redshiftQuality);

  return (
    <div className="flex gap-3">
      <Badge
        value={maxSnr ? maxSnr.toFixed(1) : 'N/A'}
        label="MAX S/N"
        compact
      />
      <Badge
        value={redshift ? redshift.toFixed(2) : 'N/A'}
        label="REDSHIFT"
        compact
      />
      <Badge
        value={qualityLabel?.label || 'Unknown'}
        label="QUALITY"
        compact
      />
      <Badge
        value={numGratings.toString()}
        label="GRATINGS"
        compact
      />
    </div>
  );
};
