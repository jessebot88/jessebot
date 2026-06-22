# Firmware Upload Portal

A small web app for uploading firmware images with a verified MD5 checksum, plus
a Nautobot job that imports them.

```
 user → upload form (Flask) → writes firmware + manifest.json into /var/www/firmware
                                           │
              your existing static server serves the files + manifest
                                           │
 Nautobot "Firmware Manifest Sync" job ─ GET manifest.json ─→ Software Image records
```

## Pieces

| File | Where it runs | What it does |
|------|---------------|--------------|
| `app.py` + `templates/index.html` | the firmware web host | Upload form; computes & verifies MD5; writes files + `manifest.json` |
| `firmware_manifest_sync.py` | Nautobot | Pulls `manifest.json`, upserts `SoftwareVersion` + `SoftwareImageFile` |
| `requirements.txt` | the firmware web host | `Flask`, `gunicorn` |

## How the MD5 works

The user *optionally* pastes the MD5 they expect. The server computes the MD5
itself while streaming the upload to disk (chunked, so a multi-GB image never
has to fit in memory). If the user supplied a value and it doesn't match, the
file is deleted and the upload is rejected. **The computed MD5 is what goes in
the manifest** — that's the value Nautobot trusts.

## Setup on the firmware host

You already serve `/var/www/firmware` statically. The Flask app handles the
POST and writes into that same directory, so your static server keeps serving
the downloads and `manifest.json` unchanged.

```bash
cd firmware_upload
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt

export FIRMWARE_DIR=/var/www/firmware
export PUBLIC_BASE_URL=https://firmware.example.com/firmware   # base for download_url
gunicorn --bind 127.0.0.1:8000 app:app
```

The user running gunicorn needs write access to `FIRMWARE_DIR`.

### Run it as a service (systemd)

```ini
# /etc/systemd/system/firmware-upload.service
[Unit]
Description=Firmware upload portal
After=network.target

[Service]
WorkingDirectory=/opt/firmware_upload
Environment=FIRMWARE_DIR=/var/www/firmware
Environment=PUBLIC_BASE_URL=https://firmware.example.com/firmware
Environment=MAX_UPLOAD_MB=4096
ExecStart=/opt/firmware_upload/venv/bin/gunicorn --workers 2 --timeout 300 --bind 127.0.0.1:8000 app:app
Restart=on-failure
User=www-data
Group=www-data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now firmware-upload
```

> The default `--timeout` (30s) is too short for large uploads; bump it as
> above. Also set the matching `MAX_UPLOAD_MB` and your reverse-proxy body-size
> limit (e.g. nginx `client_max_body_size`).

### Front it with your web server

Point the upload UI at the app while serving files statically. Example nginx:

```nginx
location /firmware/ {
    alias /var/www/firmware/;        # static downloads + manifest.json
}
location /upload-portal/ {
    client_max_body_size 4096m;
    proxy_pass http://127.0.0.1:8000/;
    proxy_request_buffering off;     # stream big uploads through
}
```

Browse to `/upload-portal/` to upload; files land in `/firmware/`.

## Manifest format

```json
{
  "generated_at": "2026-06-22T17:40:00+00:00",
  "firmware": [
    {
      "filename": "cat9k_iosxe.17.09.04a.SPA.bin",
      "platform": "cisco_ios",
      "version": "17.9.4",
      "md5": "d41d8cd98f00b204e9800998ecf8427e",
      "size_bytes": 1234567890,
      "download_url": "https://firmware.example.com/firmware/cat9k_iosxe.17.09.04a.SPA.bin",
      "uploaded_at": "2026-06-22T17:39:58+00:00"
    }
  ]
}
```

## Setup in Nautobot

1. Add `firmware_manifest_sync.py` to your Jobs repository (the same place
   `strip_device_domains.py` lives).
2. In Nautobot, enable the **Firmware Manifest Sync** job.
3. Run it with the manifest URL, e.g.
   `https://firmware.example.com/firmware/manifest.json`. Schedule it if you
   want periodic pulls.

Notes:
- Requires **Nautobot 2.2+** (core `SoftwareVersion` / `SoftwareImageFile`).
- The `platform` in each entry must match an existing **Platform** name in
  Nautobot — unknown platforms are skipped (logged), not auto-created.
- The job is idempotent: it `update_or_create`s by version + filename, so
  re-running just refreshes checksums/URLs.
