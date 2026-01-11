'use client';

import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { NircamTable } from '@/components/nircam/NircamTable';
import { NircamFilterBar, NircamFilterOptions, DEFAULT_NIRCAM_FILTERS } from '@/components/nircam/NircamFilterBar';
import { CurlScriptGenerator } from '@/components/nircam/CurlScriptGenerator';
import { getNircamImages, getNircamFilterOptions } from '@/lib/actions/nircam';
import type { NircamImage } from '@/lib/types';
import { LogIn, Loader2, ImageIcon } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

export default function NircamPage() {
  const { user, loading: authLoading } = useAuth();

  const [images, setImages] = useState<NircamImage[]>([]);
  const [filters, setFilters] = useState<NircamFilterOptions>(DEFAULT_NIRCAM_FILTERS);
  const [selectedImages, setSelectedImages] = useState<NircamImage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Available filter options
  const [availableFields, setAvailableFields] = useState<string[]>([]);
  const [availableTiles, setAvailableTiles] = useState<string[]>([]);
  const [availableFilters, setAvailableFilters] = useState<string[]>([]);
  const [availablePixelScales, setAvailablePixelScales] = useState<string[]>([]);
  const [availableVersions, setAvailableVersions] = useState<string[]>([]);
  const [availableExtensions, setAvailableExtensions] = useState<string[]>([]);

  // Fetch data
  const fetchData = useCallback(async () => {
    if (authLoading) return;

    setLoading(true);
    setError(null);

    try {
      // Fetch images and filter options in parallel
      const [imagesResult, filterOptionsResult] = await Promise.all([
        getNircamImages(),
        getNircamFilterOptions(),
      ]);

      if (imagesResult.error) {
        setError(imagesResult.error);
      } else {
        setImages(imagesResult.images);
      }

      if (!filterOptionsResult.error) {
        setAvailableFields(filterOptionsResult.fields);
        setAvailableTiles(filterOptionsResult.tiles);
        setAvailableFilters(filterOptionsResult.filters);
        setAvailablePixelScales(filterOptionsResult.pixel_scales);
        setAvailableVersions(filterOptionsResult.versions);
        setAvailableExtensions(filterOptionsResult.extensions);
      }
    } catch (err) {
      setError('Failed to fetch data');
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, [authLoading]);

  // Fetch data on mount and when user logs in
  useEffect(() => {
    fetchData();
  }, [fetchData, user]);

  const handleFilterChange = (newFilters: NircamFilterOptions) => {
    setFilters(newFilters);
  };

  const handleSelectionChange = (selected: NircamImage[]) => {
    setSelectedImages(selected);
  };

  // Show login prompt if not authenticated
  if (!authLoading && !user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'NIRCam' },
          ]}
          className="mb-6"
        />

        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card dark:bg-slate-800 rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary dark:text-slate-400" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Sign in to view NIRCam images
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6 max-w-md">
            Access to NIRCam imaging data requires authentication. Please sign in with your
            CAMPFIRE account to browse and download images.
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

  return (
    <div className="container mx-auto px-4 py-8">
      {/* Breadcrumbs */}
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'NIRCam' },
        ]}
        className="mb-6"
      />

      {/* Page Header */}
      <div className="mb-6">
        <div className="flex items-center gap-3 mb-2">
          <ImageIcon className="w-8 h-8 text-primary" />
          <h1 className="text-2xl font-bold text-text-primary dark:text-slate-100">NIRCam Imaging</h1>
        </div>
        <p className="text-text-secondary dark:text-slate-400">
          Browse and download NIRCam mosaic images from CAMPFIRE fields
        </p>
      </div>

      {/* Filter Bar */}
      <div className="mb-6">
        <NircamFilterBar
          filterState={filters}
          onFiltersChange={handleFilterChange}
          availableFields={availableFields}
          availableTiles={availableTiles}
          availableFilters={availableFilters}
          availablePixelScales={availablePixelScales}
          availableVersions={availableVersions}
          availableExtensions={availableExtensions}
        />
      </div>

      {/* Loading State */}
      {loading && (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary dark:text-slate-400">Loading images...</span>
        </div>
      )}

      {/* Error State */}
      {error && !loading && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-4">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      {/* Results */}
      {!loading && !error && (
        <>
          {/* Results Count */}
          <div className="mb-4 flex items-center justify-between">
            <span className="text-sm text-text-secondary dark:text-slate-400">
              {selectedImages.length.toLocaleString()} of {images.length.toLocaleString()} images selected
            </span>
          </div>

          {/* Curl Script Generator */}
          <div className="mb-4">
            <CurlScriptGenerator selectedImages={selectedImages} />
          </div>

          {/* Empty State */}
          {images.length === 0 ? (
            <div className="text-center py-16 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg">
              <ImageIcon className="w-12 h-12 text-text-secondary dark:text-slate-400 mx-auto mb-4" />
              <p className="text-text-secondary dark:text-slate-400">
                No NIRCam images available yet.
              </p>
              <p className="text-text-secondary dark:text-slate-400 text-sm mt-2">
                Check back later or contact the team if you expected to see data here.
              </p>
            </div>
          ) : (
            <NircamTable
              images={images}
              filters={filters}
              onSelectionChange={handleSelectionChange}
            />
          )}
        </>
      )}
    </div>
  );
}
