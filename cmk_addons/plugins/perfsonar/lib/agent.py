"""CheckMK special agent for one perfSONAR host (psetf phase 2).

Fetches, once per run:
  - the toolkit summary  /toolkit/services/host.cgi?method=get_summary
  - the pScheduler run statistics  /pscheduler/stat/runs/<state>, /pscheduler/stat/archiving/backlog
and prints them as two agent sections of one JSON line each:

  <<<perfsonar_summary:sep(0)>>>     {"host": ..., "ok": true, "url": ..., "data": {...}}  or  {"ok": false, "error": ...}
  <<<perfsonar_pscheduler:sep(0)>>>  {"host": ..., "ok": true, "stats": {"pending": 3, ...}}  or  {"ok": false, ...}

Requests go to the host's IPv4 address with the host name as Host header (the name itself
when the host has no IPv4 address), without certificate verification, as check_ps did. Unlike
check_ps they try HTTPS first (perfSONAR 5 serves the toolkit and pScheduler there; port 80 is
often filtered), reuse the scheme that answered, fetch the pScheduler statistics in parallel and
stop at an overall deadline, so one run fits CheckMK's 60 s service check timeout.

With --spool DIR the (fixed-up) summary is also written to DIR/<host>.json for the exporter
that sends the analytics "meta" documents.
"""
import argparse
import concurrent.futures
import json
import os
import re
import socket
import ssl
import sys
import time

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 >= 1.26 / 2.x
    from urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except Exception:  # pragma: no cover
    pass

SUMMARY_PATH = "/toolkit/services/host.cgi?method=get_summary"
EXPORTER_PATH = "/perfsonar_host_exporter/"   # perfsonar-host-metrics, perfSONAR 5.1+, HTTPS only
CONNECT_TIMEOUT = 15  # seconds to connect, TLS handshake included (urllib3 applies it to both); some toolkits take 5-7 s.
                      # Dead hosts must not hold up CheckMK's discovery batches (108 s per batch).
PSCHEDULER_STATES = ("pending", "running", "overdue", "missed", "failed", "nonstart", "finished")


def target(host):
    """The address to connect to: the first IPv4 address, else the host name."""
    try:
        return socket.getaddrinfo(host, 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except (socket.gaierror, IndexError, UnicodeError):
        return host


DEADLINE = 45  # seconds for the whole run: CheckMK kills the "Check_MK" service after 60 s


# TLS key exchange: classic groups only. OpenSSL 3.5 (CheckMK 2.5's) offers a post-quantum hybrid
# key share by default; its large ClientHello made some toolkits' handshakes hang (e.g. lpnhe-psl.in2p3.fr:
# 30 s timeout by default, 5 s with P-256). P-256 is mandatory for every TLS 1.3 server.
TLS_GROUPS = "X25519:prime256v1:secp384r1"


def classic_tls_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if hasattr(ctx, "set_groups"):          # Python 3.15+
        ctx.set_groups(TLS_GROUPS)
    else:
        ctx.set_ecdh_curve("prime256v1")    # a single classic group; enough for TLS 1.2 and 1.3
    return ctx


class ClassicTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = classic_tls_context()
        return HTTPAdapter.init_poolmanager(self, *args, **kwargs)


def make_session():
    session = requests.Session()
    session.mount("https://", ClassicTLSAdapter(pool_maxsize=10))
    return session


class Budget(object):
    """Time left until the run's deadline."""

    def __init__(self, seconds):
        self.end = time.time() + seconds

    def left(self):
        return max(0.0, self.end - time.time())


class FetchError(requests.exceptions.RequestException):
    """Every scheme failed. `unreachable`: all failures were failed connections (not slow answers)."""

    def __init__(self, errors):
        self.errors = errors
        self.unreachable = bool(errors) and all(
            isinstance(e, requests.exceptions.ConnectionError) and not isinstance(e, requests.exceptions.ReadTimeout)
            for _s, e in errors)
        requests.exceptions.RequestException.__init__(self, "; ".join("%s: %s" % (s, e) for s, e in errors))


def get(host, address, path, timeout, session=requests, schemes=("https", "http"), budget=None):
    """GET path, trying `schemes` in order; returns (response text, url, scheme) or raises FetchError."""
    errors = []
    hostport = "[%s]" % address if ":" in address else address
    for scheme in schemes:
        left = budget.left() if budget else timeout
        if left < 1:
            errors.append((scheme, requests.exceptions.Timeout("no time left before the deadline")))
            break
        url = "%s://%s%s" % (scheme, hostport, path)
        try:
            r = session.get(url, timeout=(min(CONNECT_TIMEOUT, left), min(timeout, left)),
                            headers={"Host": host}, verify=False)
            r.raise_for_status()
            return r.text, url, scheme
        except requests.exceptions.RequestException as e:
            errors.append((scheme, e))
    raise FetchError(errors)


def fix_external_address(host, data):
    """The toolkit may report another name as its external address (multi-homed hosts);
    point external_address at the interface that carries the monitored name, as check_ps did."""
    ext = data.get("external_address")
    if not isinstance(ext, dict) or (ext.get("dns_name") or "").strip().lower() == host:
        return data
    ext["dns_name"] = host
    for iface in data.get("interfaces") or []:
        for _ip, names in (iface.get("hostnames") or {}).items():
            if host in (names or []):
                for key in ("counters", "mtu", "speed", "iface"):
                    if key in iface:
                        ext[key] = iface[key]
                if iface.get("ipv4_address"):
                    ext["address"] = iface["ipv4_address"]
                    ext["ipv4_address"] = iface["ipv4_address"][0]
                if iface.get("ipv6_address"):
                    ext["ipv6_address"] = iface["ipv6_address"][0]
    return data


def summary_section(host, address, timeout, session=requests, budget=None):
    try:
        text, url, scheme = get(host, address, SUMMARY_PATH, timeout, session, budget=budget)
    except FetchError as e:
        # only failed connections (refused, no route, connect timeout) mean the host is unreachable;
        # a slow answer (read timeout) does not, and the pScheduler API may still answer
        return {"host": host, "ok": False, "error": "Connection failed (%s)" % e, "unreachable": e.unreachable}
    try:
        data = json.loads(text)
    except ValueError:
        return {"host": host, "ok": False, "url": url, "error": "Unable to parse toolkit metadata"}
    return {"host": host, "ok": True, "url": url, "scheme": scheme, "data": fix_external_address(host, data)}


_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)')
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_prometheus(text, prefix="perfsonar_"):
    """Prometheus text format -> [[name, {label: value}, number]] for the metrics starting with prefix."""
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        m = _SAMPLE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        labels = {k: v.replace('\\"', '"') for k, v in _LABEL.findall(m.group(2) or "")}
        out.append([m.group(1), labels, value])
    return out


def exporter_section(host, address, timeout, session=requests, budget=None):
    """The perfSONAR host exporter's perfsonar_* metrics (pScheduler, pSConfig, bundle, Lookup Service)."""
    try:
        text, url, _scheme = get(host, address, EXPORTER_PATH, timeout, session, ("https",), budget)
    except FetchError as e:
        return {"host": host, "ok": False, "error": str(e)}
    samples = parse_prometheus(text)
    if not samples:
        return {"host": host, "ok": False, "url": url, "error": "no perfsonar_* metrics in the answer"}
    return {"host": host, "ok": True, "url": url, "samples": samples}


# exporter metric -> the pScheduler statistics' names (those of /pscheduler/stat and the analytics documents)
_RUNS = {"pending": "pending", "running": "running", "overdue": "overdue", "missed": "missed", "failed": "failed",
         "nonstart": "nonstart", "finished": "finished", "on-deck": "on_deck", "cleanup": "cleanup",
         "preempted": "preempted"}


def pscheduler_from_exporter(host, exporter):
    """The pScheduler statistics from the exporter, or None when it has none."""
    stats = {}
    for name, labels, value in exporter.get("samples") or []:
        state = labels.get("state")
        if name == "perfsonar_pscheduler_runs" and state in _RUNS:
            stats[_RUNS[state]] = int(value)
        elif name == "perfsonar_pscheduler_archiving" and state in ("backlog", "upcoming"):
            stats["archiver_backlog" if state == "backlog" else "archiving_upcoming"] = int(value)
        elif name == "perfsonar_pscheduler_http_queue" and state in ("backlog", "length"):
            stats["http_queue_" + state] = int(value)
        elif name == "perfsonar_pscheduler_paused":
            stats["paused"] = int(value)
    if "pending" not in stats:
        return None
    return {"host": host, "ok": True, "stats": stats, "source": "exporter"}


def pscheduler_section(host, address, timeout, session=requests, unreachable=False, budget=None, scheme=None):
    if unreachable:   # neither HTTPS nor HTTP answered for the summary: don't wait on the same ports again
        return {"host": host, "ok": False, "error": "Error connecting to pscheduler API (host unreachable)"}
    schemes = (scheme,) + tuple(s for s in ("https", "http") if s != scheme) if scheme else ("https", "http")
    # the first request decides whether the API answers at all, and over which scheme
    try:
        text, _url, scheme = get(host, address, "/pscheduler/stat/runs/pending", timeout, session, schemes, budget)
        stats = {"pending": int(text.strip())}
    except requests.exceptions.RequestException as e:
        return {"host": host, "ok": False, "error": "Error connecting to pscheduler API (%s)" % e}
    except ValueError:
        return {"host": host, "ok": False, "error": "Unexpected answer from the pscheduler API"}
    paths = [("/pscheduler/stat/runs/" + s, s) for s in PSCHEDULER_STATES if s != "pending"]
    paths.append(("/pscheduler/stat/archiving/backlog", "archiver_backlog"))

    def one(item):
        path, name = item
        try:
            text, _url, _s = get(host, address, path, timeout, session, (scheme,), budget)
            return name, int(text.strip())
        except (requests.exceptions.RequestException, ValueError):
            return name, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(paths)) as pool:
        for name, value in pool.map(one, paths):
            if value is not None:
                stats[name] = value
    return {"host": host, "ok": True, "stats": stats}


def dns_names(host, address):
    """The names DNS gives the host: canonical name and reverse lookup of its address.
    Hosts may subscribe to their pSConfig auto-URL under one of these (check_ps used getfqdn)."""
    names = set()
    try:
        names.add(socket.getfqdn(host).lower())
    except (OSError, UnicodeError):
        pass
    try:
        names.add(socket.gethostbyaddr(address)[0].lower())
    except (OSError, UnicodeError):
        pass
    return sorted(n for n in names if n and n != host)


def spool(directory, host, section):
    if not (directory and section.get("ok")):
        return
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, ".%s.json.tmp" % host)
    with open(tmp, "w") as f:
        json.dump({"timestamp": time.time(), "host": host, "data": section["data"]}, f)
    os.rename(tmp, os.path.join(directory, host + ".json"))


def main(argv=None, out=sys.stdout, session=None):
    ap = argparse.ArgumentParser(description="CheckMK special agent for a perfSONAR host")
    ap.add_argument("host")
    ap.add_argument("--timeout", type=float, default=60, help="read timeout per HTTP request (check_ps used 60)")
    ap.add_argument("--deadline", type=float, default=DEADLINE, help="seconds for the whole run")
    default_spool = os.path.join(os.environ["OMD_ROOT"], "var", "psetf", "summaries") if os.environ.get("OMD_ROOT") else None
    ap.add_argument("--spool", default=default_spool,
                    help="directory to write <host>.json toolkit summaries to (default: $OMD_ROOT/var/psetf/summaries)")
    args = ap.parse_args(argv)
    host = args.host.strip().lower()
    session = session or make_session()
    address = target(host)
    budget = Budget(args.deadline)
    # summary and exporter in parallel; the pScheduler statistics come from the exporter when it has
    # them, else from /pscheduler/stat, started as soon as the exporter has answered (not after a
    # slow summary, which must not use up the pScheduler's time)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        summary_job = pool.submit(summary_section, host, address, args.timeout, session, budget)
        exporter = pool.submit(exporter_section, host, address, args.timeout, session, budget).result()
        pscheduler = pscheduler_from_exporter(host, exporter) if exporter.get("ok") else None
        pscheduler_job = None if pscheduler else \
            pool.submit(pscheduler_section, host, address, args.timeout, session, budget=budget)
        summary = summary_job.result()
        pscheduler = pscheduler or pscheduler_job.result()
    summary["names"] = dns_names(host, address)
    out.write("<<<perfsonar_summary:sep(0)>>>\n%s\n" % json.dumps(summary, sort_keys=True))
    out.write("<<<perfsonar_pscheduler:sep(0)>>>\n%s\n" % json.dumps(pscheduler, sort_keys=True))
    out.write("<<<perfsonar_exporter:sep(0)>>>\n%s\n" % json.dumps(exporter, sort_keys=True))
    try:
        spool(args.spool, host, summary)
    except OSError as e:
        sys.stderr.write("unable to spool the summary: %s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
