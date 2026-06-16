/**
 * Sync-only Tailwind config for design-sync's cssEntry. Mirrors the token
 * mapping in web/tailwind.config.ts, but ADDS a safelist so the shipped
 * stylesheet contains the full token-backed utility palette — not just the
 * classes the app happens to use today. The Claude Design agent builds with
 * these utilities, so they must all be present even if the app doesn't use
 * them yet. Keep the colors block in sync with web/tailwind.config.ts.
 */
const colors = {
  primary: 'var(--primary)',
  'primary-hover': 'var(--primary-hover)',
  'primary-text': 'var(--primary-text)',
  'on-primary': 'var(--on-primary)',
  'primary-soft': 'var(--primary-soft)',
  header: 'var(--header)',
  'header-elevated': 'var(--header-elevated)',
  'header-foreground': 'var(--header-foreground)',
  'header-muted': 'var(--header-muted)',
  'header-border': 'var(--header-border)',
  'header-hover': 'var(--header-hover)',
  background: 'var(--background)',
  card: 'var(--card)',
  'card-hover': 'var(--card-hover)',
  'surface-2': 'var(--surface-2)',
  'table-header': 'var(--table-header)',
  border: 'var(--border)',
  'border-strong': 'var(--border-strong)',
  success: 'var(--success)',
  warning: 'var(--warning)',
  danger: 'var(--danger)',
  info: 'var(--info)',
  text: {
    primary: 'var(--text-primary)',
    secondary: 'var(--text-secondary)',
    tertiary: 'var(--text-tertiary)',
  },
};

const tokenNames = [
  'primary', 'primary-hover', 'primary-text', 'on-primary', 'primary-soft',
  'header', 'header-elevated', 'header-foreground', 'header-muted', 'header-border', 'header-hover',
  'background', 'card', 'card-hover', 'surface-2', 'table-header',
  'border', 'border-strong', 'success', 'warning', 'danger', 'info',
];

module.exports = {
  darkMode: 'class',
  content: [
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    '../.design-sync/previews/**/*.{ts,tsx}',
  ],
  safelist: [
    { pattern: new RegExp(`^(bg|text|border)-(${tokenNames.join('|')})$`), variants: ['hover', 'focus', 'dark'] },
    { pattern: /^text-text-(primary|secondary|tertiary)$/, variants: ['hover', 'dark'] },
    'rounded-card', 'font-sans', 'font-mono',
  ],
  theme: {
    extend: {
      colors,
      fontFamily: {
        sans: ['var(--font-sans)', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['var(--font-mono)', 'ui-monospace', 'monospace'],
      },
      borderRadius: { card: '0.75rem' },
    },
  },
  plugins: [],
};
