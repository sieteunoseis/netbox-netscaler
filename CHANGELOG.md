# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

### Added
- NetScaler tab on VirtualMachine and Device pages listing every lb VIP where the object is a service-group member, with member state, GSLB names, and Last live member / Name mismatch / Ambiguous IP flags.
- Stale VIPs page listing VIPs with no live server behind them.
- Automatic HA Primary discovery per pair (reads only from the Primary).
- Per-pair inventory cache with a Refresh button; page loads never wait on NetScaler.
- `netbox_netscaler.api.memberships_for(obj)` reuse hook.
- NetScaler tab on IP address pages: VIP view (vservers, members, GSLB) and member view.
- Sync VIPs page: preview and apply creates (missing UP VIPs), updates (status, description, attachment to the current Primary, blank DNS filled from GSLB) and deprecation of orphaned VIP records. NetBox IPAM only; replaces the `netscaler_vip_sync.py` script.
