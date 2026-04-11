'use client';

import { useQuery } from '@tanstack/react-query';
import { getListsOverview, getListBySlug, getMyLists } from '@/lib/actions/lists';

export function useListsOverviewQuery(enabled: boolean = true) {
  return useQuery({
    queryKey: ['listsOverview'],
    queryFn: getListsOverview,
    staleTime: 5 * 60 * 1000,
    enabled,
  });
}

export function useListDetailQuery(slug: string, page: number = 1, enabled: boolean = true) {
  return useQuery({
    queryKey: ['listDetail', slug, page],
    queryFn: () => getListBySlug(slug, page),
    staleTime: 2 * 60 * 1000,
    enabled: enabled && !!slug,
  });
}

export function useMyListsQuery(enabled: boolean = true) {
  return useQuery({
    queryKey: ['myLists'],
    queryFn: getMyLists,
    staleTime: 2 * 60 * 1000,
    enabled,
  });
}
