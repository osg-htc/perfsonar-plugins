"""One-time (idempotent) CheckMK configuration for psetf: the rules that attach the checks.

Rules are found by their description; a rule that exists is left alone, so this is safe
to run again after a change here (add a new rule with a new description).

Run:  python3 -m psetf.site_setup --config /etc/psetf/discovery.json [--dry-run]
"""
import argparse
import ast
import json
import logging
import sys
import time

from .checkmk import CheckMK
from .discovery import FOLDER, _secret

log = logging.getLogger("psetf")


def label_condition(label):
    return {"host_label_groups": [{"operator": "and", "label_group": [{"operator": "and", "label": label}]}]}


RULES = [
    # (ruleset, description, value_raw, conditions)
    # Host state = can we open the toolkit's HTTPS port (ping is often filtered); ping stays as a
    # service, so "443 up but ping lost" vs "both down" hints at a firewall rather than a dead host.
    ("host_check_commands", "psetf: host is up when the toolkit's HTTPS port (443) accepts connections",
     "('tcp', 443)", None),
    ("active_checks:icmp", "psetf: PING service (kept for firewall hints; the host check is TCP 443)",
     "{}", None),
    ("special_agents:perfsonar", "psetf: perfSONAR toolkit special agent for all discovered hosts",
     "{'timeout': 60.0}", None),
    # the special agent runs with the "Check_MK" service: every 60 min, as check_ps did on the ETF psetf
    ("extra_service_conf:check_interval", "psetf: run the perfSONAR special agent every 60 minutes",
     "60.0", {"service_description": {"match_on": ["Check_MK$"], "operator": "one_of"}}),
    # pscheduler troubleshoot over the REST API, daily as on the ETF psetf. The check stops itself at
    # 240 s; the site's Nagios service_check_timeout must be above that (etc/nagios/nagios.d/tuning.cfg,
    # 300 on staging; not settable through the REST API).
    ("active_checks:perfsonar_diags", "psetf: pScheduler diagnostics (troubleshoot with idle test) on all hosts",
     "{'limit': 240.0}", None),
    ("extra_service_conf:check_interval", "psetf: pScheduler diagnostics once a day",
     "1440.0", {"service_description": {"match_on": ["perfSONAR services: pscheduler diags$"], "operator": "one_of"}}),
    ("custom_checks", "psetf: owamp port on latency hosts",
     repr({"service_description": "perfSONAR services: owamp",
           "command_line": "check_tcp -H $HOSTADDRESS$ -p 861"}),
     label_condition("psetf/latency:yes")),
    # owamp per address family: some sites filter 861 on IPv4 but not IPv6 (t2-pfsn2.jinr.ru,
    # sampaps01.if.usp.br, Nov 2026); the ETF check connected by name and so only ever saw IPv6
    ("custom_checks", "psetf: owamp port over IPv6 on latency hosts with an IPv6 address",
     repr({"service_description": "perfSONAR services: owamp IPv6",
           "command_line": "check_tcp -6 -H $_HOSTADDRESS_6$ -p 861"}),
     {"host_label_groups": [{"operator": "and", "label_group": [{"operator": "and", "label": "psetf/latency:yes"},
                                                                 {"operator": "and", "label": "psetf/ipv6:yes"}]}]}),
    ("custom_checks", "psetf: toolkit web over IPv6 on hosts with an IPv6 address",
     repr({"service_description": "perfSONAR services: web/https IPv6",
           "command_line": "check_tcp -6 -H $_HOSTADDRESS_6$ -p 443"}),
     label_condition("psetf/ipv6:yes")),
]


def _same_value(a, b):
    try:
        return ast.literal_eval(a) == ast.literal_eval(b)
    except (ValueError, SyntaxError):
        return a == b


def run(config, dry_run=False, cmk=None):
    cmk = cmk or CheckMK(config["api_url"], config["api_user"], _secret(config), verify=config.get("verify_tls", True))
    created = []
    for ruleset, description, value_raw, conditions in RULES:
        existing = [r for r in cmk.rules(ruleset)
                    if ((r.get("extensions") or {}).get("properties") or {}).get("description") == description]
        if existing:
            current = ((existing[0].get("extensions") or {}).get("value_raw") or "").strip()
            if _same_value(current, value_raw):
                log.info("rule exists: %s", description)
                continue
            log.info("%s rule value: %s (%s -> %s)", "would update" if dry_run else "updating", description,
                     current, value_raw)
            if not dry_run:
                cmk.update_rule(existing[0]["id"], value_raw)
            created.append(description)
            continue
        log.info("%s rule: %s", "would create" if dry_run else "creating", description)
        if not dry_run:
            cmk.create_rule(ruleset, FOLDER, value_raw, description, conditions)
        created.append(description)
    if created and not dry_run:
        # let CheckMK 2.5's automation-helper reload after the rule change before activating
        # (an activation started inside that window can be lost; see discovery.run)
        time.sleep(config.get("settle_seconds", 20))
        cmk.activate(config["site"], wait=config.get("activation_timeout", 600))
    return created


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config) as f:
        run(json.load(f), dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
