"""node_exporter in the special agent, and the units / filesystems / clock / hardware checks."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from cmk_addons.plugins.perfsonar.lib import agent, checks  # noqa: E402
from test_perfsonar_plugin import FakeResponse  # noqa: E402

NODE = """# TYPE node_systemd_unit_state gauge
node_systemd_unit_state{name="pscheduler-scheduler.service",state="active",type="simple"} 1
node_systemd_unit_state{name="pscheduler-scheduler.service",state="failed",type="simple"} 0
node_systemd_unit_state{name="pscheduler-runner.service",state="active",type="simple"} 1
node_systemd_unit_state{name="pscheduler-archiver.service",state="%(archiver)s",type="simple"} 1
node_systemd_unit_state{name="pscheduler-ticker.service",state="active",type="simple"} 1
node_systemd_unit_state{name="psconfig-pscheduler-agent.service",state="%(psconfig)s",type="simple"} 1
node_systemd_unit_state{name="perfsonar-configure_nic_parameters.service",state="inactive",type="oneshot"} 1
node_systemd_unit_state{name="sshd.service",state="active",type="notify"} 1
node_filesystem_avail_bytes{device="/dev/sda1",fstype="xfs",mountpoint="/"} %(root_avail)s
node_filesystem_size_bytes{device="/dev/sda1",fstype="xfs",mountpoint="/"} 1e+11
node_filesystem_avail_bytes{device="/dev/sda2",fstype="xfs",mountpoint="/var"} 4e+11
node_filesystem_size_bytes{device="/dev/sda2",fstype="xfs",mountpoint="/var"} 5e+11
node_filesystem_avail_bytes{device="tmpfs",fstype="tmpfs",mountpoint="/run"} 1e+09
node_filesystem_size_bytes{device="tmpfs",fstype="tmpfs",mountpoint="/run"} 2e+09
node_cpu_seconds_total{cpu="0",mode="idle"} 1
node_cpu_seconds_total{cpu="0",mode="user"} 1
node_cpu_seconds_total{cpu="1",mode="idle"} 1
node_memory_MemTotal_bytes 1.7179869184e+10
node_timex_sync_status %(sync)s
node_timex_offset_seconds 0.0004
node_dmi_info{product_name="%(product)s",sys_vendor="Dell Inc."} 1
node_os_info{name="AlmaLinux",pretty_name="AlmaLinux 9.9 (Chartreuse Bobcat)"} 1
node_network_speed_bytes{device="lo"} 0
node_network_speed_bytes{device="eno1"} 1.25e+08
node_network_speed_bytes{device="p1p1"} 1.25e+09
node_network_mtu_bytes{device="p1p1"} 9000
node_network_mtu_bytes{device="eno1"} 1500
"""


def node(**kw):
    values = dict(archiver="active", psconfig="active", root_avail="5e+10", sync="1", product="PowerEdge R650")
    values.update(kw)
    text = NODE % values

    class Session(object):
        def get(self, url, timeout=None, headers=None, verify=None):
            assert "collect[]=systemd" in url and url.startswith("https://")
            return FakeResponse(200, text)
    return agent.node_section("ps.example.org", "192.0.2.10", 60, Session(), agent.Budget(45))


def test_node_section_is_reduced_to_what_the_checks_use():
    n = node()
    assert n["ok"] and n["cpus"] == 2 and n["mem_bytes"] == 17179869184.0
    assert "sshd.service" not in n["units"] and n["units"]["pscheduler-runner.service"] == "active"
    assert set(n["filesystems"]) == {"/", "/var"}                       # no tmpfs
    assert n["nics"]["p1p1"] == {"speed_bytes": 1.25e9, "mtu": 9000.0} and "lo" not in n["nics"]
    assert n["os"].startswith("AlmaLinux 9.9") and n["clock_synchronized"] == 1.0


def test_units():
    assert checks.check_units(node())[0][0] == checks.OK
    state, summary, _d, _m = checks.check_units(node(archiver="failed"))[0]
    assert state == checks.CRIT and "pscheduler-archiver.service failed" in summary
    assert checks.check_units(node(psconfig="inactive"))[0][:2] == \
        (checks.WARN, "psconfig-pscheduler-agent.service inactive")
    missing = node()
    del missing["units"]["pscheduler-ticker.service"]
    assert "pscheduler-ticker.service missing" in checks.check_units(missing)[0][1]


def test_filesystems():
    state, summary, details, metrics = checks.check_filesystems(node())[0]
    assert state == checks.OK and ("fs_free_percent_root", 50.0) in metrics and "/var: 80.0% free" in details
    assert checks.check_filesystems(node(root_avail="8e+09"))[0][0] == checks.WARN        # 8 % free
    state, summary, _d, _m = checks.check_filesystems(node(root_avail="3e+09"))[0]        # 3 % free
    assert state == checks.CRIT and summary.startswith("/ has 3.0% free")


def test_clock():
    assert checks.check_clock(node())[0][:2] == (checks.OK, "Clock synchronized (offset 0.4 ms)")
    assert checks.check_clock(node(sync="0"))[0][0] == checks.WARN


def test_hardware_falls_back_to_node_exporter_without_the_summary():
    state, summary, details, _m = checks.check_hardware({"ok": False}, node=node())[0]
    assert state == checks.OK and summary == "CPU:2 RAM:16GB NIC:10.0Gbps/9000MTU (p1p1)"
    assert "Dell Inc. PowerEdge R650" in details
    assert checks.check_hardware({"ok": False}, node=node(product="VMware Virtual Platform"))[0][1].startswith("VM ")
    assert checks.check_hardware({"ok": False}, node={"ok": False})[0][0] == checks.UNKNOWN
