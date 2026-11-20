"""Normalize NITRO responses from one HA pair's Primary into plain dataclasses."""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .nitro import NitroClient, NitroError, find_primary

LBVSERVER = "config/lbvserver"
SG_MEMBER_BY_VSERVER = "config/lbvserver_servicegroupmember_binding?bulkbindings=yes"
SG_MEMBER = "config/servicegroup_servicegroupmember_binding?bulkbindings=yes"
SERVICE_BY_VSERVER = "config/lbvserver_service_binding?bulkbindings=yes"
GSLBVSERVER = "config/gslbvserver"
GSLB_DOMAIN = "config/gslbvserver_domain_binding?bulkbindings=yes"
GSLB_SERVICE = "config/gslbvserver_gslbservice_binding?bulkbindings=yes"

ENDPOINTS = (LBVSERVER, SG_MEMBER_BY_VSERVER, SG_MEMBER, SERVICE_BY_VSERVER, GSLBVSERVER, GSLB_DOMAIN, GSLB_SERVICE)


@dataclass
class Member:
    servicegroup: str
    ip: str
    port: int
    state: str
    servername: str


@dataclass
class Vserver:
    pair: str
    name: str
    ip: str
    port: int
    protocol: str
    state: str
    members: list[Member] = field(default_factory=list)


@dataclass
class GslbName:
    pair: str
    vserver: str
    domains: list[str]
    state: str
    service_targets: list[tuple[str, int]]


@dataclass
class PairInventory:
    name: str
    available: bool
    node: str | None
    error: str | None
    fetched_at: datetime
    vservers: list[Vserver] = field(default_factory=list)
    gslb: list[GslbName] = field(default_factory=list)


def _rows(resp: dict, key: str) -> list[dict]:
    """NITRO omits the list key entirely when there are no rows."""
    return resp.get(key, []) or []


def _port(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_inventory(pair_name: str, node: str, raw: dict[str, dict]) -> PairInventory:
    """Build one pair's inventory from the raw responses keyed by ENDPOINTS path."""
    servernames = {
        (r.get("servicegroupname"), r.get("ip"), _port(r.get("port"))): r.get("servername", "")
        for r in _rows(raw[SG_MEMBER], "servicegroup_servicegroupmember_binding")
    }

    vservers = {}
    for r in _rows(raw[LBVSERVER], "lbvserver"):
        vservers[r["name"]] = Vserver(
            pair=pair_name,
            name=r["name"],
            ip=r.get("ipv46", ""),
            port=_port(r.get("port")),
            protocol=r.get("servicetype", ""),
            state=r.get("curstate", ""),
        )

    for r in _rows(raw[SG_MEMBER_BY_VSERVER], "lbvserver_servicegroupmember_binding"):
        vserver = vservers.get(r.get("name"))
        if vserver is None:
            continue
        sg, ip, port = r.get("servicegroupname", ""), r.get("ipv46", ""), _port(r.get("port"))
        vserver.members.append(
            Member(servicegroup=sg, ip=ip, port=port, state=r.get("curstate", ""), servername=servernames.get((sg, ip, port), ""))
        )

    for r in _rows(raw[SERVICE_BY_VSERVER], "lbvserver_service_binding"):
        vserver = vservers.get(r.get("name"))
        if vserver is None:
            continue
        vserver.members.append(
            Member(
                servicegroup="",
                ip=r.get("ipv46", ""),
                port=_port(r.get("port")),
                state=r.get("curstate", ""),
                servername=r.get("servicename", ""),
            )
        )

    domains: dict[str, list[str]] = {}
    for r in _rows(raw[GSLB_DOMAIN], "gslbvserver_domain_binding"):
        domains.setdefault(r.get("name"), []).append(r.get("domainname", ""))
    targets: dict[str, list[tuple[str, int]]] = {}
    for r in _rows(raw[GSLB_SERVICE], "gslbvserver_gslbservice_binding"):
        targets.setdefault(r.get("name"), []).append((r.get("ipaddress", ""), _port(r.get("port"))))

    gslb = [
        GslbName(
            pair=pair_name,
            vserver=r["name"],
            domains=domains.get(r["name"], []),
            state=r.get("curstate", ""),
            service_targets=targets.get(r["name"], []),
        )
        for r in _rows(raw[GSLBVSERVER], "gslbvserver")
    ]

    return PairInventory(
        name=pair_name,
        available=True,
        node=node,
        error=None,
        fetched_at=datetime.now(timezone.utc),
        vservers=list(vservers.values()),
        gslb=gslb,
    )


def fetch_pair(pair: dict, defaults: dict, client_factory=NitroClient) -> PairInventory:
    """Find the pair's Primary and pull its inventory. Never raises on NetScaler errors."""
    name = pair.get("name", ", ".join(pair.get("nodes", [])))

    def make(host):
        return client_factory(
            host,
            pair.get("username") or defaults.get("username", ""),
            pair.get("password") or defaults.get("password", ""),
            defaults.get("timeout", 15),
            defaults.get("verify_ssl", False),
        )

    try:
        primary, error = find_primary(pair.get("nodes", []), make)
        if primary is None:
            raise NitroError(error)
        client = make(primary)
        raw = {path: client.get(path) for path in ENDPOINTS}
    except NitroError as exc:
        return PairInventory(
            name=name, available=False, node=None, error=f"{name}: {exc}", fetched_at=datetime.now(timezone.utc)
        )
    return build_inventory(name, primary, raw)
