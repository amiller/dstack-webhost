"""RFC 0034: a project created by a `create`-scoped token is pending until the owner
approves it. Pending projects serve normally but cannot be promoted; past the deadline
they are frozen (container stopped, files and volumes kept, ingress answers 503)."""

import asyncio
import json
import logging
import os
import time

from .audit import AuditEntry

log = logging.getLogger(__name__)

PENDING_TTL = int(os.environ.get("DAEMON_PENDING_TTL", str(7 * 86400)))
PROJECT_TOKEN_TTL = 365 * 86400
DEFAULT_MAX_PENDING = 5
NOTIFY_HOOK = os.environ.get("DAEMON_NOTIFY_HOOK", "")


def pending_by(store, token_id):
    return [p for p in store.list() if p.approval and p.approval.get("created_by") == token_id]


def mark_pending(project, token_id):
    project.approval = {"status": "pending", "deadline": time.time() + PENDING_TTL,
                        "created_by": token_id}


async def record(audit_manager, project, action):
    await audit_manager.get_audit_log(project.name).record(AuditEntry(
        timestamp=time.time(), action=action, detail=json.dumps(project.approval or {})))


def notify(event, project):
    if NOTIFY_HOOK:
        asyncio.create_task(_notify({"event": event, "project": project.name,
                                     **(project.approval or {})}))


async def _notify(payload):
    body = json.dumps(payload).encode()
    try:
        if NOTIFY_HOOK.startswith("http"):
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                await s.post(NOTIFY_HOOK, data=body, headers={"content-type": "application/json"})
        else:
            proc = await asyncio.create_subprocess_exec(NOTIFY_HOOK, stdin=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(body), 10)
    except Exception as e:
        log.error("notify hook %s failed: %s", NOTIFY_HOOK, e)


async def freeze(project, store, rtm, audit_manager):
    if project.runtime == "image":
        await rtm.stop_image(project.name)
    elif project.isolation == "container":
        await rtm.stop_isolated(project.name)
    project.approval["status"] = "frozen"
    store.save(project)
    await record(audit_manager, project, "freeze")
    notify("freeze", project)


async def approve(project, store, rtm, audit_manager):
    was_frozen = project.approval["status"] == "frozen"
    project.approval = None
    store.save(project)
    if was_frozen:
        if project.runtime == "image":
            await rtm.start_image(project)
        elif project.isolation == "container":
            await rtm.start_isolated(project)
    await record(audit_manager, project, "approve")
    notify("approve", project)


async def sweep(store, rtm, audit_manager):
    for p in store.list():
        a = p.approval
        if a and a["status"] == "pending" and a["deadline"] < time.time():
            log.info("pending deadline passed for %s; freezing", p.name)
            await freeze(p, store, rtm, audit_manager)
