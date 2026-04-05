'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { useAuth } from '@/lib/contexts/AuthContext';
import { createClient } from '@/lib/supabase/client';
import { AlertCircle, Loader2 } from 'lucide-react';

export const LoginForm: React.FC = () => {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { signIn, needsProfileSetup, user, loading: authLoading } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [processingCallback, setProcessingCallback] = useState(false);

  // Check for and process hash tokens (from invite/magic links)
  useEffect(() => {
    const processHashTokens = async () => {
      // Check if there are tokens in the URL hash
      const hash = window.location.hash;
      if (!hash || !hash.includes('access_token')) {
        return;
      }

      setProcessingCallback(true);

      try {
        const hashParams = new URLSearchParams(hash.substring(1));
        const accessToken = hashParams.get('access_token');
        const refreshToken = hashParams.get('refresh_token');
        const hashError = hashParams.get('error');
        const errorDescription = hashParams.get('error_description');

        if (hashError) {
          setError(errorDescription || hashError);
          setProcessingCallback(false);
          // Clear the hash
          window.history.replaceState(null, '', window.location.pathname);
          return;
        }

        if (accessToken && refreshToken) {
          const supabase = createClient();

          // Set the session with the tokens from the hash
          const { data, error: sessionError } = await supabase.auth.setSession({
            access_token: accessToken,
            refresh_token: refreshToken,
          });

          if (sessionError) {
            console.error('Session error:', sessionError);
            setError(sessionError.message);
            setProcessingCallback(false);
            return;
          }

          // Clear the hash from URL for cleaner UX
          window.history.replaceState(null, '', window.location.pathname);

          // Check if user has a profile and redirect accordingly
          if (data.user) {
            const { data: profileData, error: profileError } = await supabase
              .from('user_profiles')
              .select('id')
              .eq('user_id', data.user.id)
              .single();

            // Redirect to welcome if no profile found or any error occurred
            // This is defensive - better to show welcome page than block access
            if (!profileData || profileError) {
              router.push('/welcome');
              return;
            }

            // Profile exists - redirect to main app
            router.push('/nirspec');
            return;
          }

          // Fallback - session set but no user (shouldn't happen)
          setProcessingCallback(false);
        }
      } catch (err) {
        console.error('Error processing auth callback:', err);
        setError('Failed to process authentication. Please try again.');
        setProcessingCallback(false);
      }
    };

    processHashTokens();
  }, [router]);

  // Check for error messages in URL query params
  useEffect(() => {
    const urlError = searchParams.get('error');
    if (urlError) {
      setError(decodeURIComponent(urlError));
    }
  }, [searchParams]);

  // Redirect authenticated users
  useEffect(() => {
    if (!authLoading && !processingCallback && user) {
      if (needsProfileSetup) {
        // User is authenticated but needs to complete profile setup
        router.push('/welcome');
      } else {
        // User is fully set up, redirect to main app
        router.push('/nirspec');
      }
    }
  }, [authLoading, processingCallback, user, needsProfileSetup, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      const { error } = await signIn(email, password);
      if (error) {
        setError(error.message);
      }
      // Don't redirect here - let the useEffect handle it based on profile status
    } catch {
      setError('An unexpected error occurred');
    } finally {
      setLoading(false);
    }
  };

  // Show loading state while processing auth callback
  if (processingCallback) {
    return (
      <Card className="w-full max-w-md p-8">
        <div className="flex flex-col items-center justify-center py-8">
          <Loader2 className="w-12 h-12 animate-spin text-primary mb-4" />
          <p className="text-text-secondary text-lg">Setting up your account...</p>
        </div>
      </Card>
    );
  }

  return (
    <Card className="w-full max-w-md p-8">
      <h2 className="text-2xl font-bold text-text-primary mb-6 text-center">
        Sign In to CAMPFIRE
      </h2>

      {error && (
        <div className="mb-4 p-3 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg flex items-start gap-2">
          <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
          <p className="text-sm text-red-800 dark:text-red-200">{error}</p>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="email" className="block text-sm font-medium text-text-primary mb-2">
            Email
          </label>
          <input
            id="email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full px-4 py-2 bg-background text-text-primary border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
            placeholder="your.email@example.com"
            required
            disabled={loading}
          />
        </div>

        <div>
          <div className="flex items-center justify-between mb-2">
            <label htmlFor="password" className="block text-sm font-medium text-text-primary">
              Password
            </label>
            <Link
              href="/forgot-password"
              className="text-sm text-primary hover:underline"
            >
              Forgot password?
            </Link>
          </div>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full px-4 py-2 bg-background text-text-primary border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
            placeholder="••••••••"
            required
            minLength={6}
            disabled={loading}
          />
        </div>

        <Button type="submit" variant="primary" className="w-full mt-6" disabled={loading}>
          {loading ? 'Signing In...' : 'Sign In'}
        </Button>
      </form>

      <div className="mt-6 text-center">
        <p className="text-sm text-text-secondary">
          Don&apos;t have an account?{' '}
          <Link
            href="/request-access"
            className="text-primary hover:underline font-medium"
          >
            Request access
          </Link>
        </p>
      </div>
    </Card>
  );
};
