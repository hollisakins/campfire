'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Loader2, Link2, Copy, Check, Trash2, AlertTriangle, Plus } from 'lucide-react';
import {
  getShareLinks, mintShareLink, revokeShareLink, getShareableScopes,
  type ShareLinkRow,
} from '@/lib/actions/share-links';

// ---------------------------------------------------------------------------
// /admin/share-links — mint and revoke share links.
// See docs/design-public-mirror.md §7.
//
// Deliberately NOT a per-row action on /admin/deployments: a link is scoped to
// a field or observation and tracks its CURRENT state, so hanging it off one
// deployment row would imply a snapshot the link does not take.
//
// Because links never expire by default and revocation is per link, revoked_at
// is the only thing that ever retires one. So this table's real job is to make
// a forgotten link obvious -- hence age, view count, and last-seen are columns
// rather than details.
// ---------------------------------------------------------------------------

function fmt(ts: string | null): string {
  if (!ts) return '—';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function ageInDays(ts: string): number {
  return Math.floor((Date.now() - new Date(ts).getTime()) / 86_400_000);
}

function shareUrl(token: string): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : '';
  return `${origin}/s/${token}`;
}

function statusOf(link: ShareLinkRow): { label: string; className: string } {
  if (link.revoked_at) {
    return { label: 'revoked', className: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300' };
  }
  if (link.expires_at && new Date(link.expires_at) <= new Date()) {
    return { label: 'expired', className: 'bg-neutral-200 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300' };
  }
  return { label: 'active', className: 'bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300' };
}

function CopyButton({ token }: { token: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={async () => {
        await navigator.clipboard.writeText(shareUrl(token));
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
      className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
      title={shareUrl(token)}
    >
      {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
      {copied ? 'Copied' : 'Copy link'}
    </button>
  );
}

function MintForm({ onDone }: { onDone: () => void }) {
  const [label, setLabel] = useState('');
  const [scopeKind, setScopeKind] = useState<'observation' | 'field'>('observation');
  const [scope, setScope] = useState('');
  const [includeDrafts, setIncludeDrafts] = useState(false);
  const [allowDownload, setAllowDownload] = useState(true);
  const [expiresAt, setExpiresAt] = useState('');
  const [scopes, setScopes] = useState<{ observations: string[]; fields: string[] }>({ observations: [], fields: [] });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [minted, setMinted] = useState<string | null>(null);

  useEffect(() => {
    getShareableScopes().then((s) => setScopes({ observations: s.observations, fields: s.fields }));
  }, []);

  const options = scopeKind === 'observation' ? scopes.observations : scopes.fields;

  const submit = async () => {
    setBusy(true);
    setError(null);
    const result = await mintShareLink({
      label,
      observation: scopeKind === 'observation' ? scope : null,
      field: scopeKind === 'field' ? scope : null,
      includeDrafts,
      allowDownload,
      // Local end-of-day: a bare YYYY-MM-DD parses as UTC *midnight at the
      // start* of that day, which would expire "today" links immediately and
      // "tomorrow" links before that day even starts locally. "Expires <date>"
      // means valid through that date in the admin's timezone.
      expiresAt: expiresAt ? new Date(`${expiresAt}T23:59:59`).toISOString() : null,
    });
    setBusy(false);
    if (result.error) return setError(result.error);
    setMinted(result.token ?? null);
    onDone();
  };

  if (minted) {
    return (
      <Card className="p-4 mb-6">
        <div className="text-sm font-medium text-text-primary mb-2">Link created</div>
        <p className="text-xs text-text-secondary mb-3">
          This is the only place the URL is assembled for you — copy it now. You can
          always rebuild it from the token in the table below.
        </p>
        <div className="flex items-center gap-3">
          <code className="flex-1 text-xs bg-surface-2 px-3 py-2 rounded border border-border overflow-x-auto whitespace-nowrap">
            {shareUrl(minted)}
          </code>
          <CopyButton token={minted} />
        </div>
      </Card>
    );
  }

  return (
    <Card className="p-4 mb-6 space-y-4">
      <div>
        <label className="block text-xs font-medium text-text-secondary mb-1">Label</label>
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="e.g. Naidu — cosmos-web f444w"
          className="w-full px-3 py-2 text-sm rounded border border-border bg-surface-1 text-text-primary"
        />
        <p className="mt-1 text-xs text-text-secondary">
          Shown to the visitor as the name of the shared view, and how you will
          recognise this link later.
        </p>
      </div>

      <div className="flex gap-3">
        <div>
          <label className="block text-xs font-medium text-text-secondary mb-1">Scope</label>
          <select
            value={scopeKind}
            onChange={(e) => { setScopeKind(e.target.value as 'observation' | 'field'); setScope(''); }}
            className="px-3 py-2 text-sm rounded border border-border bg-surface-1 text-text-primary"
          >
            <option value="observation">NIRSpec observation</option>
            <option value="field">NIRCam field</option>
          </select>
        </div>
        <div className="flex-1">
          <label className="block text-xs font-medium text-text-secondary mb-1">&nbsp;</label>
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            className="w-full px-3 py-2 text-sm rounded border border-border bg-surface-1 text-text-primary"
          >
            <option value="">Select…</option>
            {options.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </div>
      </div>

      <div className="space-y-2">
        <label className="flex items-center gap-2 text-sm text-text-primary">
          <input type="checkbox" checked={includeDrafts} onChange={(e) => setIncludeDrafts(e.target.checked)} />
          Include drafts (in-prep reductions)
        </label>
        <p className="ml-6 text-xs text-text-secondary">
          Shows unpublished data in this scope — including a re-reduction staged
          after the link is created. Use for sharing work that should not enter
          the catalog.
        </p>
        <label className="flex items-center gap-2 text-sm text-text-primary">
          <input type="checkbox" checked={allowDownload} onChange={(e) => setAllowDownload(e.target.checked)} />
          Allow FITS downloads
        </label>
      </div>

      <div>
        <label className="block text-xs font-medium text-text-secondary mb-1">
          Expires (optional)
        </label>
        <input
          type="date"
          value={expiresAt}
          onChange={(e) => setExpiresAt(e.target.value)}
          className="px-3 py-2 text-sm rounded border border-border bg-surface-1 text-text-primary"
        />
        <p className="mt-1 text-xs text-text-secondary">
          Leave empty for a link that never expires. Revoke is the usual way to
          end a share.
        </p>
      </div>

      {error && (
        <div className="flex items-start gap-2 text-sm text-red-700 dark:text-red-400">
          <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <Button onClick={submit} disabled={busy || !label.trim() || !scope}>
        {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Create link'}
      </Button>
    </Card>
  );
}

export default function ShareLinksPage() {
  const [links, setLinks] = useState<ShareLinkRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showMint, setShowMint] = useState(false);
  const [revoking, setRevoking] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    const result = await getShareLinks();
    setLinks(result.links);
    setError(result.error ?? null);
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const revoke = async (link: ShareLinkRow) => {
    if (!confirm(`Revoke "${link.label}"? Anyone holding the URL loses access immediately.`)) return;
    setRevoking(link.token);
    const result = await revokeShareLink(link.token);
    setRevoking(null);
    if (result.error) setError(result.error);
    await load();
  };

  return (
    <div>
      <div className="flex items-start justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-text-primary flex items-center gap-2">
            <Link2 className="w-5 h-5" /> Share Links
          </h1>
          <p className="mt-1 text-sm text-text-secondary max-w-2xl">
            URLs that expose one NIRCam field or one NIRSpec observation to
            someone without a CAMPFIRE account. A link tracks the scope&apos;s
            current state, so redeploying updates what the holder sees.
          </p>
        </div>
        <Button onClick={() => setShowMint(!showMint)}>
          <Plus className="w-4 h-4 mr-1" /> New link
        </Button>
      </div>

      {showMint && <MintForm onDone={load} />}

      {error && (
        <div className="mb-4 flex items-start gap-2 text-sm text-red-700 dark:text-red-400">
          <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <Card className="overflow-x-auto">
        {loading ? (
          <div className="p-8 flex justify-center"><Loader2 className="w-5 h-5 animate-spin text-text-secondary" /></div>
        ) : links.length === 0 ? (
          <div className="p-8 text-center text-sm text-text-secondary">
            No share links yet.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="border-b border-border">
              <tr className="text-left text-xs text-text-secondary">
                <th className="px-4 py-2 font-medium">Label</th>
                <th className="px-4 py-2 font-medium">Scope</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Options</th>
                <th className="px-4 py-2 font-medium text-right">Views</th>
                <th className="px-4 py-2 font-medium">Last seen</th>
                <th className="px-4 py-2 font-medium">Age</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {links.map((link) => {
                const status = statusOf(link);
                const age = ageInDays(link.created_at);
                // Old + never visited is the shape of a link nobody is using
                // and nobody remembers minting.
                const stale = !link.revoked_at && age > 180 && link.view_count === 0;
                return (
                  <tr key={link.token} className="border-b border-border last:border-0">
                    <td className="px-4 py-2">
                      <div className="text-text-primary">{link.label}</div>
                      {!link.revoked_at && <CopyButton token={link.token} />}
                    </td>
                    <td className="px-4 py-2 font-mono text-xs text-text-secondary">
                      {link.observation ?? link.field}
                      <span className="ml-1 text-text-secondary/60">
                        {link.observation ? '(obs)' : '(field)'}
                      </span>
                    </td>
                    <td className="px-4 py-2">
                      <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${status.className}`}>
                        {status.label}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-xs text-text-secondary">
                      {link.include_drafts && <div>drafts</div>}
                      {!link.allow_download && <div>no downloads</div>}
                      {!link.include_drafts && link.allow_download && '—'}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums text-text-secondary">{link.view_count}</td>
                    <td className="px-4 py-2 text-text-secondary">{fmt(link.last_seen_at)}</td>
                    <td className="px-4 py-2 text-text-secondary">
                      {age}d
                      {stale && (
                        <span className="ml-1 text-amber-600 dark:text-amber-400" title="Never opened — consider revoking">
                          ⚠
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-right">
                      {!link.revoked_at && (
                        <button
                          onClick={() => revoke(link)}
                          disabled={revoking === link.token}
                          className="inline-flex items-center gap-1 text-xs text-red-600 dark:text-red-400 hover:underline disabled:opacity-50"
                        >
                          {revoking === link.token
                            ? <Loader2 className="w-3 h-3 animate-spin" />
                            : <Trash2 className="w-3 h-3" />}
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
