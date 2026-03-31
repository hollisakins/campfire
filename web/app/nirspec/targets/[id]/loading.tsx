import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Card } from '@/components/ui/Card';

/**
 * Loading skeleton for the spectrum detail page.
 * Shows immediately while server components fetch data.
 */
export default function SpectrumDetailLoading() {
  return (
    <div className="container mx-auto px-4 py-8 animate-pulse">
      {/* Breadcrumbs skeleton */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-4">
          <Breadcrumbs
            items={[
              { label: 'CAMPFIRE', href: '/' },
              { label: 'NIRSpec', href: '/nirspec' },
              { label: '...' },
            ]}
          />
        </div>
        {/* Pagination skeleton */}
        <div className="flex items-center space-x-4">
          <div className="w-8 h-8 bg-gray-200 dark:bg-slate-700 rounded" />
          <div className="w-16 h-5 bg-gray-200 dark:bg-slate-700 rounded" />
          <div className="w-8 h-8 bg-gray-200 dark:bg-slate-700 rounded" />
        </div>
      </div>

      {/* Header and RGB Image skeleton */}
      <div className="flex gap-6 items-start mb-6">
        {/* Left Column */}
        <div className="flex-1" style={{ minHeight: '350px' }}>
          {/* Title skeleton */}
          <div className="mb-4">
            <div className="h-9 bg-gray-200 dark:bg-slate-700 rounded w-80 mb-2" />
            <div className="h-4 bg-gray-200 dark:bg-slate-700 rounded w-48 mb-3" />
            <div className="h-4 bg-gray-200 dark:bg-slate-700 rounded w-64" />
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

          {/* Tab navigation skeleton */}
          <div className="flex gap-2">
            {[1, 2, 3, 4, 5].map((i) => (
              <div key={i} className="h-10 bg-gray-200 dark:bg-slate-700 rounded w-20" />
            ))}
          </div>
        </div>

        {/* Right Column: RGB Image skeleton */}
        <div className="flex-shrink-0" style={{ width: '300px' }}>
          <div className="w-[300px] h-[300px] bg-gray-200 dark:bg-slate-700 rounded-lg" />
        </div>
      </div>

      {/* Tab content skeleton */}
      <div className="mt-6">
        <Card className="p-6">
          <div className="h-64 bg-gray-200 dark:bg-slate-700 rounded" />
        </Card>
      </div>
    </div>
  );
}
