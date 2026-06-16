import { Tabs, TabsList, TabsTrigger, TabsContent } from 'campfire-web';

// TabsTrigger reads Tabs context — render inside a full composition.
export function InContext() {
  return (
    <Tabs defaultValue="all" className="max-w-xl">
      <TabsList>
        <TabsTrigger value="all">All spectra</TabsTrigger>
        <TabsTrigger value="flagged">Flagged</TabsTrigger>
        <TabsTrigger value="reviewed">Reviewed</TabsTrigger>
      </TabsList>
      <TabsContent value="all" className="p-4 text-sm text-text-primary">
        Each TabsTrigger is a button; the active one gets the ember underline.
      </TabsContent>
    </Tabs>
  );
}
