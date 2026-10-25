---
title: "CAMPFIRE is now campfire-jwst.org"
date: 2026-10-26
category: client
summary: "The portal and API have moved to campfire-jwst.org. Old links redirect; you will be asked to sign in once."
---

The portal lives at [campfire-jwst.org](https://campfire-jwst.org) and the API
at `api.campfire-jwst.org`. Every `campfire.hollisakins.com` link redirects to
the same page on the new domain, so bookmarks and URLs in papers keep working.

What changes for you:

- **Sign in once more.** Sessions are per domain; your account and data are
  unchanged.
- **Python client.** New installs default to `https://campfire-jwst.org/api/v1`.
  Existing installs keep working through the redirect; `git pull && python3
  install.py` (or a pip reinstall from git) picks up the new default.
- **Download scripts** generated before the move keep working until their
  tokens expire.
