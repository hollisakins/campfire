'use client';

import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useAuth } from '@/lib/contexts/AuthContext';
import type { Program, UserProfile } from '@/lib/types';
import {
  User,
  KeyRound,
  Loader2,
  LogIn,
  Check,
  Lock,
  Globe,
  AlertCircle,
  CheckCircle,
  Edit2,
  Save,
  X,
} from 'lucide-react';

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
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Sign in to view your profile
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
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
          <span className="ml-3 text-text-secondary">Loading profile...</span>
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
        <div className="bg-red-50 border border-red-200 rounded-lg p-4">
          <p className="text-red-800">{error || 'Failed to load profile'}</p>
        </div>
      </div>
    );
  }

  const publicPrograms = profileData.programs.filter(p => p.is_public);
  const grantedPrograms = profileData.programs.filter(p => p.access_type === 'granted');
  const lockedPrograms = profileData.programs.filter(p => p.access_type === 'none');

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
                    className="text-xl font-semibold text-text-primary px-2 py-1 border border-border rounded focus:outline-none focus:ring-2 focus:ring-primary"
                    autoFocus
                  />
                ) : (
                  <h1 className="text-xl font-semibold text-text-primary">
                    {profileData.profile.full_name}
                  </h1>
                )}
                <p className="text-text-secondary">{profileData.email}</p>
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
                <Button variant="secondary" size="sm" onClick={() => setIsEditing(true)}>
                  <Edit2 className="w-4 h-4 mr-2" />
                  Edit
                </Button>
              )}
            </div>
          </div>

          <div className="flex items-center justify-between pt-4 border-t border-border">
            <p className="text-sm text-text-secondary">
              Member since {new Date(profileData.profile.created_at).toLocaleDateString()}
            </p>
            <Button variant="secondary" size="sm" onClick={handleSignOut}>
              Sign Out
            </Button>
          </div>
        </Card>

        {/* Access Code Entry */}
        <Card className="p-6">
          <div className="flex items-center gap-2 mb-4">
            <KeyRound className="w-5 h-5 text-primary" />
            <h2 className="text-lg font-semibold text-text-primary">Enter Access Code</h2>
          </div>

          <p className="text-sm text-text-secondary mb-4">
            Have an access code? Enter it below to unlock additional programs.
          </p>

          {redeemError && (
            <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg flex items-start gap-2">
              <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
              <p className="text-sm text-red-800">{redeemError}</p>
            </div>
          )}

          {redeemSuccess && (
            <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded-lg flex items-start gap-2">
              <CheckCircle className="w-5 h-5 text-green-600 flex-shrink-0 mt-0.5" />
              <p className="text-sm text-green-800">{redeemSuccess}</p>
            </div>
          )}

          <form onSubmit={handleRedeemCode} className="flex gap-3">
            <input
              type="text"
              value={accessCode}
              onChange={(e) => setAccessCode(e.target.value.toUpperCase())}
              placeholder="CAMPFIRE-XXXXXX"
              className="flex-1 px-4 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary font-mono uppercase"
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
        </Card>

        {/* Program Access */}
        <Card className="p-6">
          <h2 className="text-lg font-semibold text-text-primary mb-4">Program Access</h2>

          <div className="space-y-4">
            {/* Public Programs */}
            {publicPrograms.length > 0 && (
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <Globe className="w-4 h-4 text-blue-600" />
                  <h3 className="text-sm font-medium text-text-secondary">Public Programs</h3>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  {publicPrograms.map((program) => (
                    <div
                      key={program.program_id}
                      className="flex items-center gap-2 px-3 py-2 bg-blue-50 rounded-lg"
                    >
                      <Check className="w-4 h-4 text-blue-600 flex-shrink-0" />
                      <span className="text-sm text-blue-900 truncate">
                        {program.program_name || `Program ${program.program_id}`}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Granted Programs */}
            {grantedPrograms.length > 0 && (
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <KeyRound className="w-4 h-4 text-green-600" />
                  <h3 className="text-sm font-medium text-text-secondary">Unlocked Programs</h3>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  {grantedPrograms.map((program) => (
                    <div
                      key={program.program_id}
                      className="flex items-center gap-2 px-3 py-2 bg-green-50 rounded-lg"
                    >
                      <Check className="w-4 h-4 text-green-600 flex-shrink-0" />
                      <span className="text-sm text-green-900 truncate">
                        {program.program_name || `Program ${program.program_id}`}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Proprietary Programs */}
            {lockedPrograms.length > 0 && (
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <Lock className="w-4 h-4 text-gray-400" />
                  <h3 className="text-sm font-medium text-text-secondary">Proprietary Programs</h3>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  {lockedPrograms.map((program) => (
                    <div
                      key={program.program_id}
                      className="flex items-center gap-2 px-3 py-2 bg-gray-100 rounded-lg"
                    >
                      <Lock className="w-4 h-4 text-gray-400 flex-shrink-0" />
                      <span className="text-sm text-gray-600 truncate">
                        {program.program_name || `Program ${program.program_id}`}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {profileData.programs.length === 0 && (
              <p className="text-sm text-text-secondary">No programs in the database yet.</p>
            )}
          </div>
        </Card>

        {/* Redemption History */}
        {profileData.redemptions.length > 0 && (
          <Card className="p-6">
            <h2 className="text-lg font-semibold text-text-primary mb-4">Redemption History</h2>
            <div className="space-y-2">
              {profileData.redemptions.map((redemption) => (
                <div
                  key={redemption.id}
                  className="flex items-center justify-between py-2 border-b border-border last:border-0"
                >
                  <div>
                    <span className="font-mono text-sm text-text-primary">
                      {redemption.access_codes.code}
                    </span>
                    {redemption.access_codes.description && (
                      <span className="text-sm text-text-secondary ml-2">
                        ({redemption.access_codes.description})
                      </span>
                    )}
                  </div>
                  <span className="text-sm text-text-secondary">
                    {new Date(redemption.redeemed_at).toLocaleDateString()}
                  </span>
                </div>
              ))}
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
