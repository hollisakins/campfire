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
NEXT_PUBLIC_WORKER_DOWNLOAD_URL=https://campfire-download.hollisakins.com
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

## 5. Custom Domain (0 minutes)

Nothing to do — `wrangler.toml` pins `campfire-download.hollisakins.com` as a
custom domain; deploying attaches it and Cloudflare manages DNS + certificate.
Do not create a DNS record for it by hand.

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
