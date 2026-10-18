"""Дочерние процессы: изоляция Ctrl+C и завершение только своего процесса."""
import os
import socket
import subprocess
import time


def isolated_process_options():
    """Ctrl+C обрабатывает рекордер; серверы нужны до окончания восстановления."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def spawn_logged(command, log_path, **kwargs):
    """Создать собственный процесс; дескриптор журнала родителю больше не нужен."""
    with log_path.open("wb") as log:
        return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, **isolated_process_options(), **kwargs)


def wait_port(host, port, process=None, timeout=20, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        try:
            with socket.create_connection((host, port), timeout=min(1, max(0.01, deadline-time.monotonic()))):
                return True
        except OSError:
            time.sleep(interval)
    return False
