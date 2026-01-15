import { Suspense } from 'react';
import { ForgotPasswordForm } from '@/components/auth/ForgotPasswordForm';
import { Loader2 } from 'lucide-react';

function ForgotPasswordFormFallback() {
  return (
    <div className="flex items-center justify-center">
      <Loader2 className="w-8 h-8 animate-spin text-primary" />
    </div>
  );
}

export default function ForgotPasswordPage() {
  return (
    <div className="container mx-auto px-4 py-12">
      <div className="flex items-center justify-center min-h-[60vh]">
        <Suspense fallback={<ForgotPasswordFormFallback />}>
          <ForgotPasswordForm />
        </Suspense>
      </div>
    </div>
  );
}
