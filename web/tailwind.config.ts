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
