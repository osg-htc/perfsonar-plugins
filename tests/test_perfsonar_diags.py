"""Tests for the pscheduler diags active check (troubleshoot over the REST API), with a fake server."""
import datetime
import json
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from cmk_addons.plugins.perfsonar.lib import diags  # noqa: E402

HOST = "ps.example.org"


class Response(object):
    def __init__(self, status, body):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


class FakePScheduler(object):
    """A pScheduler API at https://192.0.2.10/pscheduler; `broken` names a fault to simulate."""

    def __init__(self, broken=None):
        self.broken, self.posted, self.requests = broken, [], []
        self.end = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)).isoformat()

    def _route(self, method, url, params=None, body=None):
        path = url.split("/pscheduler", 1)[1] if "/pscheduler" in url else url
        self.requests.append((method, url))
        if self.broken == "down":
            raise requests.exceptions.ConnectionError("refused")
        if method == "POST" and path == "/tasks":
            self.posted.append(body)
            return Response(200, "https://%s/pscheduler/tasks/t1" % HOST)
        routes = {
            "/": Response(200, "This is the pScheduler API server on %s." % HOST),
            "/api": Response(200, 5),
            "/clock": Response(200, {"synchronized": self.broken != "clock"}),
            "/archivers": Response(200, []), "/contexts": Response(200, []),
            "/tests": Response(200, []), "/tools": Response(200, []),
            "/status": Response(200, {"services": {s: {"overdue": "PT5M" if (self.broken == "runner" and s == "runner")
                                                       else None} for s, _n in diags.SERVICES},
                                      "limits": {"ok": True}, "runs": {"last-finished": "2026-11-27T16:00:00Z"}}),
            "/tasks/t1": Response(200, {"detail": {"first-run-href": "https://%s/pscheduler/tasks/t1/runs/r1" % HOST}}),
            "/tasks/t1/runs/r1": Response(200, {
                "end-time": self.end, "result-href": "https://%s/pscheduler/tasks/t1/runs/r1/result" % HOST,
                "state-display": "Finished",
                "archivings": [{"completed": True, "archived": self.broken != "archive"},
                               {"completed": True, "archived": True}]}),
            "/tasks/t1/runs/r1/result": Response(200, {"succeeded": True}),
        }
        if path.startswith("/stat/"):
            return Response(200, "7")
        return routes.get(path, Response(404, "not found"))

    def get(self, url, params=None, headers=None, timeout=None, verify=None):
        assert headers["Host"] == HOST and url.startswith("https://192.0.2.10/")   # address, Host header
        return self._route("GET", url, params)

    def post(self, url, data=None, headers=None, timeout=None, verify=None):
        return self._route("POST", url, body=json.loads(data))


def run(broken=None, quick=False):
    server = FakePScheduler(broken)
    state, output = diags.run(HOST, quick=quick, session=server, address="192.0.2.10")
    return state, output, server


def test_a_healthy_host_passes_with_an_idle_test_and_statistics():
    state, output, server = run()
    first = output.splitlines()[0]
    assert state == diags.OK and first.startswith("pScheduler appears to be functioning normally |")
    assert "runs_pending=7" in first and "idle_test_seconds=" in first
    assert "Idle test: finished and archived" in output
    assert server.posted[0]["test"]["type"] == "idle" and len(server.posted[0]["archives"]) == 2
    assert all(u.startswith("https://192.0.2.10/") for _m, u in server.requests)   # hrefs rewritten to the address


def test_an_unsynchronized_clock_is_a_warning():
    state, output, _ = run("clock")
    assert state == diags.WARN and "UNSYNCHRONIZED" in output


def test_a_stopped_service_is_critical():
    state, output, _ = run("runner")
    assert state == diags.CRIT and output.startswith("The Runner service does not appear to be running")


def test_a_failed_archiving_is_critical():
    state, output, _ = run("archive")
    assert state == diags.CRIT and output.startswith("One or more archivings of the idle test failed")


def test_an_unreachable_api_is_critical():
    state, output, _ = run("down")
    assert state == diags.CRIT and output.startswith("Error connecting to the pScheduler API")


def test_quick_skips_the_idle_test():
    state, output, server = run(quick=True)
    assert state == diags.OK and "no idle test" in output and not server.posted
