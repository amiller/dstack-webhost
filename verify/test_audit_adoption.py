"""Audit ledger adoption: hashless entries written by a pre-ledger daemon are adopted
wherever they appear, and every tamper shape is still rejected."""
import json
import tempfile
from dataclasses import asdict

import pytest

from proxy.audit import AuditLogManager, AuditEntry


def _ledger_with_hashless_tail():
    m = AuditLogManager(tempfile.mkdtemp(), None)
    m._save_entry("p", AuditEntry(timestamp=1, action="deploy"))
    m._save_entry("p", AuditEntry(timestamp=2, action="promote"))
    with open(m._audit_file("p"), "a") as f:  # rolled-back daemon: no hashes, .head untouched
        f.write(json.dumps(asdict(AuditEntry(timestamp=3, action="deploy"))) + "\n")
        f.write(json.dumps(asdict(AuditEntry(timestamp=4, action="teardown"))) + "\n")
    return m


def test_hashless_tail_is_adopted_and_chained():
    m = _ledger_with_hashless_tail()
    e = m._load_entries("p")
    assert len(e) == 4 and e[3].prev_hash == e[2].entry_hash
    m._save_entry("p", AuditEntry(timestamp=5, action="deploy"))
    assert len(m._load_entries("p")) == 5


def _mutate(m, fn):
    path = m._audit_file("p")
    lines = open(path).read().splitlines()
    fn(lines)
    open(path, "w").write("\n".join(lines) + "\n")


def _edit(i, blank=False):
    def fn(lines):
        o = json.loads(lines[i])
        o["action"] = "x"
        if blank:
            o["entry_hash"] = o["prev_hash"] = ""
        lines[i] = json.dumps(o)
    return fn


@pytest.mark.parametrize("label, fn, err", [
    ("edit hashed", _edit(1), "tampered"),
    ("blank+edit hashed", _edit(1, blank=True), "chain broken"),
    ("truncate", lambda lines: lines.pop(), "truncated"),
    ("edit adopted before a hashed entry", _edit(2), "chain broken"),
    ("blank+edit last hashed", _edit(-1, blank=True), "truncated"),
])
def test_tamper_still_rejected(label, fn, err):
    m = _ledger_with_hashless_tail()
    m._save_entry("p", AuditEntry(timestamp=5, action="deploy"))
    _mutate(m, fn)
    with pytest.raises(ValueError, match=err):
        m._load_entries("p")
