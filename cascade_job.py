from nautobot.apps.jobs import Job, ObjectVar, BooleanVar, register_jobs
from nautobot.dcim.models import (
    Location, Device, Rack, Interface, Cable, Module,
    PowerFeed, VirtualDeviceContext,
)
from nautobot.ipam.models import Prefix, IPAddress, VLAN, VRF
from nautobot.circuits.models import Circuit
from nautobot.virtualization.models import VirtualMachine, VMInterface
from nautobot.extras.models import Status
from nautobot_bgp_models.models import Peering, PeerGroup, BGPRoutingInstance
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q


class UpdateLocationStatusCascade(Job):
    location = ObjectVar(model=Location)
    new_status = ObjectVar(
        model=Status,
        query_params={"name": ["Active", "Planned", "Staging", "Decommissioning"]},
    )
    dry_run = BooleanVar(default=True, description="Report changes without committing")

    class Meta:
        name = "Update Location Status (Cascade, Full)"
        description = "Set a Location's status and propagate to every location-scoped object"
        has_sensitive_variables = False

    def run(self, location, new_status, dry_run):
        loc_ids = list(location.descendants(include_self=True).values_list("pk", flat=True))
        dev_ids = list(Device.objects.filter(location__in=loc_ids).values_list("pk", flat=True))
        mod_ids = self._collect_modules(dev_ids)
        # Interfaces belong to EITHER a device OR a module (mutually exclusive FKs in
        # Nautobot 2.3+): a module-installed interface has device=NULL and module set.
        # Filtering on device alone silently drops every module interface, so include
        # the interfaces anchored to modules within these devices as well.
        intf_ids = list(
            Interface.objects.filter(
                Q(device__in=dev_ids) | Q(module__in=mod_ids)
            ).values_list("pk", flat=True)
        )
        vmi_ids = list(
            VMInterface.objects.filter(virtual_machine__cluster__location__in=loc_ids).values_list("pk", flat=True)
        )

        eligible = self._eligible_models(new_status)
        counts = {}

        with transaction.atomic():
            # ---- FAST PATH: location-anchored, bulk update by pk subquery, no signals ----
            # (model, filter, exclude). Container prefixes are top-level aggregates that span
            # locations, so they are excluded from the cascade.
            bulk_targets = [
                (Location,             {"pk__in": loc_ids},                          None),
                (Device,               {"pk__in": dev_ids},                          None),
                (Rack,                 {"location__in": loc_ids},                    None),
                (Interface,            {"pk__in": intf_ids},                         None),
                (VirtualDeviceContext, {"device__in": dev_ids},                      None),
                (PowerFeed,            {"power_panel__location__in": loc_ids},       None),
                (Prefix,               {"locations__in": loc_ids},                   {"type": "container"}),
                (VLAN,                 {"locations__in": loc_ids},                   None),
                (VRF,                  {"devices__in": dev_ids},                     None),
                (Circuit,              {"circuit_terminations__location__in": loc_ids}, None),
                (VirtualMachine,       {"cluster__location__in": loc_ids},           None),
                (VMInterface,          {"virtual_machine__cluster__location__in": loc_ids}, None),
            ]
            for model, flt, excl in bulk_targets:
                if model not in eligible:
                    self.logger.warning("Skipping %s: status not valid for it", model.__name__)
                    continue
                base = model.objects.filter(**flt).exclude(status=new_status)
                if excl:
                    base = base.exclude(**excl)
                # Materialize PKs into a literal list. A subquery on the same table
                # (UPDATE t WHERE pk IN (SELECT pk FROM t ...)) is fine on PostgreSQL but
                # raises MySQL/MariaDB error 1093 ("can't specify target table for update
                # in FROM clause"). distinct() dedupes the M2M-join rows before the IN list.
                pks = list(base.values_list("pk", flat=True).distinct())
                counts[model.__name__] = len(pks)
                if not dry_run:
                    model.objects.filter(pk__in=pks).update(status=new_status)

            # ---- SAFE PATH: validated_save() for through / GenericFK / plugin models ----
            # Modules (recursive nesting; may carry status)
            if Module in eligible:
                counts["Module"] = self._save_loop(
                    Module.objects.filter(pk__in=mod_ids).exclude(status=new_status),
                    new_status, dry_run,
                )

            # IPAddress — assigned to physical or VM interfaces via the through model in 2.x
            if IPAddress in eligible:
                ip_qs = IPAddress.objects.filter(
                    Q(interface_assignments__interface__in=intf_ids)
                    | Q(interface_assignments__vm_interface__in=vmi_ids)
                ).exclude(status=new_status).distinct()
                counts["IPAddress"] = self._save_loop(ip_qs, new_status, dry_run)

            # Cable — GenericFK terminations, reached via cached device ids. Cables don't carry
            # an "Active" status; their equivalent is "Connected", so map the chosen status to
            # the cable-side status (see _cable_status) before applying.
            cable_status = self._cable_status(new_status)
            if cable_status is not None:
                cab_qs = Cable.objects.filter(
                    _termination_a_device__in=dev_ids
                ).exclude(status=cable_status).distinct()
                counts["Cable"] = self._save_loop(cab_qs, cable_status, dry_run)
            else:
                self.logger.warning("Skipping Cable: no equivalent cable status for %r", new_status.name)

            # BGP — device-anchored plugin models (no location FK)
            ri_qs = BGPRoutingInstance.objects.filter(device__in=dev_ids)
            ri_ids = list(ri_qs.values_list("pk", flat=True))
            if BGPRoutingInstance in eligible:
                counts["BGPRoutingInstance"] = self._save_loop(
                    ri_qs.exclude(status=new_status), new_status, dry_run,
                )
            if Peering in eligible:
                pe_qs = Peering.objects.filter(
                    endpoints__routing_instance__in=ri_ids
                ).exclude(status=new_status).distinct()
                counts["Peering"] = self._save_loop(pe_qs, new_status, dry_run)
            if PeerGroup in eligible:
                pg_qs = PeerGroup.objects.filter(
                    routing_instance__in=ri_ids
                ).exclude(status=new_status).distinct()
                counts["PeerGroup"] = self._save_loop(pg_qs, new_status, dry_run)

            if dry_run:
                transaction.set_rollback(True)

        for name, n in counts.items():
            self.logger.info("%s %s: %d", "Would update" if dry_run else "Updated", name, n)
        return counts

    def _save_loop(self, qs, new_status, dry_run):
        """Per-object status set with validation + signals. Returns count touched."""
        n = 0
        for obj in qs.iterator(chunk_size=500):
            n += 1
            if dry_run:
                continue
            obj.status = new_status
            try:
                obj.validated_save()
            except ValidationError as e:
                self.logger.error("%s %s failed validation: %s", obj._meta.model_name, obj.pk, e)
                raise  # abort the whole transaction; don't leave a partial cascade
        return n

    def _collect_modules(self, dev_ids):
        """Walk nested ModuleBay/Module tree to arbitrary depth."""
        found, frontier = set(), list(
            Module.objects.filter(parent_module_bay__parent_device__in=dev_ids)
            .values_list("pk", flat=True)
        )
        while frontier:
            found.update(frontier)
            frontier = list(
                Module.objects.filter(parent_module_bay__parent_module__in=frontier)
                .exclude(pk__in=found).values_list("pk", flat=True)
            )
        return list(found)

    @staticmethod
    def _eligible_models(status):
        # Cable is handled separately (via _cable_status) since it uses a different status
        # vocabulary, so it's intentionally absent here.
        cts = set(status.content_types.values_list("id", flat=True))
        candidates = [Location, Device, Rack, Module, Interface, VirtualDeviceContext, PowerFeed,
                      Prefix, VLAN, VRF, Circuit, IPAddress,
                      VirtualMachine, VMInterface,
                      BGPRoutingInstance, Peering, PeerGroup]
        return {m for m in candidates
                if ContentType.objects.get_for_model(m).id in cts}

    # Cables use "Connected" where the rest of the fleet uses "Active". Map the chosen status
    # to its cable-side equivalent; statuses not listed fall through to a same-named cable status.
    CABLE_STATUS_ALIASES = {"Active": "Connected"}

    def _cable_status(self, new_status):
        """Resolve the Cable status equivalent to the chosen status, or None if there isn't one."""
        cable_ct = ContentType.objects.get_for_model(Cable)
        name = self.CABLE_STATUS_ALIASES.get(new_status.name, new_status.name)
        return Status.objects.filter(content_types=cable_ct, name=name).first()


register_jobs(UpdateLocationStatusCascade)
