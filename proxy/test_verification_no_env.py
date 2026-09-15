"""The public verification page must never carry project env.

GET /_api/verification/<p>?format=html is unauthenticated for attested projects (RFC 0015). It
rendered `asdict(project)` into window.verificationData, and asdict carries project.env -- so every
attested project published its secrets to anyone who asked: BRIDGE_SECRET, OPENVPN_PASS,
ZAI_API_KEY, OAuth client secrets. The template never read env; it leaked by serialisation.
"""
import ast
from pathlib import Path

INGRESS = Path(__file__).resolve().parent / "ingress.py"


def _verification_data_sources() -> list[str]:
    """Every expression assigned to `verification_data`, as source text."""
    tree = ast.parse(INGRESS.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "verification_data":
                    out.append(ast.get_source_segment(INGRESS.read_text(), node) or "")
    return out


def test_verification_data_is_assigned_somewhere():
    assert _verification_data_sources(), "verification_data vanished — retarget this test"


def test_public_page_payload_never_embeds_raw_project():
    """A bare asdict(project) reintroduces the leak, whatever it is named."""
    for src in _verification_data_sources():
        assert "asdict(project)" not in src, (
            "the public verification payload embeds the whole project record, which includes "
            "project.env (secrets). Allowlist the fields the template needs instead:\n" + src
        )


def test_env_is_filtered_out_of_the_payload():
    body = INGRESS.read_text()
    assert 'if k not in ("env",)' in body or '"env"' in body, (
        "expected an explicit exclusion of env from the public verification payload"
    )


def test_template_does_not_need_env():
    """Pins the premise of the fix: if the template ever starts reading env, this must fail."""
    tpl = INGRESS.parent / "templates" / "verification.html"
    if not tpl.exists():
        return
    text = tpl.read_text()
    assert ".env" not in text and "env[" not in text, (
        "the verification template now reads env — the allowlist fix would break it, and "
        "publishing env is not an acceptable way to satisfy it"
    )
