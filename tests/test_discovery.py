"""Tests for host discovery (src/etf_ps_plugin.py) without ncgx or the network.

Run with:  python3 -m pytest tests/
"""
import importlib.util
import json
import os
import socket
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


class _Inventory(object):
    """Records what the plugin hands to ncgx."""
    instances = []

    def __init__(self, *args):
        self.items = []
        _Inventory.instances.append(self)

    def add(self, *args, **kwargs):
        self.items.append((args, kwargs))

    def add_all(self, *args, **kwargs):
        self.items.append((args, kwargs))

    def serialize(self):
        pass

    def get_all_hosts(self):
        return sorted(set(a[0] for a, _ in self.items))


@pytest.fixture()
def plugin(monkeypatch):
    inventory = types.ModuleType("ncgx.inventory")
    inventory.Hosts = inventory.Checks = inventory.Groups = _Inventory
    monkeypatch.setitem(sys.modules, "ncgx", types.ModuleType("ncgx"))
    monkeypatch.setitem(sys.modules, "ncgx.inventory", inventory)
    spec = importlib.util.spec_from_file_location("etf_ps_plugin", os.path.join(HERE, "..", "src", "etf_ps_plugin.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _Inventory.instances = []
    return module


def template(tasks, hosts=None):
    """A pSConfig template as PWA and pwa-next publish it."""
    addresses, groups, tests, out_tasks = {}, {}, {}, {}
    for name, (group, test_type) in tasks.items():
        for key in ("addresses", "a-addresses", "b-addresses"):
            for h in group.get(key, []):
                addresses[h] = {"address": h, "host": h}
        groups[name] = dict(group, **{k: [{"name": h} for h in v] for k, v in group.items() if k.endswith("addresses")})
        tests[name] = {"type": test_type, "spec": {}}
        out_tasks[name] = {"group": name, "test": name}
    return json.dumps({"addresses": addresses, "groups": groups, "tests": tests, "tasks": out_tasks,
                       "hosts": dict((h, {}) for h in (hosts or addresses)), "_meta": {"display-name": "x"}})


def test_members_are_typed_by_what_their_meshes_test(plugin):
    members = plugin.get_members(template({
        "Latency": ({"type": "mesh", "addresses": ["a.example.org", "B.example.org"]}, "latencybg"),
        "Bandwidth": ({"type": "disjoint", "a-addresses": ["b.example.org"], "b-addresses": ["c.example.org"]},
                      "throughput"),
        "Trace": ({"type": "mesh", "addresses": ["d.example.org"]}, "trace"),
    }, hosts=["a.example.org", "b.example.org", "c.example.org", "d.example.org", "e.example.org"]))
    assert members == {"a.example.org": {"latency"}, "b.example.org": {"latency", "bandwidth"},
                       "c.example.org": {"bandwidth"}, "d.example.org": {"bandwidth"}, "e.example.org": set()}


def test_disabled_tasks_are_ignored(plugin):
    tpl = json.loads(template({"Latency": ({"type": "mesh", "addresses": ["a.example.org"]}, "latencybg")}))
    tpl["tasks"]["Latency"]["disabled"] = True
    assert plugin.get_members(json.dumps(tpl)) == {"a.example.org": set()}


def test_a_failing_mesh_falls_back_to_its_last_good_members(plugin, monkeypatch, tmp_path):
    cache = str(tmp_path / "meshes.json")
    index = json.dumps([{"include": ["https://ps.example.org/pub/config/one"]},
                        {"include": ["ps.example.org/pub/config/two"]}])
    meshes = {"https://ps.example.org/pub/config/one":
              template({"L": ({"type": "mesh", "addresses": ["a.example.org", "b.example.org"]}, "latencybg")}),
              "https://ps.example.org/pub/config/two":
              template({"T": ({"type": "mesh", "addresses": ["c.example.org"]}, "throughput")})}
    monkeypatch.setattr(plugin, "request", lambda url, *a, **k: meshes[url])
    groups, members = plugin.get_active_sonars(index, cache_file=cache)
    assert groups == {"ONE": {"a.example.org", "b.example.org"}, "TWO": {"c.example.org"}}
    assert members["c.example.org"] == {"bandwidth"}

    def failing(url, *a, **k):
        if url.endswith("/two"):
            raise IOError("HTTP 500")
        return meshes[url]
    monkeypatch.setattr(plugin, "request", failing)
    groups, members = plugin.get_active_sonars(index, cache_file=cache)
    assert groups["TWO"] == {"c.example.org"} and members["c.example.org"] == {"bandwidth"}

    os.remove(cache)                      # no cache: the mesh is skipped, the others stay
    groups, members = plugin.get_active_sonars(index, cache_file=cache)
    assert "TWO" not in groups and groups["ONE"] == {"a.example.org", "b.example.org"}


def test_run_survives_a_registry_outage_and_types_unregistered_hosts(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "MESH_CACHE", str(tmp_path / "meshes.json"))
    index = json.dumps([{"include": ["https://ps.example.org/pub/config/m"]}])
    mesh = template({"L": ({"type": "mesh", "addresses": ["lat.example.org"]}, "latencybg"),
                     "B": ({"type": "mesh", "addresses": ["bw.example.org"]}, "throughput")})

    def fake_request(url, *a, **k):
        if "gocdb" in url:
            raise IOError("GOCDB down")
        if "oim" in url:
            return b"<ResourceSummary/>"
        return index if url.endswith("/pub/config/") else mesh
    monkeypatch.setattr(plugin, "request", fake_request)
    monkeypatch.setattr(plugin, "get_fqdn", lambda host: True)
    monkeypatch.setattr(plugin.socket, "getaddrinfo",
                        lambda host, *a: [(socket.AF_INET6 if host.startswith("lat") else socket.AF_INET,)])
    plugin.run("https://ps.example.org/pub/config/", "https://gocdb.example/?x=1", "https://oim.example/",
               None, None, False)
    hosts, groups, checks = _Inventory.instances
    assert sorted(a for a, _ in hosts.items) == [("bw.example.org", ("bandwidth",)), ("lat.example.org", ("latency",))]
    ipv6 = [k["hosts"] for a, k in checks.items if a and a[0] == "perfSONAR services: web/https IPv6"]
    assert ipv6 == [("lat.example.org",)]       # only the host that has an IPv6 address
