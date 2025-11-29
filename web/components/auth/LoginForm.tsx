'use client';

import React, { useState, useEffect } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { useAuth } from '@/lib/contexts/AuthContext';
import { createClient } from '@/lib/supabase/client';
import { AlertCircle, CheckCircle, Loader2, Lock } from 'lucide-react';

export const LoginForm: React.FC = () => {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { signIn, signUp, needsProfileSetup, user, loading: authLoading } = useAuth();
  const [mode, setMode] = useState<'signin' | 'signup'>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
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
            router.push('/spectra');
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
        router.push('/spectra');
      }
    }
  }, [authLoading, processingCallback, user, needsProfileSetup, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccess(null);
    setLoading(true);

    try {
      if (mode === 'signin') {
        const { error } = await signIn(email, password);
        if (error) {
          setError(error.message);
        }
        // Don't redirect here - let the useEffect handle it based on profile status
      } else {
        if (password.length < 6) {
          setError('Password must be at least 6 characters');
          setLoading(false);
          return;
        }

        const { error } = await signUp(email, password, fullName);
        if (error) {
          setError(error.message);
        } else {
          setSuccess('Account created! Please check your email to verify your account, then sign in.');
          setMode('signin');
          setPassword('');
        }
      }
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
        {mode === 'signin' ? 'Sign In to CAMPFIRE' : 'Create Account'}
      </h2>

      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg flex items-start gap-2">
          <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
          <p className="text-sm text-red-800">{error}</p>
        </div>
      )}

      {success && (
        <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded-lg flex items-start gap-2">
          <CheckCircle className="w-5 h-5 text-green-600 flex-shrink-0 mt-0.5" />
          <p className="text-sm text-green-800">{success}</p>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        {mode === 'signup' && (
          <div>
            <label htmlFor="fullName" className="block text-sm font-medium text-text-primary mb-2">
              Full Name
            </label>
            <input
              id="fullName"
              type="text"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              className="w-full px-4 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
              placeholder="Jane Doe"
              required
              disabled={loading}
            />
            <p className="text-xs text-text-secondary mt-1">
              This is your display name shown to other team members. You&apos;ll sign in using your email address.
            </p>
          </div>
        )}

        <div>
          <label htmlFor="email" className="block text-sm font-medium text-text-primary mb-2">
            Email
          </label>
          <input
            id="email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full px-4 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
            placeholder="your.email@example.com"
            required
            disabled={loading}
          />
        </div>

        <div>
          <label htmlFor="password" className="block text-sm font-medium text-text-primary mb-2">
            Password
          </label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full px-4 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
            placeholder="••••••••"
            required
            minLength={6}
            disabled={loading}
          />
          {mode === 'signup' && (
            <p className="text-xs text-text-secondary mt-1">
              Your password is encrypted and securely stored. Team administrators cannot see your password.
            </p>
          )}
        </div>

        {/* Security Note - only shown in signup mode */}
        {mode === 'signup' && (
          <div className="p-3 bg-blue-50 border border-blue-200 rounded-lg">
            <div className="flex items-start gap-2">
              <Lock className="w-4 h-4 text-blue-600 flex-shrink-0 mt-0.5" />
              <p className="text-xs text-blue-800">
                All passwords are encrypted using industry-standard security. Your credentials are never visible to administrators.
              </p>
            </div>
          </div>
        )}

        <Button type="submit" variant="primary" className="w-full mt-6" disabled={loading}>
          {loading
            ? (mode === 'signin' ? 'Signing In...' : 'Creating Account...')
            : (mode === 'signin' ? 'Sign In' : 'Create Account')
          }
        </Button>
      </form>

      <div className="mt-6 text-center">
        {mode === 'signin' ? (
          <p className="text-sm text-text-secondary">
            Don&apos;t have an account?{' '}
            <button
              type="button"
              onClick={() => { setMode('signup'); setError(null); setSuccess(null); }}
              className="text-primary hover:underline font-medium"
            >
              Sign up
            </button>
          </p>
        ) : (
          <p className="text-sm text-text-secondary">
            Already have an account?{' '}
            <button
              type="button"
              onClick={() => { setMode('signin'); setError(null); setSuccess(null); }}
              className="text-primary hover:underline font-medium"
            >
              Sign in
            </button>
          </p>
        )}
      </div>
    </Card>
  );
};
