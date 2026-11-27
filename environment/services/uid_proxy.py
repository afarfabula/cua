#!/usr/bin/env python3
"""UID-checking TCP proxy for Grafana/Prometheus/Perfetto.

Sits in front of services on ports 3000/9090/9001. Blocks agent terminal
(UID 1000) and unknown sources (fail-closed). Only allows root (Chromium
browser) to reach backends.

Backends:
- Grafana: TCP 127.0.0.1:13000
- Prometheus: TCP 127.0.0.1:19090
- Perfetto: Unix socket /run/perfetto.sock (no TCP port to bypass)
"""
import asyncio
import os
import pathlib
import struct
import time

AGENT_UID = int(os.environ.get("AGENT_UID", "1000"))

# (listen_port, backend_type, backend_addr, name)
# backend_type: "tcp" → (host, port), "unix" → socket path
PROXIES = [
    (3000, "tcp", ("127.0.0.1", 13000), "Grafana"),
    (9090, "tcp", ("127.0.0.1", 19090), "Prometheus"),
    (9001, "unix", "/run/perfetto.sock", "Perfetto"),
]

# Cache Chromium PIDs to avoid scanning all /proc/*/fd on every request
_chromium_pids = []
_chromium_cache_time = 0.0
_CACHE_TTL = 5.0  # refresh every 5 seconds


def _find_chromium_pids():
    """Find PIDs whose cmdline contains 'chrome' (covers Chromium's network-service process)."""
    pids = []
    try:
        for entry in pathlib.Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
                if b"chrome" in cmdline or b"chromium" in cmdline:
                    pids.append(int(entry.name))
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass
    return pids


def _get_chromium_pids():
    global _chromium_pids, _chromium_cache_time
    now = time.monotonic()
    if not _chromium_pids or (now - _chromium_cache_time) > _CACHE_TTL:
        _chromium_pids = _find_chromium_pids()
        _chromium_cache_time = now
    return _chromium_pids


def _scan_fd_for_inode(pid: int, socket_marker: str) -> int:
    """Scan a single PID's /proc/<pid>/fd for a socket inode. Returns UID or -1."""
    try:
        fd_dir = pathlib.Path(f"/proc/{pid}/fd")
        for fd in fd_dir.iterdir():
            try:
                target = str(fd.readlink())
                if socket_marker in target:
                    status = pathlib.Path(f"/proc/{pid}/status").read_text()
                    for line in status.splitlines():
                        if line.startswith("Uid:"):
                            return int(line.split()[1])
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass
    return -1


def _find_inode_in_proc(proc_file: str, listen_hex: str, client_hex: str) -> str | None:
    """Read /proc/net/tcp or /proc/net/tcp6, find socket inode matching client→listen."""
    try:
        with open(proc_file) as f:
            next(f)  # skip header
            for line in f:
                parts = line.split()
                if len(parts) < 10:
                    continue
                local = parts[1]
                remote = parts[2]
                # Client endpoint: local=client_port, remote=listen_port
                if local.endswith(f":{client_hex}") and remote.endswith(f":{listen_hex}"):
                    return parts[9]
    except (IOError, OSError):
        pass
    return None


def get_client_uid(listen_port: int, client_port: int) -> int:
    """Look up UID of the process that owns the connection FROM client_port TO listen_port.

    Scans Chromium PIDs first (fast path), falls back to all PIDs if not found.
    Checks both /proc/net/tcp and /proc/net/tcp6.
    """
    listen_hex = format(listen_port, "04X")
    client_hex = format(client_port, "04X")

    # Find socket inode in /proc/net/tcp and /proc/net/tcp6
    inode = _find_inode_in_proc("/proc/net/tcp", listen_hex, client_hex)
    if not inode or inode == "0":
        inode = _find_inode_in_proc("/proc/net/tcp6", listen_hex, client_hex)
    if not inode or inode == "0":
        return -1

    socket_marker = f"socket:[{inode}]"

    # Fast path: scan only Chromium PIDs (the browser's network-service process)
    for pid in _get_chromium_pids():
        uid = _scan_fd_for_inode(pid, socket_marker)
        if uid >= 0:
            return uid

    # Slow fallback: scan all PIDs (handles curl, non-Chromium clients)
    try:
        for entry in pathlib.Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            uid = _scan_fd_for_inode(int(entry.name), socket_marker)
            if uid >= 0:
                return uid
    except (PermissionError, OSError):
        pass
    return -1


async def handle_client(reader, writer, backend_type, backend_addr, listen_port, name):
    peer = writer.get_extra_info("peername")
    if not peer:
        writer.close()
        await writer.wait_closed()
        return
    client_port = peer[1]
    uid = -1
    for attempt in range(3):
        uid = get_client_uid(listen_port, client_port)
        if uid >= 0:
            break
        await asyncio.sleep(0.05)
    if uid == AGENT_UID or uid < 0:
        writer.close()
        await writer.wait_closed()
        return
    try:
        if backend_type == "unix":
            backend_reader, backend_writer = await asyncio.open_unix_connection(backend_addr)
        else:
            backend_reader, backend_writer = await asyncio.open_connection(backend_addr[0], backend_addr[1])
    except Exception:
        writer.close()
        await writer.wait_closed()
        return

    async def pipe(src, dst):
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except Exception:
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass

    await asyncio.gather(
        pipe(reader, backend_writer),
        pipe(backend_reader, writer),
    )


async def main():
    servers = []
    for listen_port, backend_type, backend_addr, name in PROXIES:
        server = await asyncio.start_server(
            lambda r, w, bt=backend_type, ba=backend_addr, lp=listen_port, n=name:
                handle_client(r, w, bt, ba, lp, n),
            "0.0.0.0", listen_port,
        )
        backend_desc = backend_addr if backend_type == "unix" else f"{backend_addr[0]}:{backend_addr[1]}"
        print(f"[uid-proxy] {name}: :{listen_port} -> {backend_type}://{backend_desc} (block UID={AGENT_UID})", flush=True)
    await asyncio.Event().wait()  # block forever (serve_forever is 3.12+)


if __name__ == "__main__":
    asyncio.run(main())
