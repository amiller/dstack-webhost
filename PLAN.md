# Issue #22 plan

- [x] Restructure `rfcs/0019-managed-hosting-teesql.md` into reviewable form: named
      Motivation, Design (with TeeSQL data model + AttestMesh topology subsections),
      Deployment story, and Open Questions sections, each substantive, preserving the
      existing pitch content (Context, what each side brings, options a/b, constraints).
- [x] No TODO/TBD placeholders anywhere in the RFC.
- [x] End the RFC with an MVP slice section naming the first implementable step
      (option (a) pilot: one managed base-prod dstack node, mesh unused, RFC 0017
      durability floor) and its own checkable acceptance criteria, so the next
      tracking issue can be filed against it.
- [x] Tier 0: documentation only, no behavior change — no code touched.

Operator verification remains: review the rendered RFC; the follow-up tracking issue
against the MVP slice is the operator's to file.
