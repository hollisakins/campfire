'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import {
  Loader2,
  RefreshCw,
  Check,
  X,
  Clock,
  CheckCircle,
  XCircle,
  Trash2,
  Mail,
} from 'lucide-react';
import type { Program } from '@/lib/types';

type AccountRequestStatus = 'pending' | 'approved' | 'rejected';

interface AccountRequest {
  id: number;
  email: string;
  full_name: string;
  status: AccountRequestStatus;
  is_admin: boolean;
  can_comment: boolean;
  program_ids: number[];
  created_at: string;
  reviewed_at: string | null;
  reviewed_by: string | null;
  reviewed_by_name: string | null;
  rejection_reason: string | null;
}

interface RequestCounts {
  total: number;
  pending: number;
  approved: number;
  rejected: number;
}

export default function AdminRequestsPage() {
  const [requests, setRequests] = useState<AccountRequest[]>([]);
  const [programs, setPrograms] = useState<Program[]>([]);
  const [counts, setCounts] = useState<RequestCounts>({ total: 0, pending: 0, approved: 0, rejected: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<AccountRequestStatus | 'all'>('pending');

  // Approval modal state
  const [showApproveModal, setShowApproveModal] = useState(false);
  const [selectedRequest, setSelectedRequest] = useState<AccountRequest | null>(null);
  const [approveProgramIds, setApproveProgramIds] = useState<number[]>([]);
  const [approveIsAdmin, setApproveIsAdmin] = useState(false);
  const [approveCanComment, setApproveCanComment] = useState(true);
  const [processing, setProcessing] = useState(false);

  // Rejection modal state
  const [showRejectModal, setShowRejectModal] = useState(false);
  const [rejectReason, setRejectReason] = useState('');

  const fetchRequests = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const url = statusFilter === 'all'
        ? '/api/admin/account-requests'
        : `/api/admin/account-requests?status=${statusFilter}`;

      const response = await fetch(url);
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to fetch requests');
      }

      setRequests(data.requests || []);
      setCounts(data.counts || { total: 0, pending: 0, approved: 0, rejected: 0 });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch requests');
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  const fetchPrograms = useCallback(async () => {
    try {
      const response = await fetch('/api/users');
      const data = await response.json();
      if (response.ok) {
        setPrograms(data.programs || []);
      }
    } catch (err) {
      console.error('Error fetching programs:', err);
    }
  }, []);

  useEffect(() => {
    fetchRequests();
    fetchPrograms();
  }, [fetchRequests, fetchPrograms]);

  const openApproveModal = (request: AccountRequest) => {
    setSelectedRequest(request);
    setApproveProgramIds([]);
    setApproveIsAdmin(false);
    setApproveCanComment(true);
    setShowApproveModal(true);
  };

  const openRejectModal = (request: AccountRequest) => {
    setSelectedRequest(request);
    setRejectReason('');
    setShowRejectModal(true);
  };

  const handleApprove = async () => {
    if (!selectedRequest) return;

    setProcessing(true);
    try {
      const response = await fetch(`/api/admin/account-requests/${selectedRequest.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: 'approve',
          program_ids: approveProgramIds,
          is_admin: approveIsAdmin,
          can_comment: approveCanComment,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to approve request');
      }

      setShowApproveModal(false);
      setSelectedRequest(null);
      fetchRequests();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to approve request');
    } finally {
      setProcessing(false);
    }
  };

  const handleReject = async () => {
    if (!selectedRequest) return;

    setProcessing(true);
    try {
      const response = await fetch(`/api/admin/account-requests/${selectedRequest.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: 'reject',
          rejection_reason: rejectReason || null,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Failed to reject request');
      }

      setShowRejectModal(false);
      setSelectedRequest(null);
      fetchRequests();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to reject request');
    } finally {
      setProcessing(false);
    }
  };

  const handleDelete = async (request: AccountRequest) => {
    if (!confirm(`Delete the request from "${request.full_name}"? This cannot be undone.`)) {
      return;
    }

    try {
      const response = await fetch(`/api/admin/account-requests/${request.id}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to delete request');
      }

      fetchRequests();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to delete request');
    }
  };

  const toggleApproveProgram = (programId: number) => {
    setApproveProgramIds(prev =>
      prev.includes(programId)
        ? prev.filter(id => id !== programId)
        : [...prev, programId]
    );
  };

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const getStatusBadge = (status: AccountRequestStatus) => {
    switch (status) {
      case 'pending':
        return (
          <span className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-full bg-yellow-100 dark:bg-yellow-900 text-yellow-800 dark:text-yellow-300">
            <Clock className="w-3 h-3" />
            Pending
          </span>
        );
      case 'approved':
        return (
          <span className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-full bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-300">
            <CheckCircle className="w-3 h-3" />
            Approved
          </span>
        );
      case 'rejected':
        return (
          <span className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-full bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-300">
            <XCircle className="w-3 h-3" />
            Rejected
          </span>
        );
    }
  };

  if (loading && requests.length === 0) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold text-text-primary dark:text-slate-100">Account Requests</h1>
        <Button variant="secondary" size="sm" onClick={fetchRequests} disabled={loading}>
          <RefreshCw className={`w-4 h-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>

      {error && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-6">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      {/* Filter Tabs */}
      <div className="flex gap-2 mb-6">
        {(['all', 'pending', 'approved', 'rejected'] as const).map((status) => {
          const count = status === 'all' ? counts.total : counts[status];
          const isActive = statusFilter === status;
          return (
            <button
              key={status}
              onClick={() => setStatusFilter(status)}
              className={`
                px-4 py-2 rounded-lg text-sm font-medium transition-colors
                ${isActive
                  ? 'bg-primary text-white'
                  : 'bg-gray-100 dark:bg-slate-700 text-text-secondary dark:text-slate-400 hover:bg-gray-200 dark:hover:bg-slate-600'
                }
              `}
            >
              {status.charAt(0).toUpperCase() + status.slice(1)}
              {count > 0 && (
                <span className={`ml-2 px-2 py-0.5 rounded-full text-xs ${
                  isActive ? 'bg-white/20' : 'bg-gray-200 dark:bg-slate-600'
                }`}>
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Requests Table */}
      <Card className="overflow-hidden">
        <table className="w-full">
          <thead className="bg-card dark:bg-slate-800 border-b border-border dark:border-slate-700">
            <tr>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                Requester
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                Submitted
              </th>
              <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                Status
              </th>
              <th className="px-6 py-3 text-right text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="bg-white dark:bg-slate-800 divide-y divide-border dark:divide-slate-700">
            {requests.length === 0 ? (
              <tr>
                <td colSpan={4} className="px-6 py-12 text-center text-text-secondary dark:text-slate-400">
                  No {statusFilter === 'all' ? '' : statusFilter} requests found.
                </td>
              </tr>
            ) : (
              requests.map((request) => (
                <tr key={request.id}>
                  <td className="px-6 py-4">
                    <div>
                      <div className="text-sm font-medium text-text-primary dark:text-slate-100">
                        {request.full_name}
                      </div>
                      <div className="text-sm text-text-secondary dark:text-slate-400 flex items-center gap-1">
                        <Mail className="w-3 h-3" />
                        {request.email}
                      </div>
                    </div>
                  </td>
                  <td className="px-6 py-4 text-sm text-text-secondary dark:text-slate-400">
                    {formatDate(request.created_at)}
                  </td>
                  <td className="px-6 py-4">
                    <div>
                      {getStatusBadge(request.status)}
                      {request.reviewed_at && request.reviewed_by_name && (
                        <div className="text-xs text-text-secondary dark:text-slate-400 mt-1">
                          by {request.reviewed_by_name}
                        </div>
                      )}
                    </div>
                  </td>
                  <td className="px-6 py-4 text-right">
                    <div className="flex items-center justify-end gap-2">
                      {request.status === 'pending' && (
                        <>
                          <Button
                            variant="primary"
                            size="sm"
                            onClick={() => openApproveModal(request)}
                          >
                            <Check className="w-4 h-4 mr-1" />
                            Approve
                          </Button>
                          <Button
                            variant="secondary"
                            size="sm"
                            onClick={() => openRejectModal(request)}
                          >
                            <X className="w-4 h-4 mr-1" />
                            Reject
                          </Button>
                        </>
                      )}
                      <button
                        onClick={() => handleDelete(request)}
                        className="text-text-secondary dark:text-slate-400 hover:text-red-600 dark:hover:text-red-400 transition-colors p-2"
                        title="Delete request"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </Card>

      {/* Approve Modal */}
      {showApproveModal && selectedRequest && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <Card className="w-full max-w-lg p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">
                Approve Request
              </h2>
              <button
                onClick={() => setShowApproveModal(false)}
                className="text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-100"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="mb-4 p-3 bg-gray-50 dark:bg-slate-700 rounded-lg">
              <p className="text-sm text-text-primary dark:text-slate-100">
                <strong>Name:</strong> {selectedRequest.full_name}
              </p>
              <p className="text-sm text-text-primary dark:text-slate-100">
                <strong>Email:</strong> {selectedRequest.email}
              </p>
            </div>

            {/* Program Access */}
            <div className="mb-4">
              <label className="block text-sm font-medium text-text-primary dark:text-slate-100 mb-2">
                Program Access
              </label>
              {programs.length === 0 ? (
                <p className="text-sm text-text-secondary dark:text-slate-400">No programs available.</p>
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-2 max-h-48 overflow-y-auto">
                    {programs.map((program) => {
                      const selected = approveProgramIds.includes(program.program_id);
                      return (
                        <button
                          key={program.program_id}
                          onClick={() => toggleApproveProgram(program.program_id)}
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
                            {program.program_name || `Program ${program.program_id}`}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                  <div className="flex gap-2 mt-2">
                    <button
                      onClick={() => setApproveProgramIds(programs.map(p => p.program_id))}
                      className="text-xs text-primary hover:underline"
                    >
                      Select All
                    </button>
                    <button
                      onClick={() => setApproveProgramIds([])}
                      className="text-xs text-primary hover:underline"
                    >
                      Clear All
                    </button>
                  </div>
                </>
              )}
            </div>

            {/* Permissions */}
            <div className="flex gap-6 mb-6">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={approveCanComment}
                  onChange={(e) => setApproveCanComment(e.target.checked)}
                  className="w-4 h-4 rounded border-border dark:border-slate-600 text-primary focus:ring-primary dark:bg-slate-700"
                />
                <span className="text-sm text-text-primary dark:text-slate-100">Can comment/inspect</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={approveIsAdmin}
                  onChange={(e) => setApproveIsAdmin(e.target.checked)}
                  className="w-4 h-4 rounded border-border dark:border-slate-600 text-primary focus:ring-primary dark:bg-slate-700"
                />
                <span className="text-sm text-text-primary dark:text-slate-100">Admin privileges</span>
              </label>
            </div>

            {/* Actions */}
            <div className="flex justify-end gap-2">
              <Button
                variant="secondary"
                onClick={() => setShowApproveModal(false)}
                disabled={processing}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleApprove}
                disabled={processing}
              >
                {processing ? (
                  <>
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                    Approving...
                  </>
                ) : (
                  <>
                    <Check className="w-4 h-4 mr-2" />
                    Approve & Send Invite
                  </>
                )}
              </Button>
            </div>
          </Card>
        </div>
      )}

      {/* Reject Modal */}
      {showRejectModal && selectedRequest && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <Card className="w-full max-w-md p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">
                Reject Request
              </h2>
              <button
                onClick={() => setShowRejectModal(false)}
                className="text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-100"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="mb-4 p-3 bg-gray-50 dark:bg-slate-700 rounded-lg">
              <p className="text-sm text-text-primary dark:text-slate-100">
                <strong>Name:</strong> {selectedRequest.full_name}
              </p>
              <p className="text-sm text-text-primary dark:text-slate-100">
                <strong>Email:</strong> {selectedRequest.email}
              </p>
            </div>

            <div className="mb-6">
              <label className="block text-sm font-medium text-text-primary dark:text-slate-100 mb-2">
                Reason (optional)
              </label>
              <textarea
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                className="w-full px-4 py-2 border border-border dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary"
                placeholder="Enter a reason for rejection..."
                rows={3}
              />
              <p className="text-xs text-text-secondary dark:text-slate-400 mt-1">
                This is for internal tracking only.
              </p>
            </div>

            {/* Actions */}
            <div className="flex justify-end gap-2">
              <Button
                variant="secondary"
                onClick={() => setShowRejectModal(false)}
                disabled={processing}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleReject}
                disabled={processing}
                className="bg-red-600 hover:bg-red-700"
              >
                {processing ? (
                  <>
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                    Rejecting...
                  </>
                ) : (
                  <>
                    <X className="w-4 h-4 mr-2" />
                    Reject Request
                  </>
                )}
              </Button>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
