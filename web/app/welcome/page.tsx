'use client';

import React, { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { usernameFromEmail, USERNAME_REGEX } from '@/lib/utils/username';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import {
  User,
  KeyRound,
  Loader2,
  CheckCircle,
  AlertCircle,
  Sparkles,
  Lock,
} from 'lucide-react';

interface PendingInvite {
  id: number;
  email: string;
  full_name: string | null;
  program_slugs: string[];
  is_admin: boolean;
  can_comment: boolean;
  invited_by: string;
}

interface Program {
  slug: string;
  program_name: string | null;
}

export default function WelcomePage() {
  const router = useRouter();

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [userEmail, setUserEmail] = useState<string>('');
  const [invite, setInvite] = useState<PendingInvite | null>(null);
  const [programs, setPrograms] = useState<Program[]>([]);

  // Form fields
  const [fullName, setFullName] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');

  useEffect(() => {
    checkUserAndInvite();
  }, []);

  const checkUserAndInvite = async () => {
    try {
      // Fetch pending invite from API (handles auth check server-side)
      const response = await fetch('/api/invites/pending');
      const data = await response.json();

      if (!response.ok) {
        if (response.status === 401) {
          // Not authenticated - redirect to login
          router.push('/login');
          return;
        }

        if (response.status === 404) {
          // No pending invite - might already have profile, redirect to spectra
          router.push('/nirspec');
          return;
        }

        setError(data.error || 'Failed to load invitation');
        setLoading(false);
        return;
      }

      // Set invite and programs from API response
      setInvite(data.invite);
      setPrograms(data.programs || []);
      setUserEmail(data.invite.email);

      // Set name from invite if available, otherwise extract from email
      setFullName(data.invite.full_name || data.invite.email.split('@')[0] || '');
      setUsername(usernameFromEmail(data.invite.email));

      setLoading(false);
    } catch (err) {
      console.error('Error loading invite:', err);
      setError('Something went wrong. Please try again.');
      setLoading(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    // Validate
    if (!fullName.trim()) {
      setError('Please enter your full name');
      return;
    }

    if (!password) {
      setError('Password is required');
      return;
    }

    if (password.length < 6) {
      setError('Password must be at least 6 characters');
      return;
    }

    if (password !== confirmPassword) {
      setError('Passwords do not match');
      return;
    }

    if (!USERNAME_REGEX.test(username)) {
      setError('Username must be 2–40 characters: lowercase letters, numbers, dots, hyphens, and underscores, starting and ending with a letter or number.');
      return;
    }

    setSaving(true);

    try {
      // Call API to accept invite and complete setup
      const response = await fetch('/api/invites/accept', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          fullName: fullName.trim(),
          username,
          password: password,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to complete setup');
      }

      // Success - redirect to main app
      router.push('/nirspec');
    } catch (err) {
      console.error('Error completing setup:', err);
      setError(err instanceof Error ? err.message : 'Failed to complete setup');
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="container mx-auto px-4 py-12">
        <div className="flex flex-col items-center justify-center min-h-[60vh]">
          <Loader2 className="w-8 h-8 animate-spin text-primary mb-4" />
          <p className="text-text-secondary">Loading...</p>
        </div>
      </div>
    );
  }

  if (error && !invite) {
    return (
      <div className="container mx-auto px-4 py-12">
        <div className="flex items-center justify-center min-h-[60vh]">
          <Card className="w-full max-w-md p-8 text-center">
            <div className="w-16 h-16 bg-red-100 dark:bg-red-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
              <AlertCircle className="w-8 h-8 text-red-600 dark:text-red-400" />
            </div>
            <h2 className="text-xl font-semibold text-text-primary mb-2">
              Unable to Complete Setup
            </h2>
            <p className="text-text-secondary mb-6">{error}</p>
            <Button variant="secondary" onClick={() => router.push('/login')}>
              Return to Login
            </Button>
          </Card>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-12">
      <div className="flex items-center justify-center min-h-[60vh]">
        <Card className="w-full max-w-lg p-8">
          <div className="text-center mb-8">
            <div className="w-16 h-16 bg-primary/10 rounded-full flex items-center justify-center mx-auto mb-4">
              <Sparkles className="w-8 h-8 text-primary" />
            </div>
            <h1 className="text-2xl font-bold text-text-primary mb-2">
              Welcome to CAMPFIRE
            </h1>
            <p className="text-text-secondary">
              Complete your account setup to get started.
            </p>
          </div>

          {/* Show granted programs */}
          {programs.length > 0 && (
            <div className="mb-6 p-4 bg-green-50 dark:bg-green-950/50 border border-green-200 dark:border-green-800 rounded-lg">
              <div className="flex items-center gap-2 mb-2">
                <CheckCircle className="w-5 h-5 text-green-600 dark:text-green-400" />
                <span className="font-medium text-green-800 dark:text-green-200">
                  You&apos;ve been granted access to:
                </span>
              </div>
              <ul className="ml-7 space-y-1">
                {programs.map((program) => (
                  <li key={program.slug} className="text-sm text-green-700 dark:text-green-300">
                    {program.program_name || program.slug}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {error && (
            <div className="mb-6 p-3 bg-red-50 dark:bg-red-950/50 border border-red-200 dark:border-red-800 rounded-lg flex items-start gap-2">
              <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
              <p className="text-sm text-red-800 dark:text-red-200">{error}</p>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-5">
            {/* Email (read-only) */}
            <div>
              <label className="block text-sm font-medium text-text-primary mb-2">
                Email
              </label>
              <div className="flex items-center gap-2 px-4 py-2 bg-gray-50 dark:bg-slate-800 border border-border rounded-lg text-text-secondary">
                <User className="w-4 h-4" />
                {userEmail}
              </div>
            </div>

            {/* Full Name */}
            <div>
              <label htmlFor="fullName" className="block text-sm font-medium text-text-primary mb-2">
                Full Name <span className="text-red-500">*</span>
              </label>
              <input
                id="fullName"
                type="text"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                className="w-full px-4 py-2 bg-background text-text-primary border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                placeholder="Jane Doe"
                required
                disabled={saving}
              />
              <p className="text-xs text-text-secondary mt-1">
                This is your display name shown to other team members. You&apos;ll sign in using your email address.
              </p>
            </div>

            {/* Username */}
            <div>
              <label htmlFor="username" className="block text-sm font-medium text-text-primary mb-2">
                Username <span className="text-red-500">*</span>
              </label>
              <div className="flex items-center border border-border rounded-lg bg-background focus-within:ring-2 focus-within:ring-primary">
                <span className="pl-4 text-text-secondary select-none">@</span>
                <input
                  id="username"
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="flex-1 px-2 py-2 bg-transparent text-text-primary focus:outline-none"
                  placeholder="your-username"
                  required
                  disabled={saving}
                />
              </div>
              <p className="text-xs text-text-secondary mt-1">
                Lowercase letters, numbers, dots, hyphens, and underscores. 2–40 characters.
              </p>
            </div>

            {/* Password */}
            <div>
              <label htmlFor="password" className="block text-sm font-medium text-text-primary mb-2">
                <div className="flex items-center gap-2">
                  <KeyRound className="w-4 h-4" />
                  Password <span className="text-red-500">*</span>
                </div>
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full px-4 py-2 bg-background text-text-primary border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                placeholder="Enter a password (min. 6 characters)"
                minLength={6}
                required
                disabled={saving}
              />
              <p className="text-xs text-text-secondary mt-1">
                Your password is encrypted and securely stored. Team administrators cannot see your password.
              </p>
            </div>

            {/* Confirm Password */}
            <div>
              <label htmlFor="confirmPassword" className="block text-sm font-medium text-text-primary mb-2">
                Confirm Password <span className="text-red-500">*</span>
              </label>
              <input
                id="confirmPassword"
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="w-full px-4 py-2 bg-background text-text-primary border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                placeholder="Confirm your password"
                minLength={6}
                required
                disabled={saving}
              />
            </div>

            {/* Security Note */}
            <div className="p-3 bg-blue-50 dark:bg-blue-950/50 border border-blue-200 dark:border-blue-800 rounded-lg">
              <div className="flex items-start gap-2">
                <Lock className="w-4 h-4 text-blue-600 dark:text-blue-400 flex-shrink-0 mt-0.5" />
                <p className="text-xs text-blue-800 dark:text-blue-200">
                  All passwords are encrypted using industry-standard security. Your credentials are never visible to administrators.
                </p>
              </div>
            </div>

            <Button
              type="submit"
              variant="primary"
              className="w-full mt-6"
              disabled={saving || !fullName.trim() || !username || !password || !confirmPassword}
            >
              {saving ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin mr-2" />
                  Setting up your account...
                </>
              ) : (
                'Complete Setup'
              )}
            </Button>
          </form>
        </Card>
      </div>
    </div>
  );
}
