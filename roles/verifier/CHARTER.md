# Charter: verifier

**Status:** SCHEDULED (deterministic code, not an agent)
**Authority tier:** none (read-only auditor)

## Mandate
Prove the EFFECT each scheduled job is supposed to cause: rows written, files
refreshed, pages serving, invariants holding. One declarative check per job in
[`verifier/checks.py`](../../verifier/checks.py).

## Design rules
- No LLM. Every assertion is deterministic with an explicit threshold.
- Shares no code with the agents it audits.
- Writes one `verifier_results` row per check, so its own liveness is asserted
  on output.
- Exits non-zero on any hard failure so an external monitor can page.
- Grace periods are dated (`soft_until`), never permanent.

## You must never
- call a model, write to any table other than `verifier_results`, or "fix" what
  it finds. Remediation belongs to the owning role or a human.

## KPIs
- % of checks passing (reported, not optimised: deleting a check to raise the
  number is the failure mode this charter exists to name).

## Kill criterion
None. If the Verifier stops writing results, that is itself a paging alert.
