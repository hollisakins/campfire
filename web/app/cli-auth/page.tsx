'use client';

import React, { useState, useEffect, useCallback, Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { useAuth } from '@/lib/contexts/AuthContext';
import { AlertCircle, CheckCircle, XCircle, Loader2, Terminal } from 'lucide-react';

type AuthStatus = 'idle' | 'validating' | 'ready' | 'authorizing' | 'success' | 'denied' | 'error' | 'expired';

function CliAuthContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, loading: authLoading } = useAuth();

  const [userCode, setUserCode] = useState('');
  const [status, setStatus] = useState<AuthStatus>('idle');
  const [error, setError] = useState<string | null>(null);

  // Pre-fill code from URL if provided
  useEffect(() => {
    const codeFromUrl = searchParams.get('code');
    if (codeFromUrl) {
      setUserCode(codeFromUrl.toUpperCase());
      // Auto-validate if code is provided
      if (codeFromUrl.replace(/-/g, '').length === 8) {
        validateCode(codeFromUrl);
      }
    }
  }, [searchParams]);

  // Redirect to login if not authenticated
  useEffect(() => {
    if (!authLoading && !user) {
      // Save the current URL to redirect back after login
      const returnUrl = window.location.href;
      router.push(`/login?redirect=${encodeURIComponent(returnUrl)}`);
    }
  }, [authLoading, user, router]);

  const validateCode = useCallback(async (code: string) => {
    const normalizedCode = code.replace(/-/g, '').toUpperCase();
    if (normalizedCode.length !== 8) {
      setError('Code must be 8 characters');
      return;
    }

    setStatus('validating');
    setError(null);

    try {
      const response = await fetch('/api/v1/auth/device/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_code: normalizedCode }),
      });

      const data = await response.json();

      if (!response.ok) {
        if (data.error === 'expired') {
          setStatus('expired');
          setError('This code has expired. Please run "campfire login" again.');
        } else if (data.error === 'not_found') {
          setStatus('error');
          setError('Invalid code. Please check and try again.');
        } else {
          setStatus('error');
          setError(data.error_description || 'Failed to validate code');
        }
        return;
      }

      setStatus('ready');
    } catch {
      setStatus('error');
      setError('Failed to validate code. Please try again.');
    }
  }, []);

  const handleCodeChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    let value = e.target.value.toUpperCase().replace(/[^A-Z]/g, '');

    // Auto-insert dash after 4 characters
    if (value.length > 4) {
      value = value.slice(0, 4) + '-' + value.slice(4, 8);
    }

    setUserCode(value);
    setError(null);
    setStatus('idle');
  };

  const handleValidate = () => {
    validateCode(userCode);
  };

  const handleAuthorize = async () => {
    setStatus('authorizing');
    setError(null);

    try {
      const normalizedCode = userCode.replace(/-/g, '').toUpperCase();

      const response = await fetch('/api/v1/auth/device/authorize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_code: normalizedCode, action: 'authorize' }),
      });

      const data = await response.json();

      if (!response.ok) {
        setStatus('error');
        setError(data.error_description || 'Failed to authorize');
        return;
      }

      setStatus('success');
    } catch {
      setStatus('error');
      setError('Failed to authorize. Please try again.');
    }
  };

  const handleDeny = async () => {
    setStatus('authorizing');
    setError(null);

    try {
      const normalizedCode = userCode.replace(/-/g, '').toUpperCase();

      const response = await fetch('/api/v1/auth/device/authorize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_code: normalizedCode, action: 'deny' }),
      });

      if (!response.ok) {
        const data = await response.json();
        setStatus('error');
        setError(data.error_description || 'Failed to process');
        return;
      }

      setStatus('denied');
    } catch {
      setStatus('error');
      setError('Failed to process. Please try again.');
    }
  };

  // Show loading while checking auth
  if (authLoading) {
    return (
      <div className="container mx-auto px-4 py-12">
        <div className="flex items-center justify-center min-h-[60vh]">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
        </div>
      </div>
    );
  }

  // Don't render the form if not authenticated (will redirect)
  if (!user) {
    return null;
  }

  return (
    <div className="container mx-auto px-4 py-12">
      <div className="flex items-center justify-center min-h-[60vh]">
        <Card className="w-full max-w-md p-8">
          {/* Success State */}
          {status === 'success' && (
            <div className="text-center py-8">
              <CheckCircle className="w-16 h-16 text-green-500 mx-auto mb-4" />
              <h2 className="text-2xl font-bold text-text-primary mb-2">Authorized!</h2>
              <p className="text-text-secondary mb-4">
                You can now close this window and return to your terminal.
              </p>
              <p className="text-sm text-text-tertiary">
                The Python client should automatically receive your credentials.
              </p>
            </div>
          )}

          {/* Denied State */}
          {status === 'denied' && (
            <div className="text-center py-8">
              <XCircle className="w-16 h-16 text-red-500 mx-auto mb-4" />
              <h2 className="text-2xl font-bold text-text-primary mb-2">Authorization Denied</h2>
              <p className="text-text-secondary">
                The authorization request has been denied. You can close this window.
              </p>
            </div>
          )}

          {/* Expired State */}
          {status === 'expired' && (
            <div className="text-center py-8">
              <AlertCircle className="w-16 h-16 text-yellow-500 mx-auto mb-4" />
              <h2 className="text-2xl font-bold text-text-primary mb-2">Code Expired</h2>
              <p className="text-text-secondary mb-4">
                This authorization code has expired.
              </p>
              <p className="text-sm text-text-tertiary">
                Please run <code className="bg-bg-secondary px-2 py-1 rounded">campfire login</code> again to get a new code.
              </p>
            </div>
          )}

          {/* Main Form */}
          {status !== 'success' && status !== 'denied' && status !== 'expired' && (
            <>
              <div className="flex items-center justify-center mb-6">
                <Terminal className="w-8 h-8 text-primary mr-3" />
                <h2 className="text-2xl font-bold text-text-primary">
                  Authorize Python Client
                </h2>
              </div>

              <p className="text-text-secondary text-center mb-6">
                Enter the code shown in your terminal to authorize access.
              </p>

              {error && (
                <div className="mb-4 p-3 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg flex items-start gap-2">
                  <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
                  <p className="text-sm text-red-800 dark:text-red-200">{error}</p>
                </div>
              )}

              {/* Code Input */}
              <div className="mb-6">
                <label htmlFor="code" className="block text-sm font-medium text-text-primary mb-2">
                  Authorization Code
                </label>
                <input
                  id="code"
                  type="text"
                  value={userCode}
                  onChange={handleCodeChange}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && userCode.replace(/-/g, '').length === 8) {
                      if (status === 'idle') {
                        handleValidate();
                      } else if (status === 'ready') {
                        handleAuthorize();
                      }
                    }
                  }}
                  className="w-full px-4 py-3 text-2xl text-center font-mono tracking-widest bg-background text-text-primary border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                  placeholder="XXXX-XXXX"
                  maxLength={9}
                  disabled={status === 'validating' || status === 'authorizing'}
                  autoComplete="off"
                  autoFocus
                />
              </div>

              {/* Buttons based on status */}
              {status === 'idle' && (
                <Button
                  onClick={handleValidate}
                  variant="primary"
                  className="w-full"
                  disabled={userCode.replace(/-/g, '').length !== 8}
                >
                  Verify Code
                </Button>
              )}

              {status === 'validating' && (
                <Button variant="primary" className="w-full" disabled>
                  <Loader2 className="w-4 h-4 animate-spin mr-2" />
                  Validating...
                </Button>
              )}

              {status === 'ready' && (
                <div className="space-y-3">
                  <div className="p-4 bg-bg-secondary rounded-lg mb-4">
                    <p className="text-sm text-text-secondary mb-2">
                      This will allow the Python client to:
                    </p>
                    <ul className="text-sm text-text-primary space-y-1">
                      <li>• Query objects in your programs</li>
                      <li>• Download spectra you have access to</li>
                    </ul>
                  </div>

                  <Button
                    onClick={handleAuthorize}
                    variant="primary"
                    className="w-full"
                  >
                    Authorize
                  </Button>
                  <Button
                    onClick={handleDeny}
                    variant="secondary"
                    className="w-full"
                  >
                    Deny
                  </Button>
                </div>
              )}

              {status === 'authorizing' && (
                <Button variant="primary" className="w-full" disabled>
                  <Loader2 className="w-4 h-4 animate-spin mr-2" />
                  Processing...
                </Button>
              )}

              {status === 'error' && (
                <Button
                  onClick={() => {
                    setStatus('idle');
                    setError(null);
                  }}
                  variant="primary"
                  className="w-full"
                >
                  Try Again
                </Button>
              )}

              {/* Info text */}
              <p className="text-xs text-text-tertiary text-center mt-6">
                Logged in as {user.email}
              </p>
            </>
          )}
        </Card>
      </div>
    </div>
  );
}

function LoadingFallback() {
  return (
    <div className="container mx-auto px-4 py-12">
      <div className="flex items-center justify-center min-h-[60vh]">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    </div>
  );
}

export default function CliAuthPage() {
  return (
    <Suspense fallback={<LoadingFallback />}>
      <CliAuthContent />
    </Suspense>
  );
}
