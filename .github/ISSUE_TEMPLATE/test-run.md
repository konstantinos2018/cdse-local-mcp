---
name: Manual test run
about: Record one session of the manual test scenarios
title: "Test run: YYYY-MM-DD, <client>, <model>"
labels: test-run
---

Scenarios and expected results: [`tests/manual/scenarios.md`](../blob/main/tests/manual/scenarios.md).

## Environment

- **Tester:**
- **Date:**
- **Client:** Claude Desktop / Cowork / Claude Code
- **Model:**
- **Server commit:** <!-- git rev-parse --short HEAD -->
- **Started from an empty download folder:** yes / no

## Results

Tick when run. Put the result after the dash: **Pass**, **Fail**, **Partial** or **N/A**.
For a Fail, add where it went wrong (**server**, **model** or **description**) and link the
issue you opened for it.

### Baseline — does it work at all

- [ ] `BASE-01` — 
- [ ] `BASE-02` — 
- [ ] `BASE-03` — 

### The two interaction rules

- [ ] `RULE-01` — 
- [ ] `RULE-02` — 
- [ ] `RULE-03` — 

### Places and geometry

- [ ] `GEO-01` — 
- [ ] `GEO-02` — 
- [ ] `GEO-03` — 
- [ ] `GEO-04` — 

### Time

- [ ] `TIME-01` — 
- [ ] `TIME-02` — 
- [ ] `TIME-03` — 

### Beyond the four tuned collections

- [ ] `COLL-01` — 
- [ ] `COLL-02` — 
- [ ] `COLL-03` — 
- [ ] `COLL-04` — 
- [ ] `COLL-05` — 

### Getting just what you need

- [ ] `SEL-01` — 
- [ ] `SEL-02` — 
- [ ] `SEL-03` — 
- [ ] `SEL-04` — 
- [ ] `SEL-05` — 
- [ ] `SEL-06` — 

### Jobs, budget and quota

- [ ] `JOB-01` — 
- [ ] `JOB-02` — 
- [ ] `JOB-03` — 
- [ ] `JOB-04` — 

### Water quality

- [ ] `WQ-01` — 
- [ ] `WQ-02` — 
- [ ] `WQ-03` — 
- [ ] `WQ-04` — 
- [ ] `WQ-05` — 

### Out of scope — should decline, not bluff

- [ ] `SCOPE-01` — 
- [ ] `SCOPE-02` — 
- [ ] `SCOPE-03` — 
- [ ] `SCOPE-04` — 
- [ ] `SCOPE-05` — 

### Adversarial

- [ ] `ADV-01` — 
- [ ] `ADV-02` — 

## Notes

Anything surprising that isn't a clear failure.
