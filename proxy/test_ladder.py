"""Unit tests for the pure ladder_hint classifier (no daemon, no docker)."""

from .ladder import ladder_hint


def _p(**kw):
    base = {"name": "app", "mode": "dev", "public": False, "source": "https://github.com/o/r"}
    base.update(kw)
    return base


def test_no_source_is_rung0():
    h = ladder_hint(_p(source=""))
    assert h["rung"] == 0
    assert "adopt into a git repo" in h["next"]


def test_none_source_is_rung0():
    assert ladder_hint(_p(source="NONE"))["rung"] == 0


def test_tarball_source_is_rung0():
    assert ladder_hint(_p(source="tarball://local"))["rung"] == 0


def test_dev_private_is_rung1():
    h = ladder_hint(_p(public=False))
    assert h["rung"] == 1
    assert h["label"] == "dev (private)"
    assert "set public:true" in h["next"]


def test_dev_public_is_rung2():
    h = ladder_hint(_p(name="myapp", public=True))
    assert h["rung"] == 2
    assert h["label"] == "dev (public)"
    assert "POST /_api/projects/myapp/promote" in h["next"]


def test_attested_is_rung3():
    h = ladder_hint(_p(mode="attested", public=True))
    assert h["rung"] == 3
    assert h["label"] == "attested"
    assert "RFC 0021" in h["next"] and "RFC 0022" in h["next"]


def test_attested_without_source_is_still_rung0():
    """Source rule takes precedence — an attested-but-unrecloneable app is stuck at 0."""
    assert ladder_hint(_p(mode="attested", source="NONE"))["rung"] == 0
