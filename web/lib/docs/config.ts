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
const DYNAMIC_PREFIXES: string[] = [];

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
        children: [
          {
            title: 'Overview',
            slug: 'reduction/nirspec',
            description: 'The pipeline at a glance',
          },
          {
            title: 'Detector and calibration',
            slug: 'reduction/nirspec/detector-calibration',
            description: 'Count rates, the detector background, calibrated 2-D spectra',
          },
          {
            title: 'Backgrounds',
            slug: 'reduction/nirspec/backgrounds',
            description: 'Master background, closed and open shutters, nod subtraction',
          },
          {
            title: 'Slit losses',
            slug: 'reduction/nirspec/slit-losses',
            description: 'The model of the MSA optics and how it was derived',
          },
          {
            title: 'Extraction',
            slug: 'reduction/nirspec/extraction',
            description: 'The source model, overlapping spectra, the reported flux',
          },
          {
            title: 'Extended sources',
            slug: 'reduction/nirspec/extended-sources',
            description: 'Reading the 2-D spectrum, and fitting your own source model',
          },
          {
            title: 'Redshifts and lines',
            slug: 'reduction/nirspec/redshifts',
            description: 'Template redshifts and emission-line fluxes',
          },
          {
            title: 'Describing the reduction',
            slug: 'reduction/nirspec/describing',
            description: 'Versions to quote, a paragraph to adapt, choices to state',
          },
        ],
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
        description: 'Decision guide, install, and where things live',
      },
      {
        title: 'Getting Started',
        slug: 'api/getting-started',
        description: 'Install → first query → first plot, with figures',
      },
      {
        title: 'Recipes',
        slug: 'api/recipes',
        description: 'End-to-end task examples',
      },
      {
        title: 'Python Client',
        slug: 'api/python-client',
        description: 'Full reference for the Campfire class',
      },
      {
        title: 'CLI Reference',
        slug: 'api/cli',
        description: 'Bulk download, sync, and observation management',
      },
      {
        title: 'REST API',
        slug: 'api/rest',
        description: 'Direct HTTP endpoint reference',
      },
    ],
  },
];

/** The path of nav entries from the top level down to ``slug`` (a section's own entry, not its same-slug
 * "Overview" child), or undefined. Sections nest to any depth. */
function pathTo(slug: string, pages: DocPage[] = docsNav): DocPage[] | undefined {
  for (const page of pages) {
    if (page.slug === slug) return [page];
    if (page.children) {
      const below = pathTo(slug, page.children.filter(child => child.slug !== page.slug));
      if (below) return [page, ...below];
    }
  }
  return undefined;
}

/** Whether ``slug`` is ``page`` or lies anywhere below it. */
export function containsSlug(page: DocPage, slug: string): boolean {
  if (page.slug === slug) return true;
  return (page.children ?? []).some(child => containsSlug(child, slug));
}

// Helper to find a doc page by slug (supports dynamic sub-pages)
export function findDocBySlug(slug: string): DocPage | undefined {
  const path = pathTo(slug);
  if (path) return path[path.length - 1];
  // For dynamic sub-pages (e.g. data-products/programs/7076), resolve to the parent entry
  const parentSlug = getDynamicParentSlug(slug);
  if (parentSlug) return findDocBySlug(parentSlug);
  return undefined;
}

// Helper to get breadcrumb trail for a slug
export function getBreadcrumbs(slug: string): { title: string; slug: string }[] {
  const path = pathTo(slug);
  if (path) return path.map(p => ({ title: p.title, slug: p.slug }));

  // For dynamic sub-pages, build breadcrumbs from the parent dynamic entry
  const parentSlug = getDynamicParentSlug(slug);
  if (parentSlug) return getBreadcrumbs(parentSlug);

  return [];
}

// Helper to get previous and next pages for navigation
export function getAdjacentPages(slug: string): { prev?: DocPage; next?: DocPage } {
  const flatPages: DocPage[] = [];

  // Depth first; a section's same-slug "Overview" child is the section itself, so it is skipped
  const walk = (pages: DocPage[], parentSlug?: string) => {
    for (const page of pages) {
      if (page.slug === parentSlug) continue;
      flatPages.push(page);
      if (page.children) walk(page.children, page.slug);
    }
  };
  walk(docsNav);

  const currentIndex = flatPages.findIndex(p => p.slug === slug);

  return {
    prev: currentIndex > 0 ? flatPages[currentIndex - 1] : undefined,
    next: currentIndex < flatPages.length - 1 ? flatPages[currentIndex + 1] : undefined,
  };
}
