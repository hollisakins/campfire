'use client';

import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useAuth } from '@/lib/contexts/AuthContext';
import type { Program, UserProfile, ProfileStats, ProfileRecentComments } from '@/lib/types';
import {
  User,
  KeyRound,
  Loader2,
  LogIn,
  Check,
  AlertCircle,
  CheckCircle,
  Edit2,
  Save,
  X,
  Key,
  ChevronRight,
} from 'lucide-react';
import { SettingsCard } from '@/components/settings/SettingsCard';
import { ProfileStats as ProfileStatsCard } from '@/components/profile/ProfileStats';
import { CommentHistory } from '@/components/profile/CommentHistory';

interface ProgramWithAccess extends Program {
  has_access: boolean;
  access_type: 'public' | 'granted' | 'none';
}

interface Redemption {
  id: string;
  redeemed_at: string;
  access_codes: {
    code: string;
    description: string | null;
  };
}

interface ProfileData {
  profile: UserProfile;
  email: string;
  programs: ProgramWithAccess[];
  redemptions: Redemption[];
  stats: ProfileStats;
  recent_comments: ProfileRecentComments;
}

export default function ProfilePage() {
  const router = useRouter();
  const { user, loading: authLoading, signOut } = useAuth();

  const [profileData, setProfileData] = useState<ProfileData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Edit mode state
  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState('');
  const [saving, setSaving] = useState(false);

  // Access code state
  const [accessCode, setAccessCode] = useState('');
  const [redeemLoading, setRedeemLoading] = useState(false);
  const [redeemError, setRedeemError] = useState<string | null>(null);
  const [redeemSuccess, setRedeemSuccess] = useState<string | null>(null);

  const fetchProfile = useCallback(async () => {
    if (authLoading || !user) return;

    setLoading(true);
    setError(null);

    try {
      const response = await fetch('/api/profile');
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to fetch profile');
      }

      setProfileData(data);
      setEditName(data.profile.full_name);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch profile');
    } finally {
      setLoading(false);
    }
  }, [authLoading, user]);

  useEffect(() => {
    fetchProfile();
  }, [fetchProfile]);

  const handleSaveProfile = async () => {
    setSaving(true);
    try {
      const response = await fetch('/api/profile', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ full_name: editName }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update profile');
      }

      setIsEditing(false);
      fetchProfile();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update profile');
    } finally {
      setSaving(false);
    }
  };

  const handleRedeemCode = async (e: React.FormEvent) => {
    e.preventDefault();
    setRedeemLoading(true);
    setRedeemError(null);
    setRedeemSuccess(null);

    try {
      const response = await fetch('/api/codes/redeem', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: accessCode.trim().toUpperCase() }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to redeem code');
      }

      setRedeemSuccess(`Code redeemed! You now have access to ${data.programs_granted} program(s).`);
      setAccessCode('');
      fetchProfile();
    } catch (err) {
      setRedeemError(err instanceof Error ? err.message : 'Failed to redeem code');
    } finally {
      setRedeemLoading(false);
    }
  };

  const handleSignOut = async () => {
    await signOut();
    router.push('/');
  };

  // Show login prompt if not authenticated
  if (!authLoading && !user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Profile' },
          ]}
          className="mb-6"
        />

        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card dark:bg-slate-800 rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary dark:text-slate-400" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Sign in to view your profile
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6 max-w-md">
            Please sign in to manage your profile and access codes.
          </p>
          <Link
            href="/login"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </Link>
        </div>
      </div>
    );
  }

  if (loading || authLoading) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Profile' },
          ]}
          className="mb-6"
        />
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary dark:text-slate-400">Loading profile...</span>
        </div>
      </div>
    );
  }

  if (error || !profileData) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Profile' },
          ]}
          className="mb-6"
        />
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4">
          <p className="text-red-800 dark:text-red-400">{error || 'Failed to load profile'}</p>
        </div>
      </div>
    );
  }

  const grantedPrograms = profileData.programs.filter(p => p.access_type === 'granted');

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'Profile' },
        ]}
        className="mb-6"
      />

      <div className="max-w-3xl mx-auto space-y-6">
        {/* Profile Info */}
        <Card className="p-6">
          <div className="flex items-start justify-between mb-4">
            <div className="flex items-center gap-3">
              <div className="w-12 h-12 bg-primary/10 rounded-full flex items-center justify-center">
                <User className="w-6 h-6 text-primary" />
              </div>
              <div>
                {isEditing ? (
                  <input
                    type="text"
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    className="text-xl font-semibold text-text-primary dark:text-slate-100 px-2 py-1 border border-border dark:border-slate-600 rounded bg-white dark:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-primary"
                    autoFocus
                  />
                ) : (
                  <div className="flex items-center gap-2">
                    <h1 className="text-xl font-semibold text-text-primary dark:text-slate-100">
                      {profileData.profile.full_name}
                    </h1>
                    {profileData.profile.is_group_account && (
                      <span className="text-xs px-2 py-0.5 bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-300 rounded">
                        Group Account
                      </span>
                    )}
                  </div>
                )}
                <p className="text-text-secondary dark:text-slate-400">{profileData.email}</p>
              </div>
            </div>

            <div className="flex items-center gap-2">
              {isEditing ? (
                <>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      setIsEditing(false);
                      setEditName(profileData.profile.full_name);
                    }}
                    disabled={saving}
                  >
                    <X className="w-4 h-4" />
                  </Button>
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={handleSaveProfile}
                    disabled={saving || !editName.trim()}
                  >
                    {saving ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Save className="w-4 h-4" />
                    )}
                  </Button>
                </>
              ) : (
                !profileData.profile.is_group_account && (
                  <Button variant="secondary" size="sm" onClick={() => setIsEditing(true)}>
                    <Edit2 className="w-4 h-4 mr-2" />
                    Edit
                  </Button>
                )
              )}
            </div>
          </div>

          <div className="pt-4 border-t border-border dark:border-slate-700 space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-sm text-text-secondary dark:text-slate-400">
                Member since {new Date(profileData.profile.created_at).toLocaleDateString()}
              </p>
              <Button variant="secondary" size="sm" onClick={handleSignOut}>
                Sign Out
              </Button>
            </div>

            {!profileData.profile.is_group_account && (
              <div className="flex items-center gap-4 text-sm">
                <Link
                  href="/forgot-password"
                  className="text-text-secondary hover:text-primary transition-colors"
                >
                  Change password
                </Link>
                <span className="text-border dark:text-slate-600">•</span>
                <Link
                  href="/profile/change-email"
                  className="text-text-secondary hover:text-primary transition-colors"
                >
                  Change email
                </Link>
              </div>
            )}
          </div>
        </Card>

        {/* Activity Stats */}
        <ProfileStatsCard stats={profileData.stats} />

        {/* Comment History */}
        <CommentHistory
          initialComments={profileData.recent_comments.items}
          totalCount={profileData.recent_comments.total_count}
        />

        {/* Settings */}
        <SettingsCard />

        {/* Access Code Entry */}
        {!profileData.profile.is_group_account && (
          <Card className="p-6" id="access-code">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 bg-primary/10 rounded-full flex items-center justify-center">
              <KeyRound className="w-5 h-5 text-primary" />
            </div>
            <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">Access Codes</h2>
          </div>

          <p className="text-sm text-text-secondary dark:text-slate-400 mb-4">
            Enter an access code below to gain access to proprietary programs. Public programs are accessible to all users.
          </p>

          {redeemError && (
            <div className="mb-4 p-3 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg flex items-start gap-2">
              <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
              <p className="text-sm text-red-800 dark:text-red-400">{redeemError}</p>
            </div>
          )}

          {redeemSuccess && (
            <div className="mb-4 p-3 bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded-lg flex items-start gap-2">
              <CheckCircle className="w-5 h-5 text-green-600 dark:text-green-400 flex-shrink-0 mt-0.5" />
              <p className="text-sm text-green-800 dark:text-green-400">{redeemSuccess}</p>
            </div>
          )}

          <form onSubmit={handleRedeemCode} className="flex gap-3">
            <input
              type="text"
              value={accessCode}
              onChange={(e) => setAccessCode(e.target.value.toUpperCase())}
              placeholder="CAMPFIRE-XXXXXX"
              className="flex-1 px-4 py-2 border border-border dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary font-mono uppercase"
              disabled={redeemLoading}
            />
            <Button type="submit" variant="primary" disabled={redeemLoading || !accessCode.trim()}>
              {redeemLoading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin mr-2" />
                  Redeeming...
                </>
              ) : (
                'Redeem'
              )}
            </Button>
          </form>

          {/* Redemption History */}
          {profileData.redemptions.length > 0 && (
            <div className="mt-6 pt-6 border-t border-border dark:border-slate-700">
              <h3 className="text-sm font-semibold text-text-primary dark:text-slate-100 mb-3">Redemption History</h3>
              <div className="space-y-2">
                {profileData.redemptions.map((redemption) => (
                  <div
                    key={redemption.id}
                    className="flex items-center justify-between py-2 border-b border-border dark:border-slate-700 last:border-0"
                  >
                    <div>
                      <span className="font-mono text-sm text-text-primary dark:text-slate-100">
                        {redemption.access_codes.code}
                      </span>
                      {redemption.access_codes.description && (
                        <span className="text-sm text-text-secondary dark:text-slate-400 ml-2">
                          ({redemption.access_codes.description})
                        </span>
                      )}
                    </div>
                    <span className="text-sm text-text-secondary dark:text-slate-400">
                      {new Date(redemption.redeemed_at).toLocaleDateString()}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
        )}

        {/* Program Access - Only show if user has granted programs */}
        {grantedPrograms.length > 0 && (
          <Card className="p-6">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 bg-primary/10 rounded-full flex items-center justify-center">
                <Key className="w-5 h-5 text-primary" />
              </div>
              <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">Proprietary Program Access</h2>
            </div>

            <p className="text-sm text-text-secondary dark:text-slate-400 mb-4">
              You have access to the following proprietary programs:
            </p>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
              {grantedPrograms.map((program) => (
                <div
                  key={program.program_id}
                  className="flex items-center gap-2 px-3 py-2 bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded-lg"
                >
                  <Check className="w-4 h-4 text-green-600 dark:text-green-400 flex-shrink-0" />
                  <span className="text-sm text-text-primary dark:text-slate-100 truncate">
                    {program.program_name || `Program ${program.program_id}`}
                  </span>
                </div>
              ))}
            </div>
          </Card>
        )}

        {/* API Keys */}
        {!profileData.profile.is_group_account && (
          <Link href="/profile/api-keys" className="block">
          <Card className="p-6 hover:bg-background-hover dark:hover:bg-slate-700 transition-colors cursor-pointer">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="w-12 h-12 bg-purple-100 dark:bg-purple-900 rounded-full flex items-center justify-center">
                  <Key className="w-6 h-6 text-purple-600 dark:text-purple-400" />
                </div>
                <div>
                  <div className="flex items-center gap-2">
                    <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">API Keys</h2>
                    <span className="inline-flex px-2 py-0.5 text-xs font-medium bg-yellow-100 dark:bg-yellow-900 text-yellow-800 dark:text-yellow-300 rounded-full">
                      Experimental
                    </span>
                  </div>
                  <p className="text-sm text-text-secondary dark:text-slate-400">
                    Manage API keys for the Python client (experimental preview)
                  </p>
                </div>
              </div>
              <ChevronRight className="w-5 h-5 text-text-secondary dark:text-slate-400" />
            </div>
          </Card>
        </Link>
        )}
      </div>
    </div>
  );
}
