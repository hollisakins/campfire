'use client';

import React, { useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { ChevronRight, AlertCircle } from 'lucide-react';
import { MarkdownRenderer, TableOfContents, DocNavigation, type TOCItem } from '@/components/docs';
import { findDocBySlug, getBreadcrumbs, getAdjacentPages } from '@/lib/docs/config';

// Import all markdown content
import overviewContent from '@/lib/docs/content/overview.md';
import gettingStartedContent from '@/lib/docs/content/getting-started.md';
import inspectionContent from '@/lib/docs/content/inspection/index.md';
import redshiftQualityContent from '@/lib/docs/content/inspection/redshift-quality.md';
import spectralFeaturesContent from '@/lib/docs/content/inspection/spectral-features.md';
import flagsContent from '@/lib/docs/content/inspection/flags.md';
import reductionContent from '@/lib/docs/content/reduction/index.md';
import nirspecContent from '@/lib/docs/content/reduction/nirspec.md';
import nircamContent from '@/lib/docs/content/reduction/nircam.md';
import dataProductsContent from '@/lib/docs/content/data-products/index.md';
import fitsColumnsContent from '@/lib/docs/content/data-products/fits-columns.md';
import apiContent from '@/lib/docs/content/api/index.md';

// Content registry
const contentMap: Record<string, string> = {
  'overview': overviewContent,
  'getting-started': gettingStartedContent,
  'inspection': inspectionContent,
  'inspection/redshift-quality': redshiftQualityContent,
  'inspection/spectral-features': spectralFeaturesContent,
  'inspection/flags': flagsContent,
  'reduction': reductionContent,
  'reduction/nirspec': nirspecContent,
  'reduction/nircam': nircamContent,
  'data-products': dataProductsContent,
  'data-products/fits-columns': fitsColumnsContent,
  'api': apiContent,
};

export default function DocsPage() {
  const params = useParams();
  const slugArray = params.slug as string[] | undefined;
  const slug = slugArray?.join('/') || 'overview';

  const [tocItems, setTocItems] = useState<TOCItem[]>([]);

  const content = contentMap[slug];
  const docPage = findDocBySlug(slug);
  const breadcrumbs = getBreadcrumbs(slug);
  const { prev, next } = getAdjacentPages(slug);

  // 404 handling
  if (!content || !docPage) {
    return (
      <div className="text-center py-16">
        <div className="w-16 h-16 bg-red-100 dark:bg-red-950 rounded-full flex items-center justify-center mx-auto mb-4">
          <AlertCircle className="w-8 h-8 text-red-600 dark:text-red-400" />
        </div>
        <h1 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
          Page Not Found
        </h1>
        <p className="text-text-secondary dark:text-slate-400 mb-6">
          The documentation page you&apos;re looking for doesn&apos;t exist.
        </p>
        <Link
          href="/docs"
          className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
        >
          Back to Docs
        </Link>
      </div>
    );
  }

  return (
    <div className="flex gap-8">
      <article className="flex-1 min-w-0">
        {/* Breadcrumbs */}
        {breadcrumbs.length > 0 && (
          <nav className="flex items-center gap-1 text-sm text-text-secondary dark:text-slate-400 mb-6">
            <Link href="/docs" className="hover:text-primary transition-colors">
              Docs
            </Link>
            {breadcrumbs.map((crumb, index) => (
              <React.Fragment key={crumb.slug}>
                <ChevronRight className="w-4 h-4" />
                {index === breadcrumbs.length - 1 ? (
                  <span className="text-text-primary dark:text-slate-200">{crumb.title}</span>
                ) : (
                  <Link
                    href={`/docs/${crumb.slug}`}
                    className="hover:text-primary transition-colors"
                  >
                    {crumb.title}
                  </Link>
                )}
              </React.Fragment>
            ))}
          </nav>
        )}

        {/* Content */}
        <MarkdownRenderer content={content} onTOCChange={setTocItems} />

        {/* Navigation */}
        <DocNavigation prev={prev} next={next} />
      </article>

      {/* Table of Contents */}
      <TableOfContents items={tocItems} />
    </div>
  );
}
