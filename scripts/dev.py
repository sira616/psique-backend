"""Servidor de desarrollo con recarga que no se cuelga en Windows.

    ./venv/Scripts/python.exe -m scripts.dev                 # 127.0.0.1:8010
    ./venv/Scripts/python.exe -m scripts.dev --port 8013

Por qué no `uvicorn --reload`: en Windows su reloader para el worker con
`os.kill(pid, CTRL_C_EVENT)`, que solo llega si los dos procesos comparten una consola.
Lanzado sin consola (desde la app de escritorio vía `cmd /c`, un IDE o un shell en segundo
plano) el evento se pierde, el worker viejo sigue sirviendo y el reloader se queda para
siempre en `process.join()` tras "WatchFiles detected changes... Reloading...".

Aquí uvicorn corre sin `--reload` en un proceso hijo, en su propio grupo de procesos. Al
cambiar algo se le pide parar con Ctrl+Break (salida ordenada si hay consola compartida,
con `--timeout-graceful-shutdown` para no esperar a un SSE abierto) y, si no ha salido a
tiempo, se mata el árbol entero (`taskkill /T /F`: el python.exe del venv es un lanzador
que tiene debajo el intérprete real). SQLite aguanta un corte: la transacción a medias se
deshace al reabrir la base.

Vigila solo `app/` y `migrations/` (*.py), `.env` y `alembic.ini`: nunca `venv/`, `media/`
ni `psique.db`. Cambiar `.env` también recarga, porque cada arranque es un proceso nuevo
que vuelve a leerlo.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from watchfiles import Change, watch

ROOT = Path(__file__).resolve().parent.parent
WATCH_DIRS = ("app", "migrations")
WATCH_FILES = (".env", "alembic.ini")
IS_WINDOWS = sys.platform == "win32"
# Lo que tarda uvicorn en cortar conexiones abiertas (SSE) al parar, más un margen.
GRACEFUL_SECONDS = 3
STOP_TIMEOUT_SECONDS = GRACEFUL_SECONDS + 3


def log(msg: str) -> None:
    print(f"[dev] {msg}", flush=True)


def relevant(change: Change, path: str) -> bool:
    p = Path(path)
    try:
        rel = p.resolve().relative_to(ROOT)
    except ValueError:
        return False
    if rel.as_posix() in WATCH_FILES:
        return True
    return rel.parts[0] in WATCH_DIRS and p.suffix == ".py" and "__pycache__" not in rel.parts


def _kill_with_parent_windows() -> None:
    """Mete este proceso en un Job que mata a todos sus hijos si muere, aunque sea a la
    fuerza: si no, un cierre brusco del supervisor dejaría el uvicorn huérfano con el puerto."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        ctypes.c_void_p(job), 9, ctypes.byref(info), ctypes.sizeof(info)  # 9 = ExtendedLimitInformation
    )
    if ok:
        # El handle no se cierra nunca: se cierra solo al morir este proceso, y con él los hijos.
        kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(kernel32.GetCurrentProcess()))


def start(args: argparse.Namespace) -> subprocess.Popen:
    cmd = [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", args.host, "--port", str(args.port),
        "--timeout-graceful-shutdown", str(GRACEFUL_SECONDS),
        *args.uvicorn_args,
    ]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    return subprocess.Popen(cmd, cwd=ROOT, creationflags=flags, start_new_session=not IS_WINDOWS)


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            # Solo le llega a su grupo y solo con consola compartida; si no, falla o se pierde.
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(STOP_TIMEOUT_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    log("no ha parado a tiempo: se mata el proceso")
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("uvicorn_args", nargs=argparse.REMAINDER, help="Se pasan tal cual a uvicorn.")
    args = parser.parse_args(argv)
    if IS_WINDOWS:
        _kill_with_parent_windows()

    proc = start(args)
    log(f"uvicorn en http://{args.host}:{args.port} (pid {proc.pid}); vigilando app/, migrations/, .env")
    try:
        # La raíz entera y no los ficheros sueltos: un editor que guarda `.env` renombrando
        # uno temporal rompería la vigilancia de ese fichero. El filtro descarta el resto.
        for changes in watch(ROOT, watch_filter=relevant, debounce=800, raise_interrupt=False):
            names = sorted({Path(p).resolve().relative_to(ROOT).as_posix() for _, p in changes})
            log(f"cambios en {', '.join(names)}: reiniciando")
            t0 = time.monotonic()
            stop(proc)
            proc = start(args)
            log(f"reiniciado en {time.monotonic() - t0:.1f}s (pid {proc.pid})")
    finally:
        stop(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
