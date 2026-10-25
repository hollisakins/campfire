/**
 * The Supabase API url the SERVER talks to.
 *
 * `NEXT_PUBLIC_SUPABASE_URL` is inlined at build time into the client and the
 * server bundles alike, so an env file cannot change where server code
 * connects. `SUPABASE_URL` is read at runtime and lets the self-hosted stack
 * point the web container at Kong over the Docker network
 * (`http://kong:8000`) instead of back out through Cloudflare, whose 100 s
 * edge timeout is shorter than the 120 s the sync RPCs may run (ops/README.md,
 * Phase 2). Browser code keeps using the public url it was built with.
 *
 * No `server-only` import: middleware runs in the edge runtime, which still
 * reads runtime env but rejects that marker.
 */
export function serverSupabaseUrl(): string {
  return process.env.SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL!;
}
