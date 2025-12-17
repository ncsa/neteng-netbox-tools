#!/usr/bin/env python3
from django.contrib.contenttypes.models import ContentType
from django.utils.text import slugify
from extras.scripts import Script, StringVar
from tenancy.models import Tenant
from users.models import Group, ObjectPermission  # <-- NetBox's Group


class CreateOrgGroupAndPerms(Script):
    """
    Create or update a Tenant and Group for an org (e.g., org_IRST),
    attach three tenant-scoped ObjectPermissions, and also
    add that group to the global "All Groups Views" permission.
    """

    org_name = StringVar(
        description='Organization key (e.g., "org_IRST"). Used for Tenant & Group name and constraints.',
        required=True,
    )
    group_description = StringVar(
        description="Optional description (applied to the Tenant only).",
        required=False,
    )

    class Meta:
        name = "Create Tenant + Group + Scoped Permissions for Org"
        description = "Creates/updates Tenant, Group, scoped permissions, and attaches group to All Groups Views."
        commit_default = True

    # ----------------- helpers -----------------

    def _get_ct(self, app_label: str, model: str) -> ContentType:
        return ContentType.objects.get(app_label=app_label, model=model)

    def _ensure_tenant(
        self, name: str, description: str | None, commit: bool
    ) -> Tenant:
        try:
            tenant = Tenant.objects.get(name=name)
            changed = False
            if description is not None and (tenant.description or "") != description:
                changed = True
                if commit:
                    tenant.description = description
            if changed and commit:
                tenant.save()
                self.log_success(f'Updated tenant "{name}".')
            elif changed:
                self.log_info(f'[DRY-RUN] Would update tenant "{name}".')
            else:
                self.log_info(f'Tenant "{name}" already exists; no changes needed.')
            return tenant
        except Tenant.DoesNotExist:
            if commit:
                tenant = Tenant.objects.create(
                    name=name, slug=slugify(name), description=description or ""
                )
                self.log_success(f'Created tenant "{name}".')
                return tenant
            else:
                self.log_info(f'[DRY-RUN] Would create tenant "{name}".')
                return Tenant(
                    name=name, slug=slugify(name), description=description or ""
                )

    def _ensure_group(self, name: str, commit: bool) -> Group:
        try:
            grp = Group.objects.get(name=name)
            self.log_info(f'Group "{name}" already exists; no changes needed.')
            return grp
        except Group.DoesNotExist:
            if commit:
                grp = Group.objects.create(name=name)
                self.log_success(f'Created group "{name}".')
                return grp
            else:
                self.log_info(f'[DRY-RUN] Would create group "{name}".')
                return Group(name=name)

    def _ensure_permission(
        self,
        name: str,
        description: str,
        actions: list[str],
        content_types: list[ContentType],
        constraints: dict,
        attach_group: Group,
        commit: bool,
    ) -> ObjectPermission:
        desired_ct_ids = [ct.pk for ct in content_types]
        try:
            perm = ObjectPermission.objects.get(name=name)
            changed = False

            if not perm.enabled:
                changed = True
                if commit:
                    perm.enabled = True
            if perm.description != description:
                changed = True
                if commit:
                    perm.description = description
            if sorted(perm.actions or []) != sorted(actions):
                changed = True
                if commit:
                    perm.actions = actions
            if (perm.constraints or {}) != (constraints or {}):
                changed = True
                if commit:
                    perm.constraints = constraints

            if changed and commit:
                perm.save()
                self.log_success(f'Updated permission "{name}".')
            elif changed:
                self.log_info(f'[DRY-RUN] Would update permission "{name}".')
            else:
                self.log_info(f'Permission "{name}" OK.')

            # Update object types if needed
            current_ct_ids = set(perm.object_types.values_list("id", flat=True))
            if current_ct_ids != set(desired_ct_ids):
                if commit:
                    perm.object_types.set(desired_ct_ids)
                    self.log_success(f'Updated object_types for "{name}".')
                else:
                    self.log_info(f'[DRY-RUN] Would set object_types for "{name}".')

            # Attach group
            if attach_group.pk:
                if not perm.groups.filter(pk=attach_group.pk).exists():
                    if commit:
                        perm.groups.add(attach_group)
                        self.log_success(
                            f'Attached group "{attach_group.name}" to "{name}".'
                        )
                    else:
                        self.log_info(
                            f'[DRY-RUN] Would attach group "{attach_group.name}" to "{name}".'
                        )
            else:
                self.log_info(
                    f'[DRY-RUN] Would attach group "{attach_group.name}" to "{name}".'
                )

            return perm

        except ObjectPermission.DoesNotExist:
            if commit:
                perm = ObjectPermission.objects.create(
                    name=name,
                    description=description,
                    enabled=True,
                    actions=actions,
                    constraints=constraints or {},
                )
                perm.object_types.set(desired_ct_ids)
                perm.groups.add(attach_group)
                self.log_success(
                    f'Created permission "{name}" and attached group "{attach_group.name}".'
                )
                return perm
            else:
                self.log_info(f'[DRY-RUN] Would create permission "{name}".')
                return ObjectPermission(name=name)

    # ----------------- main -----------------

    def run(self, data, commit):
        org = data["org_name"].strip()
        desc = (data.get("group_description") or "").strip()
        if not org:
            self.log_failure("org_name cannot be empty.")
            return

        self.log_info(
            f'Preparing Tenant, Group & Permissions for "{org}" (commit={commit})'
        )

        tenant = self._ensure_tenant(name=org, description=desc, commit=commit)
        group = self._ensure_group(name=org, commit=commit)

        ct_ipaddress = self._get_ct("ipam", "ipaddress")
        ct_prefix = self._get_ct("ipam", "prefix")
        ct_vlan = self._get_ct("ipam", "vlan")
        ct_dns_record = self._get_ct("netbox_dns", "record")
        constraints = {"tenant__name": org}

        self._ensure_permission(
            f"{org} - Manage IP Addresses",
            f"{org} - Manage IP Addresses",
            ["view", "add", "change", "delete"],
            [ct_ipaddress],
            constraints,
            group,
            commit,
        )

        self._ensure_permission(
            f"{org} - Update Unmanaged Records",
            f"{org} - Update Unmanaged Records",
            ["view", "change"],
            [ct_dns_record],
            constraints,
            group,
            commit,
        )

        self._ensure_permission(
            f"{org} - View Prefixes and Vlans",
            f"{org} - View Prefixes and Vlans",
            ["view"],
            [ct_prefix, ct_vlan],
            constraints,
            group,
            commit,
        )

        # --- NEW TASK: add this group to "All Groups Views" permission ---
        try:
            all_views_perm = ObjectPermission.objects.get(name="1 All Groups Views")
        except ObjectPermission.DoesNotExist:
            self.log_failure(
                'Permission "All Groups Views" not found (expected existing global view permission).'
            )
            return

        if not all_views_perm.groups.filter(name=group.name).exists():
            if commit:
                all_views_perm.groups.add(group)
                self.log_success(
                    f'Added group "{group.name}" to "1 All Groups Views" permission.'
                )
            else:
                self.log_info(
                    f'[DRY-RUN] Would add group "{group.name}" to "1All Groups Views" permission.'
                )
        else:
            self.log_info(
                f'Group "{group.name}" already in "1All Groups Views"; no changes needed.'
            )

        self.log_success("Completed." if commit else "Completed (dry run).")
