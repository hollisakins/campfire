'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { useAuth } from '@/lib/contexts/AuthContext';
import { AlertCircle, CheckCircle, Mail } from 'lucide-react';

export const SignupForm: React.FC = () => {
  const router = useRouter();
  const { signUp, user, loading: authLoading, needsProfileSetup } = useAuth();
  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  // If an already-authenticated user lands here, send them onward.
  useEffect(() => {
    if (!authLoading && user) {
      router.push(needsProfileSetup ? '/welcome' : '/nirspec');
    }
  }, [authLoading, user, needsProfileSetup, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      const { error } = await signUp(email.trim(), password, fullName.trim());
      if (error) {
        setError(error.message);
      } else {
        // Email confirmation is required, so there is no session yet.
        setSubmitted(true);
      }
    } catch {
      setError('An unexpected error occurred');
    } finally {
      setLoading(false);
    }
  };

  if (submitted) {
    return (
      <Card className="w-full max-w-md p-8">
        <div className="flex items-center justify-center mb-6">
          <div className="w-16 h-16 rounded-full bg-green-100 dark:bg-green-900/30 flex items-center justify-center">
            <CheckCircle className="w-8 h-8 text-green-600 dark:text-green-400" />
          </div>
        </div>
        <h2 className="text-2xl font-bold text-text-primary mb-4 text-center">
          Confirm your email
        </h2>
        <p className="text-text-secondary text-center mb-6">
          We sent a confirmation link to <strong>{email.trim()}</strong>. Click it to
          activate your account, then sign in.
        </p>
        <div className="bg-card rounded-lg p-4 mb-6">
          <p className="text-sm text-text-secondary">
            Didn&apos;t get it? Check your spam or junk folder. The link can take a
            minute or two to arrive.
          </p>
        </div>
        <Link href="/login">
          <Button variant="primary" className="w-full">
            Go to Sign In
          </Button>
        </Link>
      </Card>
    );
  }

  return (
    <Card className="w-full max-w-md p-8">
      <div className="flex items-center justify-center mb-6">
        <div className="w-16 h-16 rounded-full bg-primary/10 flex items-center justify-center">
          <Mail className="w-8 h-8 text-primary" />
        </div>
      </div>

      <h2 className="text-2xl font-bold text-text-primary mb-2 text-center">
        Create your CAMPFIRE account
      </h2>
      <p className="text-text-secondary text-center mb-6">
        Anyone can sign up to browse public programs, comment, and tag objects.
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
            className="w-full px-4 py-2 bg-background text-text-primary placeholder:text-text-tertiary border border-border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
            placeholder="Jane Doe"
            required
            minLength={2}
            disabled={loading}
          />
        </div>

        <div>
          <label htmlFor="email" className="block text-sm font-medium text-text-primary mb-2">
            Email
          </label>
          <input
            id="email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full px-4 py-2 bg-background text-text-primary placeholder:text-text-tertiary border border-border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
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
            className="w-full px-4 py-2 bg-background text-text-primary placeholder:text-text-tertiary border border-border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
            placeholder="••••••••"
            required
            minLength={6}
            disabled={loading}
          />
          <p className="text-xs text-text-secondary mt-1">At least 6 characters.</p>
        </div>

        <Button
          type="submit"
          variant="primary"
          className="w-full mt-6"
          disabled={loading || !email.trim() || !fullName.trim() || password.length < 6}
        >
          {loading ? 'Creating account...' : 'Create Account'}
        </Button>
      </form>

      <div className="mt-6 text-center">
        <p className="text-sm text-text-secondary">
          Already have an account?{' '}
          <Link href="/login" className="text-primary hover:underline font-medium">
            Sign in
          </Link>
        </p>
      </div>
    </Card>
  );
};
