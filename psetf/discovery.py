"""psetf discovery for CheckMK: pSConfig meshes + registries -> CheckMK hosts and labels.

Replaces the ncgx plugin (src/etf_ps_plugin.py) for phase 2. It computes the hosts psetf
should monitor, with labels describing them, and makes the CheckMK folder match through
the REST API:

  psetf/latency:yes, psetf/bandwidth:yes   what the host is checked as
  psetf/mesh/<mesh>:yes                    one label per mesh the host is in (lower case)
  psetf/ipv4:yes, psetf/ipv6:yes          the host name has an IPv4 / IPv6 address
  psetf/gocdb:yes, psetf/osg:yes           where the host is registered

Which hosts, and their type, follow the ncgx plugin exactly:
- every host registered in GOCDB or OSG Topology as net.perfSONAR.Latency/Bandwidth,
  typed by its registrations;
- every host in a mesh but not registered, typed by what its meshes test (latency
  tests -> latency, anything else or no task -> bandwidth); es.net and geant hosts excepted;
- only host names that resolve.

Safety: the mesh index is required (no index, no changes). A host that disappears is
deleted only after it has been missing for `--delete-after` runs in a row, and no host is
deleted if more than `--max-delete-fraction` of the folder would go at once.

Run:  python3 -m psetf.discovery --config /etc/psetf/discovery.json [--dry-run]
"""
import argparse
import json
import logging
import os
import socket
import sys
import time

import requests

from . import sources
from .checkmk import CheckMK

log = logging.getLogger("psetf")

FOLDER = "/perfsonar"
LABEL_PREFIX = "psetf/"
SKIP_UNREGISTERED = ("es.net", "geant")
# special agents only (the perfsonar special agent), no CheckMK agent, no SNMP
HOST_TAGS = {"tag_agent": "special-agents", "tag_snmp_ds": "no-snmp"}


def address_family(labels):
    """CheckMK's address family tag from the psetf/ipv4 and psetf/ipv6 labels."""
    v4, v6 = labels.get(LABEL_PREFIX + "ipv4") == "yes", labels.get(LABEL_PREFIX + "ipv6") == "yes"
    return "ip-v4v6" if v4 and v6 else "ip-v6-only" if v6 else "ip-v4-only"


def fetch(url, timeout=120):
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "psetf-discovery"})
    r.raise_for_status()
    return r.text


def resolve(host):
    """(has IPv4, has IPv6)"""
    try:
        infos = socket.getaddrinfo(host, 443, 0, 0, socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return False, False
    return any(i[0] == socket.AF_INET for i in infos), any(i[0] == socket.AF_INET6 for i in infos)


def desired_hosts(mesh_groups, registered, resolver=resolve):
    """{host: labels} from {MESH: {host: tags}} and {"gocdb"|"osg": {host: tags}}."""
    in_meshes = {}
    for mesh, hosts in mesh_groups.items():
        for host, tags in hosts.items():
            entry = in_meshes.setdefault(host, {"tags": set(), "meshes": set()})
            entry["tags"] |= tags
            entry["meshes"].add(mesh)

    types, registries = {}, {}
    for registry, hosts in registered.items():
        for host, tags in hosts.items():
            types.setdefault(host, set()).update(tags)
            registries.setdefault(host, set()).add(registry)
    for host, entry in in_meshes.items():
        if host in types or any(s in host for s in SKIP_UNREGISTERED):
            continue
        tags = set()
        if "latency" in entry["tags"]:
            tags.add("latency")
        if "bandwidth" in entry["tags"] or not entry["tags"]:
            tags.add("bandwidth")
        types[host] = tags

    out = {}
    for host in sorted(types):
        ipv4, ipv6 = resolver(host)
        if not (ipv4 or ipv6):
            log.info("skipping %s: does not resolve", host)
            continue
        labels = {LABEL_PREFIX + t: "yes" for t in sorted(types[host])}
        labels.update({LABEL_PREFIX + "mesh/" + m.lower(): "yes"
                       for m in sorted((in_meshes.get(host) or {}).get("meshes", ()))})
        labels.update({LABEL_PREFIX + r: "yes" for r in sorted(registries.get(host, ()))})
        if ipv4:
            labels[LABEL_PREFIX + "ipv4"] = "yes"
        if ipv6:
            labels[LABEL_PREFIX + "ipv6"] = "yes"
        out[host] = labels
    return out


def plan(desired, existing, missing_counts, delete_after=3, max_delete_fraction=0.2):
    """What to change in the folder.

    desired: {host: labels}; existing: {host: attributes} currently in the folder;
    missing_counts: {host: runs missing so far}. Returns a dict with create, update
    ({host: attributes}), delete (names), and the new missing_counts.
    """
    create = {h: dict(HOST_TAGS, labels=labels, tag_address_family=address_family(labels))
              for h, labels in desired.items() if h not in existing}
    update = {}
    for h, labels in desired.items():
        if h in existing:
            changes = {}
            current = existing[h].get("labels") or {}
            others = {k: v for k, v in current.items() if not k.startswith(LABEL_PREFIX)}
            if {k: v for k, v in current.items() if k.startswith(LABEL_PREFIX)} != labels:
                changes["labels"] = dict(others, **labels)        # keep labels set by hand
            if existing[h].get("tag_address_family", "ip-v4-only") != address_family(labels):
                changes["tag_address_family"] = address_family(labels)
            for tag, value in HOST_TAGS.items():
                if existing[h].get(tag) != value:
                    changes[tag] = value
            if changes:
                update[h] = changes
    gone = sorted(h for h in existing if h not in desired)
    counts = {h: missing_counts.get(h, 0) + 1 for h in gone}
    delete = [h for h in gone if counts[h] >= delete_after]
    blocked = False
    if existing and len(delete) > max_delete_fraction * len(existing):
        log.error("refusing to delete %d of %d hosts at once (limit %.0f%%); check the sources",
                  len(delete), len(existing), 100 * max_delete_fraction)
        delete, blocked = [], True
    for h in delete:
        counts.pop(h, None)
    return {"create": create, "update": update, "delete": delete, "missing": counts,
            "delete_blocked": blocked}


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True, default=sorted)
    os.rename(tmp, path)


def run(config, dry_run=False, cmk=None, fetcher=fetch, resolver=resolve, rediscover_all=False):
    state_dir = config.get("state_dir", "/var/lib/psetf")
    mesh_cache_file = os.path.join(state_dir, "meshes.json")
    missing_file = os.path.join(state_dir, "missing.json")

    index = fetcher(config["mesh_index"])               # required: no index, no changes
    cache = {m: {h: set(t) for h, t in hosts.items()} for m, hosts in _load(mesh_cache_file, {}).items()}
    mesh_groups = sources.meshes(index, fetcher, cache)
    registered = {}
    # GOCDB is asked once per service type, as the ncgx plugin does
    gocdb = config.get("gocdb")
    for name, urls, parse in (("gocdb", [gocdb + "&service_type=" + t for t in sorted(sources.REGISTRY_TYPES)]
                               if gocdb else [], sources.gocdb_hosts),
                              ("osg", [config["osg_topology"]] if config.get("osg_topology") else [],
                               sources.topology_hosts)):
        try:
            for url in urls:
                for host, tags in parse(fetcher(url)).items():
                    registered.setdefault(name, {}).setdefault(host, set()).update(tags)
        except Exception as e:  # registries are best effort, as in the ncgx plugin
            log.error("unable to read %s (%s), continuing without it", name, e)
            registered.pop(name, None)

    desired = desired_hosts(mesh_groups, registered, resolver)
    log.info("%d meshes, %d hosts in meshes, %d registered, %d to monitor",
             len(mesh_groups), len({h for m in mesh_groups.values() for h in m}),
             len({h for r in registered.values() for h in r}), len(desired))

    cmk = cmk or CheckMK(config["api_url"], config["api_user"], _secret(config),
                         verify=config.get("verify_tls", True))
    if not dry_run:
        cmk.ensure_folder(FOLDER.strip("/"), config.get("folder_title", "perfSONAR (discovered)"))
    existing = cmk.hosts(FOLDER)
    changes = plan(desired, existing, _load(missing_file, {}),
                   config.get("delete_after", 3), config.get("max_delete_fraction", 0.2))
    log.info("create %d, update %d, delete %d, missing (not yet deleted) %d%s",
             len(changes["create"]), len(changes["update"]), len(changes["delete"]),
             len(changes["missing"]), ", DELETION BLOCKED" if changes["delete_blocked"] else "")
    if dry_run:
        return changes

    cmk.create_hosts(FOLDER, changes["create"])
    cmk.update_hosts(changes["update"])
    cmk.delete_hosts(changes["delete"])
    os.makedirs(state_dir, exist_ok=True)
    _save(mesh_cache_file, mesh_groups)
    _save(missing_file, changes["missing"])
    # services of new hosts, and of hosts whose tags changed (e.g. the agent type)
    rediscover = set(changes["create"]) | {h for h, a in changes["update"].items() if set(a) - {"labels"}}
    if rediscover_all:
        rediscover |= set(desired)
    if changes["create"] or changes["update"] or changes["delete"]:
        # CheckMK 2.5's automation-helper reloads its workers a few seconds after the host
        # files change; an activation started inside that window lost its "restart" reply and
        # stayed "running" forever on staging (2026-11-27). Let the reload happen first.
        time.sleep(config.get("settle_seconds", 20))
    # also activates changes left pending by an earlier run whose activation failed
    if cmk.activate(config["site"], wait=config.get("activation_timeout", 600)) is not None:
        log.info("changes activated on site %s", config["site"])
    if rediscover and config.get("discover_services", True):
        log.info("service discovery for %d hosts", len(rediscover))
        cmk.discover_services(rediscover)
        time.sleep(config.get("settle_seconds", 20))
        if cmk.activate(config["site"], wait=config.get("activation_timeout", 600)) is not None:
            log.info("discovered services activated on site %s", config["site"])
    return changes


def _secret(config):
    if config.get("api_secret_file"):
        with open(config["api_secret_file"]) as f:
            return f.read().strip()
    return config["api_secret"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, help="JSON: mesh_index, gocdb, osg_topology, api_url, api_user, "
                                                    "api_secret_file, site, state_dir, ...")
    ap.add_argument("--dry-run", action="store_true", help="compute and print the changes, change nothing")
    ap.add_argument("--rediscover", action="store_true", help="run service discovery on every host, not only new/changed ones")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config) as f:
        config = json.load(f)
    changes = run(config, dry_run=args.dry_run, rediscover_all=args.rediscover)
    if args.dry_run:
        print(json.dumps({"create": sorted(changes["create"]), "update": sorted(changes["update"]),
                          "delete": changes["delete"], "missing": changes["missing"],
                          "delete_blocked": changes["delete_blocked"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
