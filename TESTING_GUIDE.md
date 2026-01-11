# CAMPFIRE Python API - Testing Guide

## What's Been Built

We've successfully implemented:

1. ✅ Database migration for API keys table
2. ✅ REST API endpoints (`/api/v1/objects` and `/api/v1/spectra`)
3. ✅ API authentication middleware
4. ✅ Python client package
5. ✅ Web UI for API key management
6. ✅ Navigation integration

## Current Status

**Branch:** `feature/python-api`
**Dev Server:** Running on http://localhost:3001

## Next Steps: Testing

### Step 1: Apply Database Migration

First, you need to apply the migration to create the API keys table in Supabase.

1. Go to your Supabase project dashboard
2. Navigate to SQL Editor
3. Copy and paste the contents of `/web/supabase/migrations/010_add_api_keys.sql`
4. Execute the SQL

**Verification:** Check that the `api_keys` table was created successfully.

### Step 2: Test the Web UI

1. **Navigate to the profile page:**
   - Open http://localhost:3001 in your browser
   - Sign in to your account
   - Go to your profile page

2. **Access API Keys management:**
   - Click on the "API Keys" card
   - You should see the API Keys management page at `/profile/api-keys`

3. **Create a new API key:**
   - Click "New API Key"
   - Optionally give it a name (e.g., "Test Key")
   - Click "Create Key"
   - **IMPORTANT:** Copy the key that's displayed (you'll only see it once!)
   - Example key format: `sk_live_a1b2c3d4e5f6...`

4. **Verify the key appears in the list:**
   - After dismissing the creation dialog, verify the key prefix appears in your API keys list
   - Check that it shows "Never used" for last usage

### Step 3: Test the API Endpoints

Use the API key you just created to test the REST endpoints:

```bash
# Set your API key
export CAMPFIRE_API_KEY=sk_live_YOUR_KEY_HERE

# Test the objects endpoint (local dev server)
curl -H "Authorization: Bearer $CAMPFIRE_API_KEY" \
  "http://localhost:3001/api/v1/objects?limit=5"

# Or test against production (after deployment)
curl -H "Authorization: Bearer $CAMPFIRE_API_KEY" \
  "https://campfire.hollisakins.com/api/v1/objects?limit=5"

# Should return JSON with objects
```

**Expected response:**
```json
{
  "data": [...],
  "pagination": {
    "total": 1234,
    "limit": 5,
    "offset": 0
  }
}
```

### Step 4: Install and Test the Python Client

1. **Install the Python package:**
   ```bash
   cd /Users/hba423/simmons/campfire/python
   pip install -e .
   ```

2. **Set your API key:**
   ```bash
   export CAMPFIRE_API_KEY=sk_live_YOUR_KEY_HERE
   ```

3. **Test basic queries:**
   ```python
   from campfire import Campfire

   # Initialize client (for local testing, specify base_url)
   cf = Campfire(base_url="http://localhost:3001/api/v1")

   # For production, you can omit base_url (defaults to campfire.hollisakins.com):
   # cf = Campfire()

   # Query some objects
   results = cf.query_objects(limit=10)

   print(f"Found {len(results)} objects")
   print(results['object_id', 'ra', 'dec', 'redshift'])
   ```

4. **Test cone search:**
   ```python
   # Find objects near coordinates
   nearby = cf.query_objects(
       cone_search=(150.0, 2.5, 5.0)  # RA, Dec, radius (arcsec)
   )

   print(f"Found {len(nearby)} objects nearby")
   ```

5. **Test filters:**
   ```python
   # Query with filters
   high_z = cf.query_objects(
       redshift_range=(3.0, 6.0),
       redshift_quality=[2, 3],
       inspected_only=True,
       limit=50
   )

   print(f"Found {len(high_z)} high-z galaxies")
   ```

### Step 5: Test Spectrum Downloads

```python
from campfire import Campfire

cf = Campfire()

# Query some objects
results = cf.query_objects(limit=5)

# Check if they have spectra
if len(results) > 0 and 'spectra' in results.colnames:
    obj = results[0]
    spectra = obj['spectra']

    if len(spectra) > 0:
        print(f"Object {obj['object_id']} has {len(spectra)} spectra")

        # Try downloading one
        fits_path = spectra[0]['fits_path']
        downloaded = cf.download_spectrum(fits_path, output_path='test_spectrum.fits')
        print(f"Downloaded to: {downloaded}")
```

**Note:** This will only work if:
1. R2 is configured in your environment variables
2. The FITS files exist in R2
3. The signed URL generation is working

### Step 6: Test the Example Notebook

```bash
cd /Users/hba423/simmons/campfire/python/examples
jupyter notebook quickstart.ipynb
```

Run through the cells and verify everything works as expected.

## Troubleshooting

### "Invalid or missing API key"

**Cause:** API key not set or incorrect

**Fix:**
```bash
# Verify your API key is set
echo $CAMPFIRE_API_KEY

# If empty, set it:
export CAMPFIRE_API_KEY=sk_live_YOUR_KEY_HERE
```

### "Failed to fetch API keys" (Web UI)

**Cause:** Database migration not applied

**Fix:** Run the migration SQL in Supabase SQL Editor

### "Module not found: campfire"

**Cause:** Python package not installed

**Fix:**
```bash
cd /Users/hba423/simmons/campfire/python
pip install -e .
```

### "Download service not configured"

**Cause:** R2 environment variables not set

**Fix:** Check that these are set in Vercel or `.env.local`:
```
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET_NAME=campfire-data
```

## Verification Checklist

- [ ] Database migration applied successfully
- [ ] Can create API key in web UI
- [ ] API key appears in keys list
- [ ] Can query `/api/v1/objects` with curl
- [ ] Python client installed successfully
- [ ] Can query objects from Python
- [ ] Can filter by programs, fields, redshift
- [ ] Cone search works
- [ ] Can download spectra (if R2 configured)

## Next Steps After Testing

Once everything is working:

1. **Commit your changes:**
   ```bash
   git add .
   git commit -m "Add Python API with REST endpoints and key management UI"
   ```

2. **Push to remote:**
   ```bash
   git push origin feature/python-api
   ```

3. **Create a pull request** to merge into `develop`

4. **Update documentation:**
   - Add API documentation to CLAUDE.md
   - Update README with Python client info
   - Add user guide for API keys

5. **Deploy to staging:**
   - Merge to `develop` branch
   - Vercel will auto-deploy to preview URL
   - Apply migration to staging Supabase
   - Test on staging environment

6. **Deploy to production:**
   - Merge `develop` to `main`
   - Apply migration to production Supabase
   - Test on production

## Known Limitations

1. No rate limiting enforcement yet (column exists but not used)
2. Program names not mapped to IDs (need to use program IDs)
3. No usage analytics/dashboard
4. API key expiration not enforced
5. No API key rotation mechanism

## Future Enhancements

- Rate limiting enforcement
- API usage dashboard
- Program name → ID mapping
- API key expiration
- Batch download optimization
- WebSocket support for real-time updates
