from nautobot.apps.jobs import Job, register_jobs, ObjectVar, BooleanVar
from collections import defaultdict
from nautobot.dcim.models import Device, Location
from nautobot.ipam.models import Interface, IPAddress, Prefix, VLAN, Namespace
from nautobot.extras.models import Role, Status
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.db.models import Q
import re
import ipaddress
 
class FixIpAndPrefixMetadata(Job):
    class Meta:
        name = "Update Gateway Info (From VLAN)"
        description = (
            "Syncs IP/Prefix metadata using the ACTUAL VLAN NAME as the source of truth. "
            "Ignores interface descriptions. Formats as '[ Vlan# ] VlanName'."
        )
        approval_required = False
        has_sensitive_variables = False
 
    location = ObjectVar(
        model=Location,
        required=False,
        description="Limit scope to this location (optional).",
    )
    dry_run = BooleanVar(
        description="Dry Run (log changes without saving).",
        default=True,
    )
 
    def run(self, location=None, dry_run=True):
        # Regex to identify VLAN ID from Interface Name
        regex_svi = re.compile(r"^Vlan(\d+)$", re.IGNORECASE)
        regex_sub = re.compile(r"^.*\.(\d+)$")
 
        stats = {"ips_updated": 0, "prefixes_created": 0, "prefixes_updated": 0, "vlans_fixed": 0}
 
        self.logger.info(f"Starting sync. Source of Truth: VLAN Objects. Dry Run: {dry_run}")
        
        # 1. Scope
        device_qs = Device.objects.filter(device_type__manufacturer__name="Cisco")
        if location:
            device_qs = device_qs.filter(location=location)
 
        # 2. Prerequisites
        try:
            vlan_role = Role.objects.get(name="vlan")
            gateway_role = Role.objects.get(name="Gateway")
            loopback_role = Role.objects.get(name="Loopback")
            namespace    = Namespace.objects.get(name="global")
            try:
                active_status = Status.objects.get(name="Active")
            except ObjectDoesNotExist:
                active_status = Status.objects.get(name="active")
        except ObjectDoesNotExist as e:
            self.logger.error(f"Missing DB Object: {e}")
            return
 
        discovered_prefix_locs = defaultdict(set)

        for device in device_qs:
            dev_role = (device.role.name.lower() if device.role else "")
            target_role = loopback_role if dev_role == "access-switch" else gateway_role
            target_loc = device.location
            
            # 3. Interfaces
            interfaces = device.interfaces.filter(
                Q(name__istartswith="Vlan") | Q(name__contains=".")
            )
 
            for interface in interfaces:
                # Get VLAN ID from Name
                vlan_id = None
                match_svi = regex_svi.match(interface.name)
                match_sub = regex_sub.match(interface.name)
                if match_svi: vlan_id = int(match_svi.group(1))
                elif match_sub: vlan_id = int(match_sub.group(1))
                else: continue
 
                # --- 4. FIND VLAN OBJECT (SOURCE OF TRUTH) ---
                parent_vlan = None
                vlan_qs = VLAN.objects.filter(vid=vlan_id)
                
                # Priority A: Match Device Location
                if vlan_qs.filter(locations=target_loc).exists():
                    parent_vlan = vlan_qs.filter(locations=target_loc).first()
                
                # Priority C: Global/Any match (and fix location if needed)
                elif vlan_qs.exists():
                    parent_vlan = vlan_qs.first()
                    # We found a VLAN but it didn't match our location.
                    # We will use its name, but also tag it to this location so it matches next time.
                    msg = f"[{device.name}] Found VLAN {vlan_id} ('{parent_vlan.name}') but missing Location {target_loc}. Fixing."
                    self.logger.info(msg)
                    stats["vlans_fixed"] += 1
                    if not dry_run: parent_vlan.locations.add(target_loc)
 
                # --- 5. CONSTRUCT DESCRIPTION ---
                if parent_vlan:
                    # Format: [ Vlan10 ] OKE:UMA-Data
                    # We use .name because that is what your screenshot showed (e.g. "OKE:UMA-Data")
                    formatted_desc = f"[ Vlan{vlan_id} ] {parent_vlan.name}"
                else:
                    # Fallback if NO VLAN object exists at all
                    # We keep the brackets so it looks standard, but warn the user
                    formatted_desc = f"[ Vlan{vlan_id} ]"
                    self.logger.warning(f"[{device.name}] VLAN {vlan_id} does not exist in Nautobot! Created placeholder desc.")
 
                # 6. Process IPs
                for ip in interface.ip_addresses.all():
                    ip_str = str(ip.address).strip()
                    if not ip_str or ip_str == "/": continue
 
                    try:
                        ip_obj = ipaddress.ip_interface(ip_str)
                    except ValueError: continue
 
                    net_addr_str = str(ip_obj.network.network_address)
                    net_len_int = int(ip_obj.network.prefixlen)
 
                    # --- A. UPDATE IP ---
                    updates = []
                    if ip.dns_name != device.name:
                        ip.dns_name = device.name
                        updates.append("DNS")
                    
                    # Force description to match VLAN Source of Truth
                    if ip.description != formatted_desc:
                        ip.description = formatted_desc
                        updates.append("Desc")
                    
                    if ip.role != target_role:
                        ip.role = target_role
                        updates.append("Role")
                    
                    if ip.tenant is None:
                        ip.tenant = device.tenant
                        updates.append("Tenant")
                                            
                    if updates:
                        self.logger.info(f"[{device.name}] IP {ip_str}: Updating {', '.join(updates)}")
                        stats["ips_updated"] += 1
                        if not dry_run:
                            try:
                                ip.save()
                            except ValidationError as e:
                                self.logger.warning(f"[{device.name}] validation error on {ip_str}. Error: {e}")
                                ip.parent = None
                                ip.save()
 
                    # --- B. PREFIX MANAGEMENT ---
                    prefix = Prefix.objects.filter(
                        network=net_addr_str, 
                        prefix_length=net_len_int, 
                        namespace=namespace
                    ).first()
 
                    # CREATE
                    if not prefix:
                        if Prefix.objects.filter(network=net_addr_str, prefix_length=net_len_int).exists():
                            continue 
                        
                        self.logger.info(f"[{device.name}] Creating Prefix {ip_obj.network}")
                        stats["prefixes_created"] += 1
                        if not dry_run:
                            Prefix.objects.create(
                                network=net_addr_str,
                                prefix_length=net_len_int,
                                namespace=namespace,
                                description=formatted_desc,
                                location=target_loc,
                                role=vlan_role,
                                status=active_status,
                                vlan=parent_vlan,
                                tenant=device.tenant,
                            )
 
                    # UPDATE
                    else:
                        p_updates = []
                        
                        if prefix.description != formatted_desc:
                            p_updates.append(f"Desc ('{prefix.description}' -> '{formatted_desc}')")
                            prefix.description = formatted_desc
 
                        if target_loc:
                            discovered_prefix_locs[prefix].add(target_loc)
 
                        if prefix.role != vlan_role:
                            p_updates.append(f"Role ('{prefix.role}' -> 'vlan')")
                            prefix.role = vlan_role
 
                        if parent_vlan and prefix.vlan != parent_vlan:
                            p_updates.append(f"VLAN ('{prefix.vlan}' -> '{parent_vlan}')")
                            prefix.vlan = parent_vlan

                        if prefix.tenant != device.tenant:
                            p_updates.append(f"Tenant ('{prefix.tenant}' -> '{device.tenant}')")
                            prefix.tenant = device.tenant                         
 
                        if p_updates:
                            self.logger.info(f"[{device.name}] Updating Prefix {prefix}: {', '.join(p_updates)}")
                            stats["prefixes_updated"] += 1
                            if not dry_run:
                                prefix.save()
        
        self.logger.info("Starting Phase 2: Prefix Location Reconciliation...")
        
        for pref, valid_locs in discovered_prefix_locs.items():
            # Get the current locations from the DB
            current_locs = set(pref.locations.all())
            
            # If Nautobot's state doesn't exactly match our discovered state...
            if current_locs != valid_locs:
                old_names = [l.name for l in current_locs]
                new_names = [l.name for l in valid_locs]
                
                self.logger.info(f"Prefix {pref.network} Locations updating: {old_names} -> {new_names}")
                
                if not dry_run:
                    # .set() forcefully overwrites the list: adding missing, and deleting stale locations
                    pref.locations.set(valid_locs)
 
        self.logger.info(f"DONE. IPs: {stats['ips_updated']} | Prefixes New: {stats['prefixes_created']} | Prefixes Updated: {stats['prefixes_updated']}")
        return "Complete"
 
register_jobs(FixIpAndPrefixMetadata)
 