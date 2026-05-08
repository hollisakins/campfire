'use client';

import React, { useMemo, useState } from 'react';
import { Check, Copy } from 'lucide-react';
import type { PhotometryBand } from '@/lib/types';

interface PhotometryTableProps {
  bands: Record<string, PhotometryBand>;
}

type CopyKind = 'csv' | 'python';

function formatNumber(value: number, digits = 4): string {
  if (!isFinite(value)) return 'NaN';
  return Number(value.toPrecision(digits)).toString();
}

export const PhotometryTable: React.FC<PhotometryTableProps> = ({ bands }) => {
  const [copied, setCopied] = useState<CopyKind | null>(null);

  const rows = useMemo(() => {
    return Object.entries(bands)
      .filter(([, b]) => b.wav != null && isFinite(b.flux) && isFinite(b.flux_err))
      .map(([name, b]) => ({
        name,
        wav: b.wav!,
        flux: b.flux,
        flux_err: b.flux_err,
      }))
      .sort((a, b) => a.wav - b.wav);
  }, [bands]);

  const csv = useMemo(() => {
    const header = 'band,wav_um,flux_uJy,flux_err_uJy';
    const body = rows
      .map(r => `${r.name},${formatNumber(r.wav)},${formatNumber(r.flux)},${formatNumber(r.flux_err)}`)
      .join('\n');
    return `${header}\n${body}\n`;
  }, [rows]);

  const python = useMemo(() => {
    const fmtList = (arr: (string | number)[], quote = false) =>
      `[${arr.map(v => (quote ? `"${v}"` : v)).join(', ')}]`;
    return [
      `bands = ${fmtList(rows.map(r => r.name), true)}`,
      `wav_um = ${fmtList(rows.map(r => formatNumber(r.wav)))}`,
      `flux_uJy = ${fmtList(rows.map(r => formatNumber(r.flux)))}`,
      `flux_err_uJy = ${fmtList(rows.map(r => formatNumber(r.flux_err)))}`,
    ].join('\n') + '\n';
  }, [rows]);

  if (rows.length === 0) return null;

  const handleCopy = async (kind: CopyKind) => {
    const text = kind === 'csv' ? csv : python;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(kind);
      setTimeout(() => setCopied(c => (c === kind ? null : c)), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-sm font-medium text-text-secondary dark:text-slate-400">
          Photometry data
        </h4>
        <div className="flex items-center gap-2">
          <CopyButton
            label="Copy CSV"
            active={copied === 'csv'}
            onClick={() => handleCopy('csv')}
          />
          <CopyButton
            label="Copy Python"
            active={copied === 'python'}
            onClick={() => handleCopy('python')}
          />
        </div>
      </div>

      <div className="overflow-x-auto bg-white dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg">
        <table className="text-xs font-mono w-full">
          <tbody>
            <tr className="border-b border-border dark:border-slate-700">
              <th className="sticky left-0 bg-white dark:bg-slate-800 text-left px-3 py-1.5 font-medium text-text-secondary dark:text-slate-400 whitespace-nowrap border-r border-border dark:border-slate-700">
                Band
              </th>
              {rows.map(r => (
                <td
                  key={r.name}
                  className="px-3 py-1.5 text-text-primary dark:text-slate-200 whitespace-nowrap text-right"
                >
                  {r.name}
                </td>
              ))}
            </tr>
            <tr className="border-b border-border dark:border-slate-700">
              <th className="sticky left-0 bg-white dark:bg-slate-800 text-left px-3 py-1.5 font-medium text-text-secondary dark:text-slate-400 whitespace-nowrap border-r border-border dark:border-slate-700">
                λ (µm)
              </th>
              {rows.map(r => (
                <td
                  key={r.name}
                  className="px-3 py-1.5 text-text-primary dark:text-slate-200 whitespace-nowrap text-right"
                >
                  {formatNumber(r.wav)}
                </td>
              ))}
            </tr>
            <tr className="border-b border-border dark:border-slate-700">
              <th className="sticky left-0 bg-white dark:bg-slate-800 text-left px-3 py-1.5 font-medium text-text-secondary dark:text-slate-400 whitespace-nowrap border-r border-border dark:border-slate-700">
                Flux (µJy)
              </th>
              {rows.map(r => (
                <td
                  key={r.name}
                  className="px-3 py-1.5 text-text-primary dark:text-slate-200 whitespace-nowrap text-right"
                >
                  {formatNumber(r.flux)}
                </td>
              ))}
            </tr>
            <tr>
              <th className="sticky left-0 bg-white dark:bg-slate-800 text-left px-3 py-1.5 font-medium text-text-secondary dark:text-slate-400 whitespace-nowrap border-r border-border dark:border-slate-700">
                Error (µJy)
              </th>
              {rows.map(r => (
                <td
                  key={r.name}
                  className="px-3 py-1.5 text-text-primary dark:text-slate-200 whitespace-nowrap text-right"
                >
                  {formatNumber(r.flux_err)}
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
};

interface CopyButtonProps {
  label: string;
  active: boolean;
  onClick: () => void;
}

const CopyButton: React.FC<CopyButtonProps> = ({ label, active, onClick }) => (
  <button
    onClick={onClick}
    className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-text-primary dark:text-slate-100 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-md hover:bg-gray-50 dark:hover:bg-slate-700 transition-colors"
  >
    {active ? (
      <>
        <Check className="w-3.5 h-3.5 text-green-600 dark:text-green-400" />
        <span className="text-green-600 dark:text-green-400">Copied!</span>
      </>
    ) : (
      <>
        <Copy className="w-3.5 h-3.5" />
        <span>{label}</span>
      </>
    )}
  </button>
);
