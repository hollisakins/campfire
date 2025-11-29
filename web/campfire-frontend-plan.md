# CAMPFIRE - JWST Spectroscopy Archive Rebuild

I'm rebuilding CAMPFIRE, an internal archive for JWST NIRSpec spectroscopy data. For the frontend, I have an existing static HTML/JS site that I want to modernize with Next.js while preserving the design language and core functionality.
CAMPFIRE is an internal archive and web portal for sharing reduced JWST (James Webb Space Telescope) NIRCam imaging and NIRSpec spectroscopy data within a research team. It serves as a centralized platform for browsing, analyzing, and collaborating on high-redshift galaxy observations.

## Core Functionality

### Data Access & Organization
- **Program-based access control**: Users authenticate and see only the data from JWST programs they have access to
- **Object catalog browsing**: Searchable, filterable table of astronomical objects with NIRSpec spectroscopy
- **Individual object detail pages**: Comprehensive view of each source including metadata, spectral plots, and observational parameters
- **NIRCam image access**: Direct download links and browsing interface for reduced imaging mosaics

### Search & Filtering
Users can filter the object catalog by:
- JWST program ID
- Survey field (e.g., GOODS-N, COSMOS)
- Observation parameters (gratings used, signal-to-noise ratio)
- Redshift quality flags (confidence level: 0-4 scale)
- Object classification flags (LRDs, AGN, LAE, etc.)
- Data quality flags (contamination, chip gaps, etc.)

### Object Detail View
Each object's detail page provides:
- **Key metrics**: Maximum S/N, redshift, redshift quality, number of gratings observed
- **Tabbed interface**: 
  - Spectroscopy (plots for each grating: PRISM, G140M, G235M, G395M)
  - Redshift information and spectral features
  - Photometry (multi-wavelength measurements)
  - Notes/comments (team collaboration)
  - Context (cutout images, finding charts)
- **Observational metadata**: Exposure times, spectral coverage, MSA configuration
- **Navigation**: Previous/next buttons to browse through filtered results
- **Download options**: Individual spectrum downloads or bulk "Download All"

### Collaboration Features
- **Comments system**: Team members can add notes and observations to individual objects
- **Flag management**: Users can add/modify classification flags and data quality flags
- **Audit trail**: Track who made what changes to flags and comments
- **Inspection workflow**: Mark objects for review, flag issues, or note interesting features

### User Types
1. **Group accounts**: Shared credentials per JWST program (read-only, for proprietary data access)
2. **Individual accounts**: Personal logins with ability to add comments and flags

## Technical Architecture

### Current Implementation
- Static HTML/JS site hosted on university cluster
- Simple password protection
- Direct file serving via HTTP
- External Google Form for comments (manual updates)

### Target Implementation (This Rebuild)
- **Frontend**: Next.js 14 with TypeScript and React
- **Backend**: Next.js API routes for authentication and data access
- **Database**: Supabase (PostgreSQL) for metadata, user management, comments
- **Storage**: Cloudflare R2 for FITS files (private bucket with signed URLs)
- **Authentication**: Supabase Auth with row-level security policies
- **Deployment**: Vercel or Cloudflare Pages

### Data Pipeline Integration
- FITS files uploaded to R2 from reduction pipeline
- Object and spectrum metadata automatically pushed to Supabase database
- Web interface updates in real-time as new data is processed
- No manual migration - deployment built into reduction workflow

## Key Features for v1 (MVP)

### Must Have
- User authentication (login/logout)
- Spectra table with sidebar filters
- Object detail pages with tabs
- Spectrum metadata display
- Grating information sections
- Download button functionality (generate signed URLs)
- Basic navigation (breadcrumbs, prev/next)

### Nice to Have (Future)
- Interactive Plotly.js spectrum plots
- Inline comment/flag editing
- Real-time notifications
- NIRCam image browser
- Documentation pages
- Python client library for programmatic access
- Batch download tools

## Database Schema Highlights

### Core Tables
- **objects**: Astronomical sources (one per object)
- **spectra**: Individual observations (multiple gratings per object)
- **programs**: JWST program metadata
- **user_profiles**: Extended user information
- **user_program_access**: Links users to programs they can access
- **comments**: User comments on objects
- **flag_audit_log**: Complete history of flag changes
- **flag_definitions**: Metadata for UI rendering (labels, colors, icons)

### Key Relationships
- One object → many spectra (different gratings, reduction versions)
- One user → many programs (via access table)
- One object → many comments (threaded discussions)
- All flag changes tracked in audit log

## Success Criteria

A successful v1 will:
1. Match the functionality of the current static site
2. Provide reliable, fast access to spectra
3. Enable team collaboration (comments/flags)
4. Enforce proper access control
5. Integrate seamlessly with reduction pipeline
6. Be maintainable and extensible
7. Cost <$15/month to operate

## Design Language to Preserve

From my current site:
- **Color scheme:**
  - Primary accent: Magenta/purple (#c026d3)
  - Dark header: Slate blue-gray (#475569)
  - Light background (#f8fafc) with subtle borders (#e2e8f0)
  - Info cards: Light gray background with rounded corners
- **Layout patterns:**
  - Clean, card-based design
  - Metrics displayed in pill-shaped cards (MAX S/N, REDSHIFT, QUALITY, GRATINGS)
  - Tab-based navigation for different data views
  - Collapsible sections with disclosure triangles (▼)
  - Breadcrumb navigation
  - Pagination with prev/next arrows
- **Typography:**
  - Monospace font for object IDs and technical data
  - Clean sans-serif for UI elements
  - Large numbers in metric cards with small labels below

## Project Setup

Create a Next.js 14 project with:
- TypeScript
- App Router
- Tailwind CSS (configure with colors matching my design)
- ESLint

Install these dependencies:
```bash
npm install @supabase/supabase-js @supabase/ssr
npm install @aws-sdk/client-s3 @aws-sdk/s3-request-presigner
npm install @tanstack/react-table
npm install react-plotly.js plotly.js
npm install @types/plotly.js
npm install lucide-react  # For icons
```

## Folder Structure
```
campfire-web/
├── app/
│   ├── layout.tsx                      # Root layout with nav
│   ├── page.tsx                        # Homepage
│   ├── login/
│   │   └── page.tsx                    # Login page (dummy auth for v1)
│   ├── spectra/
│   │   ├── layout.tsx                  # Spectra section layout
│   │   ├── page.tsx                    # Spectra table with sidebar filters
│   │   └── [id]/
│   │       └── page.tsx                # Detail view with tabs
│   └── api/
│       ├── spectra/
│       │   ├── route.ts                # List with filters (mock for now)
│       │   └── [id]/
│       │       └── route.ts            # Get details (mock for now)
│       └── auth/
│           └── callback/route.ts       # Placeholder
├── components/
│   ├── ui/
│   │   ├── Card.tsx
│   │   ├── Button.tsx
│   │   ├── Badge.tsx
│   │   ├── Tabs.tsx
│   │   └── Breadcrumbs.tsx
│   ├── layout/
│   │   └── Navigation.tsx              # Top nav: Home | NIRCam | NIRSpec
│   ├── spectra/
│   │   ├── SpectraTable.tsx            # Main table component
│   │   ├── SpectraFilters.tsx          # Sidebar filters
│   │   ├── MetricCards.tsx             # S/N, Redshift, Quality, Gratings cards
│   │   ├── SpectrumTabs.tsx            # Tab navigation component
│   │   ├── GratingDetails.tsx          # Collapsible grating info section
│   │   └── Pagination.tsx              # "1 of 920" with arrows
│   └── auth/
│       └── LoginForm.tsx               # Simple login UI (dummy for v1)
├── lib/
│   ├── supabase/
│   │   ├── client.ts                   # Client-side Supabase setup
│   │   └── server.ts                   # Server-side Supabase setup
│   ├── r2.ts                           # R2 client (placeholder)
│   ├── flags.ts                        # Bitwise flag utilities
│   ├── types.ts                        # Shared TypeScript types
│   └── mock-data.ts                    # Temporary mock data
└── middleware.ts                       # Auth middleware (dummy for v1)
```

## Tailwind Configuration
```typescript
// tailwind.config.ts
import type { Config } from 'tailwindcss'

const config: Config = {
  content: [
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        primary: '#c026d3',      // Magenta accent
        'primary-hover': '#a21caf',
        header: '#475569',       // Dark slate header
        background: '#ffffff',
        card: '#f8fafc',         // Light card background
        'card-hover': '#f1f5f9',
        border: '#e2e8f0',       // Subtle borders
        text: {
          primary: '#0f172a',
          secondary: '#64748b',
        }
      },
      fontFamily: {
        mono: ['ui-monospace', 'Courier New', 'monospace'],
      },
      borderRadius: {
        'card': '0.75rem',
      }
    }
  },
  plugins: [],
}
export default config
```

## Data Structure (Temporary Mock)

Use these TypeScript types (will match Supabase schema):
```typescript
// lib/types.ts
export interface SpectrumObject {
  id: number
  object_id: string
  program_id: number
  program_name?: string
  field: string
  ra: number
  dec: number
  redshift: number | null
  redshift_quality: number  // 0-4
  spectral_features: number  // bitmask
  object_flags: number      // bitmask
  dq_flags: number         // bitmask
  spectra: Spectrum[]
  max_snr?: number         // Derived: max S/N across gratings
  num_gratings?: number    // Derived: count of spectra
}

export interface Spectrum {
  id: number
  grating: string  // 'PRISM', 'G140M', 'G235M', 'G395M'
  fits_path: string
  reduction_version: string
  signal_to_noise: number | null
  exposure_time?: number
  num_exposures?: number
  wavelength_range?: string
}

export interface FilterState {
  programs: number[]
  fields: string[]
  gratings: string[]
  redshift_quality: number[]
  snr_range: [number, number]
  flags: number[]
}
```

Create a mock data file with 3-5 example objects for initial development:
```typescript
// lib/mock-data.ts
export const mockSpectra: SpectrumObject[] = [
  {
    id: 1,
    object_id: 'capers_cosmos_p2_100391',
    program_id: 6368,
    program_name: 'CAPERS',
    field: 'COSMOS',
    ra: 150.185450,
    dec: 2.26756,
    redshift: 6.26,
    redshift_quality: 0,
    spectral_features: 0,
    object_flags: 0,
    dq_flags: 0,
    max_snr: 5.7,
    num_gratings: 1,
    spectra: [
      {
        id: 1,
        grating: 'PRISM',
        fits_path: 'v1.0/capers_cosmos_p2_100391/PRISM.fits',
        reduction_version: 'v1.0',
        signal_to_noise: 5.7,
        exposure_time: 3.2,
        num_exposures: 12,
        wavelength_range: '0.6 - 5.3 μm'
      }
    ]
  },
  // Add 2-3 more varied examples
]
```

We'll replace this with real Supabase queries after initial setup.

## Core Components to Build

### 1. Navigation Component
Simple top nav bar with:
- CAMPFIRE logo (left)
- Links: Home | NIRCam | NIRSpec (right)
- Active state styling (magenta underline)

### 2. Login Page (Dummy)
Simple centered form with:
- Email input
- Password input  
- "Login" button (magenta)
- On submit: Just navigate to /spectra (no real auth for v1)

### 3. Spectra Table Page
Layout with:
- **Left sidebar** (250px fixed width):
  - Filter controls:
    - Program multi-select
    - Field multi-select
    - Grating checkboxes
    - Redshift quality checkboxes (0-4)
    - S/N range slider
    - Flag checkboxes
  - "Clear Filters" button at bottom
- **Main content area**:
  - Breadcrumb: CAMPFIRE › NIRSpec
  - Search input at top
  - Table with columns: Object ID, Field, RA, Dec, Redshift, Quality, # Gratings, Max S/N
  - Sortable columns
  - Row hover effect
  - Click row → navigate to detail page
  - Pagination at bottom

### 4. Spectrum Detail Page
Layout with:
- **Breadcrumb**: CAMPFIRE › NIRSpec › {object_id}
- **Header section**:
  - Object ID (large, monospace)
  - Coordinates and program info (smaller text)
- **Metric cards row** (4 cards):
  - MAX S/N: {value}
  - REDSHIFT: {value}
  - QUALITY: {label}
  - GRATINGS: {count}
- **Action buttons** (right side):
  - "Download All" (magenta)
  - "Inspection Form" (gray outline)
- **Pagination** (top right): "1 of 920" with prev/next arrows
- **Tab navigation**:
  - PRISM-CLEAR (or first grating)
  - REDSHIFT
  - PHOTOMETRY
  - NOTES
  - CONTEXT
  - Active tab: magenta underline
- **Tab content area**:
  - For spectroscopy tabs:
    - Collapsible "GRATING DETAILS" section
      - Configuration, Exposure Time, # Combined, Max S/N
      - Spectral Coverage
    - Placeholder for spectrum plot

### 5. UI Components

**Card:**
```typescript
// Rounded container with light background, subtle shadow
<Card className="...">
  {children}
</Card>
```

**Badge (Metric Card):**
```typescript
// Large value on top, small label below, rounded rectangle
<Badge value="5.7" label="MAX S/N" />
```

**Button:**
```typescript
// Primary: magenta background, white text
// Secondary: gray outline, dark text
<Button variant="primary">Download All</Button>
```

**Tabs:**
```typescript
// Horizontal tab bar with underline for active tab
<Tabs defaultValue="prism">
  <TabsList>
    <TabsTrigger value="prism">PRISM-CLEAR</TabsTrigger>
    <TabsTrigger value="redshift">REDSHIFT</TabsTrigger>
  </TabsList>
  <TabsContent value="prism">...</TabsContent>
</Tabs>
```

## Initial Tasks

1. Create project structure
2. Configure Tailwind with color scheme
3. Build UI component library (Card, Button, Badge, Tabs, Breadcrumbs)
4. Create Navigation component
5. Build Login page (dummy functionality)
6. Set up mock data file with types
7. Create Spectra table page with sidebar filters (using mock data)
8. Create Spectrum detail page with tabs (using mock data)
9. Add basic routing and navigation

## What NOT to Include Yet

- Real authentication (dummy login is fine)
- Real Supabase connection (we'll add next)
- R2 file downloads (placeholders fine)
- NIRCam section (just a blank page is fine)
- Interactive plots (placeholder divs fine)
- Comments functionality

## Design Guidelines

- Generous use of rounded corners (rounded-lg, rounded-xl)
- Subtle shadows on cards
- Magenta (#c026d3) for all primary actions
- Dark slate header with clean white content
- Monospace font for technical IDs, coordinates, and measurements
- Ample whitespace and padding
- Smooth hover effects

## After Initial Setup

Once the structure is in place, I'll provide:
- Real Supabase credentials
- Export of actual data for testing
- Specific component refinements
- Plot integration details

Ready to start?



---

**Last Updated**: 2024-11-17
**Status**: Planning phase, ready to begin Next.js implementation

