"""Apply a VIP sync plan to NetBox IPAM. NetScaler is never written to."""

from functools import reduce
from operator import or_

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from extras.models import JournalEntry
from ipam.models import IPAddress
from tenancy.models import Tenant
from virtualization.models import VirtualMachine, VMInterface

from .cache import get_inventories, plugin_settings
from .sync import NbIp, SyncPlan, Target, fingerprint, plan_sync


def _nbip(address: IPAddress, vmi_type_id: int) -> NbIp:
    return NbIp(
        pk=address.pk,
        host=str(address.address.ip),
        address=str(address.address),
        role=address.role or "",
        status=address.status,
        description=address.description or "",
        dns_name=address.dns_name or "",
        interface_id=address.assigned_object_id if address.assigned_object_type_id == vmi_type_id else None,
    )


def load_records(hosts: set[str]) -> list[NbIp]:
    """Every IPAddress at the given hosts, plus every role=vip record (for orphan detection)."""
    vmi_type_id = ContentType.objects.get_for_model(VMInterface).pk
    query = Q(role="vip")
    if hosts:
        query |= reduce(or_, (Q(address__net_host=h) for h in hosts))
    return [_nbip(a, vmi_type_id) for a in IPAddress.objects.filter(query)]


def load_targets(inventories) -> dict[str, Target]:
    """Per pair: the NetBox VM named after the current Primary's short hostname, and its first interface."""
    targets = {}
    for inv in inventories:
        if not inv.available or not inv.node:
            continue
        vm = VirtualMachine.objects.filter(name__iexact=inv.node.split(".")[0]).first()
        iface = VMInterface.objects.filter(virtual_machine=vm).order_by("pk").first() if vm else None
        targets[inv.name] = Target(interface_id=iface.pk if iface else None, vm_name=vm.name if vm else None)
    return targets


def build_plan(inventories=None) -> SyncPlan:
    inventories = get_inventories() if inventories is None else inventories
    hosts = {v.ip for inv in inventories if inv.available for v in inv.vservers if v.ip and v.ip != "0.0.0.0"}
    return plan_sync(inventories, load_records(hosts), load_targets(inventories))


def plan_rows(plan: SyncPlan) -> dict[str, object]:
    rows = {f"create:{d.host}": d for d in plan.creates}
    rows.update({f"update:{c.ip.pk}": c for c in plan.updates})
    rows.update({f"orphan:{ip.pk}": ip for ip in plan.orphans})
    return rows


def _journal(address: IPAddress, user, text: str) -> None:
    JournalEntry.objects.create(assigned_object=address, created_by=user, kind="info", comments=text)


def apply(plan: SyncPlan, selected: set[str], fingerprints: dict[str, str], user) -> dict:
    """Apply the selected rows of a freshly built plan. Rows changed since the preview are skipped."""
    result = {"created": [], "updated": [], "deprecated": [], "skipped": []}
    rows = plan_rows(plan)
    slug = plugin_settings().get("vip_tenant") or ""
    tenant = Tenant.objects.filter(slug=slug).first() if slug else None

    with transaction.atomic():
        for key in sorted(selected):
            row = rows.get(key)
            if row is None:
                result["skipped"].append((key, "no longer in the plan (changed since preview)"))
                continue
            kind = key.split(":", 1)[0]

            if kind == "create":
                if IPAddress.objects.filter(address__net_host=row.host).exists():
                    result["skipped"].append((key, "a record now exists"))
                    continue
                address = IPAddress(
                    address=f"{row.host}/32",
                    status=row.status,
                    role="vip",
                    description=row.description,
                    dns_name=row.dns_name,
                    tenant=tenant,
                )
                if row.interface_id:
                    address.assigned_object = VMInterface.objects.get(pk=row.interface_id)
                address.full_clean()
                address.save()
                _journal(address, user, f"Created from NetScaler {row.pair} ({row.node}) vserver {row.vserver}")
                result["created"].append(str(address))
                continue

            if fingerprints.get(key) != fingerprint(row.ip if kind == "update" else row):
                result["skipped"].append((key, "record changed since preview"))
                continue
            address = IPAddress.objects.get(pk=(row.ip if kind == "update" else row).pk)
            address.snapshot()

            if kind == "update":
                for name, (_, new) in row.fields.items():
                    if name == "interface_id":
                        address.assigned_object = VMInterface.objects.get(pk=new)
                    else:
                        setattr(address, name, new)
                address.full_clean()
                address.save()
                changed = ", ".join(row.fields)
                _journal(address, user, f"Synced from NetScaler {row.desired.pair} ({row.desired.node}): {changed}")
                result["updated"].append(str(address))
            elif kind == "orphan":
                address.status = "deprecated"
                address.full_clean()
                address.save()
                _journal(address, user, "Deprecated: VIP not found on any configured NetScaler pair")
                result["deprecated"].append(str(address))
    return result
