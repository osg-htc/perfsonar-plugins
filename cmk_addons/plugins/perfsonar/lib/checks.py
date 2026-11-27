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
                      "archiver_backlog",
                      # only from the perfSONAR host exporter
                      "on_deck", "cleanup", "preempted", "archiving_upcoming", "http_queue_backlog",
                      "http_queue_length", "paused")


def parse(string_table):
    """A sep(0) section of one JSON line -> dict (None when empty or unreadable)."""
    try:
        return json.loads(string_table[0][0])
    except (IndexError, ValueError, TypeError):
        return None


def _data(section):
    return (section or {}).get("data") if (section or {}).get("ok") else None


# ---------------------------------------------------------------- the perfSONAR host exporter (5.1+)
def exporter_view(section):
    """The exporter section in a usable shape, or None when the exporter did not answer."""
    if not (section or {}).get("ok"):
        return None
    view = {"bundle": None, "registered": None, "psconfig": {}}
    for name, labels, value in section.get("samples") or []:
        if name == "perfsonar_bundle":
            view["bundle"] = "%s %s" % (labels.get("type", "?"), labels.get("version", "?"))
        elif name == "perfsonar_host_registered":
            view["registered"] = value
        elif name.startswith("perfsonar_psconfig_"):
            rest = name[len("perfsonar_psconfig_"):]
            for kind in ("agent_start_time", "agent_end_time", "remote", "tasks", "agent"):
                if rest.endswith("_" + kind):
                    agent = view["psconfig"].setdefault(rest[:-len(kind) - 1], {"remotes": []})
                    if kind == "remote":
                        agent["remotes"].append(labels.get("url", ""))
                    elif kind == "tasks":
                        if "url" not in labels:
                            agent["tasks"] = value
                    elif kind == "agent":
                        agent["client_uuid"] = labels.get("client_uuid")
                    else:
                        agent[kind] = value
                    break
    return view


def _bundle_note(exporter):
    view = exporter_view(exporter)
    return " (%s)" % view["bundle"] if view and view["bundle"] else ""


def check_summary(section, exporter=None):
    if not section:
        return [(UNKNOWN, "No data from the special agent", "", [])]
    if not section.get("ok"):
        error = section.get("error") or "Toolkit summary not available"
        if exporter_view(exporter) is not None and "404" in error:
            # perfSONAR is dropping the toolkit summary (perhaps from 5.3); the exporter still answers
            return [(WARN, "Toolkit summary not served%s; host metrics come from the exporter" % _bundle_note(exporter),
                     error, [])]
        return [(CRIT, error, DOCS, [])]
    return [(OK, "Toolkit metadata successfully retrieved%s" % _bundle_note(exporter), section.get("url", ""), [])]


def check_http(section, exporter=None):
    if _data(section) is None and exporter_view(exporter) is None:
        return [(CRIT, "Toolkit homepage unreachable", DOCS, [])]
    return [(OK, "Toolkit homepage reachable", "", [])]


def _is_ip(text):
    try:
        ipaddress.ip_address(text.strip("[]"))
        return True
    except ValueError:
        return False


def check_meshes(section, config_hosts=CONFIG_HOSTS, exporter=None):
    data = _data(section) or {}
    view = exporter_view(exporter)
    if view is not None and "pscheduler" in view["psconfig"]:
        # the URLs the pSConfig pScheduler agent actually uses (exporter), else the toolkit summary's
        configured = view["psconfig"]["pscheduler"]["remotes"]
    elif _data(section) is None:
        return [(UNKNOWN, "Masked by perfSONAR json summary", "", [])]
    else:
        configured = data.get("templates" if "templates" in data else "meshes")
    if configured is None:
        return [(CRIT, "Unable to parse mesh configuration", DOCS, [])]
    if not configured:
        return [(CRIT, "No meshes configured", DOCS, [])]
    host = (section or {}).get("host", "")
    names = {host, ((data.get("external_address") or {}).get("dns_name") or "").strip().lower()}
    names |= set((section or {}).get("names") or [])     # DNS names of the host (canonical, reverse), from the agent
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


def _node(section):
    return section if (section or {}).get("ok") else None


def hardware_from_node(node, target_memory=TARGET_MEMORY_GB):
    """The hardware check from node_exporter, for hosts that no longer serve the toolkit summary."""
    memory = round((node.get("mem_bytes") or 0) / 1024.0 ** 3)
    dmi = node.get("dmi") or {}
    vendor = " ".join(v for v in (dmi.get("sys_vendor") or dmi.get("system_vendor"), dmi.get("product_name")) if v)
    is_vm = any(k in vendor.lower() for k in ("vmware", "kvm", "qemu", "virtual", "xen", "openstack", "hvm"))
    nics = [(n, v) for n, v in (node.get("nics") or {}).items() if (v.get("speed_bytes") or 0) > 0]
    best = max(nics, key=lambda nv: nv[1].get("speed_bytes", 0)) if nics else None
    state, prefix = OK, ""
    if is_vm:
        prefix = "VM "
    elif memory and memory < target_memory:
        state, prefix = CRIT, "%sGB RAM (%s required) " % (memory, target_memory)
    nic = "%sGbps/%sMTU (%s)" % (best[1]["speed_bytes"] * 8 / 1e9, int(best[1].get("mtu") or 0), best[0]) if best else "--"
    summary = "%sCPU:%s RAM:%sGB NIC:%s" % (prefix, node.get("cpus") or "--", memory or "--", nic)
    details = "From node_exporter (no toolkit summary). %s; %s" % (vendor or "hardware unknown", node.get("os") or "OS unknown")
    return [(state, summary, details, [("mem_total_gb", memory)] if memory else [])]


def check_hardware(section, target_memory=TARGET_MEMORY_GB, node=None):
    data = _data(section)
    if data is None:
        if _node(node):
            return hardware_from_node(node, target_memory)
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
    source = "perfSONAR host exporter" if section.get("source") == "exporter" else "pScheduler API /stat"
    return [(OK, "pscheduler stats retrieved", "Source: %s\n%s" % (source, details), metrics)]


PSCONFIG_AGE_WARN, PSCONFIG_AGE_CRIT = 6 * 3600, 24 * 3600   # the agent normally runs every hour


def check_psconfig_agent(exporter, now):
    """Is the pSConfig pScheduler agent running, and does it have a remote configuration?"""
    view = exporter_view(exporter)
    if view is None:
        return [(UNKNOWN, "perfSONAR host exporter not available", "", [])]
    agent = view["psconfig"].get("pscheduler")
    if not agent:
        return [(CRIT, "No pSConfig pScheduler agent reported by the exporter", "", [])]
    remotes, tasks, end = agent.get("remotes") or [], agent.get("tasks"), agent.get("agent_end_time")
    details = "Remote configurations:\n" + "\n".join(remotes) if remotes else "No remote configurations"
    metrics = [("psconfig_remotes", len(remotes))]
    if tasks is not None:
        metrics.append(("psconfig_tasks", tasks))
    if end is None:
        return [(WARN, "pSConfig agent has not reported a completed run", details, metrics)]
    age = max(0.0, now - end)
    metrics.append(("psconfig_agent_age", age))
    state = CRIT if age > PSCONFIG_AGE_CRIT else WARN if age > PSCONFIG_AGE_WARN else OK
    summary = "Last pSConfig agent run %.1f h ago, %d remote configuration(s)" % (age / 3600.0, len(remotes))
    if not remotes:
        state = max(state, WARN)
        summary += " (none: this host gets no central mesh configuration)"
    return [(state, summary, details, metrics)]


def check_lookup_registration(exporter):
    view = exporter_view(exporter)
    if view is None:
        return [(UNKNOWN, "perfSONAR host exporter not available", "", [])]
    if view["registered"] is None:
        return [(UNKNOWN, "The exporter does not report Lookup Service registration", "", [])]
    if view["registered"] >= 1:
        return [(OK, "Registered in the perfSONAR Lookup Service", "", [])]
    return [(WARN, "Not registered in the perfSONAR Lookup Service", "Hosts that do not register are missing from "
             "pSConfig/PWA host lists and from Lookup Service based tools.", [])]


# ---------------------------------------------------------------- node_exporter checks
PSCHEDULER_UNITS = ("pscheduler-scheduler.service", "pscheduler-runner.service", "pscheduler-archiver.service",
                    "pscheduler-ticker.service")
PSCONFIG_UNIT = "psconfig-pscheduler-agent.service"


def check_units(node):
    """perfSONAR systemd units: failed is CRIT, a pScheduler unit missing or not active is CRIT,
    the pSConfig pScheduler agent not active is WARN. One-shot units that are normally inactive
    (NIC tuning, motd, startup) are no finding."""
    if not _node(node):
        return [(UNKNOWN, "node_exporter not available", "", [])]
    units = node.get("units") or {}
    if not units:
        return [(UNKNOWN, "node_exporter reports no perfSONAR systemd units (systemd collector off?)", "", [])]
    crit, warn = [], []
    for unit, state in sorted(units.items()):
        if state == "failed":
            crit.append("%s failed" % unit)
    for unit in PSCHEDULER_UNITS:
        if units.get(unit) is None:
            crit.append("%s missing" % unit)
        elif units[unit] not in ("active", "failed"):
            crit.append("%s %s" % (unit, units[unit]))
    if units.get(PSCONFIG_UNIT) not in ("active", "failed"):
        warn.append("%s %s" % (PSCONFIG_UNIT, units.get(PSCONFIG_UNIT) or "missing"))
    details = "\n".join("%s: %s" % (u, st) for u, st in sorted(units.items()))
    active = sum(1 for st in units.values() if st == "active")
    metrics = [("units_active", active), ("units_failed", sum(1 for st in units.values() if st == "failed"))]
    if crit:
        return [(CRIT, "; ".join(crit + warn), details, metrics)]
    if warn:
        return [(WARN, "; ".join(warn), details, metrics)]
    return [(OK, "%d perfSONAR units active, pScheduler and pSConfig running" % active, details, metrics)]


FS_WARN, FS_CRIT = 10.0, 5.0       # percent free


def check_filesystems(node):
    if not _node(node):
        return [(UNKNOWN, "node_exporter not available", "", [])]
    rows, metrics, worst = [], [], None
    for mount, fs in sorted((node.get("filesystems") or {}).items()):
        size, avail = fs.get("size") or 0, fs.get("avail")
        if not size or avail is None:
            continue
        free = 100.0 * avail / size
        rows.append("%s: %.1f%% free (%.1f of %.1f GB, %s)" % (mount, free, avail / 1e9, size / 1e9, fs.get("fstype")))
        metrics.append(("fs_free_percent_" + (mount.strip("/").replace("/", "_") or "root"), round(free, 2)))
        if worst is None or free < worst[1]:
            worst = (mount, free)
    if not rows:
        return [(UNKNOWN, "node_exporter reports no local filesystems", "", [])]
    mount, free = worst
    state = CRIT if free < FS_CRIT else WARN if free < FS_WARN else OK
    if state == OK:
        return [(OK, "%d filesystems, least free: %s %.1f%%" % (len(rows), mount, free), "\n".join(rows), metrics)]
    return [(state, "%s has %.1f%% free (warn < %.0f%%, crit < %.0f%%)" % (mount, free, FS_WARN, FS_CRIT),
             "\n".join(rows), metrics)]


def check_clock(node):
    if not _node(node):
        return [(UNKNOWN, "node_exporter not available", "", [])]
    if node.get("clock_synchronized") is None:
        return [(UNKNOWN, "node_exporter reports no clock status (timex collector off?)", "", [])]
    offset = node.get("clock_offset")
    metrics = [("clock_offset", offset)] if offset is not None else []
    if node["clock_synchronized"] >= 1:
        note = " (offset %.1f ms)" % (offset * 1000) if offset is not None else ""
        return [(OK, "Clock synchronized" + note, "", metrics)]
    return [(WARN, "Clock NOT synchronized (kernel timex status); latency results are unreliable", "", metrics)]
