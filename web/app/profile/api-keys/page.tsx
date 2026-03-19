'use client';

import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useAuth } from '@/lib/contexts/AuthContext';
import {
  Terminal,
  Key,
  Plus,
  Trash2,
  Copy,
  Check,
  AlertCircle,
  Loader2,
  LogIn,
  Eye,
  EyeOff,
  Shield,
  ExternalLink,
  XCircle,
} from 'lucide-react';
import {
  createUserApiKey,
  getUserApiKeys,
  deleteApiKey,
  type ApiKey,
} from '@/lib/actions/api-keys';
import {
  getUserSessions,
  revokeSession,
  type Session,
} from '@/lib/actions/sessions';

export default function ApiKeysPage() {
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();

  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [revokingSession, setRevokingSession] = useState<string | null>(null);

  // Create key state
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newKeyName, setNewKeyName] = useState('');
  const [creating, setCreating] = useState(false);
  const [newlyCreatedKey, setNewlyCreatedKey] = useState<string | null>(null);
  const [copiedKey, setCopiedKey] = useState(false);
  const [showKey, setShowKey] = useState(false);

  const fetchApiKeys = useCallback(async () => {
    if (authLoading || !user) return;

    setLoading(true);
    setError(null);

    try {
      const result = await getUserApiKeys();

      if (result.error) {
        throw new Error(result.error);
      }

      setApiKeys(result.keys);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch API keys');
    } finally {
      setLoading(false);
    }
  }, [authLoading, user]);

  const fetchSessions = useCallback(async () => {
    if (authLoading || !user) return;

    setSessionsLoading(true);

    try {
      const result = await getUserSessions();
      if (!result.error) {
        setSessions(result.sessions);
      }
    } catch {
      // Sessions are non-critical, don't show error
    } finally {
      setSessionsLoading(false);
    }
  }, [authLoading, user]);

  useEffect(() => {
    fetchApiKeys();
    fetchSessions();
  }, [fetchApiKeys, fetchSessions]);

  const handleCreateKey = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreating(true);
    setError(null);

    try {
      const result = await createUserApiKey(newKeyName.trim() || undefined);

      if (!result.success || !result.key) {
        throw new Error(result.error || 'Failed to create API key');
      }

      setNewlyCreatedKey(result.key);
      setNewKeyName('');
      setShowCreateForm(false);
      fetchApiKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create API key');
    } finally {
      setCreating(false);
    }
  };

  const handleDeleteKey = async (keyId: string) => {
    if (!confirm('Are you sure you want to delete this API key? This action cannot be undone.')) {
      return;
    }

    try {
      const result = await deleteApiKey(keyId);

      if (!result.success) {
        throw new Error(result.error || 'Failed to delete API key');
      }

      fetchApiKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete API key');
    }
  };

  const handleRevokeSession = async (sessionId: string) => {
    setRevokingSession(sessionId);

    try {
      const result = await revokeSession(sessionId);

      if (!result.success) {
        throw new Error(result.error || 'Failed to revoke session');
      }

      fetchSessions();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke session');
    } finally {
      setRevokingSession(null);
    }
  };

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(true);
      setTimeout(() => setCopiedKey(false), 2000);
    } catch {
      alert('Failed to copy to clipboard');
    }
  };

  const dismissNewKey = () => {
    setNewlyCreatedKey(null);
    setShowKey(false);
    setCopiedKey(false);
  };

  const breadcrumbs = [
    { label: 'CAMPFIRE', href: '/' },
    { label: 'Profile', href: '/profile' },
    { label: 'CLI & API Access' },
  ];

  // Show login prompt if not authenticated
  if (!authLoading && !user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs items={breadcrumbs} className="mb-6" />

        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Sign in to manage CLI & API access
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
            Please sign in to manage your CLI sessions and API keys.
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

  if ((loading && sessionsLoading) || authLoading) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs items={breadcrumbs} className="mb-6" />
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary">Loading...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs items={breadcrumbs} className="mb-6" />

      <div className="max-w-4xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold text-text-primary flex items-center gap-2">
              <Terminal className="w-6 h-6 text-primary" />
              CLI & API Access
            </h1>
            <p className="text-text-secondary mt-1">
              Manage CLI sessions and API keys for programmatic access
            </p>
          </div>
        </div>

        {/* Error Message */}
        {error && (
          <div className="p-4 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg flex items-start gap-2">
            <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
            <p className="text-sm text-red-800 dark:text-red-400">{error}</p>
          </div>
        )}

        {/* Newly Created Key Warning */}
        {newlyCreatedKey && (
          <Card className="p-6 border-2 border-green-500">
            <div className="flex items-start gap-3 mb-4">
              <Shield className="w-6 h-6 text-green-600 flex-shrink-0 mt-1" />
              <div className="flex-1">
                <h3 className="text-lg font-semibold text-text-primary mb-2">
                  Save Your API Key
                </h3>
                <p className="text-sm text-text-secondary mb-4">
                  This is the only time you&apos;ll see this key. Make sure to copy it now and store it somewhere safe.
                </p>

                <div className="flex items-center gap-2">
                  <div className="flex-1 relative">
                    <input
                      type={showKey ? 'text' : 'password'}
                      value={newlyCreatedKey}
                      readOnly
                      className="w-full px-4 py-3 pr-24 border-2 border-green-300 rounded-lg font-mono text-sm bg-green-50 text-green-900 focus:outline-none"
                    />
                    <button
                      onClick={() => setShowKey(!showKey)}
                      className="absolute right-2 top-1/2 -translate-y-1/2 p-2 hover:bg-green-100 rounded"
                      title={showKey ? 'Hide key' : 'Show key'}
                    >
                      {showKey ? (
                        <EyeOff className="w-4 h-4 text-green-700" />
                      ) : (
                        <Eye className="w-4 h-4 text-green-700" />
                      )}
                    </button>
                  </div>
                  <Button
                    variant="primary"
                    onClick={() => copyToClipboard(newlyCreatedKey)}
                    className="flex items-center gap-2"
                  >
                    {copiedKey ? (
                      <>
                        <Check className="w-4 h-4" />
                        Copied!
                      </>
                    ) : (
                      <>
                        <Copy className="w-4 h-4" />
                        Copy
                      </>
                    )}
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={dismissNewKey}
                  >
                    Dismiss
                  </Button>
                </div>
              </div>
            </div>
          </Card>
        )}

        {/* Getting Started */}
        <Card className="p-6">
          <h3 className="text-lg font-semibold text-text-primary mb-2">Getting Started</h3>
          <p className="text-sm text-text-secondary mb-4">
            Install the Python client and authenticate to access CAMPFIRE data programmatically.
          </p>

          <div className="space-y-4">
            <div>
              <p className="text-sm font-medium text-text-primary mb-1.5">1. Install</p>
              <div className="bg-gray-50 dark:bg-slate-800 rounded-lg p-3 font-mono text-sm border border-border">
                <span className="text-text-primary">pip install &quot;git+https://github.com/hollisakins/campfire.git#subdirectory=python/&quot;</span>
              </div>
            </div>

            <div>
              <p className="text-sm font-medium text-text-primary mb-1.5">2. Authenticate <span className="text-xs font-normal text-text-secondary">(recommended)</span></p>
              <div className="bg-gray-50 dark:bg-slate-800 rounded-lg p-3 font-mono text-sm border border-border">
                <span className="text-text-primary">campfire login</span>
              </div>
              <p className="text-xs text-text-secondary mt-1.5">
                Opens your browser for secure OAuth authentication. Tokens refresh automatically.
              </p>
            </div>

            <div>
              <p className="text-sm font-medium text-text-primary mb-1.5">3. Alternative: API key <span className="text-xs font-normal text-text-secondary">(headless environments)</span></p>
              <div className="bg-gray-50 dark:bg-slate-800 rounded-lg p-3 font-mono text-sm border border-border">
                <span className="text-text-primary">campfire login --api-key</span>
              </div>
              <p className="text-xs text-text-secondary mt-1.5">
                For servers and HPC clusters without a browser. Create an API key below.
              </p>
            </div>
          </div>

          <div className="mt-4 pt-4 border-t border-border">
            <Link
              href="/docs/api"
              className="inline-flex items-center gap-1.5 text-sm text-primary hover:text-primary-hover transition-colors"
            >
              View full documentation
              <ExternalLink className="w-3.5 h-3.5" />
            </Link>
          </div>
        </Card>

        {/* Active Sessions */}
        <Card className="p-6">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h3 className="text-lg font-semibold text-text-primary">Active Sessions</h3>
              <p className="text-sm text-text-secondary mt-0.5">
                Devices authenticated via <code className="text-xs bg-gray-100 dark:bg-slate-700 px-1.5 py-0.5 rounded">campfire login</code>
              </p>
            </div>
          </div>

          {sessionsLoading ? (
            <div className="flex items-center justify-center py-6">
              <Loader2 className="w-5 h-5 animate-spin text-text-secondary" />
            </div>
          ) : sessions.length === 0 ? (
            <div className="text-center py-6">
              <Terminal className="w-10 h-10 text-text-secondary mx-auto mb-2 opacity-50" />
              <p className="text-sm text-text-secondary">
                No active CLI sessions. Run <code className="text-xs bg-gray-100 dark:bg-slate-700 px-1.5 py-0.5 rounded">campfire login</code> to get started.
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {sessions.map((session) => (
                <div
                  key={session.id}
                  className="p-4 border border-border rounded-lg bg-background"
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <p className="text-sm font-medium text-text-primary">
                        {session.device_name || 'Python Client'}
                      </p>
                      <div className="flex items-center gap-4 text-xs text-text-secondary mt-1">
                        <span>Signed in {new Date(session.created_at).toLocaleDateString()}</span>
                        {session.last_used_at && (
                          <span>Last used {new Date(session.last_used_at).toLocaleDateString()}</span>
                        )}
                      </div>
                    </div>

                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => handleRevokeSession(session.id)}
                      disabled={revokingSession === session.id}
                      className="flex items-center gap-1.5 text-red-600 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300 hover:bg-red-50 dark:hover:bg-red-900/30"
                    >
                      {revokingSession === session.id ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      ) : (
                        <XCircle className="w-3.5 h-3.5" />
                      )}
                      Revoke
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* API Keys Section */}
        <Card className="p-6">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h3 className="text-lg font-semibold text-text-primary">API Keys</h3>
              <p className="text-sm text-text-secondary mt-0.5">
                For headless environments without a browser
              </p>
            </div>
            {!showCreateForm && (
              <Button
                variant="primary"
                onClick={() => setShowCreateForm(true)}
                className="flex items-center gap-2"
              >
                <Plus className="w-4 h-4" />
                New API Key
              </Button>
            )}
          </div>

          {/* Create Form */}
          {showCreateForm && (
            <div className="mb-6 p-4 border border-border rounded-lg">
              <form onSubmit={handleCreateKey} className="space-y-4">
                <div>
                  <label htmlFor="keyName" className="block text-sm font-medium text-text-primary mb-2">
                    Key Name (optional)
                  </label>
                  <input
                    id="keyName"
                    type="text"
                    value={newKeyName}
                    onChange={(e) => setNewKeyName(e.target.value)}
                    placeholder="e.g., My Laptop, Production Server"
                    className="w-full px-4 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                    disabled={creating}
                  />
                  <p className="text-sm text-text-secondary mt-1">
                    Give your key a name to help identify where it&apos;s being used
                  </p>
                </div>

                <div className="flex gap-3">
                  <Button
                    type="submit"
                    variant="primary"
                    disabled={creating}
                    className="flex items-center gap-2"
                  >
                    {creating ? (
                      <>
                        <Loader2 className="w-4 h-4 animate-spin" />
                        Creating...
                      </>
                    ) : (
                      <>
                        <Plus className="w-4 h-4" />
                        Create Key
                      </>
                    )}
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() => {
                      setShowCreateForm(false);
                      setNewKeyName('');
                    }}
                    disabled={creating}
                  >
                    Cancel
                  </Button>
                </div>
              </form>
            </div>
          )}

          {/* Keys List */}
          {apiKeys.length === 0 ? (
            <div className="text-center py-6">
              <Key className="w-10 h-10 text-text-secondary mx-auto mb-2 opacity-50" />
              <p className="text-sm text-text-secondary">
                No API keys yet. Create one for use on headless servers or HPC clusters.
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {apiKeys.map((key) => (
                <div
                  key={key.id}
                  className={`p-4 border rounded-lg ${
                    key.is_active
                      ? 'border-border bg-background'
                      : 'border-gray-300 dark:border-slate-600 bg-gray-50 dark:bg-slate-800 opacity-60'
                  }`}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-3 mb-2">
                        <code className="text-sm font-mono text-text-primary bg-gray-100 dark:bg-slate-700 px-2 py-1 rounded">
                          {key.key_prefix}
                        </code>
                        {!key.is_active && (
                          <span className="text-xs px-2 py-1 bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-300 rounded">
                            Revoked
                          </span>
                        )}
                      </div>

                      {key.name && (
                        <p className="text-sm font-medium text-text-primary mb-1">{key.name}</p>
                      )}

                      <div className="flex items-center gap-4 text-xs text-text-secondary">
                        <span>Created {new Date(key.created_at).toLocaleDateString()}</span>
                        {key.last_used_at && (
                          <span>
                            Last used {new Date(key.last_used_at).toLocaleDateString()}
                          </span>
                        )}
                        {!key.last_used_at && <span className="text-gray-400 dark:text-slate-500">Never used</span>}
                      </div>
                    </div>

                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => handleDeleteKey(key.id)}
                      className="flex items-center gap-2 text-red-600 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300 hover:bg-red-50 dark:hover:bg-red-900/30"
                    >
                      <Trash2 className="w-4 h-4" />
                      Delete
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
