import type { Config } from 'tailwindcss';

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
};

export default config;
