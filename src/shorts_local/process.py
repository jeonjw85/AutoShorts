import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_locks: ContextVar[tuple[int, ...]] = ContextVar("locks", default=())


class BlockedError(RuntimeError):
    """Needs configuration or an explicit recovery decision, not blind retries."""


class QuotaError(RuntimeError):
    """Included/free allowance is exhausted. Never fall back to paid requests."""


class BusyError(RuntimeError):
    pass


def atomic_json(path: Path, value: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BusyError("다른 작업이 실행 중입니다.") from exc
        token = _locks.set((*_locks.get(), stream.fileno()))
        try:
            yield
        finally:
            _locks.reset(token)
            # Closing the last inherited descriptor releases the lock. LOCK_UN here
            # would also unlock it for a supervisor still cleaning up an orphan child.


def _terminate(child: subprocess.Popen):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def _supervise() -> int:
    """Hold inherited locks until the command exits, including after parent SIGKILL."""
    alive_fd, timeout = int(sys.argv[1]), int(sys.argv[2])
    lock_fds = tuple(json.loads(sys.argv[3]))
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())

    def parent_exit():
        # Only the parent owns the write end. EOF also arrives on abrupt termination.
        with os.fdopen(alive_fd, "rb", buffering=0) as stream:
            stream.read(1)
        stopped.set()

    threading.Thread(target=parent_exit, daemon=True).start()
    try:
        child = subprocess.Popen(sys.argv[4:], start_new_session=True, pass_fds=lock_fds)
    except FileNotFoundError:
        return 127
    deadline = time.monotonic() + timeout
    try:
        while child.poll() is None:
            if stopped.wait(0.1):
                return 143
            if time.monotonic() >= deadline:
                return 124
        return child.returncode
    finally:
        _terminate(child)


def run_process(args: list[str], log: Path, timeout: int = 1800,
                stdin: str | None = None, env: dict | None = None,
                cwd: Path | None = None) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as stream:
        os.chmod(log, 0o600)
        alive_read, alive_write = os.pipe()
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "shorts_local.process", str(alive_read), str(timeout),
                 json.dumps(_locks.get()), *args],
                stdout=stream, stderr=stream, stdin=subprocess.PIPE,
                text=True, start_new_session=True, env=env, cwd=cwd,
                pass_fds=(alive_read, *_locks.get()),
            )
        except BaseException:
            os.close(alive_write)
            raise
        finally:
            os.close(alive_read)
        try:
            child.communicate(input=stdin, timeout=timeout + 10)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            _terminate(child)
            raise RuntimeError(f"작업 제한 시간 초과/중단. 로그: {log.name}") from None
        finally:
            os.close(alive_write)
        if child.returncode == 127:
            raise BlockedError(f"실행 파일을 찾을 수 없습니다: {Path(args[0]).name}")
        if child.returncode == 124:
            raise RuntimeError(f"작업 제한 시간 초과. 로그: {log.name}")
        if child.returncode:
            raise RuntimeError(f"작업 종료 코드 {child.returncode}. 로그: {log.name}")


if __name__ == "__main__":
    sys.exit(_supervise())
