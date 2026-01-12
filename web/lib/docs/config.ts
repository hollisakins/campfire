import {
  BookOpen,
  Rocket,
  Search,
  Telescope,
  Database,
  Code,
  type LucideIcon
} from 'lucide-react';

export interface DocPage {
  title: string;
  slug: string;
  icon?: LucideIcon;
  description?: string;
  children?: DocPage[];
}

export const docsNav: DocPage[] = [
  {
    title: 'Overview',
    slug: 'overview',
    icon: BookOpen,
    description: 'Introduction to CAMPFIRE and its capabilities',
  },
  {
    title: 'Getting Started',
    slug: 'getting-started',
    icon: Rocket,
    description: 'Account setup, access codes, and first steps',
  },
  {
    title: 'Visual Inspection',
    slug: 'inspection',
    icon: Search,
    description: 'Guide to inspecting and classifying NIRSpec spectra',
    children: [
      {
        title: 'Overview',
        slug: 'inspection',
        description: 'Introduction to the inspection workflow',
      },
      {
        title: 'Redshift Quality',
        slug: 'inspection/redshift-quality',
        description: 'Understanding the five quality levels',
      },
      {
        title: 'Spectral Features',
        slug: 'inspection/spectral-features',
        description: 'Identifying and tagging spectral features',
      },
      {
        title: 'Flags',
        slug: 'inspection/flags',
        description: 'Object types and data quality flags',
      },
    ],
  },
  {
    title: 'Data Reduction',
    slug: 'reduction',
    icon: Telescope,
    description: 'How raw JWST data becomes science-ready products',
    children: [
      {
        title: 'Overview',
        slug: 'reduction',
        description: 'Pipeline philosophy and architecture',
      },
      {
        title: 'NIRSpec Pipeline',
        slug: 'reduction/nirspec',
        description: 'Spectral extraction and calibration',
      },
      {
        title: 'NIRCam Pipeline',
        slug: 'reduction/nircam',
        description: 'Imaging reduction and mosaics',
      },
    ],
  },
  {
    title: 'Data Products',
    slug: 'data-products',
    icon: Database,
    description: 'Understanding the output files and formats',
    children: [
      {
        title: 'Overview',
        slug: 'data-products',
        description: 'Data product overview',
      },
      {
        title: 'FITS Reference',
        slug: 'data-products/fits-columns',
        description: 'Column definitions and header keywords',
      },
    ],
  },
  {
    title: 'API Reference',
    slug: 'api',
    icon: Code,
    description: 'Programmatic access to CAMPFIRE data',
  },
];

// Helper to find a doc page by slug
export function findDocBySlug(slug: string): DocPage | undefined {
  for (const page of docsNav) {
    if (page.slug === slug) return page;
    if (page.children) {
      for (const child of page.children) {
        if (child.slug === slug) return child;
      }
    }
  }
  return undefined;
}

// Helper to get breadcrumb trail for a slug
export function getBreadcrumbs(slug: string): { title: string; slug: string }[] {
  const breadcrumbs: { title: string; slug: string }[] = [];

  for (const page of docsNav) {
    if (page.slug === slug) {
      breadcrumbs.push({ title: page.title, slug: page.slug });
      return breadcrumbs;
    }
    if (page.children) {
      for (const child of page.children) {
        if (child.slug === slug) {
          breadcrumbs.push({ title: page.title, slug: page.slug });
          breadcrumbs.push({ title: child.title, slug: child.slug });
          return breadcrumbs;
        }
      }
    }
  }

  return breadcrumbs;
}

// Helper to get previous and next pages for navigation
export function getAdjacentPages(slug: string): { prev?: DocPage; next?: DocPage } {
  const flatPages: DocPage[] = [];

  for (const page of docsNav) {
    flatPages.push(page);
    if (page.children) {
      // Skip the first child if it has the same slug as parent (overview)
      for (const child of page.children) {
        if (child.slug !== page.slug) {
          flatPages.push(child);
        }
      }
    }
  }

  const currentIndex = flatPages.findIndex(p => p.slug === slug);

  return {
    prev: currentIndex > 0 ? flatPages[currentIndex - 1] : undefined,
    next: currentIndex < flatPages.length - 1 ? flatPages[currentIndex + 1] : undefined,
  };
}
