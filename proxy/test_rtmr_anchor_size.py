"""RTMR3 anchors stay small: the dstack 0.5.8 guest agent embeds the event log in a
cert it rebuilds on every start, and webpki refuses certs over 64 KB."""

import asyncio
import hashlib
import json
import os
import tempfile

from aiohttp import web

from .audit import AuditEntry, AuditLogManager


def test_boot_replay_is_one_event_and_records_are_small():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            events = []

            async def emit(request):
                events.append(await request.json())
                return web.json_response({})

            app = web.Application()
            app.router.add_post("/EmitEvent", emit)
            runner = web.AppRunner(app)
            await runner.setup()
            sock = os.path.join(tmp, "dstack.sock")
            await web.UnixSite(runner, sock).start()

            m = AuditLogManager(os.path.join(tmp, "audit"), sock)
            for p in ("alpha", "beta"):
                for i in range(300):
                    await m.get_audit_log(p).record(AuditEntry(
                        timestamp=float(i), action="deploy", detail="x" * 500))
            heads = {p: m._load_entries(p)[-1].entry_hash for p in ("alpha", "beta")}

            events.clear()
            await m.replay_anchors()
            assert [e["event"] for e in events] == ["tee-proxy:heads"]
            canonical = json.dumps(heads, sort_keys=True, separators=(",", ":")).encode()
            assert json.loads(bytes.fromhex(events[0]["payload"])) == {
                "heads_sha256": hashlib.sha256(canonical).hexdigest()}

            await m.get_audit_log("alpha").record(AuditEntry(
                timestamp=1.0, action="promote", detail="x" * 500))
            last = events[-1]
            assert last["event"] == "tee-proxy:promote"
            assert json.loads(bytes.fromhex(last["payload"])) == {
                "project": "alpha", "entry_hash": m._load_entries("alpha")[-1].entry_hash}
            await runner.cleanup()

    asyncio.run(run())
