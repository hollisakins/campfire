'use client';

import React from 'react';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { GRATINGS, Spectrum } from '@/lib/types';
import { GratingDetails } from './GratingDetails';
import { InspectionPanel } from './InspectionPanel';
import { SpectrumPlot } from './SpectrumPlot';

interface InspectionData {
  redshift_auto: number | null;
  redshift_inspected: number | null;
  redshift_quality: number;
  spectral_features: number;
  object_flags: number;
  dq_flags: number;
  last_inspected_at: string | null;
  last_inspected_by: string | null;
}

interface SpectrumTabsProps {
  spectra: Spectrum[];
  objectId: string;         // String object_id for display
  objectDbId: number;       // Numeric database ID for API calls
  inspectionData: InspectionData;
}

export const SpectrumTabs: React.FC<SpectrumTabsProps> = ({ spectra, objectId, objectDbId, inspectionData }) => {
  // Sort spectra by grating order
  const gratingOrder: readonly string[] = GRATINGS;
  const sortedSpectra = [...spectra].sort(
    (a, b) =>
      gratingOrder.indexOf(a.grating) - gratingOrder.indexOf(b.grating)
  );

  const firstGrating = sortedSpectra[0]?.grating || 'prism';

  return (
    <Tabs defaultValue={firstGrating.toLowerCase()} className="mt-8">
      <TabsList>
        {sortedSpectra.map((spectrum) => (
          <TabsTrigger
            key={spectrum.grating}
            value={spectrum.grating.toLowerCase()}
          >
            {spectrum.grating}
          </TabsTrigger>
        ))}
        <TabsTrigger value="redshift">REDSHIFT</TabsTrigger>
        <TabsTrigger value="photometry">PHOTOMETRY</TabsTrigger>
        <TabsTrigger value="inspect">INSPECT</TabsTrigger>
        <TabsTrigger value="context">CONTEXT</TabsTrigger>
      </TabsList>

      {/* Spectroscopy Tabs */}
      {sortedSpectra.map((spectrum) => (
        <TabsContent
          key={spectrum.grating}
          value={spectrum.grating.toLowerCase()}
          className="mt-6"
        >
          <GratingDetails spectrum={spectrum} />

          {/* Interactive spectrum plot */}
          <SpectrumPlot
            fitsPath={spectrum.fits_path}
            grating={spectrum.grating}
            initialRedshift={inspectionData.redshift_inspected ?? inspectionData.redshift_auto}
          />
        </TabsContent>
      ))}

      {/* Redshift Tab */}
      <TabsContent value="redshift" className="mt-6">
        <div className="bg-card border border-border rounded-lg p-12 text-center">
          <p className="text-text-secondary">
            Redshift information and spectral features will appear here
          </p>
        </div>
      </TabsContent>

      {/* Photometry Tab */}
      <TabsContent value="photometry" className="mt-6">
        <div className="bg-card border border-border rounded-lg p-12 text-center">
          <p className="text-text-secondary">
            Multi-wavelength photometry will appear here
          </p>
        </div>
      </TabsContent>

      {/* Inspect Tab */}
      <TabsContent value="inspect" className="mt-6">
        <InspectionPanel
          objectDbId={objectDbId}
          objectId={objectId}
          initialData={inspectionData}
        />
      </TabsContent>

      {/* Context Tab */}
      <TabsContent value="context" className="mt-6">
        <div className="bg-card border border-border rounded-lg p-12 text-center">
          <p className="text-text-secondary">
            Context images and finding charts will appear here
          </p>
        </div>
      </TabsContent>
    </Tabs>
  );
};
