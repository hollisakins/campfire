import { Database, Telescope, Code, Package, type LucideIcon } from 'lucide-react';
import type { UpdateCategory } from '@/lib/updates/types';

// Category → label / icon / text-color. Tailwind class strings live here (in
// `components/`, which Tailwind scans) rather than in `lib/`.
const META: Record<
  UpdateCategory,
  { label: string; icon: LucideIcon; className: string }
> = {
  data: { label: 'Data', icon: Database, className: 'text-info' },
  pipeline: { label: 'Pipeline', icon: Telescope, className: 'text-primary-text' },
  client: { label: 'Client', icon: Code, className: 'text-success' },
  release: { label: 'Data Release', icon: Package, className: 'text-warning' },
};

export function CategoryChip({ category }: { category: UpdateCategory }) {
  const meta = META[category] ?? META.data;
  const Icon = meta.icon;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border border-border bg-card px-2 py-0.5 text-xs font-medium ${meta.className}`}
    >
      <Icon className="w-3 h-3" />
      {meta.label}
    </span>
  );
}
