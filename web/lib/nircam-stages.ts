// NIRCam pipeline stage display helpers. Stages match the pipeline step names
// from campfire_pipeline.common.cfp.CFP_KEYS (see web/lib/types.ts).

import { NIRCAM_STAGES, type NircamStage } from '@/lib/types';

// Phase buckets — for grouping the 16 stages into broader categories
// that are useful for at-a-glance progress display.
type Phase = 'pre' | 'early' | 'mid' | 'late' | 'combine' | 'done';

const STAGE_PHASE: Record<NircamStage, Phase> = {
  uncal:         'pre',
  detector1:     'early',
  persistence:   'early',
  wisp:          'early',
  image2:        'mid',
  edge:          'mid',
  bkg:           'mid',
  diag_striping: 'mid',
  wcs_shift:     'late',
  preview:       'late',
  jhat:          'late',
  apply_mask:    'combine',
  bad_pixel:     'combine',
  outlier:       'done',
};

const PHASE_BADGE: Record<Phase, string> = {
  pre:     'bg-gray-100 dark:bg-slate-700 text-gray-600 dark:text-slate-400',
  early:   'bg-blue-100 dark:bg-blue-950 text-blue-700 dark:text-blue-300',
  mid:     'bg-indigo-100 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-300',
  late:    'bg-purple-100 dark:bg-purple-950 text-purple-700 dark:text-purple-300',
  combine: 'bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-300',
  done:    'bg-green-100 dark:bg-green-900 text-green-700 dark:text-green-300',
};

export function stageBadgeClasses(stage: NircamStage | string): string {
  const phase = STAGE_PHASE[stage as NircamStage] ?? 'pre';
  return PHASE_BADGE[phase];
}

export type { NircamStage };
export { NIRCAM_STAGES };
