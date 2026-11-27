"""psetf exporter: the analytics documents, from CheckMK (replaces n-stream).

Sends, as n-stream did, one JSON document per HTTP PUT to the Logstash collector
(http://ps-collection.atlas-ml.org, which fills the ps_meta and ps_status indices read by the
sand-ci analytics: AlarmsAndAlerts, ps-dash, triage_dashboard):

  type "meta"    the toolkit summary of each host, once a day per host (check_ps sent it when its
                 cached copy was a day old), from the special agent's spool
                 ($OMD_ROOT/var/psetf/summaries/<host>.json), transformed exactly as n-stream's
                 nstream/backends/meta.py did, with the same "header".
  type "status"  the pScheduler statistics of each new "perfSONAR services: pscheduler" result
                 (n-stream: nstream/backends/status.py), read from the site through Livestatus.

Runs inside the CheckMK site (site cron, every 5 minutes). Configuration, all optional:
$OMD_ROOT/etc/psetf/exporter.json
  {"mode": "file" | "send",                      default "file": write documents to "output"
   "ls_uri": "http://ps-collection.atlas-ml.org", "output": "$OMD_ROOT/var/psetf/exporter-out.jsonl",
   "measurement_agent": "psetf.aglt2.org", "meta_every": 86400}
Only "send" reaches the collector; staging stays on "file" so the analytics see no duplicates.
"""
import argparse
import copy
import glob
import json
import logging
import os
import socket
import sys
import time

import requests

log = logging.getLogger("psetf.exporter")

DEFAULTS = {"mode": "file", "ls_uri": "http://ps-collection.atlas-ml.org", "measurement_agent": "psetf.aglt2.org",
            "meta_every": 86400, "meta_destination": "/exchange/osg.status.raw/perfsonar.summary.meta"}
PSCHEDULER_SERVICE = "perfSONAR services: pscheduler"


# ---------------------------------------------------------------- meta: n-stream's backends/meta.py, unchanged
def convert_to_float(d, tags):
    for t in tags:
        if t not in d:
            continue
        v = d[t]
        if not v:
            continue
        if isinstance(v, float):
            continue
        d[t] = float(v)


def convert_to_int(d, tags):
    for t in list(tags):
        if t not in d:
            continue
        v = d[t]
        if not v:
            continue
        if isinstance(v, int):
            continue
        if isinstance(v, str) and v.isdigit():
            d[t] = int(v)
        else:
            d[t] = None


def fix_enabled(services):
    for d in services:
        for k, v in d.items():
            if k == "enabled" and v in ["1", "true", "True"]:
                d[k] = 1
            if k == "enabled" and v in ["0", "false", "False"]:
                d[k] = 0


def isfloat(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


def clean(data):
    to_del = []
    for tag in data.keys():
        if data[tag] is None or data[tag] == 'unknown':
            to_del.append(tag)
        if type(data[tag]) is dict:
            clean(data[tag])
    for tag in to_del:
        del data[tag]


def meta_document(summary, timestamp, header):
    """n-stream's meta transformation of one toolkit summary (as check_ps queued it)."""
    m = dict(summary)
    m["timestamp"] = timestamp
    m["header"] = header
    data = dict()
    data['type'] = 'meta'
    data.update(m)
    data.pop('interfaces', None)
    data['timestamp'] = int(float(m['timestamp']) * 1000)
    data['ps_host'] = data.get('external_address', {}).get('dns_name')
    if 'services' in data.keys():
        fix_enabled(data['services'])
    if "services" in data:
        sers = copy.deepcopy(data["services"])
        data["services"] = {}
        for s in sers:
            if "name" in s:
                service_name = s["name"]
                del s["name"]
                tps = {}
                if "testing_ports" in s:
                    for tp in s["testing_ports"]:
                        if 'type' not in tp:
                            continue
                        tps[tp['type']] = {"min_port": tp["min_port"], "max_port": tp["max_port"]}
                    s['testing_ports'] = tps
                data["services"][service_name] = s
    clean(data)
    if 'location' in data.keys():
        lat = data['location'].get('latitude', 0)
        lgt = data['location'].get('longitude', 0)
        if lat and lgt and isinstance(lat, str) and isinstance(lgt, str):
            if isfloat(lat) and isfloat(lgt):
                data['geolocation'] = "%s,%s" % (lat, lgt)
        del data['location']
    if 'ntp' in data.keys():
        n = data['ntp']
        convert_to_float(n, ['delay', 'dispersion', 'offset'])
        convert_to_int(n, ['synchronized', 'stratum', 'reach', 'polling_interval'])
    if 'external_address' in data.keys():
        ea = data['external_address']
        if 'counters' in ea.keys():
            convert_to_int(ea['counters'], ea['counters'].keys())
    convert_to_int(data, ['cpu_cores', 'cpus'])
    convert_to_float(data, ['cpu_speed'])
    return data


# ---------------------------------------------------------------- status: n-stream's backends/status.py
def parse_perfdata(perf_data):
    """'pscheduler_pending=3;;;; pscheduler_failed=0' -> {"pending": 3.0, "failed": 0.0}"""
    out = {}
    for item in (perf_data or "").split():
        name, _, rest = item.partition("=")
        value = rest.split(";", 1)[0]
        if not name.startswith("pscheduler_"):
            continue
        try:
            out[name[len("pscheduler_"):]] = float(value)
        except ValueError:
            pass
    return out


def status_document(host, last_check, perf):
    data = {"type": "status", "ps_host": host}
    for k, v in perf.items():
        data["ps_services_pscheduler_" + k] = v
    data["timestamp"] = int(float(last_check) * 1000)
    return data


def livestatus(query, path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(path)
    s.sendall((query.rstrip("\n") + "\nOutputFormat: json\n\n").encode())
    s.shutdown(socket.SHUT_WR)
    chunks = []
    while True:
        b = s.recv(65536)
        if not b:
            break
        chunks.append(b)
    s.close()
    return json.loads(b"".join(chunks).decode() or "[]")


# ---------------------------------------------------------------- the run
def collect(state, config, now, spool_dir, live_rows):
    """Documents due now, and the state after sending them (the caller keeps it only on success)."""
    docs, new = [], json.loads(json.dumps(state))
    header = {"measurement_agent": config["measurement_agent"], "destination": config["meta_destination"]}
    sent_meta = new.setdefault("meta", {})
    for path in sorted(glob.glob(os.path.join(spool_dir, "*.json"))):
        try:
            with open(path) as f:
                item = json.load(f)
        except (IOError, OSError, ValueError):
            continue
        host = item.get("host") or os.path.basename(path)[:-5]
        due = host not in sent_meta or now - sent_meta[host] >= config["meta_every"]
        if not due or not isinstance(item.get("data"), dict):
            continue
        docs.append(meta_document(item["data"], item.get("timestamp", now), header))
        sent_meta[host] = now
    sent_status = new.setdefault("status", {})
    for host, last_check, perf_data in live_rows:
        perf = parse_perfdata(perf_data)
        if not perf or last_check <= sent_status.get(host, 0):
            continue
        docs.append(status_document(host, last_check, perf))
        sent_status[host] = last_check
    return docs, new


def deliver(docs, config, session=requests):
    if config["mode"] == "send":
        headers = {"content-type": "application/json"}
        for doc in docs:
            r = session.put(config["ls_uri"], headers=headers, data=json.dumps(doc), timeout=60)
            r.raise_for_status()
    else:
        with open(config["output"], "a") as f:
            for doc in docs:
                f.write(json.dumps(doc, sort_keys=True) + "\n")


def run(omd_root, config_override=None, now=None, session=requests):
    config = dict(DEFAULTS, output=os.path.join(omd_root, "var", "psetf", "exporter-out.jsonl"))
    try:
        with open(os.path.join(omd_root, "etc", "psetf", "exporter.json")) as f:
            config.update(json.load(f))
    except (IOError, OSError):
        pass
    config.update(config_override or {})
    state_file = os.path.join(omd_root, "var", "psetf", "exporter-state.json")
    try:
        with open(state_file) as f:
            state = json.load(f)
    except (IOError, OSError, ValueError):
        state = {}
    rows = livestatus("GET services\nFilter: description = %s\nFilter: has_been_checked = 1\n"
                      "Columns: host_name last_check perf_data" % PSCHEDULER_SERVICE,
                      os.path.join(omd_root, "tmp", "run", "live"))
    docs, new_state = collect(state, config, now or time.time(),
                              os.path.join(omd_root, "var", "psetf", "summaries"), rows)
    if docs:
        deliver(docs, config, session)      # raises on failure: the state is not advanced, all is retried
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file + ".tmp", "w") as f:
        json.dump(new_state, f)
    os.rename(state_file + ".tmp", state_file)
    n_meta = sum(1 for d in docs if d["type"] == "meta")
    log.info("%s %d documents (%d meta, %d status)", "sent" if config["mode"] == "send" else "wrote",
             len(docs), n_meta, len(docs) - n_meta)
    return docs


def main(argv=None):
    ap = argparse.ArgumentParser(description="psetf exporter: analytics documents from CheckMK")
    ap.add_argument("--mode", choices=("file", "send"), help="override the configured mode")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    omd_root = os.environ.get("OMD_ROOT")
    if not omd_root:
        sys.stderr.write("run inside the CheckMK site (OMD_ROOT is not set)\n")
        return 3
    run(omd_root, {"mode": args.mode} if args.mode else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
