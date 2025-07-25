from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from telnetlib import Telnet
from typing import Iterator

import pytest
from pytestqt.qtbot import QtBot

from ..config import Config
from ..env_paths import env_paths
from ..procserv_tools import BASEPORT, AutoRestartMode, IOCProc
from ..table_delegate import IOCTableDelegate
from ..table_model import IOCTableModel

EPICS_HOST_ARCH = os.getenv("EPICS_HOST_ARCH")
TESTS_PATH = Path(__file__).parent.resolve()
ROOT_PATH = TESTS_PATH.parent.parent.resolve()
PROCSERV_BUILD = ROOT_PATH / "procserv" / "build"


def setup_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Set environment variables appropriately for the unit tests.

    This is imported in tests.interactive to use the same environment
    for interactive testing.
    """
    monkeypatch.setenv("CAMRECORD_ROOT", str(tmp_path))
    try:
        monkeypatch.setenv("PROCSERV_EXE", str(get_procserv_bin_path()))
    except RuntimeError:
        monkeypatch.delenv("PROCSERV_EXE")
    # PYPS_ROOT must be on temp path because we write to it as part of the test
    local_pyps_root = TESTS_PATH / "pyps_root"
    temp_pyps_root = tmp_path / "pyps_root"
    shutil.copytree(local_pyps_root, temp_pyps_root)
    monkeypatch.setenv("PYPS_ROOT", str(temp_pyps_root))
    monkeypatch.setenv("IOC_DATA", str(TESTS_PATH / "ioc_data"))
    monkeypatch.setenv("IOC_COMMON", str(TESTS_PATH / "ioc_common"))
    monkeypatch.setenv("TOOLS_SITE_TOP", str(TESTS_PATH / "tools"))
    monkeypatch.setenv("EPICS_SITE_TOP", str(TESTS_PATH))
    monkeypatch.setenv("SCRIPTROOT", str(TESTS_PATH / "script_root"))

    # Verify that the env_paths object is doing "something"
    assert env_paths.PYPS_ROOT == str(temp_pyps_root)


@pytest.fixture(scope="function", autouse=True)
def prepare_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Wrap setup_test_env in a fixture that gets used in every unit test.
    """
    setup_test_env(tmp_path=tmp_path, monkeypatch=monkeypatch)

    yield


@pytest.fixture(scope="function")
def procserv() -> Iterator[ProcServHelper]:
    """
    Start procServ with a "counter" IOC.

    Yields a ProcServHelper object, which contains helper functions and the port
    needed to connect to the spawned procServ instance.

    Closes the procServ afterwards.
    """
    proc_name = "counter"
    startup_dir = str(TESTS_PATH / "ioc" / "counter")
    command = "./st.cmd"
    port = 34567

    with ProcServHelper(
        proc_name=proc_name, startup_dir=startup_dir, command=command, port=port
    ) as pserv:
        yield pserv


@pytest.fixture(scope="function")
def procmgrd() -> Iterator[ProcServHelper]:
    """
    Start a procmgrd procServ instance.

    Yields a ProcServHelper object, which contains helper functions and the port
    needed to connect to the spawned procServ instance.

    Closes the procServ afterwards.
    """
    proc_name = "procmgrd"
    startup_dir = str(TESTS_PATH)
    command = "./not_procmgrd.sh"
    # The real procmgrd uses BASEPORT, or BASEPORT+2n
    port = BASEPORT

    with ProcServHelper(
        proc_name=proc_name, startup_dir=startup_dir, command=command, port=port
    ) as pserv:
        yield pserv


class ProcServHelper:
    """
    Test helper.

    Acts as a context manager that launches an attached procServ
    process (not a daemon), which helps us clean up afterwards.

    Closes the process cleanly on context exit.
    """

    def __init__(self, proc_name: str, startup_dir: str, command: str, port: int):
        self.proc = None
        self.tn = None
        self.proc_name = proc_name
        self.startup_dir = startup_dir
        self.command = command
        self.port = port

    def __enter__(self) -> ProcServHelper:
        self.open_procserv()
        return self

    def __exit__(self, *args, **kwargs):
        self.close_procserv()

    def open_procserv(self) -> subprocess.Popen:
        """
        Start a dummy procServ subprocess for unit test interaction.

        It will begin with no process running and autorestart disabled,
        so that the state is always known at the beginning of the test
        (without relying on things like subprocess startup speed).
        """
        self.close_procserv()
        # Before starting, check for leaks from previous test
        self.check_port_available()
        self.proc = subprocess.Popen(
            [
                str(get_procserv_bin_path()),
                # Keep connected to this subprocess rather than daemonize
                "--foreground",
                # Start in no restart mode for predictable init
                "--noautorestart",
                # Start with no process running for predictable init
                "--wait",
                # Select a name to show to people who connect
                f"--name={self.proc_name}",
                str(self.port),
                self.command,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=self.startup_dir,
        )
        self.tn = self.telnet_when_procserv_started()
        return self.proc

    def close_procserv(self):
        """
        Stop and clean up the procServ subprocess.

        We need to ask procServ to kill its own subprocess
        if one is running, then turn off procServ itself.
        We will always finish by killing the process just in case.
        """
        if self.proc is not None:
            # If nothing is running, this is all we need
            self.close_cmd()
            # If something was running, the previous command is ignored.
            self.toggle_running()
            # Now that nothing is running, we can try to close again.
            self.close_cmd()
            # Make sure it closes
            self.wait_procserv_closed()
            # Always kill just in case
            self.proc.kill()
            self.proc = None
        if self.tn is not None:
            self.tn.close()
            self.tn = None

    def check_port_available(self):
        """
        Raise if our configured port is already in use.

        It might be in use by us, by another user, or by a previous
        test that is leaking processes.
        """
        try:
            with Telnet("localhost", self.port, 1):
                ...
        except OSError:
            ...
        else:
            raise RuntimeError(
                f"Port {self.port} is in use. "
                "You might have a test suite issue, "
                "or maybe an iocmanager kill/cleanup routine is broken. "
                "Try to manually remove processes "
                "that look like test/ioc artifacts, "
                "such as st.cmd or procServ running as you."
            )

    def _ctrl_char(self, char: str):
        """
        Send a control character to the procServ process.
        """
        if self.tn is not None:
            try:
                self.tn.write(ctrl(char))
            except OSError:
                # telnet connection is dead, probably ok to skip
                ...

    def close_cmd(self):
        """
        Send the command to close the procServ instance.

        This is the equivalent of pressing ctrl+Q

        Requres the subprocess to be closed first.
        """
        self._ctrl_char("q")

    def toggle_autorestart(self):
        """
        Iterate through the three autorestart options.

        This is the equivalent of pressing ctrl+T

        The options are cycled through in a specific order:
        - start in OFF
        - ONESHOT after first toggle
        - ON after second toggle
        - OFF again after third toggle
        - repeat
        """
        self._ctrl_char("t")

    def toggle_running(self):
        """
        Stop or start the subprocess controlled by procServ.

        If the process is not running, this starts the process.
        If the process is running, this stops the process.

        After stopping a process, the behavior of what to do
        next depends on the autorestart mode:
        - ON = start the process again
        - OFF = keep the process off, but keep procServ running
        - ONESHOT = shutdown procServ when the process ends

        This is the equivalent of pressing ctrl+X
        """
        self._ctrl_char("x")

    def set_state_from_start(self, running: bool, mode: AutoRestartMode):
        """
        From a not running, no autorestart start, toggle to the desired state.
        """
        if running:
            # Not running -> running
            self.toggle_running()
        if mode == AutoRestartMode.ONESHOT:
            # Off -> Oneshot
            self.toggle_autorestart()
        elif mode == AutoRestartMode.ON:
            # Off -> Oneshot -> On
            self.toggle_autorestart()
            self.toggle_autorestart()
        # Wait 1s for status to stabilize
        time.sleep(1)

    def telnet_when_procserv_started(self, timeout: float = 1.0) -> Telnet:
        """
        Helper to get a working Telnet object as early as possible during startup.
        """
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            try:
                return Telnet("localhost", self.port, 1)
            except OSError:
                time.sleep(0.01)
        raise RuntimeError("Could not connect to procServ!")

    def wait_procserv_closed(self, timeout: float = 0.1):
        """
        Returns once the procServ process is closed.
        """
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            try:
                with Telnet("localhost", self.port, 1):
                    ...
            except OSError:
                break
            else:
                time.sleep(0.01)


def get_procserv_bin_path() -> Path:
    """
    Get a Path to the most correct procServ binary built in this repo.
    """
    if not PROCSERV_BUILD.exists():
        raise RuntimeError("f{PROCSERV_BUILD} not found")
    if EPICS_HOST_ARCH is not None:
        bin_path = PROCSERV_BUILD / EPICS_HOST_ARCH / "bin" / "procServ"
        if bin_path.exists():
            return bin_path
    # No host arch, just pick the highest version one that exists
    for pth in reversed(list(PROCSERV_BUILD.glob("*"))):
        bin_path = pth / "bin" / "procServ"
        if bin_path.exists():
            return bin_path
    raise RuntimeError("No procServ binary found")


def ctrl(char: str) -> bytes:
    """
    Get the bytes code for a ctrl+char combination, to be sent to a subprocess.
    """
    if len(char) != 1:
        raise ValueError("Expected a length 1 string")
    return bytes([ord(char.lower()) - ord("a") + 1])


@pytest.fixture(scope="function")
def pvs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """
    Run an IOC, return a list of PVs
    """
    monkeypatch.setenv("EPICS_CA_SERVER_PORT", "5066")
    monkeypatch.setenv("EPICS_CA_AUTO_ADDR_LIST", "NO")
    monkeypatch.setenv("EPICS_CA_ADDR_LIST", "localhost")

    proc = subprocess.Popen(
        ["python3", str(TESTS_PATH / "ioc" / "caproto" / "sysreset.py")]
    )

    yield ["IOC:PYTEST:01:SYSRESET"]

    proc.kill()


@pytest.fixture(scope="function")
def model(qtbot: QtBot) -> IOCTableModel:
    """Basic re-usable model with starting data for use in test suite."""
    config = Config(path="")
    for num in range(10):
        config.add_proc(
            IOCProc(
                name=f"ioc{num}",
                port=30001 + num,
                host="host",
                path=f"ioc/some/path/{num}",
            )
        )
    model = IOCTableModel(config=config, hutch="pytest")
    qtbot.add_widget(model.dialog_add)
    qtbot.add_widget(model.dialog_details)
    return model


@pytest.fixture(scope="function")
def delegate(model, qtbot: QtBot) -> IOCTableDelegate:
    """Basic re-usable delegate with starting model data for use in test suite."""
    delegate = IOCTableDelegate(hutch="pytest", model=model)
    qtbot.add_widget(delegate.hostdialog)
    return delegate
