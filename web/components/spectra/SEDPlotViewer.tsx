'use client';

import React, { useState, useEffect } from 'react';
import { Download, Loader2, FileX } from 'lucide-react';

interface SEDPlotViewerProps {
  objectId: string;
  className?: string;
}

export const SEDPlotViewer: React.FC<SEDPlotViewerProps> = ({
  objectId,
  className = '',
}) => {
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchSedPlot = async () => {
      setLoading(true);
      setError(null);

      try {
        const response = await fetch(`/api/sed-plot?object_id=${encodeURIComponent(objectId)}`);

        if (!response.ok) {
          if (response.status === 404) {
            setError('SED plot not found for this object');
          } else {
            const data = await response.json();
            setError(data.error || 'Failed to load SED plot');
          }
          return;
        }

        const data = await response.json();
        setPdfUrl(data.url);
      } catch (err) {
        console.error('Error fetching SED plot:', err);
        setError('Failed to load SED plot');
      } finally {
        setLoading(false);
      }
    };

    fetchSedPlot();
  }, [objectId]);

  const handleDownload = () => {
    if (pdfUrl) {
      // Create temporary link to trigger download
      const link = document.createElement('a');
      link.href = pdfUrl;
      link.download = `${objectId}_sed.pdf`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    }
  };

  if (loading) {
    return (
      <div className={`flex items-center justify-center py-16 ${className}`}>
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
        <span className="ml-3 text-text-secondary">Loading SED plot...</span>
      </div>
    );
  }

  if (error || !pdfUrl) {
    return (
      <div className={`flex flex-col items-center justify-center py-16 ${className}`}>
        <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-4">
          <FileX className="w-8 h-8 text-text-secondary" />
        </div>
        <p className="text-text-secondary text-center">{error || 'SED plot not available'}</p>
      </div>
    );
  }

  return (
    <div className={`space-y-4 ${className}`}>
      {/* Download Button */}
      <div className="flex justify-end">
        <button
          onClick={handleDownload}
          className="inline-flex items-center gap-2 px-4 py-2 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors text-sm font-medium"
        >
          <Download className="w-4 h-4" />
          Download SED Plot
        </button>
      </div>

      {/* PDF Viewer */}
      <div className="border border-border rounded-lg overflow-hidden bg-card">
        <iframe
          src={pdfUrl}
          className="w-full h-[800px] border-0"
          title={`SED plot for ${objectId}`}
        />
      </div>
    </div>
  );
};
