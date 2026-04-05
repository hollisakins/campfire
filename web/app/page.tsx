'use client';

import Link from 'next/link';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Flame, Camera, Sparkles, LogIn, BookOpen, Settings, Loader2 } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

export default function Home() {
  const { user, userProfile, loading } = useAuth();

  const isLoggedIn = !!user && !!userProfile;
  const isGroupAccount = userProfile?.is_group_account ?? false;

  return (
    <div className="container mx-auto px-4 py-12">
      <div className="max-w-4xl mx-auto">
        {/* Hero Section */}
        <div className="text-center mb-12">
          <div className="flex items-center justify-center mb-4">
            <Flame className="w-16 h-16 text-primary" />
          </div>
          <h1 className="text-5xl font-bold text-text-primary dark:text-slate-100 mb-4">
            CAMPFIRE
          </h1>
          <p className="text-xl text-text-secondary dark:text-slate-400 mb-2">
            COSMOS Archive of MultiPle-Field Internal Reductions & Extractions
          </p>
          <p className="text-lg text-text-secondary dark:text-slate-400">
            Internal archive for JWST NIRCam imaging and NIRSpec spectroscopy data
          </p>
        </div>

        {/* Loading state */}
        {loading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="w-8 h-8 animate-spin text-primary" />
          </div>
        ) : (
          <>
            {/* Data Access Cards */}
            <div className="grid md:grid-cols-2 gap-6 mb-8">
              {/* NIRCam Card */}
              <Card className="p-8">
                <div className="flex items-center mb-4">
                  <Camera className="w-8 h-8 text-primary mr-3" />
                  <h2 className="text-2xl font-bold text-text-primary dark:text-slate-100">NIRCam</h2>
                </div>
                <p className="text-text-secondary dark:text-slate-400 mb-6">
                  Browse and download reduced NIRCam imaging mosaics from multiple survey fields.
                </p>
                {isLoggedIn ? (
                  <Link href="/nircam">
                    <Button variant="secondary" className="w-full">
                      Browse NIRCam Data
                    </Button>
                  </Link>
                ) : (
                  <Button variant="secondary" className="w-full" disabled>
                    Browse NIRCam Data
                  </Button>
                )}
              </Card>

              {/* NIRSpec Card */}
              <Card className="p-8">
                <div className="flex items-center mb-4">
                  <Sparkles className="w-8 h-8 text-primary mr-3" />
                  <h2 className="text-2xl font-bold text-text-primary dark:text-slate-100">NIRSpec</h2>
                </div>
                <p className="text-text-secondary dark:text-slate-400 mb-6">
                  Explore reduced spectroscopy data with interactive tools and quality assessments.
                </p>
                {isLoggedIn ? (
                  <Link href="/nirspec">
                    <Button variant="primary" className="w-full">
                      Browse NIRSpec Spectra
                    </Button>
                  </Link>
                ) : (
                  <Button variant="primary" className="w-full" disabled>
                    Browse NIRSpec Spectra
                  </Button>
                )}
              </Card>
            </div>

            {/* Secondary Cards - depends on auth state */}
            <div className={`grid gap-6 mb-8 ${isLoggedIn && !isGroupAccount ? 'md:grid-cols-2' : 'md:grid-cols-2'}`}>
              {isLoggedIn ? (
                <>
                  {/* Profile Settings Card - only for individual accounts */}
                  {!isGroupAccount && (
                    <Card className="p-6">
                      <div className="flex items-center mb-3">
                        <Settings className="w-6 h-6 text-primary mr-2" />
                        <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">
                          Settings
                        </h3>
                      </div>
                      <p className="text-text-secondary dark:text-slate-400 text-sm mb-4">
                        Configure your display preferences, spectrum settings, and manage API keys.
                      </p>
                      <Link href="/profile">
                        <Button variant="ghost" size="sm">
                          Go to Profile
                        </Button>
                      </Link>
                    </Card>
                  )}

                  {/* Docs Card */}
                  <Card className="p-6">
                    <div className="flex items-center mb-3">
                      <BookOpen className="w-6 h-6 text-primary mr-2" />
                      <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">
                        Documentation
                      </h3>
                    </div>
                    <p className="text-text-secondary dark:text-slate-400 text-sm mb-4">
                      Learn about the data products, inspection workflow, and how to use the archive.
                    </p>
                    <Link href="/docs">
                      <Button variant="ghost" size="sm">
                        View Docs
                      </Button>
                    </Link>
                  </Card>
                </>
              ) : (
                <>
                  {/* Login/Signup Card */}
                  <Card className="p-6">
                    <div className="flex items-center mb-3">
                      <LogIn className="w-6 h-6 text-primary mr-2" />
                      <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">
                        Access Required
                      </h3>
                    </div>
                    <p className="text-text-secondary dark:text-slate-400 text-sm mb-4">
                      CAMPFIRE contains proprietary JWST data. Log in with your credentials or request access to browse the archive.
                    </p>
                    <div className="flex gap-2">
                      <Link href="/login">
                        <Button variant="primary" size="sm">
                          Log In
                        </Button>
                      </Link>
                      <Link href="/request-access">
                        <Button variant="ghost" size="sm">
                          Request Access
                        </Button>
                      </Link>
                    </div>
                  </Card>

                  {/* Getting Started Card */}
                  <Card className="p-6">
                    <div className="flex items-center mb-3">
                      <BookOpen className="w-6 h-6 text-primary mr-2" />
                      <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">
                        Getting Started
                      </h3>
                    </div>
                    <p className="text-text-secondary dark:text-slate-400 text-sm mb-4">
                      New to CAMPFIRE? Learn about the archive, data products, and how to get started.
                    </p>
                    <Link href="/docs/getting-started">
                      <Button variant="ghost" size="sm">
                        Read Guide
                      </Button>
                    </Link>
                  </Card>
                </>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
