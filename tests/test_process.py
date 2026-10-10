import os
import signal
import socket
import subprocess
import sys
import time

import pytest


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGKILL])
@pytest.mark.parametrize("lock_name", ["worker.lock", "collector.lock", "instagram.lock"])
def test_parent_death_stops_child_without_releasing_its_work_lock(tmp_path, stop_signal, lock_name):
    from shorts_local.process import BusyError, exclusive_lock

    lock = tmp_path / lock_name
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(10)
        child_code = (
            "import os,signal,socket,time,sys; "
            "signal.signal(signal.SIGTERM, lambda *_: (time.sleep(0.6), sys.exit(0))); "
            f"s=socket.create_connection({listener.getsockname()!r}); "
            "s.sendall(str(os.getpid()).encode()); time.sleep(60)"
        )
        parent_code = (
            "from pathlib import Path\n"
            "from shorts_local.process import exclusive_lock,run_process\n"
            f"with exclusive_lock(Path({str(lock)!r})):\n"
            f" run_process({[sys.executable, '-c', child_code]!r}, "
            f"Path({str(tmp_path / 'child.log')!r}), timeout=60)\n"
        )
        parent = subprocess.Popen([sys.executable, "-c", parent_code])
        child_pid = None
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(10)
                child_pid = int(connection.recv(64))
                parent.send_signal(stop_signal)
                parent.wait(timeout=5)
                # The child is still cleaning up. A replacement worker cannot enter yet.
                with pytest.raises(BusyError), exclusive_lock(lock):
                    pass
                # The supervisor must reap the child even if the parent was SIGKILLed.
                assert connection.recv(1) == b""
            deadline = time.monotonic() + 3
            while True:
                try:
                    with exclusive_lock(lock):
                        break
                except BusyError:
                    if time.monotonic() >= deadline:
                        pytest.fail("worker lock was not released after child shutdown")
                    time.sleep(0.02)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            if child_pid:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
