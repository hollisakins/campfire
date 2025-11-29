'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import {
  Loader2,
  RefreshCw,
  Globe,
  Lock,
  Users,
  FileText,
} from 'lucide-react';
import type { Program } from '@/lib/types';

interface ProgramWithStats extends Program {
  object_count: number;
  user_access_count: number;
}

export default function AdminProgramsPage() {
  const [programs, setPrograms] = useState<ProgramWithStats[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updatingProgram, setUpdatingProgram] = useState<number | null>(null);

  const fetchPrograms = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const response = await fetch('/api/programs');
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to fetch programs');
      }

      setPrograms(data.programs || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch programs');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchPrograms();
  }, [fetchPrograms]);

  const togglePublic = async (program: ProgramWithStats) => {
    setUpdatingProgram(program.program_id);
    try {
      const response = await fetch(`/api/programs/${program.program_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_public: !program.is_public }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update program');
      }

      fetchPrograms();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update program');
    } finally {
      setUpdatingProgram(null);
    }
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
        <h1 className="text-2xl font-semibold text-text-primary">Programs</h1>
        <Button variant="secondary" size="sm" onClick={fetchPrograms}>
          <RefreshCw className="w-4 h-4 mr-2" />
          Refresh
        </Button>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-6">
          <p className="text-red-800">{error}</p>
        </div>
      )}

      <div className="mb-6 p-4 bg-blue-50 border border-blue-200 rounded-lg">
        <p className="text-sm text-blue-800">
          <strong>Public programs</strong> are visible to all authenticated users.{' '}
          <strong>Private programs</strong> require users to redeem an access code.
        </p>
      </div>

      <Card className="overflow-hidden">
        <table className="w-full">
          <thead className="bg-card border-b border-border">
            <tr>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                Program
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                PI
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                Objects
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                Users with Access
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                Visibility
              </th>
              <th className="px-6 py-3 text-right text-xs font-medium text-text-secondary uppercase tracking-wider">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-border">
            {programs.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-6 py-12 text-center text-text-secondary">
                  No programs found.
                </td>
              </tr>
            ) : (
              programs.map((program) => (
                <tr key={program.program_id}>
                  <td className="px-6 py-4">
                    <div>
                      <div className="text-sm font-medium text-text-primary">
                        {program.program_name || `Program ${program.program_id}`}
                      </div>
                      <div className="text-xs text-text-secondary">
                        ID: {program.program_id}
                      </div>
                    </div>
                  </td>
                  <td className="px-6 py-4 text-sm text-text-secondary">
                    {program.pi_name || '—'}
                  </td>
                  <td className="px-6 py-4">
                    <div className="flex items-center gap-1 text-sm text-text-primary">
                      <FileText className="w-4 h-4 text-text-secondary" />
                      {program.object_count}
                    </div>
                  </td>
                  <td className="px-6 py-4">
                    <div className="flex items-center gap-1 text-sm text-text-primary">
                      <Users className="w-4 h-4 text-text-secondary" />
                      {program.is_public ? 'All' : program.user_access_count}
                    </div>
                  </td>
                  <td className="px-6 py-4">
                    <span
                      className={`inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-full ${
                        program.is_public
                          ? 'bg-blue-100 text-blue-800'
                          : 'bg-gray-100 text-gray-800'
                      }`}
                    >
                      {program.is_public ? (
                        <>
                          <Globe className="w-3 h-3" />
                          Public
                        </>
                      ) : (
                        <>
                          <Lock className="w-3 h-3" />
                          Private
                        </>
                      )}
                    </span>
                  </td>
                  <td className="px-6 py-4 text-right">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => togglePublic(program)}
                      disabled={updatingProgram === program.program_id}
                    >
                      {updatingProgram === program.program_id ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      ) : program.is_public ? (
                        <>
                          <Lock className="w-4 h-4 mr-1" />
                          Make Private
                        </>
                      ) : (
                        <>
                          <Globe className="w-4 h-4 mr-1" />
                          Make Public
                        </>
                      )}
                    </Button>
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
