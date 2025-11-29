import Link from 'next/link';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Flame, Camera, Sparkles } from 'lucide-react';

export default function Home() {
  return (
    <div className="container mx-auto px-4 py-12">
      <div className="max-w-4xl mx-auto">
        {/* Hero Section */}
        <div className="text-center mb-12">
          <div className="flex items-center justify-center mb-4">
            <Flame className="w-16 h-16 text-primary" />
          </div>
          <h1 className="text-5xl font-bold text-text-primary mb-4">
            CAMPFIRE
          </h1>
          <p className="text-xl text-text-secondary mb-2">
            COSMOS Archive of MultiPle-Field Internal Reductions & Extractions
          </p>
          <p className="text-lg text-text-secondary">
            Internal archive for JWST NIRCam imaging and NIRSpec spectroscopy data
          </p>
        </div>

        {/* Data Access Cards */}
        <div className="grid md:grid-cols-2 gap-6 mb-8">
          {/* NIRCam Card */}
          <Card className="p-8">
            <div className="flex items-center mb-4">
              <Camera className="w-8 h-8 text-primary mr-3" />
              <h2 className="text-2xl font-bold text-text-primary">NIRCam</h2>
            </div>
            <p className="text-text-secondary mb-6">
              Browse and download reduced NIRCam imaging mosaics from multiple survey fields.
            </p>
            <Link href="/nircam">
              <Button variant="secondary" className="w-full">
                Browse NIRCam Data
              </Button>
            </Link>
          </Card>

          {/* NIRSpec Card */}
          <Card className="p-8">
            <div className="flex items-center mb-4">
              <Sparkles className="w-8 h-8 text-primary mr-3" />
              <h2 className="text-2xl font-bold text-text-primary">NIRSpec</h2>
            </div>
            <p className="text-text-secondary mb-6">
              Explore reduced spectroscopy data with interactive tools and quality assessments.
            </p>
            <Link href="/spectra">
              <Button variant="primary" className="w-full">
                Browse NIRSpec Spectra
              </Button>
            </Link>
          </Card>
        </div>

        {/* Info Section */}
        <Card className="p-6">
          <h3 className="text-lg font-semibold text-text-primary mb-3">
            Access & Authentication
          </h3>
          <p className="text-text-secondary text-sm">
            CAMPFIRE contains proprietary JWST data. Access is restricted to team members with
            valid credentials. If you need access, please contact the program PI.
          </p>
        </Card>
      </div>
    </div>
  );
}
