'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { useAuth } from '@/lib/contexts/AuthContext';
import { AlertCircle, CheckCircle, Loader2, ArrowLeft, Mail } from 'lucide-react';

export const ChangeEmailForm: React.FC = () => {
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();
  const [currentEmail, setCurrentEmail] = useState('');
  const [newEmail, setNewEmail] = useState('');
  const [confirmEmail, setConfirmEmail] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (user?.email) {
      setCurrentEmail(user.email);
    }
  }, [user]);

  // Redirect if not authenticated
  useEffect(() => {
    if (!authLoading && !user) {
      router.push('/login');
    }
  }, [authLoading, user, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    // Validate emails match
    if (newEmail !== confirmEmail) {
      setError('Email addresses do not match');
      return;
    }

    // Validate email is different
    if (newEmail.toLowerCase() === currentEmail.toLowerCase()) {
      setError('This is already your current email address');
      return;
    }

    // Validate email format
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!emailRegex.test(newEmail)) {
      setError('Please enter a valid email address');
      return;
    }

    setLoading(true);

    try {
      const response = await fetch('/api/profile/change-email', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          newEmail: newEmail.toLowerCase(),
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to initiate email change');
      }

      setSuccess(true);
    } catch (err) {
      console.error('Email change error:', err);
      setError(err instanceof Error ? err.message : 'An unexpected error occurred. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  // Show loading state while checking auth
  if (authLoading) {
    return (
      <Card className="w-full max-w-md p-8">
        <div className="flex flex-col items-center justify-center py-8">
          <Loader2 className="w-12 h-12 animate-spin text-primary mb-4" />
          <p className="text-text-secondary text-lg">Loading...</p>
        </div>
      </Card>
    );
  }

  return (
    <Card className="w-full max-w-md p-8">
      <div className="mb-6">
        <Link
          href="/profile"
          className="inline-flex items-center gap-2 text-sm text-text-secondary hover:text-text-primary transition-colors mb-4"
        >
          <ArrowLeft className="w-4 h-4" />
          Back to Profile
        </Link>
        <h2 className="text-2xl font-bold text-text-primary text-center">
          Change Email Address
        </h2>
        <p className="text-text-secondary text-center mt-2">
          Update the email address associated with your account
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
                Verification emails sent
              </p>
              <p className="text-sm text-green-700 dark:text-green-300 mb-2">
                We&apos;ve sent confirmation emails to both your current and new email addresses:
              </p>
              <ul className="text-sm text-green-700 dark:text-green-300 list-disc list-inside space-y-1 mb-2">
                <li>Current: {currentEmail}</li>
                <li>New: {newEmail}</li>
              </ul>
              <p className="text-sm text-green-700 dark:text-green-300">
                Click the confirmation link in your <strong>new email address</strong> to complete the change.
                Your current email will remain active until you confirm the new one.
              </p>
            </div>
          </div>

          <div className="text-center space-y-3">
            <Button
              variant="primary"
              className="w-full"
              onClick={() => router.push('/profile')}
            >
              Return to Profile
            </Button>

            <button
              onClick={() => {
                setSuccess(false);
                setNewEmail('');
                setConfirmEmail('');
              }}
              className="text-sm text-text-secondary hover:text-text-primary"
            >
              Change to a different email
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="currentEmail" className="block text-sm font-medium text-text-primary mb-2">
              Current Email Address
            </label>
            <div className="relative">
              <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-text-secondary" />
              <input
                id="currentEmail"
                type="email"
                value={currentEmail}
                disabled
                className="w-full pl-10 pr-4 py-2 bg-background-hover text-text-secondary border border-border rounded-lg cursor-not-allowed"
              />
            </div>
          </div>

          <div>
            <label htmlFor="newEmail" className="block text-sm font-medium text-text-primary mb-2">
              New Email Address
            </label>
            <input
              id="newEmail"
              type="email"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              className="w-full px-4 py-2 bg-background text-text-primary border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary dark:bg-slate-900"
              placeholder="your.new.email@example.com"
              required
              disabled={loading}
            />
          </div>

          <div>
            <label htmlFor="confirmEmail" className="block text-sm font-medium text-text-primary mb-2">
              Confirm New Email Address
            </label>
            <input
              id="confirmEmail"
              type="email"
              value={confirmEmail}
              onChange={(e) => setConfirmEmail(e.target.value)}
              className="w-full px-4 py-2 bg-background text-text-primary border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary dark:bg-slate-900"
              placeholder="your.new.email@example.com"
              required
              disabled={loading}
            />
          </div>

          <div className="pt-2">
            <Button
              type="submit"
              variant="primary"
              className="w-full"
              disabled={loading}
            >
              {loading ? 'Sending Verification Emails...' : 'Change Email Address'}
            </Button>
          </div>

          <div className="text-center mt-4">
            <Link
              href="/profile"
              className="text-sm text-text-secondary hover:text-text-primary"
            >
              Cancel and return to profile
            </Link>
          </div>
        </form>
      )}
    </Card>
  );
};
