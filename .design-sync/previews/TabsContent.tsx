import { Tabs, TabsList, TabsTrigger, TabsContent } from 'campfire-web';

// TabsContent only renders when its value matches the active tab.
export function InContext() {
  return (
    <Tabs defaultValue="metadata" className="max-w-xl">
      <TabsList>
        <TabsTrigger value="spectrum">Spectrum</TabsTrigger>
        <TabsTrigger value="metadata">Metadata</TabsTrigger>
      </TabsList>
      <TabsContent value="spectrum" className="p-4 text-sm text-text-primary">
        Hidden panel (inactive).
      </TabsContent>
      <TabsContent value="metadata" className="p-4 text-sm text-text-primary">
        Active panel: program 6585 · NIRSpec PRISM · t_exp = 2.9 ks.
      </TabsContent>
    </Tabs>
  );
}
