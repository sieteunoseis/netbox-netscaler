"""NetBox lookups: member IP -> VM/Device, and object -> its IPs."""

from ipam.models import IPAddress

from .matching import Match


def _host(address) -> str:
    return str(address).split("/")[0]


def resolve_ip(ip: str) -> list[Match]:
    """Every NetBox IPAddress with this host part (any mask, any VRF), as Matches."""
    matches = []
    qs = IPAddress.objects.filter(address__net_host=ip).prefetch_related("assigned_object")
    for address in qs:
        assigned = address.assigned_object
        parent = getattr(assigned, "virtual_machine", None) or getattr(assigned, "device", None)
        if parent is None:
            matches.append(
                Match(
                    obj_key=f"ip:{address.pk}",
                    obj_name=str(address),
                    obj_url=address.get_absolute_url(),
                    status=address.status,
                    ip_dns_name=address.dns_name or "",
                )
            )
            continue
        kind = "vm" if hasattr(assigned, "virtual_machine") else "device"
        matches.append(
            Match(
                obj_key=f"{kind}:{parent.pk}",
                obj_name=parent.name or str(parent),
                obj_url=parent.get_absolute_url(),
                status=parent.status,
                ip_dns_name=address.dns_name or "",
            )
        )
    return matches


def object_key(obj) -> str:
    return f"vm:{obj.pk}" if obj._meta.model_name == "virtualmachine" else f"device:{obj.pk}"


def ips_for_object(obj) -> set[str]:
    """Host part of every IPAddress on any of the object's interfaces."""
    if obj._meta.model_name == "virtualmachine":
        qs = IPAddress.objects.filter(vminterface__virtual_machine=obj)
    else:
        qs = IPAddress.objects.filter(interface__device=obj)
    return {_host(a.address) for a in qs.only("address")}
