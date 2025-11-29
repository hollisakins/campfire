# Quick Start Guide

Follow these steps to get the download worker running:

## 1. Generate JWT Secret (30 seconds)

```bash
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
```

Copy the output.

## 2. Configure Next.js (1 minute)

Edit `web/.env.local`:

```bash
# Add these lines
WORKER_JWT_SECRET=<paste_your_secret_here>
NEXT_PUBLIC_WORKER_DOWNLOAD_URL=https://download.campfire.hollisakins.com
```

## 3. Set Worker Secret (1 minute)

```bash
cd workers/download-worker
wrangler secret put JWT_SECRET
# Paste the same secret from step 1
```

## 4. Install Worker Dependencies (1 minute)

```bash
npm install
```

## 5. Configure DNS in Cloudflare (2 minutes)

1. Go to https://dash.cloudflare.com/
2. Select `hollisakins.com` domain
3. DNS → Add record:
   - Type: `AAAA`
   - Name: `download.campfire`
   - IPv6: `100::`
   - Proxy: ✅ Enabled (orange cloud)
4. Save

## 6. Deploy Worker (1 minute)

```bash
wrangler deploy
```

## 7. Test! (30 seconds)

1. Go to http://localhost:3000/spectra
2. Click **"FITS ZIP"** button
3. Download should start!

---

**Total time:** ~5-7 minutes

**Need help?** See `DEPLOYMENT_GUIDE.md` for detailed instructions.
