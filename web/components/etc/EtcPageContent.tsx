'use client';

import { Suspense } from 'react';
import { LoadingState } from '@/components/ui/LoadingState';
import { ErrorState } from '@/components/ui/ErrorState';
import { useEtcModel } from '@/lib/etc/useEtcModel';
import { EtcCalculator } from './EtcCalculator';
import { EtcReport } from './EtcReport';

/**
 * Hidden empirical NIRSpec/MSA exposure-time calculator (/nirspec/etc). Not
 * linked from the navigation on purpose; share the URL directly. The model
 * file is fetched from /etc/models (a verbatim copy of the campfire-etc
 * package's bundled model), so a rebuilt model is a `npm run etc-models`
 * away and the page needs no edits.
 */
export function EtcPageContent() {
  const { data: model, isPending, error } = useEtcModel();
  return (
    <div className="container mx-auto px-4 py-8 max-w-7xl">
      <header className="mb-6">
        <div className="text-[11px] font-mono uppercase tracking-wider text-text-tertiary">
          empirical exposure-time calculator · NIRSpec / MSA
          {model ? ` · model ${model.version}` : ''}
        </div>
        <h1 className="text-3xl font-bold text-text-primary mt-1 mb-2">What will this observation actually deliver?</h1>
        <p className="text-base text-text-secondary max-w-prose">
          Noise scalings measured from {model ? model.archive.n_spectra.toLocaleString('en-US') : 'the'} CAMPFIRE archive spectra, with the two things the
          official ETC leaves out — how much of a real galaxy&apos;s light reaches the extracted spectrum, and where sources land in their shutters — built in.
          Fill the four boxes on the left; everything under <em>Assumptions</em> has an archive-based default.
        </p>
      </header>
      {error ? (
        <ErrorState message={`Could not load the noise model: ${error.message}`} />
      ) : isPending || !model ? (
        <LoadingState label="Loading the noise model…" />
      ) : (
        <>
          <Suspense fallback={<LoadingState label="Loading…" />}>
            <EtcCalculator model={model} />
          </Suspense>
          <EtcReport model={model} />
          <p className="mt-10 pt-4 border-t border-border text-xs text-text-tertiary">
            Model {model.version}, built {model.built}. {model.notes}
          </p>
        </>
      )}
    </div>
  );
}
