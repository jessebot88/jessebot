"""Nautobot Job: append a network identifier to VLAN Group names.

Iterates over VLAN Groups that are tied to a Location and rewrites the
name so that a per-location network identifier is inserted before the
trailing keyword.

    "sitename vlans"  ->  "sitename networkid vlans"

The identifier value is pulled from a field on the VLAN Group's Location.
By default it reads the Location custom field with key ``network_id``,
but the ``identifier_field`` parameter lets you point at any custom field
key (or plain Location attribute) without editing the code.
"""
from nautobot.apps.jobs import BooleanVar, Job, MultiObjectVar, StringVar, register_jobs
from nautobot.ipam.models import VLANGroup


class RenameVLANGroups(Job):
    class Meta:
        name = "Rename VLAN Groups With Network ID"
        description = (
            "Insert each Location's network identifier into its VLAN Group "
            "names, e.g. 'sitename vlans' -> 'sitename networkid vlans'."
        )
        has_sensitive_variables = False

    vlan_groups = MultiObjectVar(
        model=VLANGroup,
        required=False,
        description=(
            "Optional: limit to specific VLAN Groups for testing. "
            "Leave blank to process all VLAN Groups that have a Location."
        ),
    )
    identifier_field = StringVar(
        description=(
            "Where to read the network identifier from on the Location. "
            "Tries the Location custom field with this key first, then a "
            "plain attribute of the same name."
        ),
        default="network_id",
    )
    suffix = StringVar(
        description=(
            "Trailing keyword in the current name that the identifier is "
            "inserted *before*. If the name doesn't end with this, the "
            "identifier is appended to the end instead."
        ),
        default="vlans",
    )
    dry_run = BooleanVar(
        description="Preview changes without saving.",
        default=True,
    )

    def _get_identifier(self, location, field_key):
        """Return the network identifier for a location, or None."""
        if location is None:
            return None
        # Prefer a custom field with this key.
        value = location.cf.get(field_key)
        if value in (None, ""):
            # Fall back to a plain attribute of the same name.
            value = getattr(location, field_key, None)
        if value in (None, ""):
            return None
        return str(value).strip()

    def run(self, dry_run, identifier_field, suffix, vlan_groups=None):
        identifier_field = identifier_field.strip()
        suffix = suffix.strip()

        renamed = 0
        skipped = 0

        queryset = VLANGroup.objects.filter(location__isnull=False)
        if vlan_groups:
            queryset = queryset.filter(pk__in=[g.pk for g in vlan_groups])

        for group in queryset.select_related("location"):
            original = group.name
            location = group.location

            identifier = self._get_identifier(location, identifier_field)
            if not identifier:
                self.logger.warning(
                    "Skipping '%s': location '%s' has no '%s' value.",
                    original,
                    getattr(location, "name", "?"),
                    identifier_field,
                )
                skipped += 1
                continue

            # Idempotency: don't insert an identifier that's already present.
            if identifier.lower() in [word.lower() for word in original.split()]:
                self.logger.info(
                    "Skipping '%s': already contains '%s'.", original, identifier
                )
                skipped += 1
                continue

            # Insert the identifier before the trailing suffix when present,
            # otherwise append it to the end.
            if suffix and original.lower().endswith(suffix.lower()):
                base = original[: len(original) - len(suffix)].rstrip()
                actual_suffix = original[len(original) - len(suffix):]
                new_name = f"{base} {identifier} {actual_suffix}"
            else:
                new_name = f"{original} {identifier}"

            if new_name == original:
                skipped += 1
                continue

            if (
                VLANGroup.objects.filter(name=new_name)
                .exclude(pk=group.pk)
                .exists()
            ):
                self.logger.warning(
                    "Skipping '%s': another VLAN Group already uses '%s'.",
                    original,
                    new_name,
                )
                skipped += 1
                continue

            self.logger.info("Renaming '%s' -> '%s'", original, new_name)

            if not dry_run:
                group.name = new_name
                group.validated_save()

            renamed += 1

        verb = "Would rename" if dry_run else "Renamed"
        self.logger.info("%s %d VLAN Group(s); skipped %d.", verb, renamed, skipped)
        return f"{verb} {renamed} VLAN Group(s); skipped {skipped}."


register_jobs(RenameVLANGroups)
