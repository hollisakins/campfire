import { Tabs, TabsList, TabsTrigger, TabsContent } from 'campfire-web';

function TabsDemo() {
  return (
    <Tabs defaultValue="spectrum" className="max-w-xl">
      <TabsList>
        <TabsTrigger value="spectrum">Spectrum</TabsTrigger>
        <TabsTrigger value="photometry">Photometry</TabsTrigger>
        <TabsTrigger value="metadata">Metadata</TabsTrigger>
      </TabsList>
      <TabsContent value="spectrum" className="p-4 text-sm text-text-primary">
        1D extracted spectrum with continuum and detected emission lines.
      </TabsContent>
      <TabsContent value="photometry" className="p-4 text-sm text-text-primary">
        Aperture photometry across the available broadband filters.
      </TabsContent>
      <TabsContent value="metadata" className="p-4 text-sm text-text-primary">
        Observation program, grating, exposure time, and reduction provenance.
      </TabsContent>
    </Tabs>
  );
}

// Tabs and its parts are one compound unit — every card shows the full
// composition since the parts throw outside a <Tabs> parent.
export const Default = TabsDemo;
