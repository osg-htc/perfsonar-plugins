"""Check logic for the perfSONAR special agent's sections, without CheckMK imports (testable anywhere).

Ported from src/check_ps (toolkit summary, http/https, meshes, hardware) and
src/check_ps_psched (pScheduler statistics). Every function returns a list of
(state, summary, details, metrics) where state is 0 OK, 1 WARN, 2 CRIT, 3 UNKNOWN and
metrics is a list of (name, value).
"""
import ipaddress
import json
from urllib.parse import urlsplit

OK, WARN, CRIT, UNKNOWN = 0, 1, 2, 3

DOCS = "Documentation: https://osg-htc.org/networking/perfsonar/faq/"
CONFIG_HOSTS = ("psconfig.opensciencegrid.org", "psconfig-itb.opensciencegrid.org", "psconfig.aglt2.org",
                "psconfig-itb.aglt2.org", "psconfig.osg-htc.org", "meshconfig.opensciencegrid.org",
                "meshconfig.grid.iu.edu")
TARGET_MEMORY_GB = 4
PSCHEDULER_METRICS = ("pending", "running", "overdue", "missed", "failed", "nonstart", "finished",
                      "archiver_backlog")


def parse(string_table):
    """A sep(0) section of one JSON line -> dict (None when empty or unreadable)."""
    try:
        return json.loads(string_table[0][0])
    except (IndexError, ValueError, TypeError):
        return None


def _data(section):
    return (section or {}).get("data") if (section or {}).get("ok") else None


def check_summary(section):
    if not section:
        return [(UNKNOWN, "No data from the special agent", "", [])]
    if not section.get("ok"):
        return [(CRIT, section.get("error") or "Toolkit summary not available", DOCS, [])]
    return [(OK, "Toolkit metadata successfully retrieved", section.get("url", ""), [])]


def check_http(section):
    if _data(section) is None:
        return [(CRIT, "Toolkit homepage unreachable", DOCS, [])]
    return [(OK, "Toolkit homepage reachable", "", [])]


def _is_ip(text):
    try:
        ipaddress.ip_address(text.strip("[]"))
        return True
    except ValueError:
        return False


def check_meshes(section, config_hosts=CONFIG_HOSTS):
    data = _data(section)
    if data is None:
        return [(UNKNOWN, "Masked by perfSONAR json summary", "", [])]
    key = "templates" if "templates" in data else "meshes"
    configured = data.get(key)
    if configured is None:
        return [(CRIT, "Unable to parse mesh configuration", DOCS, [])]
    if not configured:
        return [(CRIT, "No meshes configured", DOCS, [])]
    host = section.get("host", "")
    names = {host, ((data.get("external_address") or {}).get("dns_name") or "").strip().lower()}
    names |= set(section.get("names") or [])     # DNS names of the host (canonical, reverse), from the agent
    names -= {""}
    listing = "Configured: " + ", ".join(configured)
    for url in configured:
        parts = urlsplit(url if "://" in url else "https://" + url)
        path = parts.path.rstrip("/")
        if any(path.endswith("/pub/auto/" + n) for n in names) and \
                (parts.hostname in config_hosts or _is_ip(parts.hostname or "")):
            return [(OK, "Auto-URL configured correctly", listing, [])]
    for url in configured:
        if "grid-deployment.web.cern.ch" in url:
            return [(CRIT, "Static/old URLs found in the mesh config", listing + "\n" + DOCS, [])]
        if "myosg.grid.iu.edu/pfmesh" in url:
            return [(CRIT, "Deprecated auto-URL configured", listing + "\n" + DOCS, [])]
    expected = "https://%s/pub/auto/%s (or the same path on %s)" % (config_hosts[0], host, ", ".join(config_hosts[1:]))
    return [(CRIT, "Auto-mesh URL is missing/incorrect", "Expected: %s\n%s\n%s" % (expected, listing, DOCS), [])]


def check_hardware(section, target_memory=TARGET_MEMORY_GB):
    data = _data(section)
    if data is None:
        return [(UNKNOWN, "Masked by perfSONAR json summary", "", [])]
    if "host_memory" not in data:
        return [(UNKNOWN, "Unable to parse information on installed memory (toolkit version too old?)", "", [])]
    memory = data["host_memory"]
    state, prefix = OK, ""
    if data.get("is_vm") not in (None, 0, "0", False):
        prefix = "VM "
    elif isinstance(memory, (int, float)) and memory < target_memory:
        state, prefix = CRIT, "%sGB RAM (%s required) " % (memory, target_memory)
    ext = data.get("external_address") or {}
    speed = ext.get("speed")
    net_speed = int(speed) if isinstance(speed, (int, float)) else 0
    summary = "%sCPU:%s/%scores/%sMhz RAM:%sGB NIC:%sGbps/%sMTU/%s" % (
        prefix, data.get("cpus", "1"), data.get("cpu_cores", "--"), data.get("cpu_speed", "--"), memory,
        net_speed / 1000000000.0, ext.get("mtu"), "IPv6 enabled" if ext.get("ipv6_address") else "IPv4 only")
    details = "External interface (speed, mtu, ipv4, ipv6, dns): %s, %s, %s, %s, %s" % (
        speed if speed is not None else "--", ext.get("mtu"), ext.get("ipv4_address"), ext.get("ipv6_address"),
        ext.get("dns_name"))
    metrics = [("mem_total_gb", memory)] if isinstance(memory, (int, float)) else []
    return [(state, summary, details, metrics)]


def check_pscheduler(section):
    if not section:
        return [(UNKNOWN, "No data from the special agent", "", [])]
    if not section.get("ok"):
        return [(CRIT, section.get("error") or "Error connecting to pscheduler API", "", [])]
    stats = section.get("stats") or {}
    metrics = [("pscheduler_" + name, stats[name]) for name in PSCHEDULER_METRICS if name in stats]
    details = ", ".join("%s: %s" % (name, stats[name]) for name in PSCHEDULER_METRICS if name in stats)
    return [(OK, "pscheduler stats retrieved", details, metrics)]
