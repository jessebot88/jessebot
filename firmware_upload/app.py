"""Firmware upload portal.

A small Flask app that lets users upload a firmware image plus an optional
expected MD5 checksum. The server computes the MD5 itself (streaming, so large
images do not have to fit in memory), verifies it against the value the user
supplied, stores the file under ``FIRMWARE_DIR``, and records an entry in
``manifest.json`` next to the files.

A Nautobot job (see ``firmware_manifest_sync.py``) pulls that manifest and
creates/updates the matching Software Image records.

Configuration via environment variables:

    FIRMWARE_DIR       Where firmware files + manifest.json are written.
                       Default: /var/www/firmware
    PUBLIC_BASE_URL    Public base URL the static server exposes the files at,
                       used to build each entry's download_url.
                       Default: derived from the incoming request.
    MAX_UPLOAD_MB      Reject uploads larger than this many megabytes.
                       Default: 2048 (2 GiB).

Run it for real with a WSGI server, e.g.:

    pip install -r requirements.txt
    gunicorn --bind 127.0.0.1:8000 app:app
"""
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

from flask import Flask, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

FIRMWARE_DIR = os.environ.get("FIRMWARE_DIR", "/var/www/firmware")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))
MANIFEST_NAME = "manifest.json"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
# Only used to flash one-shot messages back to the form.
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())


def manifest_path():
    return os.path.join(FIRMWARE_DIR, MANIFEST_NAME)


def load_manifest():
    """Return the manifest as a list of entries (empty if none yet)."""
    try:
        with open(manifest_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    # Tolerate either a bare list or a {"firmware": [...]} wrapper.
    if isinstance(data, dict):
        return data.get("firmware", [])
    return data if isinstance(data, list) else []


def save_manifest(entries):
    """Write the manifest atomically so a reader never sees a half file."""
    os.makedirs(FIRMWARE_DIR, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "firmware": entries,
    }
    fd, tmp = tempfile.mkstemp(dir=FIRMWARE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, manifest_path())
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def stream_to_disk_with_md5(file_storage, dest_path):
    """Save an uploaded file to ``dest_path`` while computing its MD5.

    Streams in chunks so a multi-gigabyte image never has to be buffered
    in memory. Returns the hex digest and the byte size.
    """
    md5 = hashlib.md5()
    size = 0
    with open(dest_path, "wb") as out:
        while True:
            chunk = file_storage.stream.read(1024 * 1024)
            if not chunk:
                break
            md5.update(chunk)
            size += len(chunk)
            out.write(chunk)
    return md5.hexdigest(), size


def build_download_url(filename):
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/{filename}"
    # Fall back to the host the request came in on.
    return request.url_root.rstrip("/") + "/firmware/" + filename


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", firmware=load_manifest())


@app.route("/upload", methods=["POST"])
def upload():
    uploaded = request.files.get("firmware")
    if uploaded is None or uploaded.filename == "":
        flash("Please choose a firmware file to upload.", "error")
        return redirect(url_for("index"))

    filename = secure_filename(uploaded.filename)
    if not filename:
        flash("That filename is not valid.", "error")
        return redirect(url_for("index"))

    platform = request.form.get("platform", "").strip()
    version = request.form.get("version", "").strip()
    expected_md5 = request.form.get("md5", "").strip().lower()
    if not platform or not version:
        flash("Platform and version are required.", "error")
        return redirect(url_for("index"))

    os.makedirs(FIRMWARE_DIR, exist_ok=True)
    dest_path = os.path.join(FIRMWARE_DIR, filename)

    actual_md5, size = stream_to_disk_with_md5(uploaded, dest_path)

    # If the user told us what to expect, hold them to it.
    if expected_md5 and expected_md5 != actual_md5:
        os.remove(dest_path)
        flash(
            f"MD5 mismatch: expected {expected_md5}, got {actual_md5}. "
            "Upload rejected.",
            "error",
        )
        return redirect(url_for("index"))

    entry = {
        "filename": filename,
        "platform": platform,
        "version": version,
        "md5": actual_md5,
        "size_bytes": size,
        "download_url": build_download_url(filename),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }

    # Replace any prior entry for the same file, then append the fresh one.
    entries = [e for e in load_manifest() if e.get("filename") != filename]
    entries.append(entry)
    save_manifest(entries)

    flash(
        f"Uploaded {filename} ({size:,} bytes). Verified MD5 {actual_md5}.",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/healthz")
def healthz():
    return {"status": "ok", "firmware_dir": FIRMWARE_DIR}


if __name__ == "__main__":
    # Dev server only. Use gunicorn/uwsgi in production (see README).
    app.run(host="0.0.0.0", port=8000, debug=False)
