import logging
import os
import requests
import json
import xml.etree.ElementTree as ET
import socket

from ncgx.inventory import Hosts, Checks, Groups

# suppress InsecureRequestWarning: Unverified HTTPS request is being made.
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

log = logging.getLogger('ncgx')

PS_LCORE_METRICS = (
    'perfSONAR services: owamp',
)

PS_BCORE_METRICS = (
    'perfSONAR services: bwctl',
)

PS_TOOLKIT_METRICS = (
    'perfSONAR json summary',
    #'perfSONAR services: ntp',
    #'perfSONAR services: versions',
    'perfSONAR services: http/https',
    #'perfSONAR services: ndt/npad disabled',
    #'perfSONAR services: regular testing/pscheduler',
    'perfSONAR services: pscheduler',
    'perfSONAR services: pscheduler diags',
    'perfSONAR configuration: meshes',
    #'perfSONAR configuration: contacts',
    #'perfSONAR configuration: location',
    'perfSONAR hardware check'
)

PS_LTOOLKIT_METRICS = (
    #'perfSONAR esmond freshness: owamp',
)

PS_BTOOLKIT_METRICS = (
    #'perfSONAR esmond freshness: bwctl',
    #'perfSONAR esmond freshness: trace',
)

PS_TP_METRICS = (
    #'perfSONAR services: owamp',
    #'perfSONAR services: bwctl',
    'perfSONAR services: pscheduler',
)

PS_TP_CMA_METRICS = (
    #'perfSONAR esmond freshness: bwctl',
    #'perfSONAR esmond freshness: trace',
    #'perfSONAR esmond freshness: owamp',
    'perfSONAR services: owamp',
    #'perfSONAR services: bwctl',
    'perfSONAR services: pscheduler',
)


def request(url, hostcert=None, hostkey=None, verify=False):
    if hostcert and hostkey:
        req = requests.get(url, verify=verify, timeout=120, cert=(hostcert, hostkey))
    else:
        req = requests.get(url, timeout=120, verify=verify)
    req.raise_for_status()
    return req.content


# pScheduler test types that run over owamp/twamp: hosts in such tasks must answer on port 861
LATENCY_TESTS = ("latencybg", "latency")
# last good member list of every mesh, used when a mesh cannot be fetched
MESH_CACHE = "/var/cache/ncgx/etf_ps_meshes.json"


def get_members(template):
    """{hostname: set of tags} for the hosts in one pSConfig template.

    A host is tagged 'latency' when a task it takes part in runs a latency test
    (owamp/twamp), and 'bandwidth' for any other task; hosts listed without a
    task get no tag here (the caller treats them as 'bandwidth').
    """
    tpl = json.loads(template)
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


def _load_cache(path):
    try:
        with open(path) as f:
            return {mesh: {h: set(t) for h, t in hosts.items()} for mesh, hosts in json.load(f).items()}
    except (IOError, OSError, ValueError):
        return {}


def _save_cache(path, meshes):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({mesh: {h: sorted(t) for h, t in hosts.items()} for mesh, hosts in meshes.items()}, f)
        os.rename(tmp, path)
    except (IOError, OSError) as e:
        log.warning("Unable to save mesh cache {}: {}".format(path, e))


def get_active_sonars(response, cache_file=None):
    """Read the pSConfig index (a list of {"include": [mesh URL]}) and every mesh in it.

    Returns ({MESH: set of hosts}, {host: set of tags}). A mesh that cannot be
    fetched or parsed is taken from the last good copy in cache_file, so one bad
    mesh neither stops discovery nor drops its hosts.
    """
    cache_file = cache_file or MESH_CACHE
    cache = _load_cache(cache_file)
    meshes = {}
    for entry in json.loads(response):
        url = entry["include"][0]
        if not url.startswith(("https://", "http://")):
            url = "https://" + url
        mesh = url.rstrip("/").rsplit("/", 1)[-1].upper()
        try:
            meshes[mesh] = get_members(request(url))
        except Exception as e:
            if mesh in cache:
                log.warning("Mesh {} failed ({}), using the last good member list".format(url, e))
                meshes[mesh] = cache[mesh]
            else:
                log.warning("Mesh {} failed ({}) and has no cached member list, skipping it".format(url, e))
    _save_cache(cache_file, meshes)
    mk_groups, members = {}, {}
    for mesh, hosts in meshes.items():
        mk_groups[mesh] = set(hosts)
        for host, tags in hosts.items():
            members.setdefault(host, set()).update(tags)
    return mk_groups, members


def get_gocdb_sonars(response):
    if not response:
        return None
    tree = ET.fromstring(response)
    gocdb_set = set([(x.findtext('HOSTNAME').strip(),
                      x.findtext('SERVICE_TYPE').strip(),
                      x.findtext('IN_PRODUCTION') )
                     for x in tree.findall('SERVICE_ENDPOINT')])
    gocdb_sonars = set([(host, stype) for host, stype, state in gocdb_set
                        if (stype == 'net.perfSONAR.Bandwidth' or stype == 'net.perfSONAR.Latency')])
    return gocdb_sonars


def get_oim_sonars(response):
    if not response:
        return None
    tree = ET.fromstring(response)
    oim_resources = list()
    # first take all services defined via details/endpoint
    for r in tree.findall('ResourceGroup/Resources/Resource'):
        try:
            oim_resources.extend([(x.findtext('Details/endpoint').strip(),
                                   x.findtext('Name').strip())
                                  for x in r.findall('Services/Service')])
        except AttributeError:
            continue

    # then complement this with services with just FQDN
    res_index = set([entry[0] for entry in oim_resources])
    for x in tree.findall('ResourceGroup/Resources/Resource'):
        h = x.findtext('FQDN').strip()
        st = x.findtext('Services/Service/Name').strip()
        if h not in res_index:
            oim_resources.append((h, st))

    oim_sonars = set([(host, stype) for host, stype in oim_resources
                      if stype == 'net.perfSONAR.Bandwidth' or stype == 'net.perfSONAR.Latency'])
    return oim_sonars


def get_fqdn(host):
    try:
        socket.getaddrinfo(host, 80, 0, 0, socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    return True


def run(mesh, gocdb, oim, hostcert, hostkey, wato_hosts):
    stype_map = {'net.perfSONAR.Latency': 'latency',
                 'net.perfSONAR.Bandwidth': 'bandwidth'}

    sonars = []
    # registries are best effort: if one is down, the others still define the hosts
    for label, url, parse in (("GOCDB latency", gocdb + "&service_type=net.perfSONAR.Latency", get_gocdb_sonars),
                              ("GOCDB bandwidth", gocdb + "&service_type=net.perfSONAR.Bandwidth", get_gocdb_sonars),
                              ("OIM/Topology", oim, get_oim_sonars)):
        log.info("Retrieving {} sonars ...".format(label))
        try:
            sonars.extend(parse(request(url)) or [])
        except Exception as e:
            log.error("Unable to read {} ({}), continuing without it".format(label, e))

    # the mesh index is required: if it fails, ncgx stops and the last generated configuration stays
    log.info("Retrieving meshes ...")
    (mesh_groups, members) = get_active_sonars(request(mesh))
    sonars_set = set(s[0] for s in sonars)

    non_registered = set(members) - sonars_set
    not_in_mesh = sonars_set - set(members)
    log.warning("Hosts listed in meshes, but not registered in GOCDB/OIM: {}".format(non_registered))
    log.warning("Hosts registered, but not in any mesh: {}".format(not_in_mesh))
    for s in non_registered:
        if 'es.net' in s or 'geant' in s:
            continue
        # typed by what their meshes test: latency tests need owamp/twamp checks, everything else the toolkit ones
        tags = members[s]
        if 'latency' in tags:
            sonars.append((s, 'net.perfSONAR.Latency'))
        if 'bandwidth' in tags or not tags:
            sonars.append((s, 'net.perfSONAR.Bandwidth'))

    h = Hosts()
    for host, stype in sonars:
        if get_fqdn(host):
            h.add(host, (stype_map[stype],))
    h.serialize()

    hg = Groups("host_groups")
    for mesh, mesh_members in mesh_groups.items():
        for host in mesh_members:
            hg.add(mesh, host)
    hg.serialize()

    # adding hosts to check_mk wato
    if wato_hosts:
        try:
            log.info("Generating check_mk WATO host definitions ...")
            with open('/omd/sites/etf/etc/check_mk/conf.d/wato/hosts.mk', 'w') as wato_f:
                wato_f.write("# Created by ETF ncgx\n\nall_hosts += [\n")
                for host in h.get_all_hosts():
                    if get_fqdn(host):
                        tags = "|".join(h.get_tags(host) | hg.exact_match(host))
                        wato_f.write("       \"%s|ip-v4-only|site:etf|ip-v4|wato|%s|/\" + FOLDER_PATH + \"/\",\n"
                                     % (host, tags))
                wato_f.write("]\n")
                for host in h.get_all_hosts():
                    if get_fqdn(host):
                        wato_f.write("host_attributes.update({\'%s\': {}})\n" % host)
        except Exception as e:
            log.info("Failed to write check_mk WATO hosts")

    c = Checks()
    c.add_all(PS_LCORE_METRICS, tags=["latency"])
    c.add_all(PS_TOOLKIT_METRICS, tags=["latency", "bandwidth"])
    hosts = h.get_all_hosts()
    for host in hosts:
        try:
            host_addr = socket.getaddrinfo(host, 80, 0, 0, socket.IPPROTO_TCP)
        except socket.gaierror as e:
            continue
        if any(x[0] == socket.AF_INET6 for x in host_addr):
            c.add('perfSONAR services: web/https IPv6', hosts=(host,))

    c.serialize()

