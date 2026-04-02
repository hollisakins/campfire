import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Card } from '@/components/ui/Card';

/**
 * Loading skeleton for the object detail page.
 * Shows immediately while server components fetch data.
 */
export default function ObjectDetailLoading() {
  return (
    <div className="container mx-auto px-4 py-8 animate-pulse">
      {/* Breadcrumbs skeleton */}
      <div className="flex items-center gap-4 mb-6">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Objects', href: '/nirspec?view=objects' },
            { label: '...' },
          ]}
        />
      </div>

      {/* Header skeleton */}
      <div className="mb-4">
        <div className="h-9 bg-gray-200 dark:bg-slate-700 rounded w-80 mb-2" />
        <div className="h-4 bg-gray-200 dark:bg-slate-700 rounded w-48 mb-3" />
        <div className="h-4 bg-gray-200 dark:bg-slate-700 rounded w-64 mb-4" />
      </div>

      {/* Metric Cards skeleton */}
      <div className="mb-4 grid grid-cols-4 gap-4">
        {[1, 2, 3, 4].map((i) => (
          <Card key={i} className="p-4">
            <div className="h-4 bg-gray-200 dark:bg-slate-700 rounded w-16 mb-2" />
            <div className="h-6 bg-gray-200 dark:bg-slate-700 rounded w-12" />
          </Card>
        ))}
      </div>

      {/* Action buttons skeleton */}
      <div className="flex gap-4 mb-6">
        <div className="h-10 bg-gray-200 dark:bg-slate-700 rounded w-32" />
        <div className="h-10 bg-gray-200 dark:bg-slate-700 rounded w-32" />
      </div>

      {/* Member targets table skeleton */}
      <Card className="p-6 mb-6">
        <div className="h-6 bg-gray-200 dark:bg-slate-700 rounded w-48 mb-4" />
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-10 bg-gray-200 dark:bg-slate-700 rounded" />
          ))}
        </div>
      </Card>

      {/* Spectrum viewer skeleton */}
      <Card className="p-6">
        <div className="h-64 bg-gray-200 dark:bg-slate-700 rounded" />
      </Card>
    </div>
  );
}
