'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { MultiSelect } from '@/components/ui/MultiSelect';
import {
  Plus,
  Loader2,
  Copy,
  Check,
  ToggleLeft,
  ToggleRight,
  Trash2,
  RefreshCw,
} from 'lucide-react';
import type { AccessCode, Program } from '@/lib/types';

interface CodeWithRedemptions extends AccessCode {
  code_redemptions?: { id: string; user_id: string; redeemed_at: string }[];
}

export default function AdminCodesPage() {
  const [codes, setCodes] = useState<CodeWithRedemptions[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Program selection state
  const [allPrograms, setAllPrograms] = useState<Program[]>([]);
  const [selectedPrograms, setSelectedPrograms] = useState<number[]>([]);

  // Form state
  const [showForm, setShowForm] = useState(false);
  const [formData, setFormData] = useState({
    code: '',
    description: '',
    grants_all_programs: true,
    expires_in_days: '',
    max_uses: '',
  });
  const [formLoading, setFormLoading] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Copy state
  const [copiedCode, setCopiedCode] = useState<string | null>(null);

  const fetchCodes = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const response = await fetch('/api/codes');
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to fetch codes');
      }

      setCodes(data.codes || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch codes');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchCodes();
  }, [fetchCodes]);

  // Fetch available programs for specific program selection
  useEffect(() => {
    const fetchPrograms = async () => {
      try {
        const response = await fetch('/api/programs');
        const data = await response.json();
        if (response.ok) {
          setAllPrograms(data.programs || []);
        }
      } catch (err) {
        console.error('Failed to fetch programs:', err);
      }
    };
    fetchPrograms();
  }, []);

  const generateRandomCode = () => {
    const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    let code = 'CAMPFIRE-';
    for (let i = 0; i < 6; i++) {
      code += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    setFormData(prev => ({ ...prev, code }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormLoading(true);
    setFormError(null);

    try {
      const payload: Record<string, unknown> = {
        code: formData.code,
        description: formData.description || null,
        grants_all_programs: formData.grants_all_programs,
      };

      // Add program_ids if specific programs selected
      if (!formData.grants_all_programs) {
        if (selectedPrograms.length === 0) {
          setFormError('Please select at least one program');
          setFormLoading(false);
          return;
        }
        payload.program_ids = selectedPrograms;
      }

      if (formData.expires_in_days) {
        const days = parseInt(formData.expires_in_days);
        if (days > 0) {
          const expiresAt = new Date();
          expiresAt.setDate(expiresAt.getDate() + days);
          payload.expires_at = expiresAt.toISOString();
        }
      }

      if (formData.max_uses) {
        const maxUses = parseInt(formData.max_uses);
        if (maxUses > 0) {
          payload.max_uses = maxUses;
        }
      }

      const response = await fetch('/api/codes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to create code');
      }

      // Reset form and refresh list
      setFormData({
        code: '',
        description: '',
        grants_all_programs: true,
        expires_in_days: '',
        max_uses: '',
      });
      setShowForm(false);
      fetchCodes();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to create code');
    } finally {
      setFormLoading(false);
    }
  };

  const toggleCodeActive = async (code: CodeWithRedemptions) => {
    try {
      const response = await fetch(`/api/codes/${code.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: !code.is_active }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update code');
      }

      fetchCodes();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update code');
    }
  };

  const deleteCode = async (code: CodeWithRedemptions) => {
    if (!confirm(`Delete code "${code.code}"? This cannot be undone.`)) {
      return;
    }

    try {
      const response = await fetch(`/api/codes/${code.id}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to delete code');
      }

      fetchCodes();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to delete code');
    }
  };

  const copyCode = async (code: string) => {
    await navigator.clipboard.writeText(code);
    setCopiedCode(code);
    setTimeout(() => setCopiedCode(null), 2000);
  };

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return 'Never';
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
        <h1 className="text-2xl font-semibold text-text-primary">Access Codes</h1>
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" onClick={fetchCodes}>
            <RefreshCw className="w-4 h-4 mr-2" />
            Refresh
          </Button>
          <Button variant="primary" size="sm" onClick={() => setShowForm(!showForm)}>
            <Plus className="w-4 h-4 mr-2" />
            New Code
          </Button>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-6">
          <p className="text-red-800">{error}</p>
        </div>
      )}

      {/* Create Code Form */}
      {showForm && (
        <Card className="p-6 mb-6">
          <h2 className="text-lg font-semibold text-text-primary mb-4">Create New Code</h2>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-text-primary mb-1">
                  Code
                </label>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={formData.code}
                    onChange={(e) => setFormData(prev => ({ ...prev, code: e.target.value.toUpperCase() }))}
                    placeholder="CAMPFIRE-2024"
                    className="flex-1 px-3 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary font-mono uppercase"
                    required
                  />
                  <Button type="button" variant="secondary" size="sm" onClick={generateRandomCode}>
                    Generate
                  </Button>
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-text-primary mb-1">
                  Description
                </label>
                <input
                  type="text"
                  value={formData.description}
                  onChange={(e) => setFormData(prev => ({ ...prev, description: e.target.value }))}
                  placeholder="Core team access"
                  className="w-full px-3 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                />
              </div>
            </div>

            <div className="grid grid-cols-3 gap-4">
              <div>
                <label className="block text-sm font-medium text-text-primary mb-1">
                  Access Level
                </label>
                <select
                  value={formData.grants_all_programs ? 'all' : 'specific'}
                  onChange={(e) => {
                    const isAll = e.target.value === 'all';
                    setFormData(prev => ({ ...prev, grants_all_programs: isAll }));
                    if (isAll) setSelectedPrograms([]);
                  }}
                  className="w-full px-3 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                >
                  <option value="all">All Programs</option>
                  <option value="specific">Specific Programs</option>
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium text-text-primary mb-1">
                  Expires In (days)
                </label>
                <input
                  type="number"
                  value={formData.expires_in_days}
                  onChange={(e) => setFormData(prev => ({ ...prev, expires_in_days: e.target.value }))}
                  placeholder="Never"
                  min="1"
                  className="w-full px-3 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-text-primary mb-1">
                  Max Uses
                </label>
                <input
                  type="number"
                  value={formData.max_uses}
                  onChange={(e) => setFormData(prev => ({ ...prev, max_uses: e.target.value }))}
                  placeholder="Unlimited"
                  min="1"
                  className="w-full px-3 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                />
              </div>
            </div>

            {/* Program Selection (only shown when specific programs is selected) */}
            {!formData.grants_all_programs && (
              <div>
                <MultiSelect
                  options={allPrograms.map(p => ({ id: p.program_id, name: p.program_name }))}
                  selected={selectedPrograms}
                  onChange={setSelectedPrograms}
                  label="Select Programs"
                  maxHeight="max-h-48"
                />
              </div>
            )}

            {formError && (
              <div className="bg-red-50 border border-red-200 rounded-lg p-3">
                <p className="text-red-800 text-sm">{formError}</p>
              </div>
            )}

            <div className="flex justify-end gap-2">
              <Button type="button" variant="secondary" onClick={() => setShowForm(false)}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={formLoading}>
                {formLoading ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin mr-2" />
                    Creating...
                  </>
                ) : (
                  'Create Code'
                )}
              </Button>
            </div>
          </form>
        </Card>
      )}

      {/* Codes List */}
      <Card className="overflow-hidden">
        <table className="w-full">
          <thead className="bg-card border-b border-border">
            <tr>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                Code
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                Description
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                Access
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                Uses
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                Expires
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                Status
              </th>
              <th className="px-6 py-3 text-right text-xs font-medium text-text-secondary uppercase tracking-wider">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-border">
            {codes.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-6 py-12 text-center text-text-secondary">
                  No access codes yet. Create one to get started.
                </td>
              </tr>
            ) : (
              codes.map((code) => (
                <tr key={code.id} className={!code.is_active ? 'opacity-50' : ''}>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm font-medium text-text-primary">
                        {code.code}
                      </span>
                      <button
                        onClick={() => copyCode(code.code)}
                        className="text-text-secondary hover:text-primary transition-colors"
                        title="Copy code"
                      >
                        {copiedCode === code.code ? (
                          <Check className="w-4 h-4 text-green-600" />
                        ) : (
                          <Copy className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                  </td>
                  <td className="px-6 py-4 text-sm text-text-secondary">
                    {code.description || '—'}
                  </td>
                  <td className="px-6 py-4 text-sm text-text-primary">
                    {code.grants_all_programs ? 'All programs' : 'Specific'}
                  </td>
                  <td className="px-6 py-4 text-sm text-text-primary">
                    {code.use_count}
                    {code.max_uses ? ` / ${code.max_uses}` : ''}
                  </td>
                  <td className="px-6 py-4 text-sm text-text-secondary">
                    {formatDate(code.expires_at)}
                  </td>
                  <td className="px-6 py-4">
                    <span
                      className={`inline-flex px-2 py-1 text-xs font-medium rounded-full ${
                        code.is_active
                          ? 'bg-green-100 text-green-800'
                          : 'bg-gray-100 text-gray-800'
                      }`}
                    >
                      {code.is_active ? 'Active' : 'Disabled'}
                    </span>
                  </td>
                  <td className="px-6 py-4 text-right">
                    <div className="flex items-center justify-end gap-2">
                      <button
                        onClick={() => toggleCodeActive(code)}
                        className="text-text-secondary hover:text-primary transition-colors"
                        title={code.is_active ? 'Disable' : 'Enable'}
                      >
                        {code.is_active ? (
                          <ToggleRight className="w-5 h-5 text-green-600" />
                        ) : (
                          <ToggleLeft className="w-5 h-5" />
                        )}
                      </button>
                      <button
                        onClick={() => deleteCode(code)}
                        className="text-text-secondary hover:text-red-600 transition-colors"
                        title="Delete"
                      >
                        <Trash2 className="w-5 h-5" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
