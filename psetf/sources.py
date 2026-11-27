"""Where psetf's hosts come from: pSConfig meshes (PWA / pwa-next), GOCDB and OSG Topology.

The parsing is the same as src/etf_ps_plugin.py (the ncgx plugin that live psetf runs);
this copy has no ncgx dependency so CheckMK-based discovery can use it. The ncgx plugin
is retired at the phase 2 swap.
"""
import json
import logging
import xml.etree.ElementTree as ET

log = logging.getLogger("psetf")

LATENCY_TESTS = ("latencybg", "latency")
REGISTRY_TYPES = {"net.perfSONAR.Latency": "latency", "net.perfSONAR.Bandwidth": "bandwidth"}


def mesh_members(template):
    """{hostname: set of tags} for one pSConfig template (JSON text or dict).

    A host is tagged 'latency' when a task it is part of runs a latency test, and
    'bandwidth' for any other task; hosts listed without a task get no tag.
    """
    tpl = json.loads(template) if isinstance(template, (str, bytes)) else template
    addresses = tpl.get("addresses") or {}
    groups = tpl.get("groups") or {}
    tests = tpl.get("tests") or {}

    def host_of(selector):
        name = selector.get("name") if isinstance(selector, dict) else selector
        entry = addresses.get(name) or {}
        return entry.get("host") or entry.get("address") or name

    members = {}
    for task in (tpl.get("tasks") or {}).values():
        if task.get("disabled"):
            continue
        group = groups.get(task.get("group")) or {}
        test_type = (tests.get(task.get("test")) or {}).get("type")
        tag = "latency" if test_type in LATENCY_TESTS else "bandwidth"
        for key in ("addresses", "a-addresses", "b-addresses"):
            for selector in group.get(key) or []:
                host = host_of(selector)
                if host:
                    members.setdefault(host.strip().lower(), set()).add(tag)
    for name in tpl.get("hosts") or {}:
        members.setdefault(name.strip().lower(), set())
    return members


def mesh_urls(index):
    """[(MESH NAME, url)] from a pSConfig index (a list of {"include": [url]})."""
    out = []
    for entry in json.loads(index) if isinstance(index, (str, bytes)) else index:
        url = entry["include"][0]
        if not url.startswith(("https://", "http://")):
            url = "https://" + url
        out.append((url.rstrip("/").rsplit("/", 1)[-1].upper(), url))
    return out


def meshes(index, fetch, cache=None):
    """{MESH: {host: tags}} for every mesh in the index.

    `fetch(url)` returns the body. A mesh that fails keeps its entry from `cache`
    (the previous result), so one bad mesh neither stops discovery nor drops its hosts.
    """
    cache = cache or {}
    out = {}
    for mesh, url in mesh_urls(index):
        try:
            out[mesh] = mesh_members(fetch(url))
        except Exception as e:  # any failure of one mesh: fall back, keep going
            if mesh in cache:
                log.warning("mesh %s failed (%s), using its last good member list", url, e)
                out[mesh] = cache[mesh]
            else:
                log.warning("mesh %s failed (%s) and has no cached member list, skipping it", url, e)
    return out


def gocdb_hosts(xml_text):
    """{host: set of 'latency'/'bandwidth'} from a GOCDB get_service_endpoint response."""
    out = {}
    for ep in ET.fromstring(xml_text).findall("SERVICE_ENDPOINT"):
        host, stype = (ep.findtext("HOSTNAME") or "").strip().lower(), (ep.findtext("SERVICE_TYPE") or "").strip()
        if host and stype in REGISTRY_TYPES:
            out.setdefault(host, set()).add(REGISTRY_TYPES[stype])
    return out


def topology_hosts(xml_text):
    """{host: set of 'latency'/'bandwidth'} from an OSG Topology rgsummary response.

    A service's Details/endpoint wins over its resource's FQDN, as in the ncgx plugin.
    """
    out = {}
    for res in ET.fromstring(xml_text).findall("ResourceGroup/Resources/Resource"):
        fqdn = (res.findtext("FQDN") or "").strip().lower()
        for svc in res.findall("Services/Service"):
            stype = (svc.findtext("Name") or "").strip()
            if stype not in REGISTRY_TYPES:
                continue
            host = (svc.findtext("Details/endpoint") or "").strip().lower() or fqdn
            if host:
                out.setdefault(host, set()).add(REGISTRY_TYPES[stype])
    return out
