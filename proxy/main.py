"""Entrypoint: starts socket proxies + ingress/API server."""

import asyncio
import logging
import os
import signal

from aiohttp import web

from .tracker import ContainerTracker
from .audit import AuditLog
from .docker_proxy import DockerProxy
from .dstack_proxy import DstackProxy
from .docker_client import DockerClient
from .projects import ProjectStore
from .runtimes import RuntimeManager
from . import ingress as ingress_mod
from .ingress import Ingress

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
log = logging.getLogger("tee-daemon")

PROXY_DIR = os.environ.get("PROXY_SOCKET_DIR", "/var/run/proxy")
DOCKER_SOCK = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
DSTACK_SOCK = os.environ.get("DSTACK_SOCKET", "/var/run/dstack.sock")
DATA_DIR = os.environ.get("DAEMON_DATA_DIR", "/var/lib/tee-daemon/projects")
INGRESS_PORT = int(os.environ.get("INGRESS_PORT", "8080"))


async def start():
    os.makedirs(PROXY_DIR, exist_ok=True)

    dstack_sock = DSTACK_SOCK if os.path.exists(DSTACK_SOCK) else None
    ingress_mod.DSTACK_SOCK = dstack_sock

    tracker = ContainerTracker()
    audit = AuditLog(dstack_socket=dstack_sock)
    docker = DockerClient(DOCKER_SOCK)
    store = ProjectStore(DATA_DIR)
    rtm = RuntimeManager(docker, store, tracker)

    # Docker socket proxy (existing)
    docker_proxy = DockerProxy(DOCKER_SOCK, tracker, audit)
    await docker_proxy.ensure_network()
    await docker_proxy.recover_tracked()
    log.info("Recovered %d tracked containers", len(tracker.all_ids()))

    # Connect ourselves to tee-apps network so we can proxy to runtime containers
    hostname = os.environ.get("HOSTNAME", "")
    if hostname:
        try:
            await docker.connect_network(hostname, "tee-apps")
            log.info("Connected self (%s) to tee-apps network", hostname)
        except Exception as e:
            log.warning("Could not connect to tee-apps: %s", e)

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

    # Ingress + API on TCP port
    ing = Ingress(store, docker, audit, tracker, rtm)
    ingress_app = web.Application(client_max_size=100 * 1024 * 1024)
    ingress_app.router.add_route("*", "/{path:.*}", ing.handle)
    ingress_runner = web.AppRunner(ingress_app)
    await ingress_runner.setup()
    await web.TCPSite(ingress_runner, "0.0.0.0", INGRESS_PORT).start()
    log.info("Ingress + API listening on :%d", INGRESS_PORT)

    log.info("tee-daemon running")

    stop = asyncio.Event()
    for sig_name in ("SIGINT", "SIGTERM"):
        asyncio.get_event_loop().add_signal_handler(getattr(signal, sig_name), stop.set)
    await stop.wait()
    log.info("Shutting down")


def main():
    asyncio.run(start())


if __name__ == "__main__":
    main()
