import React from 'react';
import { Navigation } from '@/components/layout/Navigation';
import { DrBanner } from '@/components/dr/DrBanner';
import { DrProvider } from '@/lib/dr/client';
import { requestDrTag } from '@/lib/dr/server';

/**
 * The per-navigation shell (P6c-1). A template re-mounts on every navigation
 * and its RSC request goes through middleware, so `requestDrTag()` here is
 * the stamp of the URL being navigated TO — a layout would keep the first
 * page's value for the session (Codex on #614). The navigation, the release
 * banner and the DrProvider the client tree reads all take the tag from this
 * one read.
 */
export default async function RootTemplate({ children }: { children: React.ReactNode }) {
  const drTag = await requestDrTag();
  return (
    <DrProvider tag={drTag}>
      <Navigation drTag={drTag} />
      <DrBanner />
      <main className="flex-1">{children}</main>
    </DrProvider>
  );
}
