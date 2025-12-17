import time

from extras.scripts import Script
from icmplib import ping as icmp_ping
from ipam.models import IPAddress, Prefix


class PingGateways(Script):
    class Meta:
        name = "Ping Gateway IPs"
        description = (
            "Ping all IP addresses tagged as gateway-address and report prefix status"
        )

    def run(self, data, commit):
        start_time = time.time()

        # Get all IP addresses with the gateway-address tag
        gateway_ips = IPAddress.objects.filter(tags__name="gateway-address")

        if not gateway_ips:
            self.log_warning("No IP addresses found with gateway-address tag")
            return

        total_count = gateway_ips.count()
        self.log_info(f"Found {total_count} gateway IP(s)")
        self.log_info("=" * 50)

        # Track statistics
        processed = 0
        up = 0
        down = 0
        errors = 0

        # Process each gateway IP
        for ip in gateway_ips:
            processed += 1

            # Progress update every 50 IPs
            if processed % 50 == 0:
                elapsed = time.time() - start_time
                self.log_info(
                    f"Progress: {processed}/{total_count} ({processed * 100 // total_count}%) - Elapsed: {elapsed:.1f}s"
                )

            ip_address = str(ip.address).split("/")[0]  # Remove CIDR notation

            # Perform ping with strict timeout
            try:
                is_alive = self.ping_host(ip_address)

                # Get associated prefix
                prefix = (
                    ip.assigned_object.parent
                    if hasattr(ip.assigned_object, "parent")
                    else None
                )
                prefix_info = f" (Prefix: {prefix})" if prefix else ""

                if is_alive:
                    up += 1
                    self.log_success(f"✓ {ip.address}{prefix_info} - UP")
                else:
                    down += 1
                    self.log_failure(f"✗ {ip.address}{prefix_info} - DOWN")
            except Exception as e:
                errors += 1
                self.log_warning(f"⚠ {ip.address} - ERROR: {str(e)}")

        # Summary
        elapsed = time.time() - start_time
        self.log_info("=" * 50)
        self.log_info(
            f"Summary: {processed} total, {up} up, {down} down, {errors} errors"
        )
        self.log_info(
            f"Completed in {elapsed:.1f} seconds ({elapsed / processed:.2f}s per IP)"
        )

    def ping_host(self, host):
        """
        Ping a host and return True if it responds, False otherwise
        """
        try:
            # Send 1 ping with 1 second timeout, privileged=False for unprivileged ICMP
            response = icmp_ping(host, count=1, timeout=1, privileged=False)
            # Check if host is reachable
            return response.is_alive
        except PermissionError as e:
            raise Exception(
                f"Permission denied - run: sudo setcap cap_net_raw+ep /opt/netbox/venv/bin/python3"
            )
        except Exception as e:
            # For any other error, log and treat as down
            raise Exception(f"Ping failed: {str(e)}")
