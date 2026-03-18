'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { createClient } from '@/lib/supabase/client';
import { useAuth } from '@/lib/contexts/AuthContext';
import { AlertCircle, CheckCircle, ArrowLeft } from 'lucide-react';

export const ForgotPasswordForm: React.FC = () => {
  const { user } = useAuth();
  const [email, setEmail] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [loading, setLoading] = useState(false);

  // Pre-fill email if user is logged in
  useEffect(() => {
    if (user?.email) {
      setEmail(user.email);
    }
  }, [user]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccess(false);
    setLoading(true);

    try {
      const supabase = createClient();

      // Note: The redirect URL is configured in the Supabase email template
      // using: https://campfire.hollisakins.com/api/auth/password-reset?token={{ .TokenHash }}&type=recovery
      const { error: resetError } = await supabase.auth.resetPasswordForEmail(email);

      if (resetError) {
        setError(resetError.message);
      } else {
        // Show success message regardless of whether email exists (security best practice)
        setSuccess(true);
      }
    } catch {
      setError('An unexpected error occurred. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  const backLink = user ? '/profile' : '/login';
  const backText = user ? 'Back to Profile' : 'Back to Sign In';

  return (
    <Card className="w-full max-w-md p-8">
      <div className="mb-6">
        <Link
          href={backLink}
          className="inline-flex items-center gap-2 text-sm text-text-secondary hover:text-text-primary transition-colors mb-4"
        >
          <ArrowLeft className="w-4 h-4" />
          {backText}
        </Link>
        <h2 className="text-2xl font-bold text-text-primary text-center">
          {user ? 'Change Your Password' : 'Reset Your Password'}
        </h2>
        <p className="text-text-secondary text-center mt-2">
          {user
            ? "We'll send you a link to securely change your password."
            : "Enter your email address and we'll send you a link to reset your password."}
        </p>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg flex items-start gap-2">
          <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
          <p className="text-sm text-red-800 dark:text-red-200">{error}</p>
        </div>
      )}

      {success ? (
        <div className="space-y-4">
          <div className="p-4 bg-green-50 dark:bg-green-950/50 border border-green-200 dark:border-green-800 rounded-lg flex items-start gap-3">
            <CheckCircle className="w-5 h-5 text-green-600 dark:text-green-400 flex-shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-medium text-green-800 dark:text-green-200 mb-1">
                Check your email
              </p>
              <p className="text-sm text-green-700 dark:text-green-300">
                If an account exists with that email address, you will receive a password reset link shortly.
              </p>
            </div>
          </div>

          <Button
            variant="secondary"
            className="w-full"
            onClick={() => {
              setSuccess(false);
              setEmail('');
            }}
          >
            Send Another Reset Link
          </Button>

          <div className="text-center">
            <Link
              href={backLink}
              className="text-sm text-primary hover:underline font-medium"
            >
              {user ? 'Return to Profile' : 'Return to Sign In'}
            </Link>
          </div>
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="email" className="block text-sm font-medium text-text-primary mb-2">
              Email Address
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

          <Button
            type="submit"
            variant="primary"
            className="w-full mt-6"
            disabled={loading}
          >
            {loading ? 'Sending Reset Link...' : 'Send Reset Link'}
          </Button>

          {!user && (
            <div className="text-center mt-4">
              <Link
                href="/login"
                className="text-sm text-text-secondary hover:text-text-primary"
              >
                Remember your password? <span className="text-primary font-medium">Sign in</span>
              </Link>
            </div>
          )}
        </form>
      )}
    </Card>
  );
};
