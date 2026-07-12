import { NircamFieldPageContent } from './NircamFieldPageContent';

interface NircamFieldPageProps {
  params: Promise<{ field: string }>;
}

export default async function NircamFieldPage({ params }: NircamFieldPageProps) {
  const { field } = await params;
  return <NircamFieldPageContent field={decodeURIComponent(field)} />;
}
