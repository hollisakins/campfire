/**
 * Shared "glass" surface vocabulary for the FitsGL map chrome (epic #337,
 * Phase 4.5). Every floating control — band rail, Display/Layers dock, status
 * pill, tool rail — sits on the same translucent, blurred panel over the
 * full-bleed viewer, so the map stays the hero (see `docs/design-fitsgl-map-ux.md`
 * §2). Centralised here so the aesthetic is defined once, not copy-pasted per
 * panel, and follows the Ember & Dusk tokens (`bg-card`, `border-border`, …) in
 * both themes.
 */

/**
 * Base translucent+blur panel surface. Compose with a radius/padding per panel.
 * Kept fairly opaque + dark-tinted (the chrome sits on the always-dark map well via
 * the `.fitsgl-chrome` token pin) so text stays legible over a white/grey stretch.
 */
export const GLASS = 'bg-[var(--glass-bg)] backdrop-blur-md border border-border shadow-xl';

/** A free-floating pill/panel (band rail, status pill): fully rounded corners. */
export const GLASS_PILL = `${GLASS} rounded-xl`;

/** Uppercase section caption inside a panel (matches the wireframes' tiny labels). */
export const PANEL_CAP =
  'text-[11px] font-medium uppercase tracking-wide text-text-tertiary';

/** A small toggle "chip" (stretch mode, band, preset). `active` fills with the accent. */
export function chipClass(active: boolean): string {
  return [
    'rounded-md px-2 py-1 text-xs transition-colors',
    active
      ? 'bg-primary-soft text-primary-text ring-1 ring-inset ring-[rgba(251,146,60,0.45)]'
      : 'text-text-secondary hover:bg-card-hover',
  ].join(' ');
}

/** A compact inset control (dropdown, channel picker) on the glass surface. */
export const INSET =
  'rounded-md border border-border bg-[var(--surface-2)] px-2 py-1 text-xs text-text-primary';
