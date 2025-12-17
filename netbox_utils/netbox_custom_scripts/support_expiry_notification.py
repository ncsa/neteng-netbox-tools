from datetime import date, datetime

from dcim.models import Device
from django.core.mail import send_mail
from django.utils import timezone
from extras.scripts import Script, StringVar

# Notify exactly on these day offsets
NOTIFY_DAYS = {180, 90, 60, 30, 15, 10, 5, 4, 3, 2, 1, 0}


class SupportExpiryNotifier(Script):
    class Meta:
        name = "Support/Warranty Expiry Notifier (email reminders)"
        description = (
            "Emails a summary of devices whose support_end_date custom field is exactly "
            "90/60/30/15/10/5/4/3/2/1 days away (uses local date)."
        )
        # Commit ON actually sends the email; OFF = preview only
        commit_default = False

    recipient_email = StringVar(
        description="Where to send reminders",
        default="hostmaster@domain.com",
        required=True,
    )
    from_email = StringVar(
        description="Envelope/From sender address",
        default="hostmaster@domain.com",
        required=True,
    )

    def _coerce_date(self, v) -> date | None:
        """
        Best-effort conversion of various date-ish values to a date.
        Handles datetime.date, and several common string formats.
        """
        if v is None:
            return None
        if isinstance(v, date) and not isinstance(v, datetime):
            return v
        # Sometimes NetBox gives strings for custom fields
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
                try:
                    return datetime.strptime(v, fmt).date()
                except Exception:
                    continue
        return None

    def _get_support_end_date_raw(self, device: Device):
        """
        Pull the raw support_end_date value from the custom field.
        Works with NetBox's custom_field_data / cf dicts.
        """
        # NetBox 3.x/4.x usually exposes JSON as custom_field_data, plus a .cf helper
        try:
            cf_data = getattr(device, "custom_field_data", None) or {}
            if "support_end_date" in cf_data:
                return cf_data.get("support_end_date")
        except Exception:
            pass

        # Fallback: some versions expose .cf as a dict-like helper
        try:
            cf = getattr(device, "cf", None)
            if cf and "support_end_date" in cf:
                return cf.get("support_end_date")
        except Exception:
            pass

        return None

    def run(self, data, commit):
        recipient = data["recipient_email"].strip()
        sender = data["from_email"].strip()

        today = timezone.localdate()  # NetBox-local date

        # Start with devices that *appear* to have a support_end_date custom field set.
        # JSONField lookup: custom_field_data__support_end_date__isnull=False
        qs = Device.objects.exclude(custom_field_data__support_end_date__isnull=True)

        due = []
        scanned = 0

        for dev in qs:
            raw = self._get_support_end_date_raw(dev)
            if not raw:
                # In case the JSON lookup still caught junk/empty values
                self.log_info(f"[{dev.id}] {dev.name}: no support_end_date; skipping.")
                continue

            exp = self._coerce_date(raw)
            if not exp:
                self.log_warning(
                    f"[{dev.id}] {dev.name}: invalid support_end_date ({raw!r}); skipping."
                )
                continue

            days_left = (exp - today).days
            scanned += 1
            self.log_info(
                f"[{dev.id}] {dev.name}: support/warranty ends {exp.isoformat()} "
                f"({days_left} days remaining)"
            )

            if days_left in NOTIFY_DAYS and days_left >= 0:
                due.append((dev, days_left))

        if not due:
            self.log_info(
                f"No devices due for support/warranty notification today. Scanned {scanned} device(s)."
            )
            return

        # Sort for tidy email
        due.sort(key=lambda t: (t[1], (t[0].name or "").lower()))
        thresholds_today = sorted({d for _, d in due})
        thresholds_str = "/".join(str(d) for d in thresholds_today)
        subject = (
            f"[NetBox] Support/Warranty expirations coming due ({thresholds_str}d)"
        )

        lines = [
            f"Date: {today.isoformat()}",
            "The following devices are approaching support/warranty end:\n",
        ]

        for dev, dleft in due:
            site = getattr(dev.site, "name", "") if getattr(dev, "site", None) else ""
            loc = (
                getattr(dev.location, "name", "")
                if getattr(dev, "location", None)
                else ""
            )
            rack = getattr(dev.rack, "name", "") if getattr(dev, "rack", None) else ""
            role = getattr(dev.role, "name", "") if getattr(dev, "role", None) else ""
            platform = (
                getattr(dev.platform, "name", "")
                if getattr(dev, "platform", None)
                else ""
            )
            serial = (dev.serial or "").strip()
            asset_tag = (dev.asset_tag or "").strip()
            primary_ip = (
                getattr(dev.primary_ip, "address", "")
                if getattr(dev, "primary_ip", None)
                else ""
            )

            parts = [
                f"- {dev.name}",
                f"support_end_date: {self._get_support_end_date_raw(dev)}",
                f"({dleft} day{'s' if dleft != 1 else ''} remaining)",
            ]

            if serial:
                parts.append(f"serial: {serial}")
            if asset_tag:
                parts.append(f"asset: {asset_tag}")
            if loc:
                parts.append(f"location: {loc}")
            if rack:
                parts.append(f"rack: {rack}")
            if platform:
                parts.append(f"platform: {platform}")

            lines.append("  |  ".join(parts))

        body = "\n".join(lines) + "\n\n— NetBox Support/Warranty Expiry Notifier"

        if not commit:
            self.log_success("Commit is OFF — preview only. Email would be:")
            self.log_info(
                f"From: {sender}\nTo: {recipient}\nSubject: {subject}\n\n{body}"
            )
            return

        try:
            sent = send_mail(
                subject=subject,
                message=body,
                from_email=sender,  # explicit envelope sender (no @localhost)
                recipient_list=[recipient],
                fail_silently=False,
            )
            if sent:
                self.log_success(
                    f"Email sent to {recipient} (from {sender}) covering {len(due)} device(s)."
                )
            else:
                self.log_failure(
                    "send_mail returned 0 (not sent). Check email backend config/logs."
                )
        except Exception as e:
            self.log_failure(f"Email send failed: {e!r}")
