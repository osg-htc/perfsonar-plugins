"""Tests for the CheckMK perfSONAR special agent and its check logic (no CheckMK, no network)."""
import io
import json
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from cmk_addons.plugins.perfsonar.lib import agent, checks  # noqa: E402

SUMMARY = {
    "templates": ["https://psconfig.aglt2.org/pub/auto/ps.example.org"],
    "host_memory": 16, "is_vm": 0, "cpus": 2, "cpu_cores": 8, "cpu_speed": 2400,
    "external_address": {"dns_name": "ps.example.org", "mtu": 9000, "speed": 10000000000,
                         "ipv4_address": "192.0.2.10", "ipv6_address": "2001:db8::10"},
}


class FakeResponse(object):
    def __init__(self, status, text):
        self.status_code, self.text = status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError("HTTP %s" % self.status_code)


class FakeSession(object):
    """Answers by path; http:// fails when `http_down`, so the agent falls back to https."""

    def __init__(self, pages, http_down=False, down=False):
        self.pages, self.http_down, self.down, self.calls = pages, http_down, down, []

    def get(self, url, timeout=None, headers=None, verify=None):
        self.calls.append((url, headers.get("Host"), verify))
        if self.down or (self.http_down and url.startswith("http://")):
            raise requests.exceptions.ConnectionError("refused")
        path = url.split("/", 3)[3]
        if "/" + path not in self.pages:
            return FakeResponse(404, "")
        return FakeResponse(200, self.pages["/" + path])


def pages(summary=SUMMARY):
    p = {agent.SUMMARY_PATH: json.dumps(summary)}
    p.update({"/pscheduler/stat/runs/" + s: str(i) for i, s in enumerate(agent.PSCHEDULER_STATES)})
    p["/pscheduler/stat/archiving/backlog"] = "42"
    return p


def sections(output):
    out, name = {}, None
    for line in output.splitlines():
        if line.startswith("<<<"):
            name = line.strip("<>").split(":")[0]
        else:
            out[name] = json.loads(line)
    return out


def run_agent(session, tmp_path, host="PS.example.org"):
    buf = io.StringIO()
    agent.target = lambda h: "192.0.2.10"
    agent.main([host, "--spool", str(tmp_path)], out=buf, session=session)
    return sections(buf.getvalue())


def test_agent_falls_back_to_https_and_reports_both_sections(tmp_path):
    session = FakeSession(pages(), http_down=True)
    s = run_agent(session, tmp_path)
    assert s["perfsonar_summary"]["ok"] and s["perfsonar_summary"]["url"].startswith("https://192.0.2.10/")
    assert s["perfsonar_pscheduler"]["stats"] == {"pending": 0, "running": 1, "overdue": 2, "missed": 3,
                                                  "failed": 4, "nonstart": 5, "finished": 6, "archiver_backlog": 42}
    assert all(h == "ps.example.org" and v is False for _u, h, v in session.calls)   # Host header, no verify
    spooled = json.load(open(str(tmp_path / "ps.example.org.json")))
    assert spooled["host"] == "ps.example.org" and spooled["data"]["cpus"] == 2


def test_agent_reports_an_unreachable_host_without_spooling(tmp_path):
    session = FakeSession(pages(), down=True)
    s = run_agent(session, tmp_path)
    assert not s["perfsonar_summary"]["ok"] and "Connection failed" in s["perfsonar_summary"]["error"]
    assert not s["perfsonar_pscheduler"]["ok"] and "unreachable" in s["perfsonar_pscheduler"]["error"]
    assert len(session.calls) == 2          # http + https for the summary, nothing more for a dead host
    assert os.listdir(str(tmp_path)) == []


def test_external_address_follows_the_monitored_name():
    data = {"external_address": {"dns_name": "other.example.org", "mtu": 1500},
            "interfaces": [{"hostnames": {"192.0.2.10": ["ps.example.org"]}, "ipv4_address": ["192.0.2.10"],
                            "ipv6_address": ["2001:db8::10"], "mtu": 9000, "speed": 1, "iface": "eth1",
                            "counters": {}}]}
    ext = agent.fix_external_address("ps.example.org", data)["external_address"]
    assert ext["dns_name"] == "ps.example.org" and ext["mtu"] == 9000 and ext["ipv4_address"] == "192.0.2.10"


def section_ok(summary=SUMMARY, host="ps.example.org"):
    return {"host": host, "ok": True, "url": "http://x", "data": summary}


def test_summary_and_http_checks():
    assert checks.check_summary(section_ok())[0][0] == checks.OK
    assert checks.check_summary({"ok": False, "error": "Connection failed (x)"})[0][:2] == (checks.CRIT, "Connection failed (x)")
    assert checks.check_summary(None)[0][0] == checks.UNKNOWN
    assert checks.check_http(section_ok())[0][0] == checks.OK
    assert checks.check_http({"ok": False})[0][0] == checks.CRIT


def test_meshes_accepts_the_new_pwa_names_and_flags_missing_urls():
    assert checks.check_meshes(section_ok())[0][:2] == (checks.OK, "Auto-URL configured correctly")
    by_ip = dict(SUMMARY, templates=["https://192.41.236.40/pub/auto/ps.example.org"])
    assert checks.check_meshes(section_ok(by_ip))[0][0] == checks.OK
    other = dict(SUMMARY, templates=["https://example.com/pub/auto/ps.example.org"])
    assert checks.check_meshes(section_ok(other))[0][:2] == (checks.CRIT, "Auto-mesh URL is missing/incorrect")
    assert checks.check_meshes(section_ok(dict(SUMMARY, templates=[])))[0][1] == "No meshes configured"
    assert checks.check_meshes({"ok": False})[0][0] == checks.UNKNOWN


def test_hardware_check_flags_small_memory_but_not_vms():
    state, summary, details, metrics = checks.check_hardware(section_ok())[0]
    assert state == checks.OK and summary == "CPU:2/8cores/2400Mhz RAM:16GB NIC:10.0Gbps/9000MTU/IPv6 enabled"
    assert metrics == [("mem_total_gb", 16)]
    assert checks.check_hardware(section_ok(dict(SUMMARY, host_memory=2)))[0][0] == checks.CRIT
    assert checks.check_hardware(section_ok(dict(SUMMARY, host_memory=2, is_vm=1)))[0][0] == checks.OK


def test_pscheduler_metrics_keep_the_names_the_analytics_use():
    state, summary, details, metrics = checks.check_pscheduler(
        {"ok": True, "stats": {"pending": 3, "finished": 10, "archiver_backlog": 0}})[0]
    assert state == checks.OK and metrics == [("pscheduler_pending", 3), ("pscheduler_finished", 10),
                                              ("pscheduler_archiver_backlog", 0)]
    assert checks.check_pscheduler({"ok": False, "error": "Error connecting"})[0][0] == checks.CRIT
