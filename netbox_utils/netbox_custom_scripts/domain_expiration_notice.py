from datetime import date, datetime

from django.core.mail import send_mail
from django.utils import timezone
from extras.scripts import Script, StringVar
from netbox_dns.models import Zone

# Notify exactly on these day offsets
NOTIFY_DAYS = {90, 60, 30, 15, 10, 5, 4, 3, 2, 1}


class DomainExpiryNotifier(Script):
    class Meta:
        name = "Domain Expiry Notifier (email reminders)"
        description = (
            "Emails a summary of zones whose expiration_date is exactly "
            "90/60/30/15/10/5/4/3/2/1 days away (uses local date)."
        )
        commit_default = False  # Commit ON actually sends the email

    recipient_email = StringVar(
        description="Where to send reminders",
        default="hostmaster@domain.com",
        required=True,
    )
    from_email = StringVar(
        description="Envelope/From sender address",
        default="netbox@domain.com",
        required=True,
    )
    registrar_name = StringVar(
        description="Optional: filter by registrar display name (blank = all registrars)",
        default="",
        required=False,
    )

    def _coerce_date(self, v) -> date | None:
        if v is None:
            return None
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
                try:
                    return datetime.strptime(v, fmt).date()
                except Exception:
                    pass
        return None

    def run(self, data, commit):
        recipient = data["recipient_email"].strip()
        sender = data["from_email"].strip()
        registrar = (data.get("registrar_name") or "").strip()

        today = timezone.localdate()  # NetBox-local date

        qs = Zone.objects.exclude(expiration_date__isnull=True)
        if registrar:
            qs = qs.filter(registrar__name__iexact=registrar)

        due = []
        scanned = 0

        for z in qs:
            exp = self._coerce_date(z.expiration_date)
            if not exp:
                self.log_warning(
                    f"[{z.id}] {z.name}: invalid expiration_date ({z.expiration_date!r}); skipping."
                )
                continue

            days_left = (exp - today).days
            scanned += 1
            self.log_info(
                f"[{z.id}] {z.name}: expires {exp.isoformat()} ({days_left} days remaining)"
            )

            if days_left in NOTIFY_DAYS and days_left >= 0:
                due.append((z, days_left))

        if not due:
            self.log_info(
                f"No domains due for notification today. Scanned {scanned} zones."
            )
            return

        # Sort for tidy email
        due.sort(key=lambda t: (t[1], t[0].name.lower()))
        thresholds_today = sorted({d for _, d in due})
        subject = f"[NetBox] Domain expirations due today ({'/'.join(str(d) for d in thresholds_today)}d)"

        lines = [
            f"Date: {today.isoformat()}",
            "The following domains are approaching expiration:\n",
        ]
        for zone, dleft in due:
            reg = (
                getattr(zone.registrar, "name", "")
                if getattr(zone, "registrar", None)
                else ""
            )
            status = (zone.domain_status or "").strip()
            lines.append(
                f"- {zone.name}  |  expires {zone.expiration_date}  "
                f"({dleft} day{'s' if dleft != 1 else ''} remaining)"
                + (f"  |  registrar: {reg}" if reg else "")
                + (f"  |  status: {status}" if status else "")
            )
        body = "\n".join(lines) + "\n\n— NetBox Domain Expiry Notifier"

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
                from_email=sender,  # <-- explicit envelope sender (no @localhost)
                recipient_list=[recipient],
                fail_silently=False,
            )
            if sent:
                self.log_success(
                    f"Email sent to {recipient} (from {sender}) covering {len(due)} domain(s)."
                )
            else:
                self.log_failure(
                    "send_mail returned 0 (not sent). Check email backend config/logs."
                )
        except Exception as e:
            self.log_failure(f"Email send failed: {e!r}")
