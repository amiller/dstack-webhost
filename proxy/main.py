"""Entrypoint: starts socket proxies + ingress/API server."""

import asyncio
import logging
import os
import signal

from aiohttp import web

from .tracker import ContainerTracker
from .audit import AuditLogManager
from .docker_proxy import DockerProxy
from .dstack_proxy import DstackProxy
from .docker_client import DockerClient
from .projects import ProjectStore
from .runtimes import RuntimeManager
from .tunnel import TunnelStore
from . import ingress as ingress_mod
from .ingress import Ingress

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
log = logging.getLogger("tee-daemon")

PROXY_DIR = os.environ.get("PROXY_SOCKET_DIR", "/var/run/proxy")
DOCKER_SOCK = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
DSTACK_SOCK = os.environ.get("DSTACK_SOCKET", "/var/run/dstack.sock")
DATA_DIR = os.environ.get("DAEMON_DATA_DIR", "/var/lib/tee-daemon/projects")
AUDIT_DIR = os.environ.get("DAEMON_AUDIT_DIR", "/var/lib/tee-daemon/audit")
TUNNEL_DIR = os.environ.get("DAEMON_TUNNEL_DIR", "/var/lib/tee-daemon/tunnels")
INGRESS_PORT = int(os.environ.get("INGRESS_PORT", "8080"))


async def start():
    os.makedirs(PROXY_DIR, exist_ok=True)

    dstack_sock = DSTACK_SOCK if os.path.exists(DSTACK_SOCK) else None
    ingress_mod.DSTACK_SOCK = dstack_sock

    tracker = ContainerTracker()
    audit_manager = AuditLogManager(AUDIT_DIR)
    docker = DockerClient(DOCKER_SOCK)
    store = ProjectStore(DATA_DIR)
    rtm = RuntimeManager(docker, store, tracker)
    tunnel_store = TunnelStore(TUNNEL_DIR)

    # Docker socket proxy (existing)
    docker_proxy = DockerProxy(DOCKER_SOCK, tracker, audit_manager)
    await docker_proxy.ensure_network()
    await docker_proxy.recover_tracked()
    log.info("Recovered %d tracked containers", len(tracker.all_ids()))

    # Connect ourselves to runtime networks so we can proxy to runtime containers
    hostname = os.environ.get("HOSTNAME", "")
    if hostname:
        for net in ("tee-apps-dev", "tee-apps-attested"):
            try:
                await docker.connect_network(hostname, net)
                log.info("Connected self (%s) to %s network", hostname, net)
            except Exception as e:
                log.warning("Could not connect to %s: %s", net, e)

    docker_app = web.Application()
    docker_app.router.add_route("*", "/{path:.*}", docker_proxy.handle)
    docker_sock_path = os.path.join(PROXY_DIR, "docker.sock")
    if os.path.exists(docker_sock_path):
        os.unlink(docker_sock_path)
    docker_runner = web.AppRunner(docker_app)
    await docker_runner.setup()
    await web.UnixSite(docker_runner, docker_sock_path).start()
    os.chmod(docker_sock_path, 0o666)
    log.info("Docker proxy listening on %s", docker_sock_path)

    # dstack socket proxy (existing)
    if dstack_sock:
        dstack_proxy = DstackProxy(dstack_sock)
        dstack_app = web.Application()
        dstack_app.router.add_route("*", "/{path:.*}", dstack_proxy.handle)
        dstack_sock_path = os.path.join(PROXY_DIR, "dstack.sock")
        if os.path.exists(dstack_sock_path):
            os.unlink(dstack_sock_path)
        dstack_runner = web.AppRunner(dstack_app)
        await dstack_runner.setup()
        await web.UnixSite(dstack_runner, dstack_sock_path).start()
        os.chmod(dstack_sock_path, 0o666)
        log.info("dstack proxy listening on %s", dstack_sock_path)
    else:
        log.warning("dstack socket not found — dstack proxy disabled")

    # Recovery: restore shared runtimes for existing projects
    await rtm.recover_all()

    # Recovery: restore tunnels from disk
    tunnel_store.recover()
    log.info("Recovered %d active tunnels", len(tunnel_store.list()))

    # Ingress + API on TCP port(s)
    ing = Ingress(store, docker, audit_manager, tracker, rtm, tunnel_store)

    # Check for port conflicts
    port_conflicts = []
    projects = store.list()
    port_to_project = {}
    for p in projects:
        if p.listen and p.listen.port:
            port = p.listen.port
            if port in port_to_project:
                port_conflicts.append(f"Port {port} requested by {p.name} and {port_to_project[port]}")
            else:
                port_to_project[port] = p.name

    if port_conflicts:
        error_msg = "Port conflicts detected:\n" + "\n".join(f"  - {c}" for c in port_conflicts)
        log.error(error_msg)
        raise RuntimeError(error_msg)

    # Update port map in ingress
    ing.update_port_map()

    # Collect all HTTP ports to bind: default ingress port + custom HTTP project ports
    ports_to_bind = set([INGRESS_PORT])
    for p in projects:
        if p.listen and p.listen.port and p.listen.protocol == "http":
            ports_to_bind.add(p.listen.port)

    # Create one app and bind to all HTTP ports
    ingress_app = web.Application(client_max_size=100 * 1024 * 1024)
    ingress_app.router.add_route("*", "/{path:.*}", ing.handle)
    ingress_runner = web.AppRunner(ingress_app)
    await ingress_runner.setup()

    # Bind to all HTTP ports
    for port in sorted(ports_to_bind):
        await web.TCPSite(ingress_runner, "0.0.0.0", port).start()
        if port == INGRESS_PORT:
            log.info("Ingress + API listening on :%d (default)", port)
        else:
            project_name = port_to_project.get(port, "unknown")
            log.info("Ingress for project '%s' listening on :%d (HTTP)", project_name, port)

    # Start TCP servers for projects with TCP protocol
    tcp_projects = ing.get_tcp_projects()
    tcp_servers = []
    for port, project_name in tcp_projects:
        server = await ing.create_tcp_server(port, project_name)
        tcp_servers.append(server)
        log.info("TCP proxy for project '%s' listening on :%d", project_name, port)

    log.info("tee-daemon running")

    # Background task: cleanup expired tunnels
    async def cleanup_expired_tunnels():
        while True:
            await asyncio.sleep(60)  # Check every minute
            tunnel_store.cleanup_expired()

    cleanup_task = asyncio.create_task(cleanup_expired_tunnels())

    stop = asyncio.Event()
    for sig_name in ("SIGINT", "SIGTERM"):
        asyncio.get_event_loop().add_signal_handler(getattr(signal, sig_name), stop.set)
    await stop.wait()
    log.info("Shutting down")

    # Cancel cleanup task
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    # Close TCP servers
    for server in tcp_servers:
        server.close()
        await server.wait_closed()


def main():
    asyncio.run(start())


if __name__ == "__main__":
    main()
