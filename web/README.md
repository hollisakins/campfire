# CAMPFIRE Web Portal

Interactive web frontend for browsing, inspecting, and downloading JWST spectroscopic and 
imaging data products. Built with Next.js, Supabase, and Tailwind CSS.
Requires specific R2 and supabase access credentials. 

## Setup

```bash
npm install
npm run dev     # http://localhost:3000
```

### Environment Variables

Create a `.env.local` file with the following:

```bash
NEXT_PUBLIC_SUPABASE_URL=       # Supabase project URL
NEXT_PUBLIC_SUPABASE_ANON_KEY=  # Supabase anon/public key
SUPABASE_SERVICE_ROLE_KEY=      # Supabase service role key (server-only)
R2_ACCOUNT_ID=                  # Cloudflare R2 account
R2_ACCESS_KEY_ID=               # R2 access key
R2_SECRET_ACCESS_KEY=           # R2 secret key
R2_BUCKET_NAME=                 # R2 bucket name
R2_PUBLIC_URL=                  # Public URL for R2 assets
```

### Local Supabase (optional)

For local development with a full database:

```bash
supabase start
cd ../supabase && supabase db reset  # Apply migrations + seed data
```

## Features

- **Spectra catalog** — Filterable, sortable table of spectroscopic objects with server-side pagination
- **Spectrum viewer** — Interactive 1D/2D spectrum display with emission line overlays
- **Inspection mode** — Keyboard-driven workflow for rapid redshift quality assessment
- **NIRCam imaging** — Leaflet-based tile viewer for deep field mosaics
- **Cone search** — Coordinate-based spatial queries (decimal degrees or HMS/DMS)
- **Bulk download** — Batch FITS file downloads via presigned R2 URLs
- **Auth + access control** — Supabase Auth with program-level access codes for proprietary data
- **Admin panel** — User management, invite system, activity tracking

## Project Structure

```
app/                    # Next.js App Router pages and API routes
components/             # React components
  ├── spectra/          #   Spectrum viewer, table, filters
  │   └── inspection/   #   Inspection mode components
  ├── nircam/           #   NIRCam tile viewer
  └── ui/               #   Shared UI primitives
lib/                    # Core logic
  ├── actions/          #   Server actions (data fetching)
  ├── contexts/         #   React contexts (auth, theme, preferences)
  ├── hooks/            #   Custom React hooks
  ├── supabase/         #   Supabase client setup (browser + server)
  ├── auth/             #   Authentication helpers
  ├── utils/            #   Coordinate parsing, URL params, WCS, tile compositing
  ├── email/            #   Email templates (Resend)
  ├── docs/             #   Documentation content (MDX)
  ├── providers/        #   React context providers
  ├── flags.ts          #   Bitmask flag definitions
  ├── types.ts          #   TypeScript type definitions
  └── r2.ts             #   Cloudflare R2 client
workers/                # Cloudflare Workers (tile serving, batch downloads)
```

## Build

```bash
npm run build           # Production build (catches TS/ESLint errors)
npm run lint            # ESLint only
```
