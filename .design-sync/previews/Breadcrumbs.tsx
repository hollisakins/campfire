import { Breadcrumbs } from 'campfire-web';

export function Default() {
  return (
    <Breadcrumbs
      items={[
        { label: 'Archive', href: '/' },
        { label: 'COSMOS', href: '/fields/cosmos' },
        { label: 'COSMOS-3142871' },
      ]}
    />
  );
}

export function TwoLevel() {
  return (
    <Breadcrumbs
      items={[
        { label: 'Programs', href: '/programs' },
        { label: 'Program 6585' },
      ]}
    />
  );
}
