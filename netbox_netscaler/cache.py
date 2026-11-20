"""Per-pair inventory cache (option A: live fetch + Django cache)."""

from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.cache import cache

from .inventory import PairInventory, fetch_pair

PLUGIN = "netbox_netscaler"


def plugin_settings() -> dict:
    from . import NetScalerConfig

    merged = dict(NetScalerConfig.default_settings)
    merged.update(settings.PLUGINS_CONFIG.get(PLUGIN, {}))
    return merged


def _key(pair: dict) -> str:
    return f"{PLUGIN}:pair:{pair.get('name')}"


def get_inventories(force: bool = False) -> list[PairInventory]:
    """Inventories for every configured pair, fetching (in parallel) any that are not cached."""
    conf = plugin_settings()
    pairs = conf.get("pairs") or []
    cached = {} if force else cache.get_many([_key(p) for p in pairs])
    missing = [p for p in pairs if _key(p) not in cached]
    if missing:
        with ThreadPoolExecutor(max_workers=len(missing)) as pool:
            fetched = list(pool.map(lambda p: fetch_pair(p, conf), missing))
        for pair, inv in zip(missing, fetched):
            # Unavailable results are cached briefly so a dead pair doesn't stall every page.
            timeout = conf["cache_timeout"] if inv.available else min(60, conf["cache_timeout"])
            cache.set(_key(pair), inv, timeout)
            cached[_key(pair)] = inv
    return [cached[_key(p)] for p in pairs]


def cached_inventories() -> list[PairInventory] | None:
    """Cache-only read: never contacts NetScaler. None if any pair is not cached."""
    pairs = plugin_settings().get("pairs") or []
    cached = cache.get_many([_key(p) for p in pairs])
    if len(cached) != len(pairs):
        return None
    return [cached[_key(p)] for p in pairs]


def clear() -> None:
    cache.delete_many([_key(p) for p in plugin_settings().get("pairs") or []])
