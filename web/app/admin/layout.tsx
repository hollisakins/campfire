'use client';

import React from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useAuth } from '@/lib/contexts/AuthContext';
import { Shield, KeyRound, Users, Loader2, AlertTriangle, FolderOpen, Activity, UserPlus, Download, Camera } from 'lucide-react';

const adminNavItems = [
  { href: '/admin/activity', label: 'Activity', icon: Activity },
  { href: '/admin/downloads', label: 'Downloads', icon: Download },
  { href: '/admin/nircam', label: 'NIRCam', icon: Camera },
  { href: '/admin/requests', label: 'Account Requests', icon: UserPlus },
  { href: '/admin/codes', label: 'Access Codes', icon: KeyRound },
  { href: '/admin/users', label: 'Users', icon: Users },
  { href: '/admin/programs', label: 'Programs', icon: FolderOpen },
];

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const { user, userProfile, loading } = useAuth();

  // Loading state
  if (loading) {
    return (
      <div className="container mx-auto px-4 py-16 flex items-center justify-center">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
        <span className="ml-3 text-text-secondary dark:text-slate-400">Loading...</span>
      </div>
    );
  }

  // Not authenticated
  if (!user) {
    return (
      <div className="container mx-auto px-4 py-16">
        <div className="max-w-md mx-auto text-center">
          <div className="w-16 h-16 bg-red-100 dark:bg-red-950 rounded-full flex items-center justify-center mx-auto mb-4">
            <AlertTriangle className="w-8 h-8 text-red-600 dark:text-red-400" />
          </div>
          <h1 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Authentication Required
          </h1>
          <p className="text-text-secondary dark:text-slate-400 mb-6">
            Please sign in to access the admin area.
          </p>
          <Link
            href="/login"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            Sign In
          </Link>
        </div>
      </div>
    );
  }

  // Not an admin
  if (!userProfile?.is_admin) {
    return (
      <div className="container mx-auto px-4 py-16">
        <div className="max-w-md mx-auto text-center">
          <div className="w-16 h-16 bg-red-100 dark:bg-red-950 rounded-full flex items-center justify-center mx-auto mb-4">
            <Shield className="w-8 h-8 text-red-600 dark:text-red-400" />
          </div>
          <h1 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Access Denied
          </h1>
          <p className="text-text-secondary dark:text-slate-400 mb-6">
            You don&apos;t have permission to access the admin area.
          </p>
          <Link
            href="/"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            Return Home
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <div className="flex gap-8">
        {/* Sidebar */}
        <aside className="w-56 flex-shrink-0">
          <div className="flex items-center gap-2 mb-6">
            <Shield className="w-6 h-6 text-primary" />
            <h1 className="text-xl font-semibold text-text-primary dark:text-slate-100">Admin</h1>
          </div>

          <nav className="space-y-1">
            {adminNavItems.map((item) => {
              const isActive = pathname.startsWith(item.href);
              const Icon = item.icon;

              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`
                    flex items-center gap-3 px-4 py-2 rounded-lg transition-colors
                    ${isActive
                      ? 'bg-primary text-white'
                      : 'text-text-secondary dark:text-slate-400 hover:bg-card dark:hover:bg-slate-700 hover:text-text-primary dark:hover:text-slate-100'
                    }
                  `}
                >
                  <Icon className="w-5 h-5" />
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </aside>

        {/* Main Content */}
        <main className="flex-1 min-w-0">
          {children}
        </main>
      </div>
    </div>
  );
}
