from dcim.models import Device
from extras.scripts import Script


class CheckPrimaryIPForFQDNDevices(Script):
    class Meta:
        name = "Validate Primary IP for FQDN-Named Devices"
        description = "Check that devices with names ending in .domain.com or .something.domain.com have a primary IP"
        commit_default = False

    def run(self, data, commit):
        devices = Device.objects.filter(role__slug="network_device", status="active")
        self.log_info(f"Found {devices.count()} active network_device devices.")

        bad_devices = []

        for device in devices:
            # Skip devices with no name
            if not device.name:
                continue

            # Only evaluate devices whose names end with .domain.com or .something.domain.com
            if not (device.name.endswith(".something.com") or device.name.endswith(".something.domain.com")):
                continue

            has_ip = device.primary_ip4 or device.primary_ip6

            if not has_ip:
                bad_devices.append(device)
                self.log_warning(f"{device.name}: missing primary IP")

        if not bad_devices:
            self.log_success("All FQDN-matching devices have a primary IP.")
        else:
            self.log_failure(f"{len(bad_devices)} FQDN-matching devices are missing a primary IP.")
