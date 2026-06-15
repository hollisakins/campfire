# New update entry

Create a new entry for the **Updates** feed shown on the CAMPFIRE landing page
and the `/updates` page.

Description of the update: $ARGUMENTS

## What to produce

Write a single markdown file to:

```
web/lib/updates/content/<YYYY-MM-DD>-<slug>.md
```

- `<YYYY-MM-DD>` is today's date (use the real current date).
- `<slug>` is a short, kebab-case summary of the update (e.g. `extended-wavelength`,
  `ember-dr1`, `cli-bulk-download`). Keep it stable — it becomes the `/updates`
  anchor.

The file has YAML frontmatter followed by a markdown body:

```markdown
---
title: "Concise, specific headline"
date: <YYYY-MM-DD>
category: <data | pipeline | client | release>
summary: "One or two sentences shown in the landing-page feed."
links:
  - { label: "Reduction docs", href: "/docs/reduction/nirspec" }
pinned: false
---

The full update body in markdown. The first paragraph should stand alone as
context. Link to relevant docs, catalog pages, or program pages. This body is
rendered in full on /updates; the landing feed shows the `summary` instead.
```

## Field guidance

- **category** — pick exactly one:
  - `data` — new observations / spectra / imaging available in the archive.
  - `pipeline` — new pipeline functionality, re-reductions, calibration changes.
  - `client` — new CLI / Python client / REST API functionality.
  - `release` — a frozen "data release" (periodic database freeze). Rendered with
    emphasis; use sparingly.
- **summary** — required-ish: if omitted, the loader falls back to the first
  paragraph of the body, but an explicit one-liner reads better in the feed.
- **links** — optional list of `{ label, href }`. Prefer internal links
  (`/docs/...`, `/nirspec`, `/profile`); external `http(s)` links open in a new tab.
- **pinned** — optional; `true` floats the entry to the top regardless of date.
  Default `false`.

## Steps

1. Infer `title`, `category`, `slug`, `summary`, and `links` from the description.
   Ask the user only if the category or a key link is genuinely ambiguous.
2. Write the file with today's date in both the filename and `date` frontmatter.
3. Confirm the path you created and show the rendered frontmatter so the user can
   eyeball it. No build step is required — the file is picked up automatically.
4. For a `release` entry, also remind the user to update
   `web/lib/updates/versions.ts` (pipeline / client versions, data-release label).
