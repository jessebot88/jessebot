"""Nautobot Job: pull the firmware manifest and upsert Software Image records.

Fetches ``manifest.json`` from the firmware web server and, for each entry,
ensures a matching ``SoftwareVersion`` and ``SoftwareImageFile`` exist with the
recorded MD5 checksum and download URL.

Requires Nautobot 2.2+ (core Software Version / Software Image File models).
The Platform named in each manifest entry must already exist in Nautobot;
entries referencing an unknown platform are skipped with a warning rather than
creating platforms implicitly.
"""
import json
import urllib.request

from nautobot.apps.jobs import Job, StringVar, register_jobs
from nautobot.dcim.models import Platform, SoftwareImageFile, SoftwareVersion


class FirmwareManifestSync(Job):
    class Meta:
        name = "Firmware Manifest Sync"
        description = "Import firmware images + MD5 checksums from the upload portal's manifest.json."
        has_sensitive_variables = False

    manifest_url = StringVar(
        description="URL of the firmware manifest, e.g. http://firmware.example.com/firmware/manifest.json",
    )

    def run(self, manifest_url):
        with urllib.request.urlopen(manifest_url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        entries = data.get("firmware", data) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            self.logger.error("Manifest did not contain a firmware list.")
            return "Aborted: unexpected manifest format."

        created = updated = skipped = 0

        for entry in entries:
            filename = entry.get("filename")
            platform_name = entry.get("platform")
            version = entry.get("version")
            md5 = entry.get("md5")

            if not all([filename, platform_name, version, md5]):
                self.logger.warning("Skipping incomplete entry: %s", entry)
                skipped += 1
                continue

            platform = Platform.objects.filter(name=platform_name).first()
            if platform is None:
                self.logger.warning(
                    "Skipping '%s': platform '%s' not found in Nautobot.",
                    filename,
                    platform_name,
                )
                skipped += 1
                continue

            software_version, _ = SoftwareVersion.objects.get_or_create(
                platform=platform,
                version=version,
                defaults={"status": self._default_status()},
            )

            image, was_created = SoftwareImageFile.objects.update_or_create(
                software_version=software_version,
                image_file_name=filename,
                defaults={
                    "image_file_checksum": md5,
                    "hashing_algorithm": "MD5",
                    "image_file_size": entry.get("size_bytes"),
                    "download_url": entry.get("download_url", ""),
                    "status": self._default_status(),
                },
            )

            if was_created:
                self.logger.info("Created image %s (%s %s)", filename, platform_name, version)
                created += 1
            else:
                self.logger.info("Updated image %s (%s %s)", filename, platform_name, version)
                updated += 1

        summary = f"Created {created}, updated {updated}, skipped {skipped}."
        self.logger.info(summary)
        return summary

    @staticmethod
    def _default_status():
        """Software Version/Image File require a Status FK; reuse 'Active'."""
        from nautobot.extras.models import Status

        return Status.objects.get(name="Active")


register_jobs(FirmwareManifestSync)
