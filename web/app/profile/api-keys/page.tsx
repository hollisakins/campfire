'use client';

import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useAuth } from '@/lib/contexts/AuthContext';
import {
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
} from 'lucide-react';
import {
  createUserApiKey,
  getUserApiKeys,
  deleteApiKey,
  type ApiKey,
} from '@/lib/actions/api-keys';

export default function ApiKeysPage() {
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();

  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  useEffect(() => {
    fetchApiKeys();
  }, [fetchApiKeys]);

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

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(true);
      setTimeout(() => setCopiedKey(false), 2000);
    } catch (err) {
      alert('Failed to copy to clipboard');
    }
  };

  const dismissNewKey = () => {
    setNewlyCreatedKey(null);
    setShowKey(false);
    setCopiedKey(false);
  };

  // Show login prompt if not authenticated
  if (!authLoading && !user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Profile', href: '/profile' },
            { label: 'API Keys' },
          ]}
          className="mb-6"
        />

        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Sign in to manage API keys
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
            Please sign in to create and manage your API keys.
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
            { label: 'Profile', href: '/profile' },
            { label: 'API Keys' },
          ]}
          className="mb-6"
        />
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary">Loading API keys...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'Profile', href: '/profile' },
          { label: 'API Keys' },
        ]}
        className="mb-6"
      />

      <div className="max-w-4xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold text-text-primary flex items-center gap-2">
              <Key className="w-6 h-6 text-primary" />
              API Keys
            </h1>
            <p className="text-text-secondary mt-1">
              Manage your API keys for the CAMPFIRE Python client
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

        {/* Error Message */}
        {error && (
          <div className="p-4 bg-red-50 border border-red-200 rounded-lg flex items-start gap-2">
            <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
            <p className="text-sm text-red-800">{error}</p>
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

        {/* Create Form */}
        {showCreateForm && (
          <Card className="p-6">
            <h3 className="text-lg font-semibold text-text-primary mb-4">Create New API Key</h3>
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
          </Card>
        )}

        {/* API Keys List */}
        <Card className="p-6">
          <h3 className="text-lg font-semibold text-text-primary mb-4">Your API Keys</h3>

          {apiKeys.length === 0 ? (
            <div className="text-center py-8">
              <Key className="w-12 h-12 text-text-secondary mx-auto mb-3 opacity-50" />
              <p className="text-text-secondary">
                No API keys yet. Create one to get started with the Python client.
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
                      : 'border-gray-300 bg-gray-50 opacity-60'
                  }`}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-3 mb-2">
                        <code className="text-sm font-mono text-text-primary bg-gray-100 px-2 py-1 rounded">
                          {key.key_prefix}
                        </code>
                        {!key.is_active && (
                          <span className="text-xs px-2 py-1 bg-red-100 text-red-700 rounded">
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
                        {!key.last_used_at && <span className="text-gray-400">Never used</span>}
                      </div>
                    </div>

                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => handleDeleteKey(key.id)}
                      className="flex items-center gap-2 text-red-600 hover:text-red-700 hover:bg-red-50"
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

        {/* Documentation Card */}
        <Card className="p-6 bg-blue-50 border-blue-200">
          <h3 className="text-lg font-semibold text-blue-900 mb-2">Using Your API Key</h3>
          <p className="text-sm text-blue-800 mb-4">
            Use your API key with the CAMPFIRE Python client to query and download spectra programmatically.
          </p>

          <div className="bg-white rounded-lg p-4 font-mono text-sm border border-blue-200">
            <div className="text-gray-600 mb-2"># Install the Python client</div>
            <div className="text-gray-900 mb-4">pip install campfire-api</div>

            <div className="text-gray-600 mb-2"># Set your API key</div>
            <div className="text-gray-900 mb-4">export CAMPFIRE_API_KEY=sk_live_...</div>

            <div className="text-gray-600 mb-2"># Use the client</div>
            <div className="text-gray-900">from campfire import Campfire</div>
            <div className="text-gray-900">cf = Campfire()</div>
            <div className="text-gray-900">results = cf.query_objects(limit=10)</div>
          </div>

          <div className="mt-4">
            <a
              href="/python/README.md"
              target="_blank"
              className="text-sm text-blue-700 hover:text-blue-800 underline"
            >
              View full Python client documentation →
            </a>
          </div>
        </Card>
      </div>
    </div>
  );
}
