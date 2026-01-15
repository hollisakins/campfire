import { Suspense } from 'react';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Card } from '@/components/ui/Card';
import { Loader2 } from 'lucide-react';
import { ChangeEmailForm } from '@/components/auth/ChangeEmailForm';

export default function ChangeEmailPage() {
  return (
    <div className="min-h-screen bg-background dark:bg-slate-900">
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Profile', href: '/profile' },
            { label: 'Change Email' },
          ]}
          className="mb-6"
        />

        <div className="flex items-center justify-center">
          <Suspense
            fallback={
              <Card className="w-full max-w-md p-8">
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="w-12 h-12 animate-spin text-primary" />
                </div>
              </Card>
            }
          >
            <ChangeEmailForm />
          </Suspense>
        </div>
      </div>
    </div>
  );
}
