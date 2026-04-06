'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import {
  Loader2,
  RefreshCw,
  Shield,
  ShieldOff,
  Trash2,
  ChevronDown,
  ChevronUp,
  Check,
  Mail,
  X,
  UserPlus,
} from 'lucide-react';
import type { UserProfile, Program } from '@/lib/types';

interface UserWithAccess extends UserProfile {
  program_access: string[];
}

interface PendingInvite {
  id: number;
  email: string;
  program_slugs: string[];
  is_admin: boolean;
  can_comment: boolean;
  invited_by: string;
  invited_by_name: string | null;
  created_at: string;
}

export default function AdminUsersPage() {
  const [users, setUsers] = useState<UserWithAccess[]>([]);
  const [programs, setPrograms] = useState<Program[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedUser, setExpandedUser] = useState<string | null>(null);
  const [savingUser, setSavingUser] = useState<string | null>(null);

  // Invite state
  const [invites, setInvites] = useState<PendingInvite[]>([]);
  const [showInviteForm, setShowInviteForm] = useState(false);
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteProgramSlugs, setInviteProgramSlugs] = useState<string[]>([]);
  const [inviteIsAdmin, setInviteIsAdmin] = useState(false);
  const [inviteCanComment, setInviteCanComment] = useState(true);
  const [sendingInvite, setSendingInvite] = useState(false);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [resendingInvite, setResendingInvite] = useState<number | null>(null);

  const fetchInvites = useCallback(async () => {
    try {
      const response = await fetch('/api/admin/invites');
      const data = await response.json();
      if (response.ok) {
        setInvites(data.invites || []);
      }
    } catch (err) {
      console.error('Error fetching invites:', err);
    }
  }, []);

  const fetchUsers = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const response = await fetch('/api/users');
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to fetch users');
      }

      setUsers(data.users || []);
      setPrograms(data.programs || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch users');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchUsers();
    fetchInvites();
  }, [fetchUsers, fetchInvites]);

  const sendInvite = async () => {
    if (!inviteEmail.trim()) {
      setInviteError('Email is required');
      return;
    }

    setSendingInvite(true);
    setInviteError(null);

    try {
      const response = await fetch('/api/admin/invites', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: inviteEmail.trim(),
          program_slugs: inviteProgramSlugs,
          is_admin: inviteIsAdmin,
          can_comment: inviteCanComment,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to send invite');
      }

      // Reset form and refresh
      setInviteEmail('');
      setInviteProgramSlugs([]);
      setInviteIsAdmin(false);
      setInviteCanComment(true);
      setShowInviteForm(false);
      fetchInvites();
    } catch (err) {
      setInviteError(err instanceof Error ? err.message : 'Failed to send invite');
    } finally {
      setSendingInvite(false);
    }
  };

  const cancelInvite = async (inviteId: number) => {
    if (!confirm('Cancel this invite?')) return;

    try {
      const response = await fetch(`/api/admin/invites/${inviteId}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to cancel invite');
      }

      fetchInvites();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to cancel invite');
    }
  };

  const resendInvite = async (inviteId: number, email: string) => {
    setResendingInvite(inviteId);

    try {
      const response = await fetch(`/api/admin/invites/${inviteId}/resend`, {
        method: 'POST',
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to resend invite');
      }

      alert(data.message || `Invite resent to ${email}`);
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to resend invite');
    } finally {
      setResendingInvite(null);
    }
  };

  // Only proprietary (non-public) programs need explicit access grants
  const proprietaryPrograms = programs.filter(p => !p.is_public);

  const toggleInviteProgram = (programSlug: string) => {
    setInviteProgramSlugs(prev =>
      prev.includes(programSlug)
        ? prev.filter(s => s !== programSlug)
        : [...prev, programSlug]
    );
  };

  const toggleAdmin = async (user: UserWithAccess) => {
    setSavingUser(user.user_id);
    try {
      const response = await fetch(`/api/users/${user.user_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_admin: !user.is_admin }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update user');
      }

      fetchUsers();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update user');
    } finally {
      setSavingUser(null);
    }
  };

  const updateProgramAccess = async (user: UserWithAccess, programSlug: string, grant: boolean) => {
    setSavingUser(user.user_id);
    try {
      const newAccess = grant
        ? [...user.program_access, programSlug]
        : user.program_access.filter(s => s !== programSlug);

      const response = await fetch(`/api/users/${user.user_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ program_access: newAccess }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update access');
      }

      fetchUsers();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update access');
    } finally {
      setSavingUser(null);
    }
  };

  const grantAllPrograms = async (user: UserWithAccess) => {
    setSavingUser(user.user_id);
    try {
      const allProgramSlugs = proprietaryPrograms.map(p => p.slug);

      const response = await fetch(`/api/users/${user.user_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ program_access: allProgramSlugs }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update access');
      }

      fetchUsers();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update access');
    } finally {
      setSavingUser(null);
    }
  };

  const revokeAllPrograms = async (user: UserWithAccess) => {
    setSavingUser(user.user_id);
    try {
      // Keep any public program access, only revoke proprietary
      const publicAccess = user.program_access.filter(s => !proprietaryPrograms.some(p => p.slug === s));
      const response = await fetch(`/api/users/${user.user_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ program_access: publicAccess }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update access');
      }

      fetchUsers();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update access');
    } finally {
      setSavingUser(null);
    }
  };

  const deleteUser = async (user: UserWithAccess) => {
    if (!confirm(`Delete user "${user.full_name}"? This cannot be undone.`)) {
      return;
    }

    try {
      const response = await fetch(`/api/users/${user.user_id}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to delete user');
      }

      fetchUsers();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to delete user');
    }
  };

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString();
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold text-text-primary dark:text-slate-100">Users</h1>
        <div className="flex gap-2">
          <Button variant="primary" size="sm" onClick={() => setShowInviteForm(true)}>
            <UserPlus className="w-4 h-4 mr-2" />
            Invite User
          </Button>
          <Button variant="secondary" size="sm" onClick={() => { fetchUsers(); fetchInvites(); }}>
            <RefreshCw className="w-4 h-4 mr-2" />
            Refresh
          </Button>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-6">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      {/* Invite Form Modal */}
      {showInviteForm && (
        <Card className="mb-6 p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100 flex items-center gap-2">
              <Mail className="w-5 h-5" />
              Invite New User
            </h2>
            <button
              onClick={() => {
                setShowInviteForm(false);
                setInviteError(null);
              }}
              className="text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-100"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          {inviteError && (
            <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-3 mb-4">
              <p className="text-sm text-red-800 dark:text-red-400">{inviteError}</p>
            </div>
          )}

          <div className="space-y-4">
            {/* Email */}
            <div>
              <label className="block text-sm font-medium text-text-primary dark:text-slate-100 mb-1">
                Email Address
              </label>
              <input
                type="email"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                placeholder="user@example.com"
                className="w-full px-4 py-2 border border-border dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>

            {/* Program Access */}
            <div>
              <label className="block text-sm font-medium text-text-primary dark:text-slate-100 mb-2">
                Program Access
              </label>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                {proprietaryPrograms.map((program) => {
                  const selected = inviteProgramSlugs.includes(program.slug);
                  return (
                    <button
                      key={program.slug}
                      onClick={() => toggleInviteProgram(program.slug)}
                      className={`
                        flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-left
                        transition-colors
                        ${selected
                          ? 'bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-300 hover:bg-green-200 dark:hover:bg-green-800'
                          : 'bg-gray-100 dark:bg-slate-700 text-gray-600 dark:text-slate-400 hover:bg-gray-200 dark:hover:bg-slate-600'
                        }
                      `}
                    >
                      {selected && <Check className="w-4 h-4 flex-shrink-0" />}
                      <span className="truncate">
                        {program.program_name || program.slug}
                      </span>
                    </button>
                  );
                })}
              </div>
              <div className="flex gap-2 mt-2">
                <button
                  onClick={() => setInviteProgramSlugs(proprietaryPrograms.map(p => p.slug))}
                  className="text-xs text-primary hover:underline"
                >
                  Select All
                </button>
                <button
                  onClick={() => setInviteProgramSlugs([])}
                  className="text-xs text-primary hover:underline"
                >
                  Clear All
                </button>
              </div>
            </div>

            {/* Permissions */}
            <div className="flex gap-6">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={inviteCanComment}
                  onChange={(e) => setInviteCanComment(e.target.checked)}
                  className="w-4 h-4 rounded border-border dark:border-slate-600 text-primary focus:ring-primary dark:bg-slate-700"
                />
                <span className="text-sm text-text-primary dark:text-slate-100">Can comment/inspect</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={inviteIsAdmin}
                  onChange={(e) => setInviteIsAdmin(e.target.checked)}
                  className="w-4 h-4 rounded border-border dark:border-slate-600 text-primary focus:ring-primary dark:bg-slate-700"
                />
                <span className="text-sm text-text-primary dark:text-slate-100">Admin privileges</span>
              </label>
            </div>

            {/* Submit */}
            <div className="flex justify-end gap-2 pt-2">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setShowInviteForm(false);
                  setInviteError(null);
                }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={sendInvite}
                disabled={sendingInvite || !inviteEmail.trim()}
              >
                {sendingInvite ? (
                  <>
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                    Sending...
                  </>
                ) : (
                  <>
                    <Mail className="w-4 h-4 mr-2" />
                    Send Invite
                  </>
                )}
              </Button>
            </div>
          </div>
        </Card>
      )}

      {/* Pending Invites */}
      {invites.length > 0 && (
        <Card className="mb-6 p-4">
          <h3 className="text-sm font-medium text-text-primary dark:text-slate-100 mb-3">
            Pending Invites ({invites.length})
          </h3>
          <div className="space-y-2">
            {invites.map((invite) => (
              <div
                key={invite.id}
                className="flex items-center justify-between py-2 px-3 bg-yellow-50 dark:bg-yellow-950 border border-yellow-200 dark:border-yellow-900 rounded-lg"
              >
                <div className="flex items-center gap-3">
                  <Mail className="w-4 h-4 text-yellow-600 dark:text-yellow-400" />
                  <div>
                    <span className="text-sm font-medium text-text-primary dark:text-slate-100">
                      {invite.email}
                    </span>
                    <div className="text-xs text-text-secondary dark:text-slate-400">
                      {invite.program_slugs.length} programs ·
                      Invited {formatDate(invite.created_at)}
                      {invite.invited_by_name && ` by ${invite.invited_by_name}`}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => resendInvite(invite.id, invite.email)}
                    disabled={resendingInvite === invite.id}
                    className="text-text-secondary dark:text-slate-400 hover:text-primary dark:hover:text-primary transition-colors disabled:opacity-50"
                    title="Resend invite email"
                  >
                    {resendingInvite === invite.id ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <RefreshCw className="w-4 h-4" />
                    )}
                  </button>
                  <button
                    onClick={() => cancelInvite(invite.id)}
                    className="text-text-secondary dark:text-slate-400 hover:text-red-600 dark:hover:text-red-400 transition-colors"
                    title="Cancel invite"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card className="overflow-hidden">
        <table className="w-full">
          <thead className="bg-card dark:bg-slate-800 border-b border-border dark:border-slate-700">
            <tr>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                User
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                Joined
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                Program Access
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                Role
              </th>
              <th className="px-6 py-3 text-right text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="bg-white dark:bg-slate-800 divide-y divide-border dark:divide-slate-700">
            {users.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-6 py-12 text-center text-text-secondary dark:text-slate-400">
                  No users found.
                </td>
              </tr>
            ) : (
              users.map((user) => (
                <React.Fragment key={user.user_id}>
                  <tr className={expandedUser === user.user_id ? 'bg-gray-50 dark:bg-slate-700' : ''}>
                    <td className="px-6 py-4">
                      <div className="flex items-center">
                        <div>
                          <div className="text-sm font-medium text-text-primary dark:text-slate-100">
                            {user.full_name}
                          </div>
                          {user.is_group_account && (
                            <span className="text-xs text-text-secondary dark:text-slate-400">(Group account)</span>
                          )}
                        </div>
                      </div>
                    </td>
                    <td className="px-6 py-4 text-sm text-text-secondary dark:text-slate-400">
                      {formatDate(user.created_at)}
                    </td>
                    <td className="px-6 py-4">
                      <button
                        onClick={() => setExpandedUser(
                          expandedUser === user.user_id ? null : user.user_id
                        )}
                        className="flex items-center gap-2 text-sm text-primary hover:underline"
                      >
                        {user.program_access.filter(s => proprietaryPrograms.some(p => p.slug === s)).length} of {proprietaryPrograms.length} proprietary
                        {expandedUser === user.user_id ? (
                          <ChevronUp className="w-4 h-4" />
                        ) : (
                          <ChevronDown className="w-4 h-4" />
                        )}
                      </button>
                    </td>
                    <td className="px-6 py-4">
                      <span
                        className={`inline-flex px-2 py-1 text-xs font-medium rounded-full ${
                          user.is_admin
                            ? 'bg-purple-100 dark:bg-purple-900 text-purple-800 dark:text-purple-300'
                            : 'bg-gray-100 dark:bg-slate-700 text-gray-800 dark:text-slate-300'
                        }`}
                      >
                        {user.is_admin ? 'Admin' : 'User'}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          onClick={() => toggleAdmin(user)}
                          disabled={savingUser === user.user_id}
                          className="text-text-secondary dark:text-slate-400 hover:text-primary transition-colors disabled:opacity-50"
                          title={user.is_admin ? 'Remove admin' : 'Make admin'}
                        >
                          {user.is_admin ? (
                            <ShieldOff className="w-5 h-5" />
                          ) : (
                            <Shield className="w-5 h-5" />
                          )}
                        </button>
                        <button
                          onClick={() => deleteUser(user)}
                          className="text-text-secondary dark:text-slate-400 hover:text-red-600 dark:hover:text-red-400 transition-colors"
                          title="Delete user"
                        >
                          <Trash2 className="w-5 h-5" />
                        </button>
                      </div>
                    </td>
                  </tr>

                  {/* Expanded row for program access */}
                  {expandedUser === user.user_id && (
                    <tr>
                      <td colSpan={5} className="px-6 py-4 bg-gray-50 dark:bg-slate-700 border-t border-border dark:border-slate-600">
                        <div className="space-y-3">
                          <div className="flex items-center justify-between">
                            <span className="text-sm font-medium text-text-primary dark:text-slate-100">
                              Program Access
                            </span>
                            <div className="flex gap-2">
                              <Button
                                variant="secondary"
                                size="sm"
                                onClick={() => grantAllPrograms(user)}
                                disabled={savingUser === user.user_id}
                              >
                                Grant All
                              </Button>
                              <Button
                                variant="secondary"
                                size="sm"
                                onClick={() => revokeAllPrograms(user)}
                                disabled={savingUser === user.user_id}
                              >
                                Revoke All
                              </Button>
                            </div>
                          </div>

                          {proprietaryPrograms.length === 0 ? (
                            <p className="text-sm text-text-secondary dark:text-slate-400">
                              No proprietary programs to manage access for.
                            </p>
                          ) : (
                            <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                              {proprietaryPrograms.map((program) => {
                                const hasAccess = user.program_access.includes(program.slug);
                                return (
                                  <button
                                    key={program.slug}
                                    onClick={() => updateProgramAccess(user, program.slug, !hasAccess)}
                                    disabled={savingUser === user.user_id}
                                    className={`
                                      flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-left
                                      transition-colors disabled:opacity-50
                                      ${hasAccess
                                        ? 'bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-300 hover:bg-green-200 dark:hover:bg-green-800'
                                        : 'bg-gray-100 dark:bg-slate-700 text-gray-600 dark:text-slate-400 hover:bg-gray-200 dark:hover:bg-slate-600'
                                      }
                                    `}
                                  >
                                    {hasAccess && <Check className="w-4 h-4 flex-shrink-0" />}
                                    <span className="truncate">
                                      {program.program_name || program.slug}
                                    </span>
                                  </button>
                                );
                              })}
                            </div>
                          )}

                          {savingUser === user.user_id && (
                            <div className="flex items-center gap-2 text-sm text-text-secondary dark:text-slate-400">
                              <Loader2 className="w-4 h-4 animate-spin" />
                              Saving changes...
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              ))
            )}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
