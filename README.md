# Cursor

Cursor API key + Cursor coding agent for Settings → LLMs. Install from Store → Gateways, then enable to show Cursor under Providers.

Desktop plugin for [UEFN-Ducky](https://github.com/UEFN-Ducky/UEFN-Ducky) (`cursor`).
Install or update from **Settings → Store** in the app — do not install from a zip by hand.

## Build

```bash
py scripts/build_zip.py
```

Writes `deploy/cursor-1.0.15.ducky-plugin.zip` (scripts/ and deploy/ are not packed).

## Secrets

Never commit tokens or keys. The app stores `cursor` locally (DPAPI), not in this package.
