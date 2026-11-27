"""CheckMK special agent for one perfSONAR host (psetf phase 2).

Fetches, once per run:
  - the toolkit summary  /toolkit/services/host.cgi?method=get_summary
  - the pScheduler run statistics  /pscheduler/stat/runs/<state>, /pscheduler/stat/archiving/backlog
and prints them as two agent sections of one JSON line each:

  <<<perfsonar_summary:sep(0)>>>     {"host": ..., "ok": true, "url": ..., "data": {...}}  or  {"ok": false, "error": ...}
  <<<perfsonar_pscheduler:sep(0)>>>  {"host": ..., "ok": true, "stats": {"pending": 3, ...}}  or  {"ok": false, ...}

Requests follow the former check_ps / check_ps_psched: HTTP first, then HTTPS without
certificate verification, to the host's IPv4 address with the host name as Host header
(the name itself when the host has no IPv4 address).

With --spool DIR the (fixed-up) summary is also written to DIR/<host>.json for the exporter
that sends the analytics "meta" documents.
"""
import argparse
import json
import os
import socket
import sys
import time

import requests

try:  # urllib3 >= 1.26 / 2.x
    from urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except Exception:  # pragma: no cover
    pass

SUMMARY_PATH = "/toolkit/services/host.cgi?method=get_summary"
CONNECT_TIMEOUT = 5   # seconds to open a connection; dead hosts must not hold up CheckMK's discovery batches
PSCHEDULER_STATES = ("pending", "running", "overdue", "missed", "failed", "nonstart", "finished")


def target(host):
    """The address to connect to: the first IPv4 address, else the host name."""
    try:
        return socket.getaddrinfo(host, 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except (socket.gaierror, IndexError, UnicodeError):
        return host


def get(host, address, path, timeout, session=requests):
    """GET path over HTTP, then HTTPS; returns (response text, url) or raises the last error."""
    error = None
    hostport = "[%s]" % address if ":" in address else address
    for scheme in ("http", "https"):
        url = "%s://%s%s" % (scheme, hostport, path)
        try:
            r = session.get(url, timeout=(min(CONNECT_TIMEOUT, timeout), timeout), headers={"Host": host}, verify=False)
            r.raise_for_status()
            return r.text, url
        except requests.exceptions.RequestException as e:
            error = e
    raise error


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


def summary_section(host, address, timeout, session=requests):
    try:
        text, url = get(host, address, SUMMARY_PATH, timeout, session)
    except requests.exceptions.RequestException as e:
        return {"host": host, "ok": False, "error": "Connection failed (%s)" % e,
                "unreachable": isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))}
    try:
        data = json.loads(text)
    except ValueError:
        return {"host": host, "ok": False, "url": url, "error": "Unable to parse toolkit metadata"}
    return {"host": host, "ok": True, "url": url, "data": fix_external_address(host, data)}


def pscheduler_section(host, address, timeout, session=requests, unreachable=False):
    if unreachable:   # neither HTTP nor HTTPS answered for the summary: don't wait on the same ports again
        return {"host": host, "ok": False, "error": "Error connecting to pscheduler API (host unreachable)"}
    stats = {}
    paths = [("/pscheduler/stat/runs/" + s, s) for s in PSCHEDULER_STATES]
    paths.append(("/pscheduler/stat/archiving/backlog", "archiver_backlog"))
    for path, name in paths:
        try:
            text, _url = get(host, address, path, timeout, session)
            stats[name] = int(text.strip())
        except requests.exceptions.RequestException as e:
            if name == "pending":   # the first request decides whether the API answers at all
                return {"host": host, "ok": False, "error": "Error connecting to pscheduler API (%s)" % e}
        except ValueError:
            pass
    return {"host": host, "ok": True, "stats": stats}


def spool(directory, host, section):
    if not (directory and section.get("ok")):
        return
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, ".%s.json.tmp" % host)
    with open(tmp, "w") as f:
        json.dump({"timestamp": time.time(), "host": host, "data": section["data"]}, f)
    os.rename(tmp, os.path.join(directory, host + ".json"))


def main(argv=None, out=sys.stdout, session=requests):
    ap = argparse.ArgumentParser(description="CheckMK special agent for a perfSONAR host")
    ap.add_argument("host")
    ap.add_argument("--timeout", type=float, default=30)
    default_spool = os.path.join(os.environ["OMD_ROOT"], "var", "psetf", "summaries") if os.environ.get("OMD_ROOT") else None
    ap.add_argument("--spool", default=default_spool,
                    help="directory to write <host>.json toolkit summaries to (default: $OMD_ROOT/var/psetf/summaries)")
    args = ap.parse_args(argv)
    host = args.host.strip().lower()
    address = target(host)
    summary = summary_section(host, address, args.timeout, session)
    out.write("<<<perfsonar_summary:sep(0)>>>\n%s\n" % json.dumps(summary, sort_keys=True))
    out.write("<<<perfsonar_pscheduler:sep(0)>>>\n%s\n"
              % json.dumps(pscheduler_section(host, address, args.timeout, session,
                                              unreachable=summary.get("unreachable", False)), sort_keys=True))
    try:
        spool(args.spool, host, summary)
    except OSError as e:
        sys.stderr.write("unable to spool the summary: %s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
