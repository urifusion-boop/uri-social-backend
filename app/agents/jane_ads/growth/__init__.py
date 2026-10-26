"""
Jane + Ads — Growth Intelligence (Growth Intelligence & Ad Optimization PRD).

Collect → validate → compare → detect → diagnose → recommend → authorize → act →
measure → learn, for Meta campaigns Jane launched.

The rule the whole package keeps: a metric moving is not a reason to change an ad.
Data is checked for freshness and sample size first, every cause is a labelled
hypothesis with a confidence, the user's mode and limits decide whether anything
runs, and a before/after comparison is recorded as an association, never proof.

Modules:
  metrics   Meta insights → daily snapshots, with source and retrieval time
  detect    pure rules over snapshots: what changed, with the evidence
  diagnose  pure: finding → hypotheses, alternatives, one smallest useful action
  policy    pure: modes, caps, cooldowns, emergency stop → may this run?
  actions   the executor: idempotent, pre-checked, verified, audited
  evaluate  post-change comparison against the window before it
  leads     inbox conversations started from an ad → lead stages, cost per qualified lead
  creative  variant comparison that won't crown a winner from a tiny sample
  forecast  a range, not a number, for the rest of the flight
  engine    one pass per campaign, run by the scheduler
"""
