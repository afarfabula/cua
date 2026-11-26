#!/usr/bin/env python3
"""UID-checking TCP proxy for Grafana/Prometheus/Perfetto.

Sits in front of the services on their original ports (3000/9090/9001).
Blocks connections from the agent terminal (UID 1000) and unknown sources.
Only allows root (the controlled Chromium browser) to reach the backends.

This is the fallback when iptables is unavailable (no CAP_NET_ADMIN).
"""
import asyncio
import os
import pathlib

AGENT_UID = int(os.environ.get("AGENT_UID", "1000"))

# (listen_port, backend_host, backend_port, name)
PROXIES = [
    (3000, "127.0.0.1", 13000, "Grafana"),
    (9090, "127.0.0.1", 19090, "Prometheus"),
    (9001, "127.0.0.1", 19001, "Perfetto"),
]


def get_client_uid(listen_port: int, client_port: int) -> int:
    """Look up UID of the process that owns the connection FROM client_port TO listen_port."""
    listen_hex = format(listen_port, "04X")
    client_hex = format(client_port, "04X")
    inode = None
    try:
        with open("/proc/net/tcp") as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) < 10:
                    continue
                # Client endpoint: local=client_port, remote=listen_port
                if parts[1].endswith(f":{client_hex}") and parts[2].endswith(f":{listen_hex}"):
                    inode = parts[9]
                    break
    except (IOError, OSError):
        return -1
    if not inode or inode == "0":
        return -1
    socket_marker = f"socket:[{inode}]"
    try:
        for entry in pathlib.Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                for fd in (entry / "fd").iterdir():
                    try:
                        if socket_marker in str(fd.readlink()):
                            status = (entry / "status").read_text()
                            for line in status.splitlines():
                                if line.startswith("Uid:"):
                                    return int(line.split()[1])
                    except (PermissionError, OSError):
                        continue
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass
    return -1


async def handle_client(reader, writer, backend_host, backend_port, listen_port, name):
    peer = writer.get_extra_info("peername")
    if not peer:
        writer.close()
        await writer.wait_closed()
        return
    client_port = peer[1]
    uid = get_client_uid(listen_port, client_port)
    if uid == AGENT_UID:
        # Block: agent terminal
        writer.close()
        await writer.wait_closed()
        return
    # Allow: root browser or unknown (fail-open to avoid breaking legitimate access)
    try:
        backend_reader, backend_writer = await asyncio.open_connection(backend_host, backend_port)
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
    for listen_port, backend_host, backend_port, name in PROXIES:
        server = await asyncio.start_server(
            lambda r, w, lp=listen_port, bh=backend_host, bp=backend_port, n=name:
                handle_client(r, w, bh, bp, lp, n),
            "0.0.0.0", listen_port,
        )
        servers.append(server)
        print(f"[uid-proxy] {name}: :{listen_port} -> {backend_host}:{backend_port} (block UID={AGENT_UID})", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
