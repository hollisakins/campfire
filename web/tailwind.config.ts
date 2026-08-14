import type { Config } from 'tailwindcss';

const config: Config = {
  darkMode: 'class',
  content: [
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
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
        }
      },
      fontFamily: {
        sans: ['var(--font-sans)', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['var(--font-mono)', 'ui-monospace', 'monospace'],
      },
      borderRadius: {
        'card': '0.75rem',
      },
      keyframes: {
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'zoom-in': {
          '0%': { opacity: '0', transform: 'scale(0.95)' },
          '100%': { opacity: '1', transform: 'scale(1)' },
        },
        // Panel entrance for the pinned-objects bucket: anchored top-right, so
        // it grows out of the collapsed chip rather than zooming from center.
        'unfurl': {
          '0%': { opacity: '0', transform: 'scale(0.92) translateY(-4px)' },
          '100%': { opacity: '1', transform: 'scale(1) translateY(0)' },
        },
        // Per-card stagger inside the bucket panel (used with animation-delay
        // and `backwards` fill so delayed cards start invisible).
        'slide-in-up': {
          '0%': { opacity: '0', transform: 'translateY(6px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        // Override Tailwind's default `spin` keyframe, which only defines a
        // `to: rotate(360deg)` frame. With an implicit `from` (the element's
        // underlying `none`), Firefox interpolates `none -> rotate(360deg)` via
        // matrix decomposition instead of component-wise angle interpolation.
        // Because rotate(360deg) decomposes to the identity matrix, Firefox's
        // decompose/recompose of the intermediate frames leaks a 3D rotation
        // component, making `animate-spin` flip around a vertical axis instead
        // of spinning flat (Chrome/Safari are unaffected). Declaring both
        // endpoints as matching rotate() functions keeps the interpolation 2D.
        spin: {
          from: { transform: 'rotate(0deg)' },
          to: { transform: 'rotate(360deg)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 200ms ease-out',
        'zoom-in': 'zoom-in 200ms ease-out',
        'unfurl': 'unfurl 180ms cubic-bezier(0.16, 1, 0.3, 1)',
        'slide-in-up': 'slide-in-up 250ms ease-out backwards',
        spin: 'spin 1s linear infinite',
      },
    }
  },
  plugins: [],
};

export default config;
