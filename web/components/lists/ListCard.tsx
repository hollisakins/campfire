'use client';

import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { ListBadge } from './ListBadge';
import { Hash, User } from 'lucide-react';
import { getContrastColor } from '@/lib/flags';
import type { ObjectListOverview } from '@/lib/types';

interface ListCardProps {
  list: ObjectListOverview;
}

export function ListCard({ list }: ListCardProps) {
  return (
    <Link href={`/nirspec/tags/${list.slug}`}>
      <Card hover className="p-5 h-full">
        <div className="flex items-start justify-between mb-2">
          <div className="flex items-center gap-2">
            {list.icon && (
              <span className="text-lg">{list.icon}</span>
            )}
            {list.color && !list.icon && (
              <span
                className="w-4 h-4 rounded-full flex-shrink-0"
                style={{ backgroundColor: list.color }}
              />
            )}
            <div>
              <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">
                {list.name}
              </h3>
              <span className="text-xs font-mono text-text-secondary dark:text-slate-500">#{list.slug}</span>
            </div>
          </div>
          <ListBadge visibility={list.visibility} isSystem={list.is_system} />
        </div>

        {list.description && (
          <p className="text-sm text-text-secondary dark:text-slate-400 mb-3 line-clamp-2">
            {list.description}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2 mt-auto">
          <span className="inline-flex items-center gap-1 px-2.5 py-1 bg-primary/10 text-primary rounded-full text-xs font-medium">
            <Hash className="w-3 h-3" />
            {list.member_count.toLocaleString()} {list.member_count === 1 ? 'object' : 'objects'}
          </span>
          {list.creator_name && (
            <span className="inline-flex items-center gap-1 text-xs text-text-secondary dark:text-slate-500">
              <User className="w-3 h-3" />
              {list.creator_name}
            </span>
          )}
        </div>
      </Card>
    </Link>
  );
}
