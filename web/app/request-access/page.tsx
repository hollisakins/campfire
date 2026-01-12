'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { AlertCircle, CheckCircle, Loader2, Mail, Clock } from 'lucide-react';

type RequestStatus = 'pending' | 'approved' | 'rejected';

interface RequestResult {
  success: boolean;
  status: RequestStatus;
  message: string;
  created_at?: string;
}

export default function RequestAccessPage() {
  const [email, setEmail] = useState('');
  const [fullName, setFullName] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RequestResult | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      const response = await fetch('/api/account-requests', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: email.trim(),
          full_name: fullName.trim(),
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to submit request');
      }

      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An unexpected error occurred');
    } finally {
      setLoading(false);
    }
  };

  // Show success/status view after submission
  if (result) {
    return (
      <div className="container mx-auto px-4 py-12">
        <div className="flex items-center justify-center min-h-[60vh]">
          <Card className="w-full max-w-md p-8">
            {result.status === 'pending' && (
              <>
                <div className="flex items-center justify-center mb-6">
                  <div className="w-16 h-16 rounded-full bg-yellow-100 dark:bg-yellow-900/30 flex items-center justify-center">
                    <Clock className="w-8 h-8 text-yellow-600 dark:text-yellow-400" />
                  </div>
                </div>
                <h2 className="text-2xl font-bold text-text-primary mb-4 text-center">
                  Request Submitted
                </h2>
                <p className="text-text-secondary text-center mb-6">
                  {result.message}
                </p>
                <div className="bg-gray-50 dark:bg-slate-800 rounded-lg p-4 mb-6">
                  <p className="text-sm text-text-secondary">
                    <strong>What happens next?</strong>
                  </p>
                  <ul className="text-sm text-text-secondary mt-2 space-y-1">
                    <li>1. An administrator will review your request</li>
                    <li>2. You&apos;ll receive an email when approved</li>
                    <li>3. Follow the email link to set your password</li>
                  </ul>
                </div>
              </>
            )}

            {result.status === 'approved' && (
              <>
                <div className="flex items-center justify-center mb-6">
                  <div className="w-16 h-16 rounded-full bg-green-100 dark:bg-green-900/30 flex items-center justify-center">
                    <CheckCircle className="w-8 h-8 text-green-600 dark:text-green-400" />
                  </div>
                </div>
                <h2 className="text-2xl font-bold text-text-primary mb-4 text-center">
                  Already Approved
                </h2>
                <p className="text-text-secondary text-center mb-6">
                  {result.message}
                </p>
              </>
            )}

            {result.status === 'rejected' && (
              <>
                <div className="flex items-center justify-center mb-6">
                  <div className="w-16 h-16 rounded-full bg-red-100 dark:bg-red-900/30 flex items-center justify-center">
                    <AlertCircle className="w-8 h-8 text-red-600 dark:text-red-400" />
                  </div>
                </div>
                <h2 className="text-2xl font-bold text-text-primary mb-4 text-center">
                  Request Not Approved
                </h2>
                <p className="text-text-secondary text-center mb-6">
                  {result.message}
                </p>
              </>
            )}

            <div className="flex flex-col gap-3">
              <Link href="/login">
                <Button variant="primary" className="w-full">
                  Go to Sign In
                </Button>
              </Link>
              <Link href="/request-access/status">
                <Button variant="secondary" className="w-full">
                  Check Request Status
                </Button>
              </Link>
            </div>
          </Card>
        </div>
      </div>
    );
  }

  // Show request form
  return (
    <div className="container mx-auto px-4 py-12">
      <div className="flex items-center justify-center min-h-[60vh]">
        <Card className="w-full max-w-md p-8">
          <div className="flex items-center justify-center mb-6">
            <div className="w-16 h-16 rounded-full bg-primary/10 flex items-center justify-center">
              <Mail className="w-8 h-8 text-primary" />
            </div>
          </div>

          <h2 className="text-2xl font-bold text-text-primary mb-2 text-center">
            Request Access
          </h2>
          <p className="text-text-secondary text-center mb-6">
            Submit your information to request access to CAMPFIRE. An administrator will review your request.
          </p>

          {error && (
            <div className="mb-4 p-3 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg flex items-start gap-2">
              <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
              <p className="text-sm text-red-800 dark:text-red-200">{error}</p>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
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
                minLength={2}
                disabled={loading}
              />
            </div>

            <div>
              <label htmlFor="email" className="block text-sm font-medium text-text-primary mb-2">
                Email Address
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
              <p className="text-xs text-text-secondary mt-1">
                You&apos;ll receive an email at this address when your request is approved.
              </p>
            </div>

            <Button
              type="submit"
              variant="primary"
              className="w-full mt-6"
              disabled={loading || !email.trim() || !fullName.trim()}
            >
              {loading ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  Submitting...
                </>
              ) : (
                'Submit Request'
              )}
            </Button>
          </form>

          <div className="mt-6 pt-6 border-t border-border">
            <div className="flex flex-col gap-2 text-center">
              <p className="text-sm text-text-secondary">
                Already have an account?{' '}
                <Link href="/login" className="text-primary hover:underline font-medium">
                  Sign in
                </Link>
              </p>
              <p className="text-sm text-text-secondary">
                Already submitted a request?{' '}
                <Link href="/request-access/status" className="text-primary hover:underline font-medium">
                  Check status
                </Link>
              </p>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
