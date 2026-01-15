'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { AlertCircle, CheckCircle, Loader2, Search, Clock, XCircle, ArrowLeft } from 'lucide-react';

type RequestStatus = 'pending' | 'approved' | 'rejected' | 'not_found';

interface StatusResult {
  status: RequestStatus;
  message: string;
  created_at?: string;
  reviewed_at?: string;
}

export default function RequestStatusPage() {
  const [email, setEmail] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<StatusResult | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setResult(null);
    setLoading(true);

    try {
      const response = await fetch(`/api/account-requests/status?email=${encodeURIComponent(email.trim())}`);
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to check status');
      }

      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An unexpected error occurred');
    } finally {
      setLoading(false);
    }
  };

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const getStatusIcon = (status: RequestStatus) => {
    switch (status) {
      case 'pending':
        return <Clock className="w-8 h-8 text-yellow-600" />;
      case 'approved':
        return <CheckCircle className="w-8 h-8 text-green-600" />;
      case 'rejected':
        return <XCircle className="w-8 h-8 text-red-600" />;
      case 'not_found':
        return <Search className="w-8 h-8 text-gray-400 dark:text-slate-500" />;
    }
  };

  const getStatusColor = (status: RequestStatus) => {
    switch (status) {
      case 'pending':
        return 'bg-yellow-100 dark:bg-yellow-900/30';
      case 'approved':
        return 'bg-green-100 dark:bg-green-900/30';
      case 'rejected':
        return 'bg-red-100 dark:bg-red-900/30';
      case 'not_found':
        return 'bg-gray-100 dark:bg-slate-700';
    }
  };

  const getStatusTitle = (status: RequestStatus) => {
    switch (status) {
      case 'pending':
        return 'Request Pending';
      case 'approved':
        return 'Request Approved';
      case 'rejected':
        return 'Request Not Approved';
      case 'not_found':
        return 'No Request Found';
    }
  };

  return (
    <div className="container mx-auto px-4 py-12">
      <div className="flex items-center justify-center min-h-[60vh]">
        <Card className="w-full max-w-md p-8">
          <div className="flex items-center justify-center mb-6">
            <div className="w-16 h-16 rounded-full bg-primary/10 flex items-center justify-center">
              <Search className="w-8 h-8 text-primary" />
            </div>
          </div>

          <h2 className="text-2xl font-bold text-text-primary mb-2 text-center">
            Check Request Status
          </h2>
          <p className="text-text-secondary text-center mb-6">
            Enter your email address to check the status of your access request.
          </p>

          {error && (
            <div className="mb-4 p-3 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg flex items-start gap-2">
              <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
              <p className="text-sm text-red-800 dark:text-red-200">{error}</p>
            </div>
          )}

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
                className="w-full px-4 py-2 bg-background text-text-primary border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary dark:bg-slate-900"
                placeholder="your.email@example.com"
                required
                disabled={loading}
              />
            </div>

            <Button
              type="submit"
              variant="primary"
              className="w-full"
              disabled={loading || !email.trim()}
            >
              {loading ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  Checking...
                </>
              ) : (
                'Check Status'
              )}
            </Button>
          </form>

          {/* Status Result */}
          {result && (
            <div className="mt-6 pt-6 border-t border-border">
              <div className="flex items-center justify-center mb-4">
                <div className={`w-16 h-16 rounded-full ${getStatusColor(result.status)} flex items-center justify-center`}>
                  {getStatusIcon(result.status)}
                </div>
              </div>

              <h3 className="text-lg font-semibold text-text-primary text-center mb-2">
                {getStatusTitle(result.status)}
              </h3>

              <p className="text-text-secondary text-center text-sm mb-4">
                {result.message}
              </p>

              {result.created_at && (
                <div className="bg-gray-50 dark:bg-slate-800 rounded-lg p-3 text-sm">
                  <p className="text-text-secondary">
                    <strong>Submitted:</strong> {formatDate(result.created_at)}
                  </p>
                  {result.reviewed_at && (
                    <p className="text-text-secondary mt-1">
                      <strong>Reviewed:</strong> {formatDate(result.reviewed_at)}
                    </p>
                  )}
                </div>
              )}

              {result.status === 'not_found' && (
                <div className="mt-4">
                  <Link href="/request-access">
                    <Button variant="primary" className="w-full">
                      Submit a Request
                    </Button>
                  </Link>
                </div>
              )}

              {result.status === 'approved' && (
                <div className="mt-4">
                  <Link href="/login">
                    <Button variant="primary" className="w-full">
                      Go to Sign In
                    </Button>
                  </Link>
                </div>
              )}
            </div>
          )}

          <div className="mt-6 pt-6 border-t border-border text-center">
            <Link
              href="/request-access"
              className="inline-flex items-center gap-2 text-sm text-primary hover:underline"
            >
              <ArrowLeft className="w-4 h-4" />
              Back to Request Access
            </Link>
          </div>
        </Card>
      </div>
    </div>
  );
}
