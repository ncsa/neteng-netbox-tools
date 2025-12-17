import os
import subprocess
from pathlib import Path

from extras.scripts import Script
from netbox_dns.models import Zone


class ExportActiveZones(Script):
    class Meta:
        name = "Export Active DNS Zones"
        description = "Exports active DNS zones from NetBox to BIND config files for internal and external views."
        commit_internal = False

    INTERNAL_ZONE_OUTPUT = Path("/services/bind/config/active_zones.conf")
    SPLIT_HORIZON_ZONE_OUTPUT = Path("/services/bind/config/external_zones.conf")
    EXPORT_BASE = Path("/services/bind")

    def run(self, data, commit):
        internal_zone_lines = []
        split_zone_lines = []

        # --- Step 1: Temporarily change ownership to netbox:netbox ---
        self.log_info("Changing ownership to netbox:netbox for export directory")
        try:
            subprocess.run(
                [
                    "/usr/bin/sudo",
                    "chown",
                    "-R",
                    "netbox:netbox",
                    str(self.EXPORT_BASE),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            self.log_failure(
                f"Failed to change ownership to netbox:netbox:\nSTDERR: {e.stderr}"
            )
            return

        try:
            # --- Step 2: Generate config content ---
            zones = Zone.objects.filter(status="active")

            for zone in zones:
                view_name = getattr(zone.view, "name", None)
                if view_name == "internal":
                    internal_zone_lines.append(
                        f'''zone "{zone.name}" {{
  type master;
  file "/services/bind/zonefiles/internal/{zone.name}";
  notify yes;
}};'''
                    )
                elif view_name == "external":
                    split_zone_lines.append(
                        f'''zone "{zone.name}" {{
  type master;
  file "/services/bind/zonefiles/external/{zone.name}";
  notify yes;
}};'''
                    )

            # --- Step 3: Write config files ---
            self.log_info(
                f"Writing zone definitions to {self.internal_ZONE_OUTPUT} and {self.SPLIT_HORIZON_ZONE_OUTPUT}"
            )
            os.makedirs(self.internal_ZONE_OUTPUT.parent, exist_ok=True)
            os.makedirs(self.SPLIT_HORIZON_ZONE_OUTPUT.parent, exist_ok=True)

            with open(self.internal_ZONE_OUTPUT, "w") as f:
                f.write("\n".join(internal_zone_lines))
            with open(self.SPLIT_HORIZON_ZONE_OUTPUT, "w") as f:
                f.write("\n".join(split_zone_lines))

            self.log_info(
                f"Written {len(internal_zone_lines)} zones to {self.internal_ZONE_OUTPUT}"
            )
            self.log_info(
                f"Written {len(split_zone_lines)} zones to {self.SPLIT_HORIZON_ZONE_OUTPUT}"
            )

        finally:
            # --- Step 4: Restore ownership to bind:bind ---
            self.log_info("Restoring ownership to bind:bind")
            try:
                subprocess.run(
                    [
                        "/usr/bin/sudo",
                        "chown",
                        "-R",
                        "bind:bind",
                        str(self.EXPORT_BASE),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                self.log_failure(
                    f"Failed to restore ownership to bind:bind:\nSTDERR: {e.stderr}"
                )
                return

        # --- Step 5: Reload named ---
        self.log_info("Attempting to reload 'named' configuration via bind_reconfig.sh")
        try:
            subprocess.run(
                ["/usr/bin/sudo", "/services/bind/scripts/bind_reconfig.sh"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.log_success("Successfully reloaded 'named' configuration.")
        except subprocess.CalledProcessError as e:
            self.log_failure(f"Failed to reload 'named': {e.stderr.strip() or e}")
            return

        return f"Export complete: {len(internal_zone_lines)} internal zones, {len(split_zone_lines)} split-horizon zones."
