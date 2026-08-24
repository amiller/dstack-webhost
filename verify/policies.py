"""
Reference consumer policies for Facts (RFC 0020 §3, "Policy lives in the consumer").

These are ORDINARY consumer functions — they are NOT part of the verify()
library's return path and they are NOT methods on Facts. The library renders no
verdict; a consumer picks a policy (or writes its own) and calls it on the
Facts. Two are provided as examples matching the RFC:

  (a) allowlist_policy(facts, allowed_tree_hashes)
        accept iff facts.source.tree_hash is in the allowlist.

  (b) strict_policy(facts, expected_chain_id)
        accept iff the quote verified, the app is on-chain-approved on the
        expected chain, and the gateway channel is attested — i.e.
        quote_valid && onchain_approved && gateway_attested && chain_id == X.

The third, strongest consumer step the binding makes possible — pull the source
at tree_hash and reason about the actual code — is a consumer *action*, not a
boolean predicate on Facts, so it is out of scope here.

That policy (a) and policy (b) can disagree on the SAME Facts object is the
point of RFC 0020: there is no universal verdict. See
verify/test_facts.py::test_two_policies_disagree_on_same_facts for the
demonstration.
"""

from __future__ import annotations

from typing import Iterable

from .facts import Facts


def allowlist_policy(facts: Facts, allowed_tree_hashes: Iterable[str]) -> bool:
    """Policy (a): accept iff facts.source.tree_hash is in the allowlist.

    This is the policy that catches a tampered tree_hash: the consumer
    allowlists the tree_hash it trusts, and any other value — including a
    bundle tampered at app.source.tree_hash — is rejected.
    """
    return facts.source.tree_hash in set(allowed_tree_hashes)


def strict_policy(facts: Facts, expected_chain_id: str | int) -> bool:
    """Policy (b): accept iff quote_valid AND onchain_approved AND the gateway
    channel is attested AND onchain.chain_id == expected_chain_id.

    `gateway_attested` is taken as `channel.zt_cert_ref` being non-empty — i.e.
    a gateway cert was cited. Full zt-cert verification is the same class of
    work as DCAP/QVL (tracked separately); the policy does not assert more
    than the Facts support.
    """
    return (
        facts.quote.quote_valid
        and facts.onchain.approved
        and bool(facts.channel.zt_cert_ref)
        and facts.onchain.chain_id == str(expected_chain_id)
    )
