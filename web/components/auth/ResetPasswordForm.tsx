'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { createClient } from '@/lib/supabase/client';
import { parseAuthLink, settleAuthLink, type LinkInvalidReason } from '@/lib/auth/auth-link';
import { AlertCircle, CheckCircle, Loader2, Eye, EyeOff } from 'lucide-react';

type LinkState = { status: 'pending' } | { status: 'ready' } | { status: 'invalid'; reason: LinkInvalidReason };

const INVALID_COPY: Record<LinkInvalidReason, { title: string; body: string }> = {
  expired: { title: 'Link expired', body: 'This link has already been used or has expired.' },
  rejected: { title: 'Invalid link', body: 'This link is not valid.' },
  wrong_type: { title: 'Invalid link', body: 'This link is not a password reset link.' },
  missing: { title: 'Invalid link', body: 'This link is not valid.' },
};

export const ResetPasswordForm: React.FC = () => {
  const router = useRouter();
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [loading, setLoading] = useState(false);
  const [link, setLink] = useState<LinkState>({ status: 'pending' });
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);

  // Acquire the recovery session from the auth client's events, not from a
  // snapshot: the PKCE `?code=` exchange runs asynchronously inside the
  // client (and strips `code` from the URL when it succeeds), so a one-off
  // `getSession()` races it in both directions and misreports a valid,
  // single-use link as expired (#641). The implicit `#access_token=` branch
  // (recovery type required) and GoTrue's `error_code=otp_expired` landing
  // are handled inside `settleAuthLink`.
  useEffect(() => {
    const supabase = createClient();
    return settleAuthLink(supabase.auth, parseAuthLink(window.location.href), {
      expectType: 'recovery',
      readHref: () => window.location.href,
      onSettled: (outcome) => {
        if (outcome.status === 'ready') {
          setLink({ status: 'ready' });
          window.history.replaceState(null, '', window.location.pathname);
        } else {
          setLink(outcome);
        }
      },
    });
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    // Validate password match
    if (password !== confirmPassword) {
      setError('Passwords do not match');
      return;
    }

    // Validate password length
    if (password.length < 8) {
      setError('Password must be at least 8 characters long');
      return;
    }

    setLoading(true);

    try {
      const supabase = createClient();

      // Update the user's password
      const { data, error: updateError } = await supabase.auth.updateUser({
        password: password,
      });

      if (updateError) {
        setError(updateError.message);
        setLoading(false);
        return;
      }

      // Log the password reset
      if (data.user) {
        try {
          await fetch('/api/auth/log-password-reset', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({
              userId: data.user.id,
            }),
          });
        } catch (logError) {
          // Non-critical error - don't fail the reset
          console.error('Failed to log password reset:', logError);
        }
      }

      setSuccess(true);
      setLoading(false);

      // Redirect to login after 3 seconds
      setTimeout(() => {
        router.push('/login?message=Password reset successful. Please sign in with your new password.');
      }, 3000);
    } catch (err) {
      console.error('Password reset error:', err);
      setError('An unexpected error occurred. Please try again.');
      setLoading(false);
    }
  };

  // Pending: the client is still exchanging the link; never an error here.
  if (link.status === 'pending') {
    return (
      <Card className="w-full max-w-md p-8">
        <div className="flex flex-col items-center justify-center py-8">
          <Loader2 className="w-12 h-12 animate-spin text-primary mb-4" />
          <p className="text-text-secondary text-lg">Checking link…</p>
        </div>
      </Card>
    );
  }

  // Invalid: the link was used, expired or refused. Re-clicking cannot work,
  // so the primary action requests a new email.
  if (link.status === 'invalid') {
    const copy = INVALID_COPY[link.reason];
    return (
      <Card className="w-full max-w-md p-8">
        <h2 className="text-2xl font-bold text-text-primary mb-6 text-center">
          {copy.title}
        </h2>

        <div className="mb-6 p-3 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg flex items-start gap-2">
          <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
          <p className="text-sm text-red-800 dark:text-red-200">{copy.body}</p>
        </div>

        <div className="space-y-3">
          <Button
            variant="primary"
            className="w-full"
            onClick={() => router.push('/forgot-password')}
          >
            Request a new link
          </Button>

          <Button
            variant="secondary"
            className="w-full"
            onClick={() => router.push('/login')}
          >
            Sign in
          </Button>
        </div>
      </Card>
    );
  }

  // Show success state
  if (success) {
    return (
      <Card className="w-full max-w-md p-8">
        <div className="text-center space-y-4">
          <div className="flex justify-center">
            <div className="rounded-full bg-green-100 dark:bg-green-900/30 p-3">
              <CheckCircle className="w-12 h-12 text-green-600 dark:text-green-400" />
            </div>
          </div>

          <h2 className="text-2xl font-bold text-text-primary">
            Password Reset Successful
          </h2>

          <p className="text-text-secondary">
            Your password has been successfully reset. You will be redirected to the sign in page shortly.
          </p>

          <Button
            variant="primary"
            className="w-full mt-6"
            onClick={() => router.push('/login')}
          >
            Continue to Sign In
          </Button>
        </div>
      </Card>
    );
  }

  // Show password reset form
  return (
    <Card className="w-full max-w-md p-8">
      <h2 className="text-2xl font-bold text-text-primary mb-6 text-center">
        Set New Password
      </h2>

      {error && (
        <div className="mb-4 p-3 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg flex items-start gap-2">
          <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
          <p className="text-sm text-red-800 dark:text-red-200">{error}</p>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="password" className="block text-sm font-medium text-text-primary mb-2">
            New Password
          </label>
          <div className="relative">
            <input
              id="password"
              type={showPassword ? 'text' : 'password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-4 py-2 pr-10 bg-background text-text-primary placeholder:text-text-tertiary border border-border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
              placeholder="Enter new password"
              required
              minLength={8}
              disabled={loading}
            />
            <button
              type="button"
              onClick={() => setShowPassword(!showPassword)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-text-tertiary hover:text-text-primary"
            >
              {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
            </button>
          </div>
          <p className="mt-1 text-xs text-text-secondary">
            Must be at least 8 characters long
          </p>
        </div>

        <div>
          <label htmlFor="confirmPassword" className="block text-sm font-medium text-text-primary mb-2">
            Confirm New Password
          </label>
          <div className="relative">
            <input
              id="confirmPassword"
              type={showConfirmPassword ? 'text' : 'password'}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="w-full px-4 py-2 pr-10 bg-background text-text-primary placeholder:text-text-tertiary border border-border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
              placeholder="Confirm new password"
              required
              minLength={8}
              disabled={loading}
            />
            <button
              type="button"
              onClick={() => setShowConfirmPassword(!showConfirmPassword)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-text-tertiary hover:text-text-primary"
            >
              {showConfirmPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
            </button>
          </div>
        </div>

        <Button
          type="submit"
          variant="primary"
          className="w-full mt-6"
          disabled={loading}
        >
          {loading ? 'Resetting Password...' : 'Reset Password'}
        </Button>

        <div className="text-center mt-4">
          <Link
            href="/login"
            className="text-sm text-text-secondary hover:text-text-primary"
          >
            Remember your password? <span className="text-primary font-medium">Sign in</span>
          </Link>
        </div>
      </form>
    </Card>
  );
};
