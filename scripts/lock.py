"""Exclusive file lock on DATA_DIR/execute.lock, shared by execute.py and manage.py.
Only one order-sending action runs at a time. A second caller fails fast instead of waiting."""
import fcntl, os, contextlib
import config

class LockBusy(RuntimeError):
    pass

@contextlib.contextmanager
def exclusive(name="execute.lock"):
    path = config.data_path(name)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise LockBusy(f"another run holds {path}; try again later")
        os.ftruncate(fd, 0); os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        try: fcntl.flock(fd, fcntl.LOCK_UN)
        finally: os.close(fd)
