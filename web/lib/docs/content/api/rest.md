# REST API

For direct HTTP access without the Python client. All endpoints are under `https://campfire.hollisakins.com/api/v1/`.

## Authentication

All requests require an `Authorization` header:

```bash
# Using API key
curl -H "Authorization: Bearer sk_your_api_key" \
  https://campfire.hollisakins.com/api/v1/objects

# Using JWT access token (from device flow)
curl -H "Authorization: Bearer eyJ..." \
  https://campfire.hollisakins.com/api/v1/objects
```

---

## Data Endpoints

### GET /objects

Query objects with filters.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `programs` | string | Comma-separated program slugs |
| `fields` | string | Comma-separated field names |
| `gratings` | string | Comma-separated grating types |
| `observations` | string | Comma-separated observation names |
| `redshift_min` | float | Minimum redshift |
| `redshift_max` | float | Maximum redshift |
| `redshift_quality` | string | Comma-separated quality codes |
| `max_snr_min` | float | Minimum max SNR |
| `max_snr_max` | float | Maximum max SNR |
| `spectral_features_include_any` | int | Match any of these flags |
| `spectral_features_include_all` | int | Must have all flags |
| `spectral_features_exclude` | int | Must not have any flags |
| `object_flags_include_any` | int | Same pattern |
| `object_flags_include_all` | int | |
| `object_flags_exclude` | int | |
| `dq_flags_include_any` | int | Same pattern |
| `dq_flags_include_all` | int | |
| `dq_flags_exclude` | int | |
| `inspected_only` | boolean | Filter to inspected objects |
| `search` | string | Text search on object_id |
| `ra` | float | RA for cone search (degrees) |
| `dec` | float | Dec for cone search (degrees) |
| `radius` | float | Search radius (arcsec) |
| `limit` | int | Max results (default: 1000) |
| `offset` | int | Pagination offset |
| `sort` | string | Sort column |
| `sort_dir` | string | 'asc' or 'desc' |

**Response:**

```json
{
  "data": [
    {
      "object_id": "ember_uds_p4_123456",
      "ra": 34.1234,
      "dec": -5.4567,
      "redshift": 2.345,
      "redshift_quality": 3,
      "field": "UDS",
      "spectra": [
        {"grating": "PRISM", "fits_path": "spectra/ember_uds_p4/..."}
      ]
    }
  ],
  "pagination": {"total": 1500, "limit": 1000, "offset": 0}
}
```

### GET /spectra

Get a signed URL for downloading a FITS file.

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | FITS file path (required) |

**Response:** `{"url": "https://..."}`

### GET /spectrum

Get spectrum JSON data for plotting.

| Parameter | Type | Description |
|-----------|------|-------------|
| `object_id` | string | Object ID |
| `grating` | string | Grating type |

**Response:**

```json
{
  "wave": [1.0, 1.1, ...],
  "fnu": [0.5, 0.6, ...],
  "fnu_err": [0.1, 0.1, ...],
  "snr_2d": [[...], [...]],
  "n_spatial": 10,
  "n_wave": 500,
  "profile": [...],
  "profile_fit": [...],
  "profile_pix": [...]
}
```

### GET /redshift-fit

Get redshift fitting results.

| Parameter | Type | Description |
|-----------|------|-------------|
| `object_id` | string | Object ID |
| `grating` | string | Grating type |

**Response:**

```json
{
  "redshift": 2.345,
  "chi2_min": 1.23,
  "confidence": 95.5,
  "z_grid": [0.0, 0.1, ...],
  "chi2_grid": [100, 95, ...],
  "model_wave": [...],
  "model_fnu": [...]
}
```

### GET /metadata

Get available filter options.

**Response:**

```json
{
  "programs": [
    {"slug": "ember-uds", "program_name": "EMBER-UDS", "pi_name": "...", "is_public": false}
  ],
  "fields": ["COSMOS", "UDS"],
  "gratings": ["PRISM", "G395M"],
  "observations": ["ember_uds_p4"]
}
```

### GET /observations

List observations with stats.

**Response:**

```json
{
  "observations": [
    {
      "observation": "ember_uds_p4",
      "program_name": "EMBER-UDS",
      "field": "UDS",
      "object_count": 450,
      "spectrum_count": 1350,
      "total_size_bytes": 2147483648
    }
  ]
}
```

---

## Auth Endpoints

### GET /auth/whoami

Get current user info. **Response:** `{"user_id": "uuid", "email": "...", "full_name": "..."}`

### POST /auth/device

Initiate device flow authorization (for CLI).

**Response:**

```json
{
  "device_code": "...",
  "user_code": "WDJB-MJPQ",
  "verification_uri": "https://campfire.hollisakins.com/cli-auth",
  "verification_uri_complete": "https://campfire.hollisakins.com/cli-auth?code=WDJB-MJPQ",
  "expires_in": 900,
  "interval": 5
}
```

### POST /auth/device/token

Poll for tokens after user authorization.

**Request:** `{"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": "..."}`

**Response (success):** `{"access_token": "...", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "..."}`

**Response (pending):** `{"error": "authorization_pending"}`

### POST /auth/refresh

Refresh an access token.

**Request:** `{"grant_type": "refresh_token", "refresh_token": "..."}`

**Response:** `{"access_token": "...", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "..."}`

---

## Error Codes

| Code | Description |
|------|-------------|
| 200 | Success |
| 400 | Invalid parameters |
| 401 | Invalid/missing authentication |
| 403 | No access to resource |
| 404 | Not found |
| 429 | Rate limited |
| 500 | Server error |

## Rate Limits

- **Standard:** 100 requests/minute
- **Burst:** Up to 10 concurrent requests
