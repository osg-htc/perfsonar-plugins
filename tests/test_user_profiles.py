"""Tests for src/cmk_user_profiles (CILogon users must exist for CheckMK after a recreate)."""
import importlib.machinery
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture()
def tool():
    path = os.path.join(HERE, "..", "src", "cmk_user_profiles")
    loader = importlib.machinery.SourceFileLoader("cmk_user_profiles", path)
    spec = importlib.util.spec_from_loader("cmk_user_profiles", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


USERS_MK = """# Written by Checkmk store
multisite_users.update({'cmkadmin': {'alias': 'cmkadmin', 'roles': ['admin'], 'connector': 'htpasswd'},
 'smckee@umich.edu': {'alias': 'Shawn McKee', 'roles': ['user', 'admin'], 'connector': 'htpasswd'},
 'mbabik@cern.ch': {'alias': 'Marian Babik', 'roles': ['user', 'admin'], 'connector': 'htpasswd'}})
"""


def test_users_without_a_profile_get_one_and_existing_profiles_are_left_alone(tool, tmp_path):
    users_mk = tmp_path / "users.mk"
    users_mk.write_text(USERS_MK)
    web = tmp_path / "web"
    (web / "cmkadmin").mkdir(parents=True)
    (web / "cmkadmin" / "serial.mk").write_text("7")              # an existing profile keeps its serial
    (web / "mbabik@cern.ch").mkdir()
    (web / "mbabik@cern.ch" / "transids.mk").write_text("[]")     # transids.mk alone also counts

    assert tool.load_users(str(users_mk)) == ["cmkadmin", "mbabik@cern.ch", "smckee@umich.edu"]
    assert tool.ensure_profiles(tool.load_users(str(users_mk)), str(web)) == ["smckee@umich.edu"]
    serial = web / "smckee@umich.edu" / "serial.mk"
    assert serial.read_text() == "0" and oct(serial.stat().st_mode & 0o777) == "0o660"
    assert oct((web / "smckee@umich.edu").stat().st_mode & 0o777) == "0o700"
    assert (web / "cmkadmin" / "serial.mk").read_text() == "7"
    assert not (web / "mbabik@cern.ch" / "serial.mk").exists()
    assert tool.ensure_profiles(tool.load_users(str(users_mk)), str(web)) == []   # second run: nothing to do


def test_odd_user_ids_never_escape_the_profile_dir(tool, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    assert tool.ensure_profiles(["../evil", "..", "", "ok"], str(web)) == ["ok"]
    assert sorted(os.listdir(str(web))) == ["ok"]


def test_missing_users_mk_is_not_an_error(tool, tmp_path, capsys):
    assert tool.main(["x", str(tmp_path / "absent.mk"), str(tmp_path)]) == 0
    assert "nothing to do" in capsys.readouterr().out
