import { Tabs, TabsList, TabsTrigger, TabsContent } from 'campfire-web';

// TabsList/TabsTrigger read Tabs context — render the full composition.
export function InContext() {
  return (
    <Tabs defaultValue="spectrum" className="max-w-xl">
      <TabsList>
        <TabsTrigger value="spectrum">Spectrum</TabsTrigger>
        <TabsTrigger value="photometry">Photometry</TabsTrigger>
        <TabsTrigger value="metadata">Metadata</TabsTrigger>
      </TabsList>
      <TabsContent value="spectrum" className="p-4 text-sm text-text-primary">
        The TabsList renders the row of triggers with the active underline.
      </TabsContent>
    </Tabs>
  );
}
