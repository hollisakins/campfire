'use client';

import React from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { Flame, LogOut, User, Shield } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

export const Navigation: React.FC = () => {
  const pathname = usePathname();
  const router = useRouter();
  const { user, userProfile, signOut } = useAuth();

  const isActive = (path: string) => {
    if (path === '/') return pathname === '/';
    return pathname.startsWith(path);
  };

  const navLinks = [
    { href: '/', label: 'Home' },
    { href: '/nircam', label: 'NIRCam' },
    { href: '/spectra', label: 'NIRSpec' },
  ];

  const handleSignOut = async () => {
    await signOut();
    router.push('/login');
  };

  return (
    <nav className="bg-header text-white shadow-md">
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

            {/* User Menu */}
            {user ? (
              <div className="flex items-center space-x-4 ml-4 pl-4 border-l border-gray-600">
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
                className="text-sm font-medium text-gray-300 hover:text-white transition-colors ml-4 pl-4 border-l border-gray-600"
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
