"""Services from the perfSONAR special agent (psetf phase 2).

The service names are those of the former ETF checks, so states, history and the old
performance graphs carry over. The logic is in ..lib.checks.
"""
import time

from cmk.agent_based.v2 import (
    AgentSection,
    CheckPlugin,
    CheckResult,
    DiscoveryResult,
    Metric,
    Result,
    Service,
    State,
    StringTable,
)

from cmk_addons.plugins.perfsonar.lib import checks

_STATES = {checks.OK: State.OK, checks.WARN: State.WARN, checks.CRIT: State.CRIT, checks.UNKNOWN: State.UNKNOWN}


def _parse(string_table: StringTable):
    return checks.parse(string_table)


def _results(rows) -> CheckResult:
    for state, summary, details, metrics in rows:
        yield Result(state=_STATES[state], summary=summary, details=details or summary)
        for name, value in metrics:
            yield Metric(name, value)


def _discover(section) -> DiscoveryResult:
    yield Service()


agent_section_perfsonar_summary = AgentSection(name="perfsonar_summary", parse_function=_parse)
agent_section_perfsonar_pscheduler = AgentSection(name="perfsonar_pscheduler", parse_function=_parse)
agent_section_perfsonar_exporter = AgentSection(name="perfsonar_exporter", parse_function=_parse)


def _discover_both(section_perfsonar_summary, section_perfsonar_exporter) -> DiscoveryResult:
    yield Service()


def _discover_exporter(section) -> DiscoveryResult:
    # only where the perfSONAR host exporter answers (perfSONAR 5.1+, not blocked).
    # A plugin with one section gets it as "section"; with several, as "section_<name>".
    if checks.exporter_view(section) is not None:
        yield Service()


def _check_summary(section_perfsonar_summary, section_perfsonar_exporter) -> CheckResult:
    yield from _results(checks.check_summary(section_perfsonar_summary, section_perfsonar_exporter))


def _check_http(section_perfsonar_summary, section_perfsonar_exporter) -> CheckResult:
    yield from _results(checks.check_http(section_perfsonar_summary, section_perfsonar_exporter))


def _check_meshes(section_perfsonar_summary, section_perfsonar_exporter) -> CheckResult:
    yield from _results(checks.check_meshes(section_perfsonar_summary, exporter=section_perfsonar_exporter))


def _check_psconfig_agent(section) -> CheckResult:
    yield from _results(checks.check_psconfig_agent(section, time.time()))


def _check_lookup(section) -> CheckResult:
    yield from _results(checks.check_lookup_registration(section))


def _check_hardware(section) -> CheckResult:
    yield from _results(checks.check_hardware(section))


def _check_pscheduler(section) -> CheckResult:
    yield from _results(checks.check_pscheduler(section))


check_plugin_perfsonar_summary = CheckPlugin(
    name="perfsonar_summary", service_name="perfSONAR json summary",
    sections=["perfsonar_summary", "perfsonar_exporter"],
    discovery_function=_discover_both, check_function=_check_summary)

check_plugin_perfsonar_http = CheckPlugin(
    name="perfsonar_http", service_name="perfSONAR services: http/https",
    sections=["perfsonar_summary", "perfsonar_exporter"],
    discovery_function=_discover_both, check_function=_check_http)

check_plugin_perfsonar_meshes = CheckPlugin(
    name="perfsonar_meshes", service_name="perfSONAR configuration: meshes",
    sections=["perfsonar_summary", "perfsonar_exporter"],
    discovery_function=_discover_both, check_function=_check_meshes)

check_plugin_perfsonar_psconfig_agent = CheckPlugin(
    name="perfsonar_psconfig_agent", service_name="perfSONAR configuration: pSConfig agent",
    sections=["perfsonar_exporter"], discovery_function=_discover_exporter, check_function=_check_psconfig_agent)

check_plugin_perfsonar_lookup = CheckPlugin(
    name="perfsonar_lookup", service_name="perfSONAR registration: Lookup Service",
    sections=["perfsonar_exporter"], discovery_function=_discover_exporter, check_function=_check_lookup)

check_plugin_perfsonar_hardware = CheckPlugin(
    name="perfsonar_hardware", service_name="perfSONAR hardware check", sections=["perfsonar_summary"],
    discovery_function=_discover, check_function=_check_hardware)

check_plugin_perfsonar_pscheduler = CheckPlugin(
    name="perfsonar_pscheduler", service_name="perfSONAR services: pscheduler", sections=["perfsonar_pscheduler"],
    discovery_function=_discover, check_function=_check_pscheduler)
