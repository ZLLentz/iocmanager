from __future__ import annotations

import os
import shutil
from copy import copy
from pathlib import Path

import pytest

from ..config import (
    Config,
    DuplicatePortError,
    IOCProc,
    IOCStatusFile,
    check_auth,
    check_special,
    check_ssh,
    find_iocs,
    get_host_os,
    get_hutch_list,
    read_config,
    read_status_dir,
    write_config,
)
from ..env_paths import env_paths
from . import CFG_FOLDER


def test_basic_operations():
    config = Config(path="")
    assert not config.hosts
    ioc_proc = IOCProc(name="ioc_name", port=30001, host="host", path="/ioc/path")
    config.add_proc(proc=ioc_proc)
    assert "ioc_name" in config.procs
    assert config.hosts == ["host"]
    ioc_proc.host = "jost"
    config.update_proc(proc=ioc_proc)
    assert "ioc_name" in config.procs
    assert config.hosts == ["host", "jost"]
    config.delete_proc("ioc_name")
    assert "ioc_name" not in config.procs
    assert not config.procs


@pytest.mark.parametrize(
    "cfg", (str(CFG_FOLDER / "pytest" / "iocmanager.cfg"), "pytest")
)
def test_read_config(cfg: str):
    config = read_config(cfg)

    if Path(cfg).is_file():
        filename = cfg
    else:
        filename = str(CFG_FOLDER / cfg / "iocmanager.cfg")

    assert config.mtime == os.stat(filename).st_mtime

    assert config.procs == {
        "ioc-counter": IOCProc(
            name="ioc-counter",
            host="test-server2",
            port=30002,
            path="ioc/counter",
            alias="",
            disable=False,
            cmd="",
            delay=0,
            history=["ioc/old"],
            parent="",
            hard=False,
        ),
        "ioc-shouter": IOCProc(
            name="ioc-shouter",
            host="test-server1",
            port=30001,
            path="ioc/shouter",
            alias="SHOUTER",
            disable=False,
            delay=1,
            cmd="",
            history=[],
            parent="",
            hard=False,
        ),
    }

    assert config.hosts == [
        "test-server1",
        "test-server2",
    ]

    assert config.commithost == "localhost"
    assert config.allow_console


@pytest.mark.parametrize(
    "host,expected",
    (
        ("test-server1", "rocky9"),
        ("test-server2", "rhel7"),
        ("test-server3", "rhel5"),
        ("not-a-server", ""),
    ),
)
def test_get_host_os(host: str, expected: str):
    host_os = get_host_os(hosts_list=["test-server1", "test-server2", "test-server3"])
    if expected:
        assert host_os[host] == expected
    else:
        with pytest.raises(KeyError):
            host_os[host]


def test_write_config(tmp_path: Path):
    # Just write back our example config, it should be the same
    config = read_config("pytest")
    write_config(cfgname=str(tmp_path / "iocmanager.cfg"), config=config)

    with open(CFG_FOLDER / "pytest" / "iocmanager.cfg", "r") as fd:
        expected = fd.readlines()

    with open(tmp_path / "iocmanager.cfg", "r") as fd:
        actual = fd.readlines()

    assert actual == expected


def test_write_config_invalid(tmp_path: Path):
    config = read_config("pytest")
    # Add a two new procs with a duplicate host/port, should error
    port = 30010
    host = "some_host"
    config.add_proc(
        proc=IOCProc(
            name="bad_proc1",
            port=port,
            host=host,
            path="",
        )
    )
    config.add_proc(
        proc=IOCProc(
            name="bad_proc2",
            port=port,
            host=host,
            path="",
        )
    )
    with pytest.raises(DuplicatePortError):
        write_config(cfgname=str(tmp_path / "iocmanager.cfg"), config=config)

    assert not (tmp_path / "iocmanager.cfg").exists()


def test_check_auth():
    assert check_auth("user_for_test_check_auth", "pytest")
    assert not check_auth("some_rando", "pytest")


def test_check_special_two_variants():
    # We should get True for the two versions we have but not for others
    assert check_special("has_two_variants", "pytest", "ioc/variant/opt1")
    assert check_special("has_two_variants", "pytest", "ioc/variant/opt2")
    assert not check_special("has_two_variants", "pytest", "what_the_heck")


def test_check_special_just_name():
    # With just a name and no variants, we should get true with no version arg
    assert check_special("just_a_name", "pytest")
    assert not check_special("any_other_name", "pytest")


def test_check_ssh():
    assert check_ssh("most_users", "pytest")
    assert not check_ssh("tstopr", "pytest")


def test_find_iocs():
    search1 = find_iocs(id="ioc-counter")
    assert len(search1) == 1
    assert search1[0][1].host == "test-server2"

    search2 = find_iocs(host="test-server1")
    assert len(search2) == 1
    assert search2[0][1].name == "ioc-shouter"


def test_get_hutch_list():
    # See folders in pyps_root/config
    assert sorted(get_hutch_list()) == [
        "commit_test",
        "pytest",
        "second_hutch",
    ]


def test_validate_config():
    # Only checks for port conflicts at time of writing
    good_config = Config(path="")
    good_config.add_proc(IOCProc(name="one", host="host1", port=10000, path=""))
    good_config.add_proc(IOCProc(name="two", host="host1", port=20000, path=""))
    good_config.add_proc(IOCProc(name="thr", host="host2", port=20000, path=""))

    bad_config = Config(path="")
    bad_config.add_proc(IOCProc(name="one", host="host1", port=10000, path=""))
    bad_config.add_proc(IOCProc(name="two", host="host1", port=10000, path=""))
    bad_config.add_proc(IOCProc(name="thr", host="host2", port=20000, path=""))
    good_config.validate()
    with pytest.raises(DuplicatePortError):
        bad_config.validate()


def test_read_status_dir():
    # Status directory is at $PYPS_ROOT/config/.status/$HUTCH
    # During this test suite, that's a temp dir
    # Which is filled with the contents of the local tests/pyps_root
    # Note: this never matches a prod dir, even if PYPS_ROOT is set to a prod value
    status_dir = Path(env_paths.PYPS_ROOT) / "config" / ".status" / "pytest"
    if not status_dir.is_dir():
        raise RuntimeError(
            f"Error in test writing: status dir {status_dir} does not exist."
        )

    # Set up some expectations/starting state
    counter_path = status_dir / "ioc-counter"
    counter_info = IOCStatusFile(
        name=counter_path.name,
        port=30002,
        host="test-server2",
        path="iocs/counter",
        pid=12345,
        mtime=os.stat(counter_path).st_mtime,
    )
    shouter_path = status_dir / "ioc-shouter"
    shouter_info = IOCStatusFile(
        name=shouter_path.name,
        port=30001,
        host="test-server1",
        path="iocs/shouter",
        pid=23456,
        mtime=os.stat(shouter_path).st_mtime,
    )

    # Run once: files should not change, result should be complete
    iocs1 = read_status_dir("pytest")
    assert len(iocs1) == 2
    assert counter_info in iocs1
    assert shouter_info in iocs1
    assert counter_path.is_file()
    assert shouter_path.is_file()

    # Make two new status files, before/after alphabetically.
    # These should supercede the old ones
    new_counter_path = status_dir / "ioc-a-counter"
    shutil.copy(counter_path, new_counter_path)
    new_counter_info = copy(counter_info)
    new_counter_info.name = new_counter_path.name
    new_counter_info.mtime = os.stat(new_counter_path).st_mtime

    new_shouter_path = status_dir / "ioc-z-counter"
    shutil.copy(shouter_path, new_shouter_path)
    new_shouter_info = copy(shouter_info)
    new_shouter_info.name = new_shouter_path.name
    new_shouter_info.mtime = os.stat(new_shouter_path).st_mtime

    # Make some bad files, it should be deleted and no info returned
    bad_file_path = status_dir / "not-an-ioc"
    with open(bad_file_path, "w") as fd:
        fd.write("12345 PIZZA SODA")

    assert bad_file_path.is_file()

    # Empty files are ignored and not deleted, for whatever reason
    empty_file_path = status_dir / "empty"
    empty_file_path.touch()
    assert empty_file_path.is_file()

    # Run again: should have new info, the old and bad files should be gone
    iocs2 = read_status_dir("pytest")
    assert len(iocs2) == 2
    assert new_counter_info in iocs2
    assert new_shouter_info in iocs2
    assert not counter_path.exists()
    assert not shouter_path.exists()
    assert not bad_file_path.exists()
    assert new_counter_path.is_file()
    assert new_shouter_path.is_file()
    assert empty_file_path.is_file()
