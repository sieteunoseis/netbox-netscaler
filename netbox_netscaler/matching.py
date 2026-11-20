"""Pure matching rules: which VIPs depend on a server, and which VIPs are stale.

NetBox lookups are injected as a Resolver so this module stays testable without Django.
"""

from dataclasses import dataclass, field
from typing import Callable

from .inventory import GslbName, Member, PairInventory, Vserver

RETIRED_STATUSES = {"decommissioning", "offline"}


@dataclass
class Match:
    obj_key: str  # "vm:<id>", "device:<id>", or "ip:<id>" for an unassigned IP
    obj_name: str
    obj_url: str
    status: str
    ip_dns_name: str


Resolver = Callable[[str], list[Match]]


@dataclass
class Membership:
    vserver: Vserver
    member: Member
    matches: list[Match]
    ambiguous: bool
    name_mismatch: bool
    last_live: bool
    gslb: list[GslbName] = field(default_factory=list)


@dataclass
class StaleRow:
    vserver: Vserver
    reason: str
    members: list[tuple[Member, list[Match]]]
    gslb: list[GslbName]
    name_mismatch: bool


def _short(name: str) -> str:
    return (name or "").split(".")[0].lower()


def is_name_mismatch(servername: str, matches: list[Match]) -> bool:
    """True when the NetScaler server name matches neither the NetBox object nor the IP's DNS name."""
    if not servername or not matches:
        return False
    short = _short(servername)
    return all(short != m.obj_name.lower() and short != _short(m.ip_dns_name) for m in matches)


def gslb_for(vserver: Vserver, inventories: list[PairInventory]) -> list[GslbName]:
    """GSLB vservers (on any pair) whose services point at this lb vserver's VIP:port.

    GSLB config is synced to every site, so the same vserver/domains on several pairs is listed once.
    """
    target = (vserver.ip, vserver.port)
    seen, result = set(), []
    for inv in inventories:
        if not inv.available:
            continue
        for g in inv.gslb:
            key = (g.vserver, tuple(g.domains))
            if target in g.service_targets and key not in seen:
                seen.add(key)
                result.append(g)
    return result


def _is_last_live(vserver: Vserver, obj_key: str, resolve: Resolver) -> bool:
    for member in vserver.members:
        if member.state != "UP":
            continue
        if obj_key not in {m.obj_key for m in resolve(member.ip)}:
            return False
    return True


def memberships_for_ips(
    ips: set[str], obj_key: str, inventories: list[PairInventory], resolve: Resolver
) -> list[Membership]:
    """Every service-group member, on every available pair, whose IP is one of the object's IPs."""
    result = []
    for inv in inventories:
        if not inv.available:
            continue
        for vserver in inv.vservers:
            mine = [member for member in vserver.members if member.ip in ips]
            if not mine:
                continue
            last_live = _is_last_live(vserver, obj_key, resolve)
            gslb = gslb_for(vserver, inventories)
            for member in mine:
                matches = resolve(member.ip)
                result.append(
                    Membership(
                        vserver=vserver,
                        member=member,
                        matches=matches,
                        ambiguous=len(matches) > 1,
                        name_mismatch=is_name_mismatch(member.servername, matches),
                        last_live=last_live,
                        gslb=gslb,
                    )
                )
    return result


def _stale_reason(resolved: list[tuple[Member, list[Match]]]) -> str | None:
    if not resolved:
        return "no members"

    def retired(matches):
        return bool(matches) and all(m.status in RETIRED_STATUSES for m in matches)

    if not all(member.state != "UP" or retired(matches) or not matches for member, matches in resolved):
        return None
    if all(member.state != "UP" for member, _ in resolved):
        return "all members down"
    if all(retired(matches) for _, matches in resolved):
        return "members retired"
    return "member not in NetBox"


def stale_vservers(inventories: list[PairInventory], resolve: Resolver) -> list[StaleRow]:
    """lb vservers where no member is a live server: DOWN, retired in NetBox, or unknown to NetBox."""
    rows = []
    for inv in inventories:
        if not inv.available:
            continue
        for vserver in inv.vservers:
            resolved = [(member, resolve(member.ip)) for member in vserver.members]
            reason = _stale_reason(resolved)
            if reason is None:
                continue
            rows.append(
                StaleRow(
                    vserver=vserver,
                    reason=reason,
                    members=resolved,
                    gslb=gslb_for(vserver, inventories),
                    name_mismatch=any(is_name_mismatch(m.servername, matches) for m, matches in resolved),
                )
            )
    return sorted(rows, key=lambda r: (r.vserver.pair, r.vserver.name))
