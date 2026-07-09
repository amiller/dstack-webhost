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
from .tokens import TokenStore
from .broker import BrokerStore, BrokerProxy
from .browser_pool import BrowserPool, parse_binds
from . import ingress as ingress_mod
from . import deploy as deploy_mod
from .ingress import Ingress

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
log = logging.getLogger("tee-daemon")

PROXY_DIR = os.environ.get("PROXY_SOCKET_DIR", "/var/run/proxy")
# Broker socket lives in its OWN dir, separate from PROXY_DIR (which holds
# docker.sock). Only this dir is shared into attested apps — never docker.sock.
BROKER_SOCKET_DIR = os.environ.get("BROKER_SOCKET_DIR", "/var/run/broker")
DOCKER_SOCK = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
DSTACK_SOCK = os.environ.get("DSTACK_SOCKET", "/var/run/dstack.sock")
DATA_DIR = os.environ.get("DAEMON_DATA_DIR", "/var/lib/tee-daemon/projects")
AUDIT_DIR = os.environ.get("DAEMON_AUDIT_DIR", "/var/lib/tee-daemon/audit")
TUNNEL_DIR = os.environ.get("DAEMON_TUNNEL_DIR", "/var/lib/tee-daemon/tunnels")
TOKEN_DIR = os.environ.get("DAEMON_TOKEN_DIR", "/var/lib/tee-daemon/tokens")
BROKER_DIR = os.environ.get("DAEMON_BROKER_DIR", "/var/lib/tee-daemon/broker")
CREDS_DIR = os.environ.get("DAEMON_CREDS_DIR", "/var/lib/tee-daemon/creds")
INGRESS_PORT = int(os.environ.get("INGRESS_PORT", "8080"))


async def start():
    os.makedirs(PROXY_DIR, exist_ok=True)
    os.makedirs(BROKER_SOCKET_DIR, exist_ok=True)

    dstack_sock = DSTACK_SOCK if os.path.exists(DSTACK_SOCK) else None
    ingress_mod.DSTACK_SOCK = dstack_sock
    deploy_mod.DSTACK_SOCK = dstack_sock

    tracker = ContainerTracker()
    audit_manager = AuditLogManager(AUDIT_DIR)
    docker = DockerClient(DOCKER_SOCK)
    store = ProjectStore(DATA_DIR)
    rtm = RuntimeManager(docker, store, tracker)
    tunnel_store = TunnelStore(TUNNEL_DIR)
    token_store = TokenStore(TOKEN_DIR)

    # Broker store for sealed credential grants (only when dstack is available)
    broker_store = None
    if dstack_sock:
        broker_store = BrokerStore(BROKER_DIR, CREDS_DIR, dstack_sock)

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
        # Serve in BROKER_SOCKET_DIR (NOT PROXY_DIR) so apps can be given the
        # broker without also getting docker.sock.
        dstack_sock_path = os.path.join(BROKER_SOCKET_DIR, "dstack.sock")
        if os.path.exists(dstack_sock_path):
            os.unlink(dstack_sock_path)
        dstack_runner = web.AppRunner(dstack_app)
        await dstack_runner.setup()
        await web.UnixSite(dstack_runner, dstack_sock_path).start()
        os.chmod(dstack_sock_path, 0o666)
        log.info("dstack proxy (filtered broker) listening on %s", dstack_sock_path)
        if os.environ.get("BROKER_VOLUME_NAME"):
            log.info("Broker shared to attested apps via BROKER_VOLUME_NAME=%s "
                     "(appears at /run/broker/dstack.sock)", os.environ["BROKER_VOLUME_NAME"])
    else:
        log.warning("dstack socket not found — dstack proxy disabled")

    # Recovery: restore shared runtimes for existing projects
    await rtm.recover_all()

    # Recovery: restore tunnels from disk
    tunnel_store.recover()
    log.info("Recovered %d active tunnels", len(tunnel_store.list()))

    # Recovery: restore scoped API tokens from disk
    token_store.recover()
    log.info("Recovered %d scoped API tokens", len(token_store.list()))

    # Recovery: restore broker grants from disk
    if dstack_sock:
        await broker_store.recover()
        log.info("Recovered %d active grants", len(broker_store.list()))

        # Pass broker_store to RuntimeManager for token auth
        rtm.set_broker_store(broker_store)

        # Serve creds.sock in BROKER_SOCKET_DIR (rides the broker volume)
        broker_proxy = BrokerProxy(broker_store, rtm)
        broker_app = web.Application()
        broker_app.router.add_route("*", "/{path:.*}", broker_proxy.handle)
        creds_sock_path = os.path.join(BROKER_SOCKET_DIR, "creds.sock")
        if os.path.exists(creds_sock_path):
            os.unlink(creds_sock_path)
        broker_runner = web.AppRunner(broker_app)
        await broker_runner.setup()
        await web.UnixSite(broker_runner, creds_sock_path).start()
        os.chmod(creds_sock_path, 0o666)
        log.info("Broker proxy listening on %s", creds_sock_path)
    else:
        log.warning("dstack socket not found — broker disabled")

    # RFC 0028: browser render pool (opt-in via BROWSER_POOL_IMAGE). A warm
    # pool of isolated browser-bridge containers, leased per request with
    # per-lease jar injection + reset. Like the broker, absent = disabled.
    # Constructed here so Ingress can reference it; warmed in the background
    # after the ingress binds so a slow image pull never blocks readiness.
    browser_pool = None
    pool_image = os.environ.get("BROWSER_POOL_IMAGE", "")
    if pool_image:
        pool_cmd = os.environ.get("BROWSER_POOL_CMD", "").split()
        pool_binds = parse_binds(os.environ.get("BROWSER_POOL_BINDS", ""))
        browser_pool = BrowserPool(
            docker, tracker, pool_image, pool_cmd, pool_binds,
            port=int(os.environ.get("BROWSER_POOL_PORT", "3000")),
            size=int(os.environ.get("BROWSER_POOL_SIZE", "2")),
            network=os.environ.get("BROWSER_POOL_NETWORK", "tee-apps-attested"),
            lease_ttl=float(os.environ.get("BROWSER_POOL_LEASE_TTL", "30")))
    else:
        log.info("BROWSER_POOL_IMAGE unset — browser pool disabled")

    # Ingress + API on TCP port(s)
    ing = Ingress(store, docker, audit_manager, tracker, rtm, tunnel_store, token_store, broker_store, browser_pool)

    # Check for port conflicts. The default ingress port (INGRESS_PORT) is
    # path-based-routing-only — multiple projects may "request" it, but they
    # all coexist on /<name>/ rather than via port-based routing.
    port_conflicts = []
    projects = store.list()
    port_to_project = {}
    for p in projects:
        if p.listen and p.listen.port and p.listen.port != INGRESS_PORT:
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
        if p.listen and p.listen.port and p.listen.protocol == "http" and p.listen.port != INGRESS_PORT:
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

    # Warm the browser pool now that the ingress is answering. Failures are
    # logged (not swallowed into readiness): the pool reports started=false
    # and /_api/browser/pool surfaces the state to the operator.
    if browser_pool:
        async def _warm_browser_pool():
            try:
                await browser_pool.start()
            except Exception as e:
                log.error("Browser pool failed to start: %s", e)
        asyncio.create_task(_warm_browser_pool())

    # Background task: cleanup expired short-lived grants
    async def cleanup_expired_grants():
        while True:
            await asyncio.sleep(60)  # Check every minute
            tunnel_store.cleanup_expired()
            token_store.cleanup_expired()
            if dstack_sock:
                broker_store.cleanup_expired()

    cleanup_task = asyncio.create_task(cleanup_expired_grants())

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

    if browser_pool:
        await browser_pool.stop()

    # Close TCP servers
    for server in tcp_servers:
        server.close()
        await server.wait_closed()


def main():
    asyncio.run(start())


if __name__ == "__main__":
    main()
