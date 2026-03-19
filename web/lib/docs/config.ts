import {
  BookOpen,
  Rocket,
  Search,
  Telescope,
  Database,
  Code,
  NotebookPen,
  type LucideIcon
} from 'lucide-react';

// Dynamic (non-markdown) page prefixes — exact match or prefix match (with /)
const DYNAMIC_PREFIXES = ['data-products/programs'];

/** Check if a slug is a dynamic (non-markdown) page */
export function isDynamicSlug(slug: string): boolean {
  return DYNAMIC_PREFIXES.some(
    prefix => slug === prefix || slug.startsWith(prefix + '/')
  );
}

/** For a dynamic sub-page (e.g. data-products/programs/7076), return the parent dynamic slug */
export function getDynamicParentSlug(slug: string): string | undefined {
  return DYNAMIC_PREFIXES.find(
    prefix => slug.startsWith(prefix + '/')
  );
}

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
        title: 'NIRSpec Programs',
        slug: 'data-products/programs',
        description: 'Browse JWST programs with data in CAMPFIRE',
      },
      {
        title: 'FITS Reference',
        slug: 'data-products/fits-columns',
        description: 'Column definitions and header keywords',
      },
    ],
  },
  {
    title: 'Usage Guidelines',
    slug: 'usage',
    icon: NotebookPen,
    description: 'Guidelines for using and publishing with CAMPFIRE data products',
  },
  {
    title: 'Programmatic Access',
    slug: 'api',
    icon: Code,
    description: 'CLI, Python client, and REST API reference',
    children: [
      {
        title: 'Overview',
        slug: 'api',
        description: 'Installation, authentication, and architecture',
      },
      {
        title: 'CLI Reference',
        slug: 'api/cli',
        description: 'Bulk download, sync, and observation management',
      },
      {
        title: 'Python Client',
        slug: 'api/python-client',
        description: 'Interactive querying, spectrum access, and plotting',
      },
      {
        title: 'REST API',
        slug: 'api/rest',
        description: 'Direct HTTP endpoint reference',
      },
    ],
  },
];

// Helper to find a doc page by slug (supports dynamic sub-pages)
export function findDocBySlug(slug: string): DocPage | undefined {
  for (const page of docsNav) {
    if (page.slug === slug) return page;
    if (page.children) {
      for (const child of page.children) {
        if (child.slug === slug) return child;
      }
    }
  }
  // For dynamic sub-pages (e.g. data-products/programs/7076), resolve to the parent entry
  const parentSlug = getDynamicParentSlug(slug);
  if (parentSlug) return findDocBySlug(parentSlug);
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

  // For dynamic sub-pages, build breadcrumbs from the parent dynamic entry
  const parentSlug = getDynamicParentSlug(slug);
  if (parentSlug) {
    const parentBreadcrumbs = getBreadcrumbs(parentSlug);
    return parentBreadcrumbs;
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
