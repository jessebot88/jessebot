"""Nautobot Job: strip FQDN domain suffix from device names.

Device names matching the pattern ``xx-xxxx-xxxxxx.domain.com`` will be
shortened to ``xx-xxxx-xxxxxx`` (everything before the first dot).
"""
from nautobot.apps.jobs import BooleanVar, Job, register_jobs
from nautobot.dcim.models import Device


class StripDeviceDomains(Job):
    class Meta:
        name = "Strip Device Domains"
        description = "Remove the domain suffix (e.g. .domain.com) from device names."
        has_sensitive_variables = False

    dry_run = BooleanVar(
        description="Preview changes without saving.",
        default=True,
    )

    def run(self, dry_run):
        renamed = 0
        skipped = 0

        for device in Device.objects.filter(name__contains="."):
            original = device.name
            new_name = original.split(".", 1)[0]

            if not new_name or new_name == original:
                skipped += 1
                continue

            if Device.objects.filter(name=new_name).exclude(pk=device.pk).exists():
                self.logger.warning(
                    "Skipping '%s': another device already uses '%s'.",
                    original,
                    new_name,
                )
                skipped += 1
                continue

            self.logger.info("Renaming '%s' -> '%s'", original, new_name)

            if not dry_run:
                device.name = new_name
                device.validated_save()

            renamed += 1

        verb = "Would rename" if dry_run else "Renamed"
        self.logger.info("%s %d device(s); skipped %d.", verb, renamed, skipped)
        return f"{verb} {renamed} device(s); skipped {skipped}."


register_jobs(StripDeviceDomains)
