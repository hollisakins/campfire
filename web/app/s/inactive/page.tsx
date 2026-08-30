import type { Metadata } from 'next';
import { Logo } from '@/components/brand/Logo';

// The dead-link page (docs/design-public-mirror.md §7.1). Reached when a token
// is unknown, revoked, or expired -- and, since links never expire by default,
// in practice this means "an admin revoked it".
//
// It says nothing about what the link used to point at. A revoked link must not
// confirm the scope it once exposed, so there is no label, no field name, and
// no distinction in the copy between "revoked" and "never existed". The reason
// is read only to keep the wording honest for the expiry case.
//
// No login form and no "request access" flow either: whoever holds this URL got
// it from a person, and that person is who they should go back to.
export const metadata: Metadata = {
  title: 'Link no longer active - CAMPFIRE',
  robots: { index: false, follow: false },
};

export default async function InactiveSharePage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string }>;
}) {
  const { reason } = await searchParams;
  const expired = reason === 'expired';

  return (
    <div className="flex min-h-[60vh] items-center justify-center px-4">
      <div className="w-full max-w-md text-center">
        <div className="mb-8 flex justify-center">
          <Logo />
        </div>

        <h1 className="text-2xl font-semibold text-text-primary">
          This link is no longer active.
        </h1>

        <p className="mt-4 text-sm text-text-secondary">
          {expired
            ? 'This shared view has expired.'
            : 'This shared view has been turned off, or the link is incorrect.'}
        </p>

        <p className="mt-6 text-sm text-text-secondary">
          If you were expecting to see data here, contact the person who shared
          the link with you.
        </p>
      </div>
    </div>
  );
}
