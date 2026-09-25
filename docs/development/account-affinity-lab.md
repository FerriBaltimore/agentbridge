# Routing affinity LAB

Objective: reduce unnecessary account changes in a five-turn, one-model fixture
from three to at most one while still moving after a fresh applicable quota
window reaches 100%. Preserve unknown quota, model eligibility, and per-turn
route immutability.

Hypothesis: selecting the current eligible account before comparing usage of
other accounts avoids cache-disrupting changes. A fresh exhausted window remains
the trigger to choose another account.

LAB: `tests/test_routing_affinity_selection.py::test_lab_affinity_removes_unnecessary_switches_and_keeps_exhaustion_switch`
calls the production `select_route` with the same account pairs in both arms:
`(20,40), (50,40), (30,40), (60,40), (100,40)` percent used. The baseline
omits affinity; the candidate feeds the prior selected account back in. Time is
fixed to 1000 seconds and each quota observation has the same age and model.
The experiment makes no provider request.

Result: baseline routes `a,b,a,b,b` (three changes); affinity routes
`a,a,a,a,b` (one change). The candidate records the applicable primary window
when A reaches 100%. Focused tests cover unknown quota, temporary busy/cooldown,
expired observations, reset, and complete active-window projection.

Decision: advance to focused integration. The result proves deterministic
selection behavior only. Live cache hits, provider quota accuracy, and savings
remain unverified.
