"""Tests for phase 2 discovery (psetf.sources, psetf.discovery) without network or CheckMK."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from psetf import discovery, sources  # noqa: E402

GOCDB = """<results>
 <SERVICE_ENDPOINT><HOSTNAME>lat.example.eu</HOSTNAME><SERVICE_TYPE>net.perfSONAR.Latency</SERVICE_TYPE></SERVICE_ENDPOINT>
 <SERVICE_ENDPOINT><HOSTNAME>Both.example.eu</HOSTNAME><SERVICE_TYPE>net.perfSONAR.Latency</SERVICE_TYPE></SERVICE_ENDPOINT>
 <SERVICE_ENDPOINT><HOSTNAME>both.example.eu</HOSTNAME><SERVICE_TYPE>net.perfSONAR.Bandwidth</SERVICE_TYPE></SERVICE_ENDPOINT>
 <SERVICE_ENDPOINT><HOSTNAME>ce.example.eu</HOSTNAME><SERVICE_TYPE>CREAM-CE</SERVICE_TYPE></SERVICE_ENDPOINT>
</results>"""

TOPOLOGY = """<ResourceSummary><ResourceGroup><Resources>
 <Resource><FQDN>bw.example.edu</FQDN><Services>
   <Service><Name>net.perfSONAR.Bandwidth</Name></Service></Services></Resource>
 <Resource><FQDN>alias.example.edu</FQDN><Services>
   <Service><Name>net.perfSONAR.Latency</Name><Details><endpoint>real-lat.example.edu</endpoint></Details></Service>
   <Service><Name>XRootD</Name></Service></Services></Resource>
</Resources></ResourceGroup></ResourceSummary>"""


def template(tasks, hosts):
    addresses, groups, tests, out = {}, {}, {}, {}
    for name, (members, test_type) in tasks.items():
        for h in members:
            addresses[h] = {"address": h}
        groups[name] = {"type": "mesh", "addresses": [{"name": h} for h in members]}
        tests[name] = {"type": test_type, "spec": {}}
        out[name] = {"group": name, "test": name}
    return json.dumps({"addresses": addresses, "groups": groups, "tests": tests, "tasks": out,
                       "hosts": {h: {} for h in hosts}})


def test_registries_are_parsed_by_service_type():
    assert sources.gocdb_hosts(GOCDB) == {"lat.example.eu": {"latency"},
                                          "both.example.eu": {"latency", "bandwidth"}}
    assert sources.topology_hosts(TOPOLOGY) == {"bw.example.edu": {"bandwidth"},
                                                "real-lat.example.edu": {"latency"}}


def test_hosts_and_labels_follow_the_ncgx_plugin_rules():
    meshes = {"US-ATLAS": {"lat.example.eu": {"bandwidth"},          # registered: registry wins
                           "only-lat.example.org": {"latency"},      # unregistered: typed by mesh
                           "untasked.example.org": set(),            # no task: bandwidth
                           "ps.es.net": {"latency"},                 # unregistered ESnet: skipped
                           "nodns.example.org": {"latency"}},        # does not resolve: skipped
              "DUNE": {"only-lat.example.org": {"latency"}}}
    registered = {"gocdb": sources.gocdb_hosts(GOCDB), "osg": sources.topology_hosts(TOPOLOGY)}

    def resolver(host):   # (IPv4, IPv6)
        return host not in ("nodns.example.org", "v6only.example.eu"), host in ("both.example.eu", "v6only.example.eu")

    registered["gocdb"]["v6only.example.eu"] = {"bandwidth"}

    hosts = discovery.desired_hosts(meshes, registered, resolver)
    assert sorted(hosts) == ["both.example.eu", "bw.example.edu", "lat.example.eu", "only-lat.example.org",
                             "real-lat.example.edu", "untasked.example.org", "v6only.example.eu"]
    assert hosts["lat.example.eu"] == {"psetf/latency": "yes", "psetf/mesh/us-atlas": "yes", "psetf/gocdb": "yes",
                                       "psetf/ipv4": "yes"}
    assert hosts["both.example.eu"] == {"psetf/latency": "yes", "psetf/bandwidth": "yes",
                                        "psetf/gocdb": "yes", "psetf/ipv4": "yes", "psetf/ipv6": "yes"}
    assert hosts["only-lat.example.org"] == {"psetf/latency": "yes", "psetf/mesh/dune": "yes",
                                             "psetf/mesh/us-atlas": "yes", "psetf/ipv4": "yes"}
    assert hosts["untasked.example.org"] == {"psetf/bandwidth": "yes", "psetf/mesh/us-atlas": "yes", "psetf/ipv4": "yes"}
    assert [discovery.address_family(hosts[h]) for h in ("lat.example.eu", "both.example.eu", "v6only.example.eu")] \
        == ["ip-v4-only", "ip-v4v6", "ip-v6-only"]


def test_plan_creates_updates_and_keeps_labels_set_by_hand():
    desired = {"new.example.org": {"psetf/bandwidth": "yes", "psetf/ipv4": "yes"},
               "same.example.org": {"psetf/latency": "yes", "psetf/ipv4": "yes"},
               "changed.example.org": {"psetf/latency": "yes", "psetf/ipv4": "yes", "psetf/ipv6": "yes"}}
    existing = {"same.example.org": {"labels": {"psetf/latency": "yes", "psetf/ipv4": "yes"},
                                     "tag_address_family": "ip-v4-only"},
                "changed.example.org": {"labels": {"psetf/latency": "yes", "psetf/ipv4": "yes", "owner": "shawn"},
                                        "tag_address_family": "ip-v4-only"}}
    p = discovery.plan(desired, existing, {})
    assert list(p["create"]) == ["new.example.org"]
    assert p["create"]["new.example.org"]["tag_agent"] == "no-agent"
    assert p["create"]["new.example.org"]["tag_address_family"] == "ip-v4-only"
    assert p["update"] == {"changed.example.org": {"labels": {"owner": "shawn", "psetf/latency": "yes",
                                                              "psetf/ipv4": "yes", "psetf/ipv6": "yes"},
                                                   "tag_address_family": "ip-v4v6"}}
    assert p["delete"] == [] and p["missing"] == {}


def test_hosts_are_deleted_only_after_missing_several_runs():
    existing = {"h%d.example.org" % i: {"labels": {}} for i in range(20)}
    desired = {h: {} for h in existing if h != "h0.example.org"}
    missing = {}
    for run in (1, 2):
        p = discovery.plan(desired, existing, missing, delete_after=3)
        assert p["delete"] == [] and p["missing"] == {"h0.example.org": run}
        missing = p["missing"]
    p = discovery.plan(desired, existing, missing, delete_after=3)
    assert p["delete"] == ["h0.example.org"] and p["missing"] == {}
    # back again before the limit: its count is forgotten
    assert discovery.plan(dict(desired, **{"h0.example.org": {}}), existing, {"h0.example.org": 2})["missing"] == {}


def test_mass_deletion_is_refused():
    existing = {"h%d.example.org" % i: {"labels": {}} for i in range(10)}
    p = discovery.plan({"h0.example.org": {}}, existing, {h: 5 for h in existing}, max_delete_fraction=0.2)
    assert p["delete"] == [] and p["delete_blocked"]
    assert len(p["missing"]) == 9                          # still counted, deleted once sources recover


class FakeCheckMK(object):
    def __init__(self, hosts):
        self.existing, self.calls = hosts, []

    def ensure_folder(self, name, title):
        self.calls.append(("folder", name))

    def hosts(self, folder):
        return self.existing

    def create_hosts(self, folder, hosts):
        self.calls.append(("create", sorted(hosts)))

    def update_hosts(self, hosts):
        self.calls.append(("update", sorted(hosts)))

    def delete_hosts(self, names):
        self.calls.append(("delete", sorted(names)))

    def activate(self, site, wait=600):
        self.calls.append(("activate", site))
        return {}


def test_run_end_to_end_with_a_failing_mesh_and_registry(tmp_path):
    index = json.dumps([{"include": ["https://pwa.example/pub/config/good"]},
                        {"include": ["pwa.example/pub/config/broken"]}])
    bodies = {"https://pwa.example/pub/config/": index,
              "https://pwa.example/pub/config/good": template({"L": (["a.example.org"], "latencybg")},
                                                              ["a.example.org"]),
              "https://gocdb.example/?m=x&service_type=net.perfSONAR.Bandwidth": GOCDB,
              "https://gocdb.example/?m=x&service_type=net.perfSONAR.Latency": GOCDB}

    def fetcher(url):
        if url not in bodies:
            raise IOError("HTTP 500 for " + url)
        return bodies[url]

    config = {"mesh_index": "https://pwa.example/pub/config/", "gocdb": "https://gocdb.example/?m=x",
              "osg_topology": "https://topology.example/", "site": "psetf", "state_dir": str(tmp_path),
              "settle_seconds": 0}
    cmk = FakeCheckMK({"old.example.org": {"labels": {"psetf/bandwidth": "yes"}}})
    changes = discovery.run(config, cmk=cmk, fetcher=fetcher, resolver=lambda h: (True, False))
    assert sorted(changes["create"]) == ["a.example.org", "both.example.eu", "lat.example.eu"]
    assert changes["missing"] == {"old.example.org": 1}
    assert ("activate", "psetf") in cmk.calls
    saved = json.load(open(str(tmp_path / "meshes.json")))
    assert saved == {"GOOD": {"a.example.org": ["latency"]}}  # the broken mesh had no cache: skipped

    dry = discovery.run(config, dry_run=True, cmk=FakeCheckMK({}), fetcher=fetcher, resolver=lambda h: (True, False))
    assert sorted(dry["create"]) == ["a.example.org", "both.example.eu", "lat.example.eu"]
