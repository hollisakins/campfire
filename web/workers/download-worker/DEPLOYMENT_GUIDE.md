# Cloudflare Worker Deployment Guide

This guide will walk you through deploying the CAMPFIRE download worker to Cloudflare.

## Prerequisites

- Cloudflare account (you have account ID: `2d136e3d61aca8a4ae08a2ea760f6d23`)
- Wrangler CLI installed
- FITS objects on R2 (`campfire`) and/or OSN — the Worker fetches presigned URLs,
  so it needs **no** bucket binding and holds no object-store credentials
- Domain: `campfire-download.hollisakins.com` (custom domain pinned in
  `wrangler.toml`; a first-level subdomain so the free Universal SSL wildcard
  covers it — deeper names like `download.campfire.…` get no certificate)
- Free Workers plan is sufficient (one subrequest, ~0 CPU per request)

## Step 1: Install Worker Dependencies

Navigate to the worker directory and install packages:

```bash
cd workers/download-worker
npm install
```

This will install:
- `@cloudflare/workers-types` - TypeScript types
- `wrangler` - Cloudflare deployment tool

The Worker has no runtime dependencies — it holds no credentials and does not
zip; the browser zips client-side with `fflate`, which lives in the web app.

## Step 2: Generate JWT Secret

Generate a secure random secret for JWT signing (this will be shared between Next.js and the Worker):

```bash
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
```

**Copy this value!** You'll need it in steps 3 and 4.

Example output:
```
a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2
```

## Step 3: Set Worker Secret

Set the JWT secret in Cloudflare (replace `YOUR_SECRET_HERE` with the value from Step 2):

```bash
cd workers/download-worker
wrangler secret put JWT_SECRET
# When prompted, paste your secret and press Enter
```

## Step 4: Add Secret to Next.js Environment

Add the same JWT secret to your Next.js `.env.local` file:

```bash
cd ../../  # Back to web directory
```

Add to `.env.local`:
```bash
# Cloudflare Worker Configuration
WORKER_JWT_SECRET=YOUR_SECRET_HERE
NEXT_PUBLIC_WORKER_DOWNLOAD_URL=https://campfire-download.hollisakins.com
```

**Important:** Use the SAME secret value from Step 2!

## Step 5: Custom Domain

Nothing manual: `wrangler.toml` pins the custom domain
(`campfire-download.hollisakins.com`, `custom_domain = true`), so `wrangler
deploy` attaches it and Cloudflare manages the DNS record and certificate.
Do NOT create a DNS record for it by hand — a manually-created record
conflicts with the custom-domain attachment (an earlier manual `AAAA 100::`
record for `download.campfire.…` is how the old, never-working domain came
about). The `campfire-download.<account>.workers.dev` hostname keeps serving
the same Worker, so builds baked with the old URL don't break.

## Step 6: Test Worker Locally

Before deploying to production, test the worker locally:

```bash
cd workers/download-worker
wrangler dev
```

This will start a local development server (usually on `http://localhost:8787`).

You can test with a sample JWT token (we'll skip this for now, but it's available if needed).

Press `Ctrl+C` to stop the local server when done testing.

## Step 7: Deploy to Cloudflare

Deploy the worker:

```bash
cd workers/download-worker
wrangler deploy
```

You should see output like:
```
Total Upload: XX.XX KiB / gzip: XX.XX KiB
Uploaded campfire-download (X.XX sec)
Published campfire-download (X.XX sec)
  https://campfire-download.hollisakins.com
Current Deployment ID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

## Step 8: Smoke-Test the Proxy

The Worker has no R2 binding — it proxies presigned URLs. Confirm it's up and
rejecting unsigned requests:

```bash
curl -i 'https://campfire-download.hollisakins.com/proxy'
# expect: HTTP/1.1 400  Missing url or sig parameter
```

Also confirm `ALLOWED_FETCH_HOSTS` in `wrangler.toml` lists both the OSN host and
your R2 account host, so both backends are fetchable. A real download is exercised
end-to-end from the web app in Step 9.

## Step 9: Test from Next.js

1. Make sure Next.js dev server is running:
   ```bash
   cd ../../  # Back to web directory
   npm run dev
   ```

2. Navigate to: http://localhost:3000/spectra

3. Apply some filters (or use all results)

4. Click **"FITS ZIP"** button

5. You should get a ZIP file download with FITS files!

## Step 10: Monitor Worker Logs

You can monitor worker logs in real-time:

```bash
cd workers/download-worker
wrangler tail
```

This will show:
- Requests received
- Errors (if any)
- Performance metrics

Press `Ctrl+C` to stop tailing logs.

## Troubleshooting

### Error: "Token expired" or "Invalid token"

**Cause:** JWT secret mismatch between Next.js and Worker

**Fix:**
1. Verify secrets match in both places
2. Regenerate and update both if needed

### A file fails with 403 "Target not allowed"

**Cause:** The presigned URL's host is not in `ALLOWED_FETCH_HOSTS` (e.g. an object
still on R2 while the var lists only OSN, or a mismatched R2 account host).

**Fix:**
1. Confirm the presign host is `uaz1.osn.mghpcc.org` (OSN) or
   `<accountid>.r2.cloudflarestorage.com` (R2).
2. Add the missing host to `ALLOWED_FETCH_HOSTS` in `wrangler.toml` and redeploy.

### A file fails with 403 "Invalid signature"

**Cause:** `JWT_SECRET` (Worker) and `WORKER_JWT_SECRET` (Next.js) differ.

**Fix:** Set both to the same value and redeploy.

### Error: "CORS error" in browser

**Cause:** Origin not in ALLOWED_ORIGINS

**Fix:**
1. Edit `wrangler.toml`
2. Add your domain to `ALLOWED_ORIGINS`
3. Redeploy: `wrangler deploy`

### Download is slow or timing out

**Cause:** Too many files or files too large

**Fix:**
1. Reduce number of objects (current limit: 200)
2. Check individual FITS file sizes
3. Consider implementing Durable Objects for large downloads (future enhancement)

### Worker deployment fails

**Cause:** Authentication issue

**Fix:**
```bash
wrangler login
```

Follow the browser authentication flow, then try deploying again.

## Updating the Worker

When you make changes to the worker code:

```bash
cd workers/download-worker
wrangler deploy
```

Changes take effect immediately (no need to restart).

## Production Checklist

Before going to production:

- [ ] JWT secret generated and set in both Next.js and Worker
- [ ] Custom domain serving (`curl -i https://campfire-download.hollisakins.com/proxy` → 400)
- [ ] Worker deployed successfully
- [ ] Proxy smoke test passes (`GET /proxy` → 400) and `ALLOWED_FETCH_HOSTS` lists OSN + R2
- [ ] Test download with small dataset (1-5 objects)
- [ ] Test download with larger dataset (50-100 objects)
- [ ] Test download with max limit (200 objects)
- [ ] Verify ZIP file integrity (files open correctly)
- [ ] Test from production domain (not just localhost)
- [ ] Monitor logs for first few days

## Monitoring & Maintenance

### View Worker Analytics

Go to Cloudflare Dashboard:
1. **Workers & Pages** > **campfire-download**
2. View **Metrics**:
   - Requests per day
   - Error rate
   - CPU time
   - Data transfer

### Update Wrangler (Optional)

You mentioned you have an update available:

```bash
npm install -g wrangler@latest
```

Current version: 4.28.0 (latest is 3.94.0+ or 4.x depending on your setup).

## Cost Estimates

With Cloudflare Workers:
- **First 100,000 requests/day:** FREE
- **R2 storage:** First 10 GB FREE
- **R2 egress to Workers:** FREE (no bandwidth charges within Cloudflare network!)

Expected costs for moderate usage:
- ~100-500 downloads/day: **$0/month**
- ~1000-5000 downloads/day: **~$0.50-$2/month**

## Support

If you run into issues:

1. **Check worker logs:** `wrangler tail`
2. **Check Next.js logs:** Terminal running `npm run dev`
3. **Browser console:** Look for errors (F12 → Console)
4. **Cloudflare Dashboard:** Check Worker status and errors

## Next Steps

After deployment is working:

1. **Test with production URL** (once Next.js is deployed to Vercel)
2. **Monitor usage** for first week
3. **Gather user feedback** on download experience
4. **Consider Durable Objects** if users need >200 objects regularly

---

**Ready to deploy?** Start with Step 1 and work through each step. Let me know if you run into any issues!
