# netbox-netscaler

Read-only NetBox plugin that shows which Citrix NetScaler (ADC) VIPs depend on a server, so that
decommissions stop missing the load balancer.

- **NetScaler tab** on every VM and Device with an IP: each lb VIP where the server is a
  service-group member, across all configured HA pairs, with the member state, the GSLB names in
  front of the VIP, and flags:
  - **Last live member**: no other server behind the VIP is UP, so the VIP goes down with this one.
  - **Name mismatch**: the NetScaler server object's name matches neither the NetBox object nor the
    IP's DNS name. This usually means the NetScaler config is wrong or stale.
  - **Ambiguous IP**: more than one NetBox IP record has the member's address.
- **Stale VIPs** page (Plugins menu): every VIP with no live server behind it. That covers VIPs
  whose members are all DOWN, all on decommissioning or offline objects, or not in NetBox, as well
  as VIPs with no members at all.

- **NetScaler tab on IP address pages**: for a VIP, its vservers, members (linked to their NetBox objects)
  and GSLB names; for a member IP, the VIPs it belongs to.
- **Sync VIPs** page (Plugins menu): compares live VIPs with NetBox IPAM and lets an engineer preview, tick and
  apply changes. Run it whenever you like; there is no schedule.
  - **Create**: VIPs that are UP but have no NetBox IP record. They're created as `/32`, role VIP, attached to
    the pair's current Primary NetScaler VM.
  - **Update**: VIP records whose status, description or attachment no longer matches NetScaler. Each row shows
    old → new values. DNS names are only filled in when blank, never replaced.
  - **Orphaned**: VIP records whose address isn't on any NetScaler. The only action is setting them to
    deprecated; nothing is ever deleted. Orphans are hidden while any pair is unreachable.
  - Non-VIP records at a VIP address are listed as conflicts and never changed. Every change is recorded in the
    changelog under the user who applied it, plus a journal entry.

This replaces the `netscaler_vip_sync.py` custom script.

The plugin never changes NetScaler. It only sends NITRO `GET` requests. The only writes are to NetBox IPAM, and
only when someone applies a sync.

## How it works

1. For each configured HA pair, the plugin asks the first node that answers which node is
   **Primary** (`config/hanode`), and reads only from the Primary. UP/DOWN health state is only
   authoritative on the Primary, and the Primary changes with every failover.
2. It pulls lb vservers, service-group members and GSLB bindings using bulk NITRO calls (about
   8 requests per pair).
3. It caches each pair's inventory in NetBox's cache (default 15 minutes; a Refresh button clears
   it).
4. It matches each member IP to NetBox IP addresses, then to the VM or Device they're assigned to,
   when it renders a page.

Page loads never wait on NetScaler. The tab's visibility check and badge read the cache only.

## Permissions

- The tabs and the Stale VIPs page need view permission on the object or on IP addresses.
- Sync VIPs: previewing needs `ipam.view_ipaddress`; applying needs `ipam.add_ipaddress` to create and
  `ipam.change_ipaddress` to update or deprecate.

## Requirements

- NetBox 4.7.5 or newer
- A **read-only** NetScaler account with NITRO API access on every node

## Installation

```bash
pip install netbox-netscaler
```

Add the plugin to `configuration/plugins.py`:

```python
import os

PLUGINS = ["netbox_netscaler"]

PLUGINS_CONFIG = {
    "netbox_netscaler": {
        "username": os.environ.get("NETSCALER_API_USERNAME", ""),
        "password": os.environ.get("NETSCALER_API_PASSWORD", ""),
        "pairs": [
            {"name": "Site A", "nodes": ["ns-a1.example.com", "ns-a2.example.com"]},
            {"name": "Site B", "nodes": ["ns-b1.example.com", "ns-b2.example.com"]},
            # A standalone appliance is a one-node pair:
            # {"name": "Lab", "nodes": ["ns-lab.example.com"]},
            # Per-pair credentials override the defaults:
            # {"name": "DMZ", "nodes": [...], "username": "...", "password": "..."},
        ],
    },
}
```

Restart NetBox. No migrations are needed; the plugin has no database models.

## Settings

| Setting | Default | Description |
|---|---|---|
| `pairs` | `[]` | HA pairs: `{"name", "nodes", "username"?, "password"?}` |
| `username` / `password` | `""` | Default NITRO credentials |
| `cache_timeout` | `900` | Seconds to cache each pair's inventory. An unreachable pair is cached for at most 60 s. |
| `timeout` | `15` | NITRO request timeout in seconds |
| `verify_ssl` | `False` | Verify NetScaler TLS certificates |
| `vip_tenant` | `""` | Tenant slug set on VIP records created by Sync VIPs (blank = no tenant) |
| `tab_weight` | `9005` | Position of the NetScaler tab on object pages |
| `top_level_menu` | `False` | `True` gives the plugin its own sidebar menu instead of an entry under Plugins |

## Reuse

Other code can ask what NetScaler config depends on an object:

```python
from netbox_netscaler.api import memberships_for

for m in memberships_for(vm):
    print(m.vserver.pair, m.vserver.name, m.member.ip, m.last_live, [d for g in m.gslb for d in g.domains])
```

## Support

If you find this plugin helpful, consider supporting development:

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-support-yellow?style=flat&logo=buy-me-a-coffee)](https://buymeacoffee.com/automatebldrs)

## License

Apache-2.0
