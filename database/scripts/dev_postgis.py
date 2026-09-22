#!/usr/bin/env python3
"""Portable PostgreSQL 16 + PostGIS for local development on Windows without Docker.

Downloads the official EDB PostgreSQL binaries zip and the OSGeo PostGIS bundle into
%LOCALAPPDATA%\\urbanview-dev\\pg16 (override with URBANVIEW_PG_HOME), initialises a cluster and
runs it on 127.0.0.1:55432 with trust authentication (local development only). No installer, no
Windows service, no PATH changes; `uninstall` removes the folder.

    python database/scripts/dev_postgis.py install                 # download, extract, initdb
    python database/scripts/dev_postgis.py start                   # start the server
    python database/scripts/dev_postgis.py createdb urbanview_test # database with PostGIS
    python database/scripts/dev_postgis.py url urbanview_test      # print the SQLAlchemy URL
    python database/scripts/dev_postgis.py status | stop | uninstall

Only the standard library is used, so it runs with any Python 3.11+.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

PG_ZIP_URL = os.environ.get(
    "URBANVIEW_PG_ZIP_URL",
    "https://get.enterprisedb.com/postgresql/postgresql-16.9-1-windows-x64-binaries.zip",
)
POSTGIS_ZIP_URL = os.environ.get(
    "URBANVIEW_POSTGIS_ZIP_URL",
    "https://download.osgeo.org/postgis/windows/pg16/postgis-bundle-pg16-3.6.2x64.zip",
)
DEFAULT_HOME = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    / "urbanview-dev"
    / "pg16"
)
HOME = Path(os.environ.get("URBANVIEW_PG_HOME", DEFAULT_HOME))
HOST = "127.0.0.1"
PORT = int(os.environ.get("URBANVIEW_PG_PORT", "55432"))
SUPERUSER = "postgres"

PGSQL = HOME / "pgsql"
BIN = PGSQL / "bin"
DATA = HOME / "data"
DOWNLOADS = HOME / "downloads"
LOG = HOME / "server.log"


def exe(name: str) -> Path:
    return BIN / (name + (".exe" if os.name == "nt" else ""))


def environment() -> dict[str, str]:
    env = dict(os.environ)
    env.update(PGHOST=HOST, PGPORT=str(PORT), PGUSER=SUPERUSER, PGCLIENTENCODING="UTF8")
    proj_dirs = sorted((PGSQL / "share" / "contrib").glob("postgis-*/proj"))
    if proj_dirs:
        env.setdefault("PROJ_LIB", str(proj_dirs[-1]))
    if (PGSQL / "gdal-data").is_dir():
        env.setdefault("GDAL_DATA", str(PGSQL / "gdal-data"))
    return env


def run(cmd: list, *, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=check, env=environment(), **kwargs)


# download.osgeo.org serves ~30 KB/s per connection but allows several at once; large archives are
# fetched as parallel HTTP Range segments. Set URBANVIEW_DOWNLOAD_CONNECTIONS=1 to disable.
PARALLEL_CONNECTIONS = max(1, int(os.environ.get("URBANVIEW_DOWNLOAD_CONNECTIONS", "8")))
PARALLEL_MIN_BYTES = 16 << 20


def remote_info(url: str) -> tuple[int | None, bool]:
    """(Content-Length, server supports byte ranges)."""
    request = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(request, timeout=60) as response:
        length = response.headers.get("Content-Length")
        ranges = response.headers.get("Accept-Ranges", "").lower() == "bytes"
        return (int(length) if length else None), ranges


def _fetch_range(
    url: str, path: Path, start: int, end: int, progress: list[int], index: int
) -> None:
    """Write bytes [start, end] of the URL into ``path`` at the same offsets, resuming whenever the
    server closes the connection early. Gives up after 5 consecutive attempts without progress."""
    offset = start
    stalled = 0
    while offset <= end:
        before = offset
        request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-{end}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, path.open("r+b") as out:
                if response.status != 206:
                    raise OSError("server ignored the Range request")
                out.seek(offset)
                while chunk := response.read(1 << 18):
                    out.write(chunk)
                    offset += len(chunk)
                    progress[index] = offset - start
        except Exception:  # noqa: BLE001 - any transport error: resume from the current offset
            pass
        if offset <= end:
            stalled = stalled + 1 if offset == before else 0
            if stalled >= 5:
                raise RuntimeError(f"segment {index} stalled at byte {offset}")
            time.sleep(2)


def _download_parallel(url: str, part: Path, expected: int, connections: int) -> None:
    with part.open("wb") as out:
        out.truncate(expected)
    segment = -(-expected // connections)
    bounds = [
        (i * segment, min(expected, (i + 1) * segment) - 1)
        for i in range(connections)
        if i * segment < expected
    ]
    progress = [0] * len(bounds)
    errors: list[Exception] = []

    def worker(index: int, start: int, end: int) -> None:
        try:
            _fetch_range(url, part, start, end, progress, index)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i, s, e), daemon=True)
        for i, (s, e) in enumerate(bounds)
    ]
    for thread in threads:
        thread.start()
    while any(thread.is_alive() for thread in threads):
        time.sleep(2)
        done = sum(progress) / 1e6
        print(
            f"\r  {done:6.0f} / {expected / 1e6:.0f} MB ({len(bounds)} connections)",
            end="",
            flush=True,
        )
    print()
    if errors:
        sys.exit(f"download of {url} failed: {errors[0]}")


def _download_sequential(url: str, part: Path, expected: int | None, attempts: int = 6) -> None:
    for attempt in range(1, attempts + 1):
        done = part.stat().st_size if part.exists() else 0
        if expected is not None and done >= expected:
            return
        request = urllib.request.Request(url, headers={"Range": f"bytes={done}-"} if done else {})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, part.open("ab") as out:
                if done and response.status != 206:  # server ignored the range: start over
                    out.truncate(0)
                    done = 0
                while chunk := response.read(1 << 20):
                    out.write(chunk)
                    done += len(chunk)
                    if expected:
                        print(
                            f"\r  {done / 1e6:6.0f} / {expected / 1e6:.0f} MB", end="", flush=True
                        )
            print()
        except Exception as exc:  # noqa: BLE001
            print(f"\n  attempt {attempt} interrupted ({exc}); retrying")
            time.sleep(3)
            continue
        if expected is None or done >= expected:
            return
        print(f"  attempt {attempt}: connection closed at {done} of {expected} bytes; resuming")
        time.sleep(2)


def download(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` verifying Content-Length, resuming interrupted transfers and
    validating the zip. Mirrors sometimes close connections early or throttle per connection."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    expected, ranges_ok = remote_info(url)
    if dest.exists():
        if expected is None or dest.stat().st_size == expected:
            print(f"  already downloaded: {dest.name}")
            return
        print(f"  {dest.name} is incomplete ({dest.stat().st_size} of {expected} bytes)")
        dest.unlink()

    part = dest.with_suffix(dest.suffix + ".part")
    print(f"  downloading {url}")
    if expected and ranges_ok and expected >= PARALLEL_MIN_BYTES and PARALLEL_CONNECTIONS > 1:
        _download_parallel(url, part, expected, PARALLEL_CONNECTIONS)
    else:
        _download_sequential(url, part, expected)

    if expected is not None and (not part.exists() or part.stat().st_size != expected):
        sys.exit(f"download of {url} failed (incomplete); re-run install to resume")
    part.replace(dest)
    if not zipfile.is_zipfile(dest):
        dest.unlink()
        sys.exit(f"{dest.name} is not a valid zip archive; re-run install")


def installed() -> bool:
    return exe("pg_ctl").exists() and (PGSQL / "share" / "extension" / "postgis.control").exists()


def install() -> None:
    if exe("pg_ctl").exists():
        print(f"  PostgreSQL already extracted in {PGSQL}")
    else:
        pg_zip = DOWNLOADS / Path(PG_ZIP_URL).name
        download(PG_ZIP_URL, pg_zip)
        print("  extracting PostgreSQL")
        with zipfile.ZipFile(pg_zip) as archive:
            archive.extractall(HOME)  # the archive has a single top-level 'pgsql/' folder
        if not exe("pg_ctl").exists():
            sys.exit(f"unexpected archive layout: {exe('pg_ctl')} not found")

    if (PGSQL / "share" / "extension" / "postgis.control").exists():
        print("  PostGIS already installed")
    else:
        postgis_zip = DOWNLOADS / Path(POSTGIS_ZIP_URL).name
        download(POSTGIS_ZIP_URL, postgis_zip)
        print("  extracting PostGIS bundle")
        staging = HOME / "postgis-staging"
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(postgis_zip) as archive:
            archive.extractall(staging)
        # The bundle has one top-level folder whose contents (bin/, lib/, share/…) are copied
        # over the PostgreSQL folder, as its README instructs.
        entries = list(staging.iterdir())
        source = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
        shutil.copytree(source, PGSQL, dirs_exist_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        if not installed():
            sys.exit("PostGIS bundle did not install postgis.control; check the bundle version")

    if not (DATA / "PG_VERSION").exists():
        print("  initdb")
        run(
            [
                exe("initdb"),
                "-D",
                DATA,
                "-U",
                SUPERUSER,
                "--auth=trust",
                "-E",
                "UTF8",
                "--locale=C",
                "--no-instructions",
            ],
            stdout=subprocess.DEVNULL,
        )
    print(f"installed in {HOME}")


def is_running() -> bool:
    if not (DATA / "PG_VERSION").exists():
        return False
    result = run(
        [exe("pg_ctl"), "status", "-D", DATA],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def start() -> None:
    if not installed() or not (DATA / "PG_VERSION").exists():
        sys.exit("not installed: run `install` first")
    if is_running():
        print(f"already running on {HOST}:{PORT}")
        return
    # stdout/stderr must not be inherited by the postmaster, or callers would hang on the pipe.
    run(
        [
            exe("pg_ctl"),
            "-D",
            DATA,
            "-l",
            LOG,
            "-o",
            f"-p {PORT} -c listen_addresses={HOST}",
            "-w",
            "start",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"started on {HOST}:{PORT} (log: {LOG})")


def stop() -> None:
    if not is_running():
        print("not running")
        return
    run(
        [exe("pg_ctl"), "-D", DATA, "-m", "fast", "-w", "stop"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("stopped")


def status() -> None:
    if not installed():
        print(f"not installed ({HOME})")
        return
    result = run([exe("pg_ctl"), "status", "-D", DATA], check=False, capture_output=True, text=True)
    print((result.stdout or result.stderr).strip())
    print(f"home: {HOME}")


def url(name: str) -> str:
    return f"postgresql+asyncpg://{SUPERUSER}@{HOST}:{PORT}/{name}"


def createdb(name: str) -> None:
    if not is_running():
        sys.exit("server is not running: run `start` first")
    exists = run(
        [
            exe("psql"),
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{name}'",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if exists != "1":
        run([exe("createdb"), name], stdout=subprocess.DEVNULL)
    run(
        [
            exe("psql"),
            "-d",
            name,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "CREATE EXTENSION IF NOT EXISTS postgis",
        ],
        stdout=subprocess.DEVNULL,
    )
    version = run(
        [exe("psql"), "-d", name, "-tAc", "SELECT postgis_version()"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"{name}: postgis {version}")
    print(url(name))


def uninstall() -> None:
    if installed() and is_running():
        stop()
    shutil.rmtree(HOME, ignore_errors=True)
    print(f"removed {HOME}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "start", "stop", "status", "uninstall"):
        sub.add_parser(name)
    sub.add_parser("createdb").add_argument("name")
    sub.add_parser("url").add_argument("name")
    args = parser.parse_args()
    if args.command == "install":
        install()
    elif args.command == "start":
        start()
    elif args.command == "stop":
        stop()
    elif args.command == "status":
        status()
    elif args.command == "uninstall":
        uninstall()
    elif args.command == "createdb":
        createdb(args.name)
    elif args.command == "url":
        print(url(args.name))


if __name__ == "__main__":
    main()
