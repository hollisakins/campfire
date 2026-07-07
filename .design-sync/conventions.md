# Campfire UI — how to build with this design system

Campfire is a JWST spectroscopy archive. These are its React UI primitives
("Ember & Dusk" theme): a warm, near-paper light mode and a deep warm-violet
dark mode, with an ember-orange accent. Build interfaces out of these
components and style your own layout glue with the Tailwind token utilities
below — never hard-coded colors.

## Setup

- **No provider/wrapper is required.** Components are plain React; render them
  directly. (`ThemeToggle` reads a theme context but falls back to sensible
  defaults when none is present.)
- **Theme tokens are CSS variables** defined on `:root` (light) and `.dark`
  (dark) in `styles.css`. To render dark mode, add the `dark` class to a root
  ancestor (e.g. `<html class="dark">`); every token utility flips
  automatically. Do not restyle for dark mode by hand.
- **Fonts:** `font-sans` is Inter (UI text), `font-mono` is JetBrains Mono
  (data, coordinates, code). They are wired to `--font-sans` / `--font-mono`.

## Styling idiom — Tailwind utilities backed by tokens

This is a Tailwind utility-class system. Color/surface utilities map to theme
tokens, so they adapt to light/dark automatically. **Always prefer these over
literal Tailwind palette colors** (`bg-red-500` etc.) so output stays on-brand:

| Purpose | Utilities |
|---|---|
| Accent (ember) | `bg-primary`, `hover:bg-primary-hover`, `text-primary`, `text-on-primary` (on a primary fill), `bg-primary-soft` (selected/active wash) |
| Page & surfaces | `bg-background` (page + plot wells), `bg-card` (panels), `hover:bg-card-hover`, `bg-surface-2` (headers/insets), `bg-table-header` |
| Text | `text-text-primary`, `text-text-secondary`, `text-text-tertiary` (muted/placeholder) |
| Borders | `border-border`, `border-border-strong` (inputs/emphasis) |
| Status | `text-success` / `bg-success`, `text-warning`, `text-danger`, `text-info` (and `bg-`/`border-` variants) |
| Shape & type | `rounded-card` (panel radius), `font-sans`, `font-mono` |
| Top nav chrome | `bg-header`, `text-header-foreground`, `text-header-muted` (an always-dusk bar in both themes) |

Spacing, fl/grid layout, sizing, etc. use standard Tailwind utilities.

## Where the truth lives

- **`styles.css`** (and its `@import` closure, incl. `_ds_bundle.css`) — the
  tokens, fonts, and component styles. Read it before styling.
- **`components/<group>/<Name>/<Name>.d.ts`** — each component's prop contract.
- **`components/<group>/<Name>/<Name>.prompt.md`** — usage notes per component.

Note: filter/dropdown components (`FilterChip`, `FilterChipWithMode`,
`RangeFilterChip`, `CoordinateSearchChip`, `ColumnVisibilityDropdown`) render a
chip/button trigger and open their panel **on click**. `Badge` is a **stat**
badge (`value` + `label`), not a tag.

## Example

```tsx
// Components load to window.CampfireUI (from the root _ds_bundle.js).
const { Card, Button, Badge } = window.CampfireUI;

function TargetSummary() {
  return (
    <Card className="p-6 max-w-md">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-text-primary font-sans">COSMOS-3142871</h3>
        <span className="text-sm text-text-secondary font-mono">z = 4.812</span>
      </div>
      <div className="flex gap-3 mt-4">
        <Badge value={3} label="Spectra" compact />
        <Badge value="PRISM" label="Grating" compact />
      </div>
      <div className="mt-5 flex gap-2">
        <Button variant="primary" size="sm">Open</Button>
        <Button variant="ghost" size="sm">Download</Button>
      </div>
    </Card>
  );
}
```
