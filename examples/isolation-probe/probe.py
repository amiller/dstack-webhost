"""Tiny tenant that exposes its own kernel-namespace evidence.

Lets a relying party corroborate the substrate's runtime claim
(/_api/substrate) with on-tenant data the substrate cannot forge.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


def read(p: str) -> str:
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return ""


def probe() -> dict:
    u = os.uname()
    return {
        "uid_inside": os.getuid(),
        "gid_inside": os.getgid(),
        "hostname": u.nodename,
        "kernel_release": u.release,
        "uid_map": read("/proc/self/uid_map").strip(),
        "gid_map": read("/proc/self/gid_map").strip(),
        "user_ns": os.readlink("/proc/self/ns/user"),
        "pid_ns": os.readlink("/proc/self/ns/pid"),
        "mount_ns": os.readlink("/proc/self/ns/mnt"),
        "cgroup": read("/proc/self/cgroup").strip(),
    }


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith("/api/probe"):
            body = json.dumps(probe(), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        with open("/app/index.html", "rb") as f:
            html = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, *a, **kw):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), H).serve_forever()
