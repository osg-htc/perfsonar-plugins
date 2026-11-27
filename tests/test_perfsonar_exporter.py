"""Tests for the psetf exporter (analytics documents from CheckMK, replacing n-stream)."""
import json
import os
import sys

import pytest
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from cmk_addons.plugins.perfsonar.lib import exporter  # noqa: E402

HEADER = {"measurement_agent": "psetf.aglt2.org", "destination": "/exchange/osg.status.raw/perfsonar.summary.meta"}
SUMMARY = {
    "external_address": {"dns_name": "ps.example.org", "mtu": 9000, "counters": {"rx": "12", "tx": "x"}},
    "interfaces": [{"iface": "eth0"}],
    "services": [{"name": "owamp", "enabled": "1", "testing_ports": [{"type": "test", "min_port": 8760, "max_port": 9960},
                                                                     {"min_port": 1}]},
                 {"name": "psconfig", "enabled": "false"}, {"no_name": True}],
    "location": {"latitude": "42.28", "longitude": "-83.74"},
    "ntp": {"offset": "0.25", "synchronized": "1", "stratum": "2", "reach": "377x"},
    "cpus": "2", "cpu_cores": 16, "cpu_speed": "2400", "privacy_text": None, "toolkit_name": "unknown",
    "administrator": {"name": "A", "email": None},
}


def test_meta_document_matches_the_n_stream_transformation():
    d = exporter.meta_document(json.loads(json.dumps(SUMMARY)), 1795795324.714, HEADER)
    assert d["type"] == "meta" and d["ps_host"] == "ps.example.org" and d["timestamp"] == 1795795324714
    assert d["header"] == HEADER and "interfaces" not in d
    assert d["services"] == {"owamp": {"enabled": 1, "testing_ports": {"test": {"min_port": 8760, "max_port": 9960}}},
                             "psconfig": {"enabled": 0}}
    assert d["geolocation"] == "42.28,-83.74" and "location" not in d
    assert d["ntp"] == {"offset": 0.25, "synchronized": 1, "stratum": 2, "reach": None}
    assert d["external_address"]["counters"] == {"rx": 12, "tx": None}
    assert d["cpus"] == 2 and d["cpu_speed"] == 2400.0
    assert "privacy_text" not in d and "toolkit_name" not in d and d["administrator"] == {"name": "A"}


def test_status_document_matches_n_stream():
    perf = exporter.parse_perfdata("pscheduler_pending=3;;;; pscheduler_archiver_backlog=0 mem_total_gb=16")
    d = exporter.status_document("ps.example.org", 1795802511, perf)
    assert d == {"type": "status", "ps_host": "ps.example.org", "timestamp": 1795802511000,
                 "ps_services_pscheduler_pending": 3.0, "ps_services_pscheduler_archiver_backlog": 0.0}


def spool(tmp_path, host="ps.example.org", ts=1000.0):
    d = tmp_path / "summaries"
    d.mkdir(exist_ok=True)
    (d / (host + ".json")).write_text(json.dumps({"host": host, "timestamp": ts, "data": SUMMARY}))
    return str(d)


def test_meta_once_a_day_and_status_only_for_new_results(tmp_path):
    config = dict(exporter.DEFAULTS)
    rows = [["ps.example.org", 2000, "pscheduler_pending=1"], ["down.example.org", 2000, ""]]
    docs, state = exporter.collect({}, config, 5000, spool(tmp_path), rows)
    assert [d["type"] for d in docs] == ["meta", "status"]      # no perf data: no status document
    docs, state = exporter.collect(state, config, 5000 + 3600, spool(tmp_path), rows)
    assert docs == []                                           # same result, meta sent within a day
    rows[0][1] = 5600
    docs, state = exporter.collect(state, config, 5000 + 86400, spool(tmp_path), rows)
    assert [d["type"] for d in docs] == ["meta", "status"]


class FakeSession(object):
    def __init__(self, fail=False):
        self.fail, self.puts = fail, []

    def put(self, url, headers=None, data=None, timeout=None):
        self.puts.append((url, json.loads(data)))
        r = requests.Response()
        r.status_code = 500 if self.fail else 200
        return r


def site(tmp_path, monkeypatch, mode):
    root = tmp_path / "site"
    (root / "etc" / "psetf").mkdir(parents=True)
    (root / "var" / "psetf").mkdir(parents=True)
    (root / "etc" / "psetf" / "exporter.json").write_text(json.dumps({"mode": mode, "ls_uri": "http://collector.example"}))
    spool(root / "var" / "psetf")
    monkeypatch.setattr(exporter, "livestatus", lambda q, p: [["ps.example.org", 2000, "pscheduler_pending=1"]])
    return str(root)


def test_file_mode_writes_documents_and_sends_nothing(tmp_path, monkeypatch):
    root = site(tmp_path, monkeypatch, "file")
    session = FakeSession()
    exporter.run(root, now=5000, session=session)
    lines = open(os.path.join(root, "var", "psetf", "exporter-out.jsonl")).read().splitlines()
    assert [json.loads(l)["type"] for l in lines] == ["meta", "status"] and session.puts == []


def test_send_mode_puts_each_document_and_retries_after_a_failure(tmp_path, monkeypatch):
    root = site(tmp_path, monkeypatch, "send")
    with pytest.raises(requests.exceptions.HTTPError):
        exporter.run(root, now=5000, session=FakeSession(fail=True))
    session = FakeSession()
    exporter.run(root, now=5100, session=session)               # nothing was marked sent: all again
    assert [(u, d["type"]) for u, d in session.puts] == [("http://collector.example", "meta"),
                                                         ("http://collector.example", "status")]
