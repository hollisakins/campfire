'use client';

import React, { useState } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { KeyRound, Loader2, CheckCircle, AlertCircle } from 'lucide-react';

interface AccessCodePromptProps {
  onSuccess?: () => void;
  showDismiss?: boolean; // Allow dismissing the prompt (for non-blocking contexts)
}

export const AccessCodePrompt: React.FC<AccessCodePromptProps> = ({ onSuccess }) => {
  const [code, setCode] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!code.trim()) {
      setError('Please enter an access code');
      return;
    }

    setLoading(true);
    setError(null);
    setSuccess(null);

    try {
      const response = await fetch('/api/codes/redeem', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code.trim() }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to redeem code');
      }

      setSuccess(data.message);
      setCode('');

      // Callback after short delay to show success message
      if (onSuccess) {
        setTimeout(() => {
          onSuccess();
        }, 1500);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to redeem code');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex flex-col items-center justify-center py-16">
      <Card className="max-w-md w-full p-8">
        <div className="text-center mb-6">
          <div className="w-16 h-16 bg-primary/10 rounded-full flex items-center justify-center mx-auto mb-4">
            <KeyRound className="w-8 h-8 text-primary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Enter Access Code
          </h2>
          <p className="text-text-secondary">
            To access proprietary programs, you need to redeem an access code. Public programs are accessible to all users without a code.
          </p>
          <p className="text-sm text-text-tertiary mt-2">
            Enter the code provided by your PI or the CAMPFIRE team.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <input
              type="text"
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              placeholder="e.g., CAMPFIRE-2024"
              className="w-full px-4 py-3 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary text-center font-mono text-lg uppercase"
              disabled={loading || !!success}
              autoFocus
            />
          </div>

          {error && (
            <div className="flex items-center gap-2 text-red-600 bg-red-50 p-3 rounded-lg">
              <AlertCircle className="w-5 h-5 flex-shrink-0" />
              <span className="text-sm">{error}</span>
            </div>
          )}

          {success && (
            <div className="flex items-center gap-2 text-green-600 bg-green-50 p-3 rounded-lg">
              <CheckCircle className="w-5 h-5 flex-shrink-0" />
              <span className="text-sm">{success}</span>
            </div>
          )}

          <Button
            type="submit"
            variant="primary"
            className="w-full"
            disabled={loading || !!success}
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin mr-2" />
                Validating...
              </>
            ) : success ? (
              <>
                <CheckCircle className="w-4 h-4 mr-2" />
                Access Granted!
              </>
            ) : (
              'Submit Code'
            )}
          </Button>
        </form>

        <p className="text-sm text-text-secondary text-center mt-6">
          Don&apos;t have a code? Contact your PI or{' '}
          <a
            href="mailto:campfire@example.com"
            className="text-primary hover:underline"
          >
            request access via email
          </a>.
        </p>
      </Card>
    </div>
  );
};
