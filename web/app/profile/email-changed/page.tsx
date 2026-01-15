'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { createClient } from '@/lib/supabase/client';
import { CheckCircle, AlertCircle, Loader2 } from 'lucide-react';

export default function EmailChangedPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [success, setSuccess] = useState(false);
  const [newEmail, setNewEmail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const verifyEmailChange = async () => {
      // Check if there's a code parameter (from Supabase redirect)
      const urlParams = new URLSearchParams(window.location.search);
      const code = urlParams.get('code');

      if (code) {
        const supabase = createClient();

        // Check if we have a valid session (Supabase should have set it)
        const { data: { session }, error: sessionError } = await supabase.auth.getSession();

        if (sessionError || !session) {
          console.error('Session error:', sessionError);
          setError('Invalid or expired email change link. Please try again.');
          setLoading(false);
          return;
        }

        // Email change successful - session exists
        setSuccess(true);
        setNewEmail(session.user.email || null);
        setLoading(false);

        // Clear the code from URL
        window.history.replaceState(null, '', window.location.pathname);
      } else {
        // No code in URL - user navigated directly
        setError('Invalid email change link. Please use the link from your email.');
        setLoading(false);
      }
    };

    verifyEmailChange();
  }, []);

  return (
    <div className="min-h-screen bg-background dark:bg-slate-900">
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Profile', href: '/profile' },
            { label: 'Email Changed' },
          ]}
          className="mb-6"
        />

        <div className="flex items-center justify-center">
          <Card className="w-full max-w-md p-8">
            {loading ? (
              <div className="flex flex-col items-center justify-center py-8">
                <Loader2 className="w-12 h-12 animate-spin text-primary mb-4" />
                <p className="text-text-secondary text-lg">Verifying email change...</p>
              </div>
            ) : success ? (
              <div className="text-center space-y-4">
                <div className="flex justify-center">
                  <div className="rounded-full bg-green-100 dark:bg-green-900/30 p-3">
                    <CheckCircle className="w-12 h-12 text-green-600 dark:text-green-400" />
                  </div>
                </div>

                <h2 className="text-2xl font-bold text-text-primary">
                  Email Changed Successfully
                </h2>

                {newEmail && (
                  <p className="text-text-secondary">
                    Your email address has been updated to:
                    <br />
                    <span className="font-medium text-text-primary">{newEmail}</span>
                  </p>
                )}

                <p className="text-sm text-text-secondary">
                  You can now use your new email address to sign in to your account.
                </p>

                <Button
                  variant="primary"
                  className="w-full mt-6"
                  onClick={() => router.push('/profile')}
                >
                  Return to Profile
                </Button>
              </div>
            ) : (
              <div className="text-center space-y-4">
                <div className="flex justify-center">
                  <div className="rounded-full bg-red-100 dark:bg-red-900/30 p-3">
                    <AlertCircle className="w-12 h-12 text-red-600 dark:text-red-400" />
                  </div>
                </div>

                <h2 className="text-2xl font-bold text-text-primary">
                  Email Change Failed
                </h2>

                <p className="text-text-secondary">{error}</p>

                <div className="space-y-3">
                  <Button
                    variant="primary"
                    className="w-full"
                    onClick={() => router.push('/profile/change-email')}
                  >
                    Try Again
                  </Button>

                  <Link
                    href="/profile"
                    className="block text-sm text-text-secondary hover:text-text-primary"
                  >
                    Return to Profile
                  </Link>
                </div>
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
