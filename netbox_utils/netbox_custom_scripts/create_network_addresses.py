#!/usr/bin/env python3
# NetBox custom script: Allocate active NETWORK/GATEWAY(/BROADCAST) IPs per prefix
#
# - Idempotent: avoids re-saving when nothing changed
# - Compares IPv4/IPv6 semantically (host + prefixlen)
# - Stores address as "<host>/<parent prefixlen>" (host form, not network)
# - Forces:
#     status = active  (uses Status model when available; else falls back to "active")
#     tenant = org_nerd
#     description = NETWORK / GATEWAY / BROADCAST
#     role tag present

from django.core.exceptions import ValidationError

from extras.scripts import Script, MultiObjectVar
from ipam.models import Prefix, IPAddress
from tenancy.models import Tenant
from extras.models import Tag

# Try to import Status for newer NetBox; gracefully handle older versions
try:
    from extras.models import Status  # NetBox 3.x/4.x
except Exception:  # pragma: no cover
    Status = None

from netaddr import IPAddress as NA_IPAddress, IPNetwork as NA_IPNetwork


class Allocate_Active_Addresses(Script):
    """
    For each active, non-container Prefix:
      - IPv4: upsert NETWORK (active), GATEWAY (active), BROADCAST (active)
      - IPv6: upsert NETWORK (active), GATEWAY (active)

    All IPs (new and existing) are forced to:
      - status = active (Status FK when available; else "active" slug)
      - tenant = org_nerd
      - description = NETWORK / GATEWAY / BROADCAST
      - address normalized to <host>/<parent prefixlen> (host form)
      - role tag present
    """

    class Meta:
        name = "Allocate/Update active IPs for prefixes (tenant=org_nerd)"
        description = (
            "Creates or updates NETWORK/GATEWAY/BROADCAST (IPv4) and NETWORK/GATEWAY (IPv6) "
            "as active for active, non-container prefixes. Tenant forced to org_nerd. "
            "Idempotent: avoids no-op saves that cause noisy change records."
        )
        commit_default = True

    prefixes = MultiObjectVar(
        model=Prefix,
        required=False,
        description="Select prefixes. If empty, processes ALL active, non-container prefixes.",
    )

    # ---- Tag, status & tenant helpers -------------------------------------------

    def _get_or_create_tag(self, name, color):
        tag, _ = Tag.objects.get_or_create(name=name, defaults={"color": color})
        return tag

    def _role_tags(self):
        return {
            "NETWORK": self._get_or_create_tag("network-address", "607d8b"),     # blue-grey
            "GATEWAY": self._get_or_create_tag("gateway-address", "4caf50"),             # green
            "BROADCAST": self._get_or_create_tag("broadcast-address", "ff9800"), # orange
        }

    def _resolve_active_status(self):
        """
        Return the appropriate 'active' status for the IPAddress model:
        - If Status model is available: return the Status instance scoped to IPAddress.
        - Otherwise: return the slug "active".
        """
        if Status is None:
            return "active"
        try:
            return Status.objects.get_for_model(IPAddress).get(slug="active")
        except Exception:
            # Fallback for odd/older deployments
            return "active"

    def _get_nerd_tenant(self):
        # Prefer slug (stable), fall back to exact name if needed
        try:
            return Tenant.objects.get(slug="org_nerd")
        except Tenant.DoesNotExist:
            try:
                return Tenant.objects.get(name="org_nerd")
            except Tenant.DoesNotExist:
                raise RuntimeError("Tenant 'org_nerd' (slug 'org_nerd') does not exist in NetBox!")

    # ---- Status handling (robust across versions) --------------------------------

    def _status_slug(self, obj):
        """
        NetBox StatusField can be a related object (with .slug) or a plain string.
        Return the slug/string so comparisons are robust & idempotent.
        """
        val = getattr(obj, "status", None)
        slug = getattr(val, "slug", None)
        if slug:
            return slug
        if isinstance(val, str):
            return val
        return None

    # ---- Upsert helpers (use netaddr for math; assign host-form strings) --------

    def _variants_for_lookup(self, host_ip: NA_IPAddress, parent_plen: int):
        """
        Return possible address encodings that might exist already for this host.
        We search by strings for efficiency, then compare semantically after load.
        """
        if host_ip.version == 4:
            return [f"{host_ip}/{parent_plen}", f"{host_ip}/32"]
        else:
            return [f"{host_ip}/{parent_plen}", f"{host_ip}/128"]

    def _ensure_updated(
        self,
        ip_obj,
        *,
        vrf,
        tenant,
        description,
        role_tag,
        parent_plen: int,
        host_ip: NA_IPAddress,
    ):
        """
        Update an existing IP to match desired settings.
        Compare IPv4/IPv6 semantically (host + prefixlen). Do NOT force updates just
        because the string formatting differs; NetBox may re-serialize addresses.
        Always assign address as the host-form string "<host>/<parent_plen>" if semantic differs.
        """
        changed = []

        desired_str = f"{host_ip}/{parent_plen}"
        desired_net = NA_IPNetwork(desired_str)

        # Use semantic comparison only; string form may legitimately differ after save
        stored_str = str(ip_obj.address)
        try:
            stored_net = NA_IPNetwork(stored_str)
        except Exception:
            stored_net = None  # If unparsable, force correction

        if (
            stored_net is None
            or stored_net.ip != desired_net.ip
            or stored_net.prefixlen != desired_net.prefixlen
        ):
            ip_obj.address = desired_str
            changed.append("address(semantic-normalize)")

        if ip_obj.vrf_id != getattr(vrf, "id", None):
            ip_obj.vrf = vrf
            changed.append("vrf")

        if ip_obj.tenant_id != getattr(tenant, "id", None):
            ip_obj.tenant = tenant
            changed.append("tenant=org_nerd")

        # Status: use FK object if we have one; else use slug
        if isinstance(self._active_status, str):
            if self._status_slug(ip_obj) != "active":
                ip_obj.status = "active"
                changed.append("status=active")
        else:
            if ip_obj.status_id != self._active_status.id:
                ip_obj.status = self._active_status
                changed.append("status=active")

        if ip_obj.description != description:
            ip_obj.description = description
            changed.append("description")

        # Ensure role tag exists (merge, do not wipe existing)
        if role_tag and not ip_obj.tags.filter(pk=role_tag.pk).exists():
            # If there are pending field changes, save first so M2M add is safe
            if changed:
                try:
                    ip_obj.save()
                except ValidationError as e:
                    self.log_failure(f"  Failed to save before tag add {desired_str} ({description}): {e}")
                    return
                changed = []
            ip_obj.tags.add(role_tag)
            self.log_info(f"  Tag added: {role_tag.name} → {desired_str} ({description})")

        if changed:
            try:
                ip_obj.save()
                self.log_success(f"  Updated: {ip_obj.address} ({description}) — changed: {', '.join(changed)}")
            except ValidationError as e:
                self.log_failure(f"  Failed to update {desired_str} ({description}): {e}")
        else:
            self.log_info(f"  No change: {ip_obj.address} ({description})")

    def _create_ip(
        self,
        *,
        vrf,
        tenant,
        description,
        role_tag,
        parent_plen: int,
        host_ip: NA_IPAddress,
    ):
        addr_str = f"{host_ip}/{parent_plen}"
        try:
            # Use the resolved status (object or slug)
            ip = IPAddress(
                address=addr_str,
                vrf=vrf,
                tenant=tenant,
                status=(self._active_status if not isinstance(self._active_status, str) else "active"),
                description=description,
            )
            ip.save()
            if role_tag:
                ip.tags.add(role_tag)
            self.log_success(f"  Created: {ip.address} ({description}, tenant=org_nerd, status=active)")
            return ip
        except ValidationError as e:
            self.log_failure(f"  Failed to create {addr_str} ({description}): {e}")
            return None

    def _upsert_ip(
        self,
        *,
        vrf,
        tenant,
        description,
        role_tag,
        parent_plen: int,
        host_ip: NA_IPAddress,
    ):
        """
        Find by host in same VRF (regardless of stored mask).
        Try fast string variants first; if not found, create.
        """
        variants = self._variants_for_lookup(host_ip, parent_plen)
        existing = IPAddress.objects.filter(vrf=vrf, address__in=variants).order_by("pk")
        if existing.exists():
            ip_obj = existing.first()
            self._ensure_updated(
                ip_obj,
                vrf=vrf,
                tenant=tenant,
                description=description,
                role_tag=role_tag,
                parent_plen=parent_plen,
                host_ip=host_ip,
            )
            return ip_obj
        else:
            return self._create_ip(
                vrf=vrf,
                tenant=tenant,
                description=description,
                role_tag=role_tag,
                parent_plen=parent_plen,
                host_ip=host_ip,
            )

    # ---- Main --------------------------------------------------------------------

    def run(self, data, commit):
        # Active only; skip container role
        qs = (
            Prefix.objects.filter(status="active")
            .exclude(role__name__iexact="container")
            .exclude(role__slug__iexact="container")
        )
        if data.get("prefixes"):
            qs = qs.filter(pk__in=[p.pk for p in data["prefixes"]])

        # Resolve dependencies once (stable across the run)
        self._active_status = self._resolve_active_status()
        tags = self._role_tags()
        nerd_tenant = self._get_nerd_tenant()

        processed = 0

        for pfx in qs.iterator():
            processed += 1

            net = NA_IPNetwork(str(pfx.prefix))
            vrf = pfx.vrf
            plen = net.prefixlen

            self.log_info(f"Processing prefix: {net} (forced tenant=org_nerd)")

            # NETWORK (v4 & v6)
            self._upsert_ip(
                vrf=vrf,
                tenant=nerd_tenant,
                description="NETWORK",
                role_tag=tags["NETWORK"],
                parent_plen=plen,
                host_ip=net.network,
            )

            if net.version == 4:
                # If subnet has usable hosts: gateway & broadcast
                if net.size >= 4:
                    # GATEWAY = first usable
                    self._upsert_ip(
                        vrf=vrf,
                        tenant=nerd_tenant,
                        description="GATEWAY",
                        role_tag=tags["GATEWAY"],
                        parent_plen=plen,
                        host_ip=net.network + 1,
                    )
                    # BROADCAST = last address
                    self._upsert_ip(
                        vrf=vrf,
                        tenant=nerd_tenant,
                        description="BROADCAST",
                        role_tag=tags["BROADCAST"],
                        parent_plen=plen,
                        host_ip=net.broadcast,
                    )
                else:
                    self.log_info(f"  Skipping gateway/broadcast for tiny IPv4 prefix ({net}).")
            else:
                # IPv6: no broadcast; add gateway when possible
                if net.size >= 2:
                    self._upsert_ip(
                        vrf=vrf,
                        tenant=nerd_tenant,
                        description="GATEWAY",
                        role_tag=tags["GATEWAY"],
                        parent_plen=plen,
                        host_ip=net.network + 1,
                    )
                else:
                    self.log_info(f"  Skipping gateway for tiny IPv6 prefix ({net}).")

        self.log_success(
            f"Done. Processed prefixes: {processed}. All updates enforce active status, org_nerd tenant, descriptions, and tags."
        )
