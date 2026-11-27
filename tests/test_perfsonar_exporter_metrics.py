"""The perfSONAR host exporter (perfSONAR 5.1+) in the special agent and the checks."""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from cmk_addons.plugins.perfsonar.lib import agent, checks, exporter as analytics  # noqa: E402
from test_perfsonar_plugin import SUMMARY, FakeSession, pages, sections  # noqa: E402

NOW = 1795806000.0
EXPORTER = """# HELP perfsonar_bundle Information about perfSONAR bundle installed
# TYPE perfsonar_bundle gauge
perfsonar_bundle{type="perfsonar-toolkit",version="5.2.6-1.el9"} 1
perfsonar_pscheduler_paused{state="is_paused"} 0
perfsonar_pscheduler_archiving{state="backlog"} 4
perfsonar_pscheduler_archiving{state="upcoming"} 1
perfsonar_pscheduler_http_queue{state="backlog"} 0
perfsonar_pscheduler_http_queue{state="length"} 0
perfsonar_pscheduler_runs{state="pending"} 25111
perfsonar_pscheduler_runs{state="on-deck"} 0
perfsonar_pscheduler_runs{state="running"} 44
perfsonar_pscheduler_runs{state="finished"} 132561
perfsonar_pscheduler_runs{state="overdue"} 0
perfsonar_pscheduler_runs{state="missed"} 2
perfsonar_pscheduler_runs{state="failed"} 1745
perfsonar_pscheduler_runs{state="nonstart"} 168
perfsonar_psconfig_pscheduler_agent_start_time{guid="ee7b"} %(start)d
perfsonar_psconfig_pscheduler_agent_end_time{guid="ee7b"} %(end)d
perfsonar_psconfig_pscheduler_tasks{guid="ee7b",src="remote"} 349
perfsonar_psconfig_pscheduler_tasks{guid="ee7b",src="remote",url="http://psconfig.opensciencegrid.org/pub/auto/ps.example.org"} 349
perfsonar_psconfig_pscheduler_remote{url="http://www.example.org/mesh-local.json",configure_archives="True"} 1
perfsonar_psconfig_pscheduler_remote{url="http://psconfig.opensciencegrid.org/pub/auto/ps.example.org",configure_archives="True"} 1
perfsonar_psconfig_pscheduler_agent{client_uuid="0E3A"} 1
perfsonar_psconfig_grafana_agent_end_time{guid="22d7"} %(end)d
perfsonar_host_registered{uuid="09EA"} %(registered)d
node_load1 0.87
"""


def exporter_text(age=1800, registered=1):
    return EXPORTER % {"start": NOW - age - 100, "end": NOW - age, "registered": registered}


def run(summary=True, exporter=True):
    p = pages() if summary else {}
    if not summary:   # keep only the pScheduler API
        p.update({k: v for k, v in pages().items() if k != agent.SUMMARY_PATH})
    if exporter:
        p[agent.EXPORTER_PATH] = exporter_text()
    session = FakeSession(p)
    buf = io.StringIO()
    agent.target = lambda h: "192.0.2.10"
    agent.dns_names = lambda h, a: []
    agent.main(["ps.example.org", "--spool", "/nonexistent/dir"], out=buf, session=session)
    return sections(buf.getvalue()), session


def test_prometheus_text_is_parsed_to_perfsonar_samples():
    samples = agent.parse_prometheus(exporter_text())
    assert ["perfsonar_bundle", {"type": "perfsonar-toolkit", "version": "5.2.6-1.el9"}, 1.0] in samples
    assert not any(name.startswith("node_") for name, _l, _v in samples)


def test_with_the_exporter_the_pscheduler_statistics_need_no_stat_requests():
    s, session = run()
    assert s["perfsonar_exporter"]["ok"]
    ps = s["perfsonar_pscheduler"]
    assert ps["source"] == "exporter" and ps["stats"]["pending"] == 25111 and ps["stats"]["archiver_backlog"] == 4
    assert ps["stats"]["on_deck"] == 0 and ps["stats"]["http_queue_length"] == 0 and ps["stats"]["paused"] == 0
    assert not any("/pscheduler/stat/" in u for u, _h, _v in session.calls)


def test_without_the_exporter_the_statistics_come_from_the_api():
    s, session = run(exporter=False)
    assert not s["perfsonar_exporter"]["ok"] and s["perfsonar_pscheduler"]["ok"]
    assert "source" not in s["perfsonar_pscheduler"] and any("/pscheduler/stat/" in u for u, _h, _v in session.calls)


def test_a_missing_toolkit_summary_is_a_warning_when_the_exporter_answers():
    s, _ = run(summary=False)
    state, summary, _d, _m = checks.check_summary(s["perfsonar_summary"], s["perfsonar_exporter"])[0]
    assert state == checks.WARN and "not served (perfsonar-toolkit 5.2.6-1.el9)" in summary
    assert checks.check_http(s["perfsonar_summary"], s["perfsonar_exporter"])[0][0] == checks.OK
    state, summary, _d, _m = checks.check_meshes(s["perfsonar_summary"], exporter=s["perfsonar_exporter"])[0]
    assert (state, summary) == (checks.OK, "Auto-URL configured correctly")    # URLs from pSConfig
    s, _ = run(summary=False, exporter=False)
    assert checks.check_summary(s["perfsonar_summary"], s["perfsonar_exporter"])[0][0] == checks.CRIT


def test_the_summary_service_shows_the_bundle():
    s, _ = run()
    assert checks.check_summary(s["perfsonar_summary"], s["perfsonar_exporter"])[0][1] == \
        "Toolkit metadata successfully retrieved (perfsonar-toolkit 5.2.6-1.el9)"


def section(text):
    return {"ok": True, "samples": agent.parse_prometheus(text)}


def test_psconfig_agent_age_thresholds_and_remotes():
    for age, state in ((1800, checks.OK), (8 * 3600, checks.WARN), (30 * 3600, checks.CRIT)):
        st, summary, details, metrics = checks.check_psconfig_agent(section(exporter_text(age=age)), NOW)[0]
        assert st == state and "2 remote configuration(s)" in summary and "mesh-local.json" in details
        assert ("psconfig_remotes", 2) in metrics and ("psconfig_tasks", 349.0) in metrics
    no_remotes = "\n".join(l for l in exporter_text().splitlines() if "_remote{" not in l)
    assert checks.check_psconfig_agent(section(no_remotes), NOW)[0][0] == checks.WARN
    assert checks.check_psconfig_agent({"ok": False}, NOW)[0][0] == checks.UNKNOWN


def test_lookup_service_registration():
    assert checks.check_lookup_registration(section(exporter_text()))[0][0] == checks.OK
    assert checks.check_lookup_registration(section(exporter_text(registered=0)))[0][0] == checks.WARN


def test_the_analytics_status_documents_keep_n_streams_fields():
    s, _ = run()
    rows = checks.check_pscheduler(s["perfsonar_pscheduler"])[0][3]
    perf = " ".join("%s=%s" % (k, v) for k, v in rows)
    doc = analytics.status_document("ps.example.org", NOW, analytics.parse_perfdata(perf))
    assert sorted(k for k in doc if k.startswith("ps_services")) == \
        sorted("ps_services_pscheduler_" + f for f in analytics.STATUS_FIELDS)


def test_a_hanging_exporter_does_not_starve_the_pscheduler_statistics():
    seen = {}

    class Slow(FakeSession):
        def get(self, url, timeout=None, headers=None, verify=None):
            if agent.EXPORTER_PATH in url:
                seen["timeout"] = timeout
                raise agent.requests.exceptions.ReadTimeout("504 after 60 s")
            return FakeSession.get(self, url, timeout, headers, verify)

    buf = io.StringIO()
    agent.target = lambda h: "192.0.2.10"
    agent.dns_names = lambda h, a: []
    agent.main(["ps.example.org", "--spool", "/nonexistent/dir"], out=buf, session=Slow(pages()))
    s = sections(buf.getvalue())
    assert seen["timeout"][1] <= agent.EXPORTER_TIMEOUT                 # the exporter gets at most 15 s
    assert not s["perfsonar_exporter"]["ok"] and s["perfsonar_pscheduler"]["ok"]   # statistics from the API
