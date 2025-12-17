from extras.scripts import Script
from ipam.models import VLAN, Prefix, IPAddress
from django.db.models import Q


class SyncTenantFromVlan(Script):
    class Meta:
        name = "Sync Tenant from VLAN to Prefixes and IPs"
        description = (
            "Propagates each VLAN's tenant to its Prefixes, "
            "and then to all IP Addresses contained within those Prefixes. "
            "Skips any IP addresses already assigned to the 'org_nerd' tenant."
        )
        commit_default = True

    def run(self, data, commit):
        updated_prefixes = 0
        updated_ips = 0
        skipped_nerd_ips = 0

        # Iterate through all VLANs that have a tenant
        for vlan in VLAN.objects.exclude(tenant=None):
            tenant = vlan.tenant
            self.log_info(f"Processing VLAN {vlan.name} (Tenant: {tenant})")

            # Get all prefixes associated with this VLAN
            prefixes = Prefix.objects.filter(vlan=vlan)
            for prefix in prefixes:
                # Update prefix tenant if different
                if prefix.tenant != tenant:
                    if prefix.tenant and prefix.tenant.name == "org_nerd":
                        self.log_info(
                            f" - Skipping Prefix {prefix.prefix} (owned by org_nerd)"
                        )
                        continue
                    self.log_info(
                        f" - Updating Prefix {prefix.prefix} tenant → {tenant}"
                    )
                    prefix.tenant = tenant
                    prefix.save()
                    updated_prefixes += 1

                # Get all IPs contained in this prefix
                ip_addresses = IPAddress.objects.filter(
                    Q(address__net_contained_or_equal=prefix.prefix)
                )

                for ip in ip_addresses:
                    # Skip IPs already assigned to org_nerd
                    if ip.tenant and ip.tenant.name == "org_nerd":
                        skipped_nerd_ips += 1
                        continue

                    if ip.tenant != tenant:
                        self.log_info(
                            f"   - Updating IP {ip.address} tenant → {tenant}"
                        )
                        ip.tenant = tenant
                        ip.save()
                        updated_ips += 1

        self.log_success(
            f"Updated {updated_prefixes} prefixes and {updated_ips} IPs "
            f"(skipped {skipped_nerd_ips} org_nerd IPs)."
        )
