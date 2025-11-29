# Cloudflare Worker Deployment Guide

This guide will walk you through deploying the CAMPFIRE download worker to Cloudflare.

## Prerequisites

- Cloudflare account (you have account ID: `2d136e3d61aca8a4ae08a2ea760f6d23`)
- Wrangler CLI installed (you have version 4.28.0)
- R2 bucket named `campfire` with FITS files
- Domain: `download.campfire.hollisakins.com`

## Step 1: Install Worker Dependencies

Navigate to the worker directory and install packages:

```bash
cd workers/download-worker
npm install
```

This will install:
- `fflate` - For ZIP streaming
- `@cloudflare/workers-types` - TypeScript types
- `wrangler` - Cloudflare deployment tool

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
NEXT_PUBLIC_WORKER_DOWNLOAD_URL=https://download.campfire.hollisakins.com
```

**Important:** Use the SAME secret value from Step 2!

## Step 5: Configure DNS in Cloudflare

Before deploying, set up the subdomain in Cloudflare:

1. Go to Cloudflare Dashboard: https://dash.cloudflare.com/
2. Select your domain: `hollisakins.com`
3. Go to **DNS** > **Records**
4. Click **Add record**
5. Configure:
   - **Type:** `AAAA`
   - **Name:** `download.campfire` (will create `download.campfire.hollisakins.com`)
   - **IPv6 address:** `100::` (Cloudflare Workers placeholder)
   - **Proxy status:** ✅ Proxied (orange cloud)
6. Click **Save**

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
  https://download.campfire.hollisakins.com
Current Deployment ID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

## Step 8: Verify R2 Bucket Binding

Check that the worker can access your R2 bucket:

```bash
cd workers/download-worker
wrangler whoami
```

Then verify the bucket is bound:
```bash
wrangler r2 bucket list
```

You should see `campfire` in the list.

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

### Error: "File not found in R2"

**Cause:** FITS file path in database doesn't match R2 object key

**Fix:**
1. Check a sample FITS path in your database
2. Verify it exists in R2: `wrangler r2 object get campfire <path>`
3. Ensure paths match exactly (case-sensitive!)

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
- [ ] DNS record created for `download.campfire.hollisakins.com`
- [ ] Worker deployed successfully
- [ ] R2 bucket binding working
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
