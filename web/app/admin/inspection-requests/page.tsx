'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Loader2, RefreshCw, Check, X, Telescope } from 'lucide-react';

interface InspectionRequest {
  id: number;
  user_id: string;
  status: 'pending' | 'approved' | 'rejected';
  message: string | null;
  created_at: string;
  reviewed_at: string | null;
  full_name: string;
  username: string;
  email: string | null;
  can_inspect: boolean;
}

type StatusFilter = 'pending' | 'all';

export default function AdminInspectionRequestsPage() {
  const [requests, setRequests] = useState<InspectionRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<StatusFilter>('pending');
  const [acting, setActing] = useState<number | null>(null);

  const fetchRequests = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/admin/inspection-requests?status=${filter}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to fetch requests');
      setRequests(data.requests || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch requests');
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    fetchRequests();
  }, [fetchRequests]);

  const review = async (id: number, action: 'approve' | 'reject') => {
    setActing(id);
    try {
      const res = await fetch(`/api/admin/inspection-requests/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.error || 'Failed to update request');
      }
      fetchRequests();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update request');
    } finally {
      setActing(null);
    }
  };

  const formatDate = (s: string) => new Date(s).toLocaleString();

  const statusBadge = (status: InspectionRequest['status']) => {
    const styles: Record<InspectionRequest['status'], string> = {
      pending: 'bg-yellow-100 dark:bg-yellow-900 text-yellow-800 dark:text-yellow-300',
      approved: 'bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-300',
      rejected: 'bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-300',
    };
    return (
      <span className={`inline-flex px-2 py-1 text-xs font-medium rounded-full capitalize ${styles[status]}`}>
        {status}
      </span>
    );
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold text-text-primary flex items-center gap-2">
          <Telescope className="w-6 h-6 text-primary" />
          Inspection Requests
        </h1>
        <div className="flex gap-2">
          <div className="inline-flex rounded-lg border border-border overflow-hidden">
            {(['pending', 'all'] as StatusFilter[]).map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-3 py-1.5 text-sm capitalize transition-colors ${
                  filter === f
                    ? 'bg-primary text-on-primary'
                    : 'bg-card text-text-secondary hover:text-text-primary'
                }`}
              >
                {f}
              </button>
            ))}
          </div>
          <Button variant="secondary" size="sm" onClick={fetchRequests}>
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

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
        </div>
      ) : requests.length === 0 ? (
        <Card className="p-12 text-center text-text-secondary">
          No {filter === 'pending' ? 'pending ' : ''}inspection requests.
        </Card>
      ) : (
        <div className="space-y-3">
          {requests.map((req) => (
            <Card key={req.id} className="p-4">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium text-text-primary">{req.full_name}</span>
                    <span className="text-xs text-text-secondary">@{req.username}</span>
                    {statusBadge(req.status)}
                    {req.can_inspect && req.status !== 'approved' && (
                      <span className="text-xs text-green-700 dark:text-green-400">(already has access)</span>
                    )}
                  </div>
                  {req.email && (
                    <div className="text-xs text-text-secondary mt-1">{req.email}</div>
                  )}
                  {req.message && (
                    <p className="text-sm text-text-primary mt-2 bg-surface-2 rounded-lg p-2">{req.message}</p>
                  )}
                  <div className="text-xs text-text-secondary mt-2">
                    Requested {formatDate(req.created_at)}
                    {req.reviewed_at && ` · Reviewed ${formatDate(req.reviewed_at)}`}
                  </div>
                </div>

                {req.status === 'pending' && (
                  <div className="flex items-center gap-2 flex-shrink-0">
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => review(req.id, 'approve')}
                      disabled={acting === req.id}
                    >
                      {acting === req.id ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      ) : (
                        <>
                          <Check className="w-4 h-4 mr-1" />
                          Approve
                        </>
                      )}
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => review(req.id, 'reject')}
                      disabled={acting === req.id}
                    >
                      <X className="w-4 h-4 mr-1" />
                      Reject
                    </Button>
                  </div>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
