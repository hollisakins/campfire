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
        // Override Tailwind's default `spin` keyframe (a single
        // `to: rotate(360deg)` frame). Firefox interpolates transform
        // animations by decomposing the endpoints into rotation quaternions and
        // slerping between them. A full turn, rotate(360deg), maps to the
        // quaternion antipodal to the identity, so the rotation axis is
        // undefined and Firefox slerps around an arbitrary axis (Y) — the
        // spinner flips in/out of the page instead of spinning flat.
        // Chrome/Safari special-case 2D rotate() and interpolate the scalar
        // angle, so they're unaffected. Breaking the turn into quarter-turn
        // waypoints keeps every interpolation segment an unambiguous 90deg
        // Z-rotation, which no engine can reinterpret as a 3D flip. Applies to
        // every `animate-spin` (all Loader2 icons + the border spinner).
        spin: {
          '0%': { transform: 'rotate(0deg)' },
          '25%': { transform: 'rotate(90deg)' },
          '50%': { transform: 'rotate(180deg)' },
          '75%': { transform: 'rotate(270deg)' },
          '100%': { transform: 'rotate(360deg)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 200ms ease-out',
        'zoom-in': 'zoom-in 200ms ease-out',
        spin: 'spin 1s linear infinite',
      },
    }
  },
  plugins: [],
};

export default config;
