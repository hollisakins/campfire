"""Quick test script to debug the API connection."""

import os
from campfire import Campfire

# Get API key from environment
api_key = os.environ.get('CAMPFIRE_API_KEY')
print(f"API Key: {api_key[:20]}..." if api_key else "No API key set!")

# Create client pointing to localhost
cf = Campfire(
    api_key=api_key,
    base_url="http://localhost:3001/api/v1"  # Point to local dev server
)

print(f"Base URL: {cf.base_url}")

# Test query
print("\nTesting query_objects...")
try:
    results = cf.query_objects(limit=5)
    print(f"Success! Found {len(results)} objects")
    if len(results) > 0:
        print("\nFirst object:")
        print(f"  ID: {results[0]['object_id']}")
        print(f"  RA: {results[0]['ra']}")
        print(f"  Dec: {results[0]['dec']}")
except Exception as e:
    print(f"Error: {type(e).__name__}: {e}")

    # Try to get more details
    import requests
    url = f"{cf.base_url}/objects"
    params = {"limit": 5}
    headers = {"Authorization": f"Bearer {api_key}"}

    print(f"\nDirect request to: {url}")
    response = requests.get(url, params=params, headers=headers)
    print(f"Status code: {response.status_code}")
    print(f"Response headers: {dict(response.headers)}")
    print(f"Response text (first 200 chars): {response.text[:200]}")
