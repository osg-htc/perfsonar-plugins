"""Active check "perfSONAR services: pscheduler diags": pscheduler troubleshoot over the REST API.

Replaces `pscheduler troubleshoot --host HOST --stats` (run daily by the ETF psetf), which needs
the pScheduler client tools; the CheckMK image has none. The same steps, against the host's
pScheduler API (pscheduler-core/pscheduler-core/troubleshoot in perfsonar/pscheduler):

  1. the API answers                       GET /pscheduler/, /pscheduler/api
  2. the clock is synchronized             GET /pscheduler/clock        (WARN if not; troubleshoot: "not fatal")
  3. the API works                         GET archivers, contexts, tests, tools
  4. its services run, limits are fine     GET /pscheduler/status
  5. statistics                            GET /pscheduler/stat/...     (performance data)
  6. an idle test runs and is archived     POST /pscheduler/tasks (idle PT1S, two bitbucket archives),
                                           wait for the run, check the result and both archivings
Not done: troubleshoot's MTU measurement, which needs a local pScheduler on the checking side.

Nagios plugin output: one status line with performance data, details below; exit 0/1/2/3.
"""
import argparse
import datetime
import json
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from cmk_addons.plugins.perfsonar.lib.agent import make_session, target

OK, WARN, CRIT, UNKNOWN = 0, 1, 2, 3
STATE_NAMES = ("OK", "WARNING", "CRITICAL", "UNKNOWN")
SERVICES = (("ticker", "Ticker"), ("scheduler", "Scheduler"), ("runner", "Runner"), ("archiver", "Archiver"))
STATISTICS = (("runs_pending", "runs/pending"), ("runs_running", "runs/running"), ("runs_finished", "runs/finished"),
              ("runs_overdue", "runs/overdue"), ("runs_missed", "runs/missed"), ("runs_failed", "runs/failed"),
              ("runs_nonstart", "runs/nonstart"), ("archiving_backlog", "archiving/backlog"))
IDLE_TASK = {
    "schema": 1,
    "test": {"type": "idle", "spec": {"schema": 1, "duration": "PT1S"}},
    "schedule": {},
    "archives": [{"archiver": "bitbucket", "data": {}, "transform": {"script": "."}, "ttl": "PT1M"},
                 {"archiver": "bitbucket", "data": {}, "transform": {"script": "."}, "ttl": "PT1M"}],
}


class Failed(Exception):
    """A fatal finding: the check ends CRITICAL with this message."""


class API(object):
    """GETs and POSTs to one host's pScheduler API over HTTPS, IPv4 address with the host name as Host."""

    def __init__(self, host, address, session, deadline, timeout=30):
        self.host, self.address, self.session, self.deadline, self.timeout = host, address, session, deadline, timeout
        self.base = "https://%s/pscheduler" % ("[%s]" % address if ":" in address else address)

    def _url(self, path_or_url):
        if path_or_url.startswith("http"):            # an href the server returned: keep its path, use our address
            parts = urlsplit(path_or_url)
            return urlunsplit(("https", urlsplit(self.base).netloc, parts.path, parts.query, ""))
        return self.base + path_or_url

    def _timeout(self):
        left = self.deadline - time.time()
        if left < 1:
            raise Failed("Gave up: the check's time limit was reached")
        return (min(15, left), min(self.timeout, left))

    def get(self, path_or_url, params=None, json_body=True):
        r = self.session.get(self._url(path_or_url), params=params, headers={"Host": self.host},
                             timeout=self._timeout(), verify=False)
        return r.status_code, (_json(r) if json_body else r.text)

    def post(self, path, body):
        r = self.session.post(self._url(path), data=json.dumps(body), timeout=self._timeout(), verify=False,
                              headers={"Host": self.host, "Content-Type": "application/json"})
        return r.status_code, _json(r)


def _json(r):
    try:
        return r.json()
    except ValueError:
        return r.text.strip()


def _iso(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def check(api, quick=False, notes=None, perf=None, sleep=time.sleep):
    """Run the steps; returns the (state, summary). Fatal findings raise Failed."""
    notes = notes if notes is not None else []
    perf = perf if perf is not None else []
    state = OK

    status, root = api.get("/", json_body=False)
    if status != 200:
        raise Failed("pScheduler API not answering (HTTP %s)" % status)
    status, level = api.get("/api")
    if status != 200:
        raise Failed("Unable to fetch the API level (HTTP %s)" % status)
    notes.append("API level: %s" % level)

    status, clock = api.get("/clock")
    if status != 200 or not isinstance(clock, dict):
        raise Failed("Unable to check the clock (HTTP %s)" % status)
    if not clock.get("synchronized"):
        state = WARN
        notes.append("Clock: UNSYNCHRONIZED (troubleshoot does not consider this fatal; it spoils latency results)")
    else:
        notes.append("Clock: synchronized")

    for endpoint in ("archivers", "contexts", "tests", "tools"):
        status, _body = api.get("/" + endpoint)
        if status != 200:
            raise Failed("API endpoint /%s failed (HTTP %s)" % (endpoint, status))
    notes.append("API endpoints: OK")

    status, server = api.get("/status")
    if status != 200 or not isinstance(server, dict) or server.get("services") is None:
        raise Failed("Server did not provide service status (HTTP %s)" % status)
    for key, name in SERVICES:
        service = server["services"].get(key)
        if service is None:
            raise Failed("pScheduler did not report status for the %s service" % name)
        if service.get("overdue") is not None:
            raise Failed("The %s service does not appear to be running" % name)
    notes.append("Services: %s running" % ", ".join(n for _k, n in SERVICES))
    limits = server.get("limits")
    if isinstance(limits, dict) and not limits.get("ok", False):
        raise Failed("Limit configuration: %s" % limits.get("error", "Unspecified error"))
    runs = server.get("runs") or {}
    for key in ("last-scheduled", "last-finished"):
        if runs.get(key):
            notes.append("%s: %s" % (key.replace("-", " ").capitalize(), runs[key]))

    for name, path in STATISTICS:
        status, value = api.get("/stat/" + path, json_body=False)
        if status == 200:
            try:
                perf.append((name, int(str(value).strip())))
            except ValueError:
                pass

    if quick:
        return state, "pScheduler appears to be functioning normally (no idle test)"

    started = time.time()
    status, task_url = api.post("/tasks", IDLE_TASK)
    if status != 200 or not isinstance(task_url, str):
        raise Failed("Failed to post the idle test task (HTTP %s: %s)" % (status, str(task_url)[:200]))
    status, task = api.get(task_url, params={"detail": True})
    first_run = ((task or {}).get("detail") or {}).get("first-run-href") if isinstance(task, dict) else None
    if status != 200 or not first_run:
        raise Failed("Server returned incomplete data for the idle test task (HTTP %s)" % status)
    status, run = api.get(first_run)
    if status == 404:
        raise Failed("The server never scheduled the idle test; it is too busy or pscheduler-scheduler has a problem")
    if status != 200 or not isinstance(run, dict) or "end-time" not in run or "result-href" not in run:
        raise Failed("Error getting the idle test's run (HTTP %s)" % status)
    wait = (_iso(run["end-time"]) - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if wait > 0:
        if time.time() + wait > api.deadline:
            raise Failed("The idle test is scheduled too late (%.0f s from now)" % wait)
        sleep(wait)
    status, run = api.get(first_run, params={"wait-merged": True})
    if status == 404:
        raise Failed("The idle test was scheduled but not run; check that pscheduler-runner is running")
    if status != 200 or not isinstance(run, dict):
        raise Failed("Error while waiting for the idle test (HTTP %s)" % status)
    status, result = api.get(run["result-href"], params={"wait-merged": True})
    if status != 200 or not isinstance(result, dict):
        raise Failed("The idle test did not produce a result (HTTP %s)" % status)
    if not result.get("succeeded"):
        raise Failed("The idle test failed to run properly (%s)" % (run.get("state-display") or "no state"))

    wanted, done = len(IDLE_TASK["archives"]), 0
    end = time.time() + 5
    archived = []
    while True:
        status, run = api.get(first_run)
        if status != 200 or not isinstance(run, dict):
            raise Failed("Unable to fetch the archiving status (HTTP %s)" % status)
        archivings = run.get("archivings") or []
        done = sum(1 for a in archivings if a.get("completed", a.get("archived", False)))
        archived = [a for a in archivings if a.get("archived", False)]
        if done >= wanted or time.time() >= end:
            break
        sleep(1)
    if done < wanted:
        raise Failed("Archiving of the idle test never completed")
    if len(archived) < wanted:
        raise Failed("One or more archivings of the idle test failed")
    took = time.time() - started
    perf.append(("idle_test_seconds", round(took, 1)))
    notes.append("Idle test: finished and archived in %.0f s" % took)
    return state, "pScheduler appears to be functioning normally"


def run(host, timeout=30, limit=240, quick=False, session=None, address=None):
    """(exit code, output) for one host."""
    host = host.strip().lower()
    api = API(host, address or target(host), session or make_session(), time.time() + limit, timeout)
    notes, perf = [], []
    try:
        state, summary = check(api, quick, notes, perf)
    except Failed as e:
        state, summary = CRIT, str(e)
    except requests.exceptions.RequestException as e:
        state, summary = CRIT, "Error connecting to the pScheduler API (%s)" % e
    perfdata = " ".join("%s=%s" % (k, v) for k, v in perf)
    output = "%s%s" % (summary, (" | " + perfdata) if perfdata else "")
    if notes:
        output += "\n" + "\n".join(notes)
    return state, output


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(description="pscheduler troubleshoot over the REST API (Nagios plugin)")
    ap.add_argument("host")
    ap.add_argument("--timeout", type=float, default=30, help="per request (seconds)")
    ap.add_argument("--limit", type=float, default=240, help="for the whole check (seconds)")
    ap.add_argument("--quick", action="store_true", help="skip the idle test")
    args = ap.parse_args(argv)
    state, output = run(args.host, args.timeout, args.limit, args.quick)
    out.write(output + "\n")
    return state


if __name__ == "__main__":
    sys.exit(main())
