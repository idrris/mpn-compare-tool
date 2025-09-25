
# MPN Attribute Compare (Localhost)

A tiny Flask app that lets you enter **two MPNs** and displays a side‑by‑side attribute table.
Matching values are highlighted in light green.

## Quick start (Windows + VS Code)

1. **Unzip** this folder somewhere (e.g. `C:\dev\mpn-compare-tool`).
2. **Open in VS Code** (File → Open Folder…).
3. Create a virtual environment and install deps:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   ```

4. Create a `.env` file by copying `.env.example` and filling in your keys (see below).

5. Run the app:

   ```powershell
   python app.py
   ```

6. Open http://127.0.0.1:5000 and compare two MPNs.

## API keys / env vars

This app reuses your existing extractor, which first tries Digi‑Key and falls back to Mouser.
Set either **Digi‑Key** or **Mouser** (or both) in `.env`:

```
# Option A: Digi-Key client-credentials (recommended)
DIGIKEY_CLIENT_ID=your_client_id
DIGIKEY_CLIENT_SECRET=your_client_secret

# Option B: Pre-issued Digi-Key access token
# DIGIKEY_ACCESS_TOKEN=eyJhbGciOi...

# Optional Mouser (used as fallback if Digi-Key fails)
# MOUSER_API_KEY=your_mouser_api_key
```

> If neither service is configured, the attribute fetch may return empty results.

## How it works

- `EOL/eol_attr_extractor.py` fetches a product by MPN and returns a normalized `attributes` map.
- The Flask view queries both MPNs, merges the parameter names, and marks matches by case‑insensitive string equality.
- The web UI is a simple HTML table with matching cells highlighted.

## Notes

- This tool is read‑only; it never writes data anywhere.
- If Digi‑Key rate limits are hit, try again later or add Mouser as fallback.
