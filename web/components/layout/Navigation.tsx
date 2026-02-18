'use client';

import React from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { Flame, LogOut, User, Shield, Sun, Moon, Monitor } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';
import { useTheme } from '@/lib/contexts/ThemeContext';

export const Navigation: React.FC = () => {
  const pathname = usePathname();
  const router = useRouter();
  const { user, userProfile, signOut } = useAuth();
  const { theme, setTheme } = useTheme();

  const cycleTheme = () => {
    const themes: Array<'light' | 'dark' | 'system'> = ['light', 'system', 'dark'];
    const currentIndex = themes.indexOf(theme);
    const nextIndex = (currentIndex + 1) % themes.length;
    setTheme(themes[nextIndex]);
  };

  const ThemeIcon = theme === 'dark' ? Moon : theme === 'light' ? Sun : Monitor;

  const isActive = (path: string) => {
    if (path === '/') return pathname === '/';
    return pathname.startsWith(path);
  };

  const navLinks = [
    { href: '/', label: 'Home' },
    { href: '/nircam', label: 'NIRCam' },
    { href: '/spectra', label: 'NIRSpec' },
    { href: '/map', label: 'Map' },
    { href: '/docs', label: 'Docs' },
  ];

  const handleSignOut = async () => {
    await signOut();
    router.push('/login');
  };

  return (
    <nav className="bg-header dark:bg-slate-900 text-white shadow-md">
      <div className="container mx-auto px-4 py-4">
        <div className="flex items-center justify-between">
          {/* Logo */}
          <Link href="/" className="flex items-center space-x-2 hover:opacity-80 transition-opacity">
            <Flame className="w-8 h-8 text-primary" />
            <span className="text-xl font-bold">CAMPFIRE</span>
          </Link>

          {/* Navigation Links */}
          <div className="flex items-center space-x-8">
            {navLinks.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                className={`
                  text-sm font-medium transition-colors pb-1 border-b-2
                  ${isActive(link.href)
                    ? 'text-white border-primary'
                    : 'text-gray-300 border-transparent hover:text-white hover:border-gray-400'
                  }
                `}
              >
                {link.label}
              </Link>
            ))}

            {/* Theme Toggle */}
            <button
              onClick={cycleTheme}
              className="flex items-center space-x-1 text-sm text-gray-300 hover:text-white transition-colors"
              aria-label={`Current theme: ${theme}. Click to change.`}
              title={`Theme: ${theme}`}
            >
              <ThemeIcon className="w-4 h-4" />
            </button>

            {/* User Menu */}
            {user ? (
              <div className="flex items-center space-x-4 ml-4 pl-4 border-l border-gray-600 dark:border-slate-700">
                {userProfile?.is_admin && (
                  <Link
                    href="/admin"
                    className="flex items-center space-x-1 text-sm text-gray-300 hover:text-white transition-colors"
                  >
                    <Shield className="w-4 h-4" />
                    <span>Admin</span>
                  </Link>
                )}
                <Link
                  href="/profile"
                  className="flex items-center space-x-2 text-sm text-gray-300 hover:text-white transition-colors"
                >
                  <User className="w-4 h-4" />
                  <span>{userProfile?.full_name || user.email}</span>
                </Link>
                <button
                  onClick={handleSignOut}
                  className="flex items-center space-x-1 text-sm text-gray-300 hover:text-white transition-colors"
                  aria-label="Sign out"
                >
                  <LogOut className="w-4 h-4" />
                </button>
              </div>
            ) : (
              <Link
                href="/login"
                className="text-sm font-medium text-gray-300 hover:text-white transition-colors ml-4 pl-4 border-l border-gray-600 dark:border-slate-700"
              >
                Sign In
              </Link>
            )}
          </div>
        </div>
      </div>
    </nav>
  );
};
