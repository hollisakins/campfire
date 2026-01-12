# API Reference

CAMPFIRE provides a REST API for programmatic access to spectroscopic data.

> **Note:** The API is currently experimental. Contact the team for access.

## Authentication

*Documentation coming soon!*

API access requires an API key, which can be generated from your [Profile](/profile) page.

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
  https://campfire.vercel.app/api/v1/spectra
```

## Endpoints

*Documentation coming soon!*

### List Spectra

```
GET /api/v1/spectra
```

Query parameters:

| Parameter | Type | Description |
|-----------|------|-------------|
| `program` | integer | Filter by program ID |
| `field` | string | Filter by field name |
| `limit` | integer | Maximum results (default: 100) |
| `offset` | integer | Pagination offset |

### Get Spectrum

```
GET /api/v1/spectra/{object_id}
```

Returns metadata and download URLs for a specific object.

### Download FITS

```
GET /api/v1/spectra/{object_id}/fits/{grating}
```

Downloads the FITS file for a specific grating.

## Response Format

*Documentation coming soon!*

All responses are JSON:

```json
{
  "data": [...],
  "pagination": {
    "total": 1000,
    "limit": 100,
    "offset": 0
  }
}
```

## Rate Limits

*Documentation coming soon!*

Lorem ipsum dolor sit amet...

## Error Handling

*Documentation coming soon!*

| Status | Description |
|--------|-------------|
| 400 | Bad request |
| 401 | Unauthorized |
| 404 | Not found |
| 429 | Rate limited |
| 500 | Server error |

## Python Client

*Documentation coming soon!*

```python
# Example usage (coming soon)
from campfire import Client

client = Client(api_key="YOUR_API_KEY")
spectra = client.search(program=1234, field="COSMOS")

for spec in spectra:
    print(spec.object_id, spec.redshift)
```

## Examples

*Documentation coming soon!*

### Bulk Download Script

```python
# Coming soon
```

### Catalog Query

```python
# Coming soon
```
