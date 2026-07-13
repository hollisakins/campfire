'use client';

import React, { useState, useMemo } from 'react';
import Link from 'next/link';
import { ChevronDown, ChevronUp, Download, Copy, Check, Info } from 'lucide-react';
import type { NircamProductRow } from '@/lib/types';
import {
  generateNircamMosaicDownloadUrls,
  generateNircamExpmapDownloadUrls,
} from '@/lib/actions/download';

interface CurlScriptGeneratorProps {
  selectedImages: NircamProductRow[];
  className?: string;
}

// Helper to format file size
const formatFileSize = (bytes: number): string => {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
};

export const CurlScriptGenerator: React.FC<CurlScriptGeneratorProps> = ({
  selectedImages,
  className = '',
}) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const [script, setScript] = useState('');
  const [generating, setGenerating] = useState(false);

  // Transfer estimate: stored (gzipped) bytes when recorded, logical otherwise
  // — this is what the curl downloads actually move.
  const totalSize = useMemo(() => {
    return selectedImages.reduce(
      (sum, img) => sum + (img.file_size_stored ?? img.file_size ?? 0), 0);
  }, [selectedImages]);

  // Build the curl script. Presigned + HMAC-authorized proxy URLs come from
  // the server actions (authorized per-viewer under RLS — mosaics against
  // nircam_images, expmaps against storage_objects), so the script carries no
  // credentials — just `curl` against the proxy URL. Async because it awaits
  // the presign round-trip.
  const buildScript = React.useCallback(async (): Promise<string> => {
    if (selectedImages.length === 0) return '';

    const mosaicPaths = selectedImages
      .filter((p) => p.kind !== 'expmap')
      .map((p) => p.file_path);
    const expmapKeys = selectedImages
      .filter((p) => p.kind === 'expmap')
      .map((p) => p.file_path);

    const [mosaicRes, expmapRes] = await Promise.all([
      mosaicPaths.length > 0
        ? generateNircamMosaicDownloadUrls(mosaicPaths)
        : Promise.resolve({ urls: {} as Record<string, string>, error: null }),
      expmapKeys.length > 0
        ? generateNircamExpmapDownloadUrls(expmapKeys)
        : Promise.resolve({ urls: {} as Record<string, string>, error: null }),
    ]);
    const urls = { ...mosaicRes.urls, ...expmapRes.urls };

    // Only include products we were authorized to presign.
    const images = selectedImages.filter((img) => urls[img.file_path]);
    if (images.length === 0) return '';

    // Group by field for organization
    const fields = [...new Set(images.map((img) => img.field))];

    let scriptContent = `#!/bin/bash
# CAMPFIRE NIRCam Data Download Script
# Generated: ${new Date().toISOString()}
# Files: ${images.length}
# Total size: ${formatFileSize(totalSize)}
#
# Download URLs below are pre-signed and expire after ~6 hours. Re-generate
# this script if the links have expired.

echo "============================="
echo "CAMPFIRE NIRCam Data Download"
echo "============================="
echo ""
echo "This script will download ${images.length} files (${formatFileSize(totalSize)} total)"
echo ""

# Create output directory
mkdir -p nircam_data
cd nircam_data

echo "Starting download..."
echo ""

`;

    // Add download commands grouped by field
    fields.forEach((field) => {
      const fieldImages = images.filter((img) => img.field === field);
      scriptContent += `# Field: ${field.toUpperCase()} (${fieldImages.length} files)\n`;
      scriptContent += `mkdir -p ${field}\n`;
      scriptContent += `cd ${field}\n\n`;

      fieldImages.forEach((img, index) => {
        const filename = img.file_path.split('/').pop() || img.file_path;
        scriptContent += `# File ${index + 1}/${fieldImages.length}: ${filename}\n`;
        scriptContent += `echo "Downloading ${filename}..."\n`;
        scriptContent += `curl -L --progress-bar -o "${filename}" "${urls[img.file_path]}"\n`;
        if (index < fieldImages.length - 1) {
          scriptContent += `\n`;
        }
      });

      scriptContent += `\ncd ..\n\n`;
    });

    scriptContent += `echo ""
echo "Download complete!"
echo "Files saved in: $(pwd)"
`;

    return scriptContent;
  }, [selectedImages, totalSize]);

  // (Re)generate the script whenever the panel is open and the selection
  // changes. Presigned URLs are per-request, so we rebuild rather than memoize.
  React.useEffect(() => {
    if (!isExpanded || selectedImages.length === 0) {
      setScript('');
      return;
    }
    let cancelled = false;
    setGenerating(true);
    buildScript()
      .then((content) => {
        if (!cancelled) setScript(content);
      })
      .catch((err) => {
        console.error('Failed to generate NIRCam download script:', err);
        if (!cancelled) setScript('');
      })
      .finally(() => {
        if (!cancelled) setGenerating(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isExpanded, buildScript, selectedImages.length]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(script);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy script:', err);
    }
  };

  const handleDownload = () => {
    const blob = new Blob([script], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'download_nircam_data.sh';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  if (selectedImages.length === 0) {
    return null;
  }

  return (
    <div className={`bg-card border border-border rounded-lg ${className}`}>
      {/* Toggle header */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-card-hover transition-colors rounded-lg"
      >
        <div className="flex items-center gap-2">
          {isExpanded ? (
            <ChevronUp className="w-4 h-4 text-text-secondary" />
          ) : (
            <ChevronDown className="w-4 h-4 text-text-secondary" />
          )}
          <span className="text-sm font-medium text-text-primary">
            Bulk download {selectedImages.length} file{selectedImages.length === 1 ? '' : 's'}
          </span>
          <span className="text-sm text-text-secondary">
            ({formatFileSize(totalSize)} total)
          </span>
        </div>
      </button>

      {/* Expanded content */}
      {isExpanded && (
        <div className="border-t border-border">
          {/* Script preview */}
          <div className="p-4">
            <div className="bg-gray-900 rounded-lg overflow-hidden">
              <div className="flex items-center justify-between px-4 py-2 bg-gray-800 border-b border-gray-700">
                <span className="text-sm text-gray-400 font-mono">download_nircam_data.sh</span>
                <div className="flex items-center gap-2">
                  {/* Plain buttons: the code panel is always dark, so the
                      theme-aware Button ghost variant is unreadable here. */}
                  <button
                    onClick={handleCopy}
                    disabled={generating || !script}
                    className="inline-flex items-center rounded-md px-2.5 py-1.5 text-sm font-medium text-gray-300 hover:text-white hover:bg-gray-700 transition-colors disabled:opacity-50 disabled:pointer-events-none"
                  >
                    {copied ? (
                      <>
                        <Check className="w-4 h-4 mr-1.5" />
                        Copied
                      </>
                    ) : (
                      <>
                        <Copy className="w-4 h-4 mr-1.5" />
                        Copy
                      </>
                    )}
                  </button>
                  <button
                    onClick={handleDownload}
                    disabled={generating || !script}
                    className="inline-flex items-center rounded-md px-2.5 py-1.5 text-sm font-medium text-gray-300 hover:text-white hover:bg-gray-700 transition-colors disabled:opacity-50 disabled:pointer-events-none"
                  >
                    <Download className="w-4 h-4 mr-1.5" />
                    Download
                  </button>
                </div>
              </div>
              <pre className="p-4 text-sm text-gray-300 font-mono overflow-x-auto max-h-96 overflow-y-auto">
                <code>{generating ? 'Generating pre-signed download links…' : script}</code>
              </pre>
            </div>
          </div>

          {/* Programmatic access pointer */}
          <div className="px-4 pb-4">
            <div className="flex items-start gap-2 bg-background border border-border rounded-lg p-3">
              <Info className="w-4 h-4 text-text-secondary mt-0.5 shrink-0" />
              <p className="text-sm text-text-secondary">
                Regularly bulk-downloading CAMPFIRE data? See{' '}
                <Link href="/docs/api" className="text-primary hover:underline">
                  programmatic access
                </Link>{' '}
                for the CLI and Python client.
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
