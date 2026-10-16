#!/usr/bin/env bash
# End-to-end check that PostgREST runs dr_request_context as its pre-request
# hook (P6b-2): signs in as the seed user and calls dr_browse_tag through the
# Data API. With `X-Campfire-DR: DR-seed` the body must be "DR-seed"; with an
# unknown tag the request must be a 404; without the header the tag is null.
# A missing or unregistered hook would answer null / 200 to every call, which
# no SQL guard can see.
#   usage: supabase/tests/hook_e2e.sh <API_URL> <ANON_KEY>
set -euo pipefail
API="${1:?API_URL}"; ANON="${2:?ANON_KEY}"
tok=$(curl -sS -X POST "$API/auth/v1/token?grant_type=password" \
        -H "apikey: $ANON" -H "Content-Type: application/json" \
        -d '{"email":"user@campfire.dev","password":"password123"}' \
      | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
tmp=$(mktemp)
call() {  # <tag or empty> -> prints the HTTP code, body in $tmp
  if [ -n "$1" ]; then
    curl -sS -o "$tmp" -w '%{http_code}' -X POST "$API/rest/v1/rpc/dr_browse_tag" \
      -H "apikey: $ANON" -H "Authorization: Bearer $tok" -H "Content-Type: application/json" \
      -H "X-Campfire-DR: $1" -d '{}'
  else
    curl -sS -o "$tmp" -w '%{http_code}' -X POST "$API/rest/v1/rpc/dr_browse_tag" \
      -H "apikey: $ANON" -H "Authorization: Bearer $tok" -H "Content-Type: application/json" -d '{}'
  fi
}
fail=0
code=$(call DR-seed); body=$(cat "$tmp"); echo "DR-seed   -> HTTP $code body $body"
if [ "$code" != "200" ] || [ "$body" != '"DR-seed"' ]; then echo "FAIL: the hook did not select DR-seed"; fail=1; fi
code=$(call DR-nope); body=$(cat "$tmp"); echo "DR-nope   -> HTTP $code body $body"
if [ "$code" != "404" ]; then echo "FAIL: an unknown tag should be a 404"; fail=1; fi
code=$(call ""); body=$(cat "$tmp"); echo "no header -> HTTP $code body $body"
if [ "$code" != "200" ] || [ "$body" != "null" ]; then echo "FAIL: without the header the tag should be null"; fail=1; fi
rm -f "$tmp"
if [ "$fail" -ne 0 ]; then exit 1; fi
echo "hook_e2e: OK (pre-request hook wired)"
