import 'server-only';

import { headers } from 'next/headers';
import { NextResponse } from 'next/server';
import { DR_HEADER, parseDrTag } from './tag';

/**
 * The release this request browses, from the header middleware stamps for a
 * `?dr=` URL (never from the client directly: middleware strips an incoming
 * `x-campfire-dr`). Null for the live catalog, and in any context without
 * request headers.
 */
export async function requestDrTag(): Promise<string | null> {
  try {
    const h = await headers();
    return parseDrTag(h.get(DR_HEADER));
  } catch {
    return null;
  }
}

export class ReadOnlyReleaseError extends Error {
  readonly status = 409;
  constructor() {
    super('read-only under a data release');
    this.name = 'ReadOnlyReleaseError';
  }
}

/**
 * Mutations and live-file paths refuse to run under a release (P6c-1): a
 * `?dr=` page must never write the live catalog or hand out live files.
 * Route handlers: `const ro = await readOnlyResponse(); if (ro) return ro;`.
 * Server actions: `await assertLiveRequest()` inside their try, so the
 * throw becomes the action's error shape.
 */
export async function assertLiveRequest(): Promise<void> {
  if (await requestDrTag()) throw new ReadOnlyReleaseError();
}

/** The 409 a route handler returns under a release, else null. */
export async function readOnlyResponse(): Promise<NextResponse | null> {
  if (!(await requestDrTag())) return null;
  return NextResponse.json({ error: 'read-only under a data release' }, { status: 409 });
}
