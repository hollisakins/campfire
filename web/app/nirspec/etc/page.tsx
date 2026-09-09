import type { Metadata } from 'next';
import { EtcPageContent } from '@/components/etc/EtcPageContent';

export const metadata: Metadata = {
  title: 'NIRSpec ETC — CAMPFIRE',
  description:
    'Empirical JWST NIRSpec/MSA exposure-time calculator fitted to the CAMPFIRE archive: continuum S/N, line detectability and 5σ depths for every disperser.',
};

/**
 * /nirspec/etc — deliberately unlisted (not in the navbar); see
 * components/etc/EtcPageContent.tsx. The site is noindex everywhere already.
 */
export default function NirspecEtcPage() {
  return <EtcPageContent />;
}
