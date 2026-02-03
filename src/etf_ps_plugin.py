import logging
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
    'perfSONAR services: ndt/npad disabled',
    'perfSONAR services: regular testing/pscheduler',
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
    'perfSONAR services: bwctl',
    'perfSONAR services: pscheduler',
)


def request(url, hostcert=None, hostkey=None, verify=False):
    if hostcert and hostkey:
        req = requests.get(url, verify=verify, timeout=120, cert=(hostcert, hostkey))
    else:
        req = requests.get(url, timeout=120, verify=verify)
    req.raise_for_status()
    return req.content


def get_members(mesh_config):
    members = set()
    mesh_c = json.loads(mesh_config)
    for entry in mesh_c['tests']:
        if 'members' in entry['members'].keys():
            for h in entry['members']['members']:
                members.add(h)
        if 'a_members' in entry['members'].keys():
            for h in entry['members']['a_members']:
                members.add(h)
        if 'b_members' in entry['members'].keys():
            for h in entry['members']['b_members']:
                members.add(h)
    return members


def get_active_sonars(response):
    mesh_urls = json.loads(response)
    mk_groups = {}
    members = set()
    for entry in mesh_urls:
        url = entry['include'][0]
        if not url.startswith("https://"):
            url = "https://" + url
        mesh = url.rsplit('/', 1)[-1].upper()
        response = request(url + '/?format=meshconfig')
        hosts = get_members(response)
        mk_groups[mesh] = hosts
        for h in hosts:
            members.add(h)
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

    log.info("Retrieving GOCDB sonars ...")
    sonars = list(get_gocdb_sonars(request(gocdb+"&service_type=net.perfSONAR.Latency")))
    sonars_b = list(get_gocdb_sonars(request(gocdb+"&service_type=net.perfSONAR.Bandwidth")))
    sonars.extend(sonars_b)

    log.info("Retrieving OIM sonars ...")
    oim_sonars = list(get_oim_sonars(request(oim)))
    sonars.extend(oim_sonars)

    log.info("Retrieving meshes ...")
    (mesh_groups, members) = get_active_sonars(request(mesh))
    sonars_set = set()
    for s in sonars:
        sonars_set.add(s[0])

    non_registered = members - sonars_set
    not_in_mesh = set(sonars_set) - members
    log.warning("Hosts listed in meshes, but not registered in GOCDB/OIM: {}".format(non_registered))
    log.warning("Hosts registered, but not in any mesh: {}".format(not_in_mesh))
    for s in non_registered:
        if 'es.net' in s or 'geant' in s:
            continue
        sonars.append((s, 'net.perfSONAR.Latency'))
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
    c.add_all(PS_BCORE_METRICS, tags=["bandwidth"])
    c.add_all(PS_TOOLKIT_METRICS, tags=["latency", "bandwidth"])
    #c.add_all(PS_LTOOLKIT_METRICS, tags=["latency"])
    #c.add_all(PS_BTOOLKIT_METRICS, tags=["bandwidth"])
    hosts = h.get_all_hosts()
    for host in hosts:
        try:
            host_addr = socket.getaddrinfo(host, 80, 0, 0, socket.IPPROTO_TCP)
        except socket.gaierror as e:
            continue
        ip6 = filter(lambda x: x[0] == socket.AF_INET6, host_addr)
        if ip6:
            c.add('perfSONAR services: web/https IPv6', hosts=(host,))

    c.serialize()

