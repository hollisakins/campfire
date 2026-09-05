---
title: "Python client 0.5.0: cursor pagination for catalog walks"
date: 2026-09-05
category: client
summary: "The `/api/v1` list endpoints and the Python client now page with an opaque cursor, so walking the full catalog costs the same per page at any depth. Clients older than 0.5.0 must upgrade to sync."
---

`cf.iter_objects()` and `cf.iter_spectra()` now follow a server-issued cursor
instead of stepping through numeric offsets. Each page is fetched at a flat
cost, the row count is requested only once, and the walk ends without a
trailing empty request — a full-catalog `campfire query objects` drops from
tens of seconds to a few. `cf.query_objects()` / `cf.query_spectra()` expose
the same mechanism for manual paging through `table.meta['pagination']
['next_cursor']`, and the REST endpoints accept it as `cursor=` (see the
[REST reference](/docs/api/rest#pagination)). `offset=` keeps working for one
release and is answered with a `Deprecation` header.

The `/api/v1/sync/*` endpoints behind `campfire sync` no longer accept offset
pagination at all: the keyset walk they have used since July is now the only
one, and the extended database timeout that existed for offset clients is
gone. **Clients older than 0.5.0 will fail on the second page of a sync with an
upgrade message.** Update with `git pull && python3 install.py` from a repo
checkout, or reinstall from git with pip as described in the
[getting-started guide](/docs/api/getting-started).
