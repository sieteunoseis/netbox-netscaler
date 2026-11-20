"""Reuse hook: what NetScaler config depends on a NetBox VM or Device."""

from functools import lru_cache

from .cache import get_inventories
from .matching import Membership, memberships_for_ips
from .resolve import ips_for_object, object_key, resolve_ip


def memberships_for(obj, inventories=None) -> list[Membership]:
    """Every lb vserver membership (with GSLB names) for one VirtualMachine or Device."""
    ips = ips_for_object(obj)
    if not ips:
        return []
    inventories = get_inventories() if inventories is None else inventories
    resolve = lru_cache(maxsize=None)(resolve_ip)  # per call, so NetBox status changes show up
    return memberships_for_ips(ips, object_key(obj), inventories, resolve)
