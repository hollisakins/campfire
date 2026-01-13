import type { Metadata } from 'next';
import { Roboto, Roboto_Mono } from 'next/font/google';
import './globals.css';
import { Navigation } from '@/components/layout/Navigation';
import { AuthProvider } from '@/lib/contexts/AuthContext';
import { ThemeProvider } from '@/lib/contexts/ThemeContext';
import { PreferencesProvider } from '@/lib/contexts/PreferencesContext';
import { QueryProvider } from '@/lib/providers/QueryProvider';
import { Analytics } from '@vercel/analytics/next';

const roboto = Roboto({
  subsets: ['latin'],
  weight: ['400', '500', '700'],
  variable: '--font-roboto',
  display: 'swap',
});

const robotoMono = Roboto_Mono({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-roboto-mono',
  display: 'swap',
});

// Inline script to prevent flash of wrong theme
const themeScript = `
  (function() {
    try {
      const theme = localStorage.getItem('campfire-theme');
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      if (theme === 'dark' || (theme === 'system' && prefersDark) || (!theme && prefersDark)) {
        document.documentElement.classList.add('dark');
      }
    } catch (e) {}
  })();
`;

export const metadata: Metadata = {
  title: 'CAMPFIRE - JWST Spectroscopy Archive',
  description: 'COSMOS Archive of MultiPle-Field Internal Reductions & Extractions',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className={`${roboto.variable} ${robotoMono.variable} font-sans antialiased min-h-screen bg-background dark:bg-slate-900 text-text-primary dark:text-slate-100`}>
        <QueryProvider>
          <AuthProvider>
            <ThemeProvider>
              <PreferencesProvider>
                <Navigation />
                <main>{children}</main>
              </PreferencesProvider>
            </ThemeProvider>
          </AuthProvider>
        </QueryProvider>
        <Analytics />
      </body>
    </html>
  );
}
