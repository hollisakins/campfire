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
      },
      animation: {
        'fade-in': 'fade-in 200ms ease-out',
        'zoom-in': 'zoom-in 200ms ease-out',
      },
    }
  },
  plugins: [],
};

export default config;
