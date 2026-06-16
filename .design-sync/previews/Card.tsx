import { Card } from 'campfire-web';

export function Basic() {
  return (
    <Card className="p-6 max-w-md">
      <h3 className="text-lg font-semibold text-text-primary">COSMOS-3142871</h3>
      <p className="text-sm text-text-secondary mt-1">
        NIRSpec PRISM · z = 4.812 · 3 spectra
      </p>
      <p className="text-sm text-text-primary mt-4 leading-relaxed">
        A high-redshift galaxy observed across multiple gratings. Continuum and
        emission lines are well detected in the prism reduction.
      </p>
    </Card>
  );
}

export function Hoverable() {
  return (
    <Card hover className="p-5 max-w-md">
      <div className="text-sm font-medium text-text-primary">Program 6585</div>
      <div className="text-xs text-text-secondary mt-1">
        Hover to highlight — click-through target row
      </div>
    </Card>
  );
}
