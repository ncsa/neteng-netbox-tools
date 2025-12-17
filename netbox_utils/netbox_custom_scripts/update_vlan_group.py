# ipam_scripts/ensure_vlan_group.py

from django.db import transaction, IntegrityError
from django.core.exceptions import ValidationError
from extras.scripts import Script, StringVar, BooleanVar
from ipam.models import VLAN, VLANGroup


class EnsureAllVLANsInDefaultGroup(Script):
    """
    Ensure ALL VLANs are assigned to the VLAN Group identified by `group_slug`.
    - Dry run by default (Commit toggle controls DB writes).
    - Creates the group if missing (optional).
    - Can override existing group assignments (optional).
    - Logs conflicts (e.g., duplicate VID within target group) and errors.
    """

    class Meta:
        name = "Ensure VLANs are in default group"
        description = "Assign every VLAN to the VLAN Group with the given slug (default: 'default-vlan-group')."

    # Default to DRY RUN (Commit toggle off in the UI)
    commit_default = False

    group_slug = StringVar(
        description="Slug of the VLAN Group to enforce",
        default="default-vlan-group",
    )
    create_group_if_missing = BooleanVar(
        description="Create the VLAN Group if it does not exist",
        default=True,
    )
    override_existing = BooleanVar(
        description="Override VLANs already assigned to a different group",
        default=True,
    )

    # ---- helpers ---------------------------------------------------------------

    def _prettify_slug(self, slug: str) -> str:
        # Turn "default-vlan-group" -> "Default Vlan Group"
        name = slug.replace("-", " ").replace("_", " ").strip()
        return " ".join(word.capitalize() for word in name.split())

    def _ensure_group(self, slug: str, allow_create: bool) -> VLANGroup:
        vg = VLANGroup.objects.filter(slug=slug).first()
        if vg:
            self.log_success(f"Using existing VLAN Group: name='{vg.name}', slug='{vg.slug}'")
            return vg

        if not allow_create:
            raise RuntimeError(f"VLAN Group with slug '{slug}' does not exist and creation is disabled.")

        derived_name = self._prettify_slug(slug)
        vg = VLANGroup(name=derived_name, slug=slug)
        vg.full_clean()
        vg.save()
        self.log_success(f"Created VLAN Group: name='{vg.name}', slug='{vg.slug}' (derived from slug)")
        return vg

    # ---- main -----------------------------------------------------------------

    def run(self, data, commit):
        slug = data["group_slug"].strip()
        allow_create = data["create_group_if_missing"]
        override_existing = data["override_existing"]

        self.log_info(
            f"Target group slug='{slug}', override_existing={override_existing}, allow_create={allow_create}"
        )
        if not commit:
            self.log_warning("DRY RUN: No changes will be committed (transaction will be rolled back).")

        try:
            target_group = self._ensure_group(slug, allow_create)
        except Exception as e:
            self.log_failure(f"Unable to prepare target VLAN Group: {e}")
            return

        totals = {
            "scanned": 0,
            "already_in_group": 0,
            "updated": 0,
            "overridden": 0,
            "conflicts": 0,
            "errors": 0,
        }

        qs = VLAN.objects.select_related("group", "site").order_by("vid", "name")

        with transaction.atomic():
            for vlan in qs.iterator():
                totals["scanned"] += 1
                current_group = vlan.group

                if current_group and current_group.id == target_group.id:
                    totals["already_in_group"] += 1
                    continue

                if current_group and not override_existing:
                    self.log_info(
                        f"[SKIP] VLAN {vlan.vid} '{vlan.name}' is in group '{current_group.slug}', "
                        f"override_existing=False"
                    )
                    continue

                old_slug = current_group.slug if current_group else None
                vlan.group = target_group

                try:
                    vlan.full_clean()
                    vlan.save()
                    totals["updated"] += 1
                    if old_slug:
                        totals["overridden"] += 1
                        self.log_success(f"[MOVED] VLAN {vlan.vid} '{vlan.name}': {old_slug} -> {slug}")
                    else:
                        self.log_success(f"[SET] VLAN {vlan.vid} '{vlan.name}': group=None -> {slug}")
                except ValidationError as ve:
                    totals["conflicts"] += 1
                    self.log_failure(
                        f"[CONFLICT] VLAN {vlan.vid} '{vlan.name}' could not be moved to '{slug}': "
                        f"{ve.message_dict or ve}"
                    )
                except IntegrityError as ie:
                    totals["conflicts"] += 1
                    self.log_failure(
                        f"[CONFLICT] VLAN {vlan.vid} '{vlan.name}' DB constraint blocked move to '{slug}': {ie}"
                    )
                except Exception as e:
                    totals["errors"] += 1
                    self.log_failure(f"[ERROR] VLAN {vlan.vid} '{vlan.name}' move failed: {e}")

            if not commit:
                transaction.set_rollback(True)

        # Summary
        self.log_info("======== SUMMARY ========")
        self.log_info(f"Scanned:           {totals['scanned']}")
        self.log_info(f"Already in group:  {totals['already_in_group']}")
        self.log_info(f"Updated:           {totals['updated']}")
        self.log_info(f"Overridden:        {totals['overridden']}")
        self.log_info(f"Conflicts:         {totals['conflicts']}")
        self.log_info(f"Errors:            {totals['errors']}")
        if totals["conflicts"] > 0:
            self.log_warning(
                "Conflicts usually mean duplicate VLAN IDs already exist in the target group. "
                "You may need per-site/per-scope groups or to consolidate duplicates."
            )
