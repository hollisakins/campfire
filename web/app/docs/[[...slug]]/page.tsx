import React from 'react';
import Link from 'next/link';
import { notFound } from 'next/navigation';
import { ChevronRight } from 'lucide-react';
// Direct imports, not the '@/components/docs' barrel: the barrel also
// re-exports the CLIENT markdown renderer, and a client module reached
// through the server graph still gets a client chunk — that put the whole
// parser (react-markdown + remark + rehype-highlight, 89 kB gz) back into
// this static route's first-load JS.
import MarkdownServer from '@/components/docs/MarkdownServer';
import TableOfContents from '@/components/docs/TableOfContents';
import DocNavigation from '@/components/docs/DocNavigation';
import { findDocBySlug, getBreadcrumbs, getAdjacentPages } from '@/lib/docs/config';
import { extractHeadings } from '@/lib/docs/toc';

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
import usageContent from '@/lib/docs/content/usage/index.md';
import apiContent from '@/lib/docs/content/api/index.md';
import apiGettingStartedContent from '@/lib/docs/content/api/getting-started.md';
import apiRecipesContent from '@/lib/docs/content/api/recipes.md';
import cliContent from '@/lib/docs/content/api/cli.md';
import pythonClientContent from '@/lib/docs/content/api/python-client.md';
import restApiContent from '@/lib/docs/content/api/rest.md';

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
  'usage': usageContent,
  'api': apiContent,
  'api/getting-started': apiGettingStartedContent,
  'api/recipes': apiRecipesContent,
  'api/cli': cliContent,
  'api/python-client': pythonClientContent,
  'api/rest': restApiContent,
};


// Static prose: every page is prerendered at build time from the registry
// above (perf T1-7 / #503). Before, this route was a client component that
// shipped all 18 documents plus the markdown parser to the browser and was a
// lambda MISS on every request. Unknown slugs 404 at the edge.
export const dynamicParams = false;

export function generateStaticParams(): { slug: string[] }[] {
  return Object.keys(contentMap).map((slug) => ({
    // The index route (/docs) is the overview: an empty catch-all segment.
    slug: slug === 'overview' ? [] : slug.split('/'),
  }));
}

export default async function DocsPage({ params }: { params: Promise<{ slug?: string[] }> }) {
  const { slug: slugArray } = await params;
  const slug = slugArray?.join('/') || 'overview';

  const content = contentMap[slug];
  const docPage = findDocBySlug(slug);
  if (!content || !docPage) notFound();

  const breadcrumbs = getBreadcrumbs(slug);
  const adjacent = getAdjacentPages(slug);
  // Link fields only — DocPage.icon is a component and can't cross into the
  // client-rendered DocNavigation.
  const link = (d?: { slug: string; title: string }) => (d ? { slug: d.slug, title: d.title } : undefined);
  const prev = link(adjacent.prev);
  const next = link(adjacent.next);
  const tocItems = extractHeadings(content);

  return (
    <div className="flex gap-8">
      <article className="flex-1 min-w-0">
        {/* Breadcrumbs */}
        {breadcrumbs.length > 0 && (
          <nav className="flex items-center gap-1 text-sm text-text-secondary mb-6">
            <Link href="/docs" className="hover:text-primary transition-colors">
              Docs
            </Link>
            {breadcrumbs.map((crumb, index) => (
              <React.Fragment key={crumb.slug}>
                <ChevronRight className="w-4 h-4" />
                {index === breadcrumbs.length - 1 ? (
                  <span className="text-text-primary">{crumb.title}</span>
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

        {/* Content (rendered at build time) */}
        <MarkdownServer content={content} />

        {/* Navigation */}
        <DocNavigation prev={prev} next={next} />
      </article>

      <TableOfContents items={tocItems} />
    </div>
  );
}
