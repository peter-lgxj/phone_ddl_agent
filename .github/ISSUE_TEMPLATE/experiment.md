# Experiment / Criteria Change Issue

> Title format: `[experiment] short description`
> Used for: any change that affects experiment numbers, such as the task set, repeat count, metric formulas, judging criteria, baseline integration, reset/max-steps.
> **Without two-person confirmation, do not change code and do not start a run.**

## 1. Purpose of the change

- What problem is being solved:
- What happens if it is not changed:
- Related paper/report section:

## 2. Change content

- [ ] `experiments/tasks.json` (task text/App/theoretical steps/Level/repeat count)
- [ ] `experiments/metrics.py` (numerator/denominator/filter conditions)
- [ ] `experiments/run_experiment.py` (reset/max steps/retry/lock/success writing)
- [ ] `test.md` / `deliverable/docs/02-experiment-protocol.md` (formulas/judging/repeats)
- [ ] Baseline integration method/model/version
- [ ] Judging-table fields/success criteria

Specific change (old → new):

```text
Old:
New:
```

## 3. Impact analysis

| Metric | Affected | Notes |
|---|---|---|
| TSR | | |
| Multi-App TSR | | |
| SE | | |
| AT | | |
| ST | | |
| CS | | |
| Whether already-run data is comparable | | |
| Whether a full batch rerun is needed | | |

## 4. Items awaiting decision

- Who needs to confirm (team lead/experiment owner/advisor):
- Deadline (if any):
- Confirmation record (comment link/date):

## 5. Acceptance criteria

- [ ] Code, `docs/02` and `test.md` are consistent in all three places
- [ ] Corresponding tests exist (at least covering the boundaries of the new criteria)
- [ ] "Old criteria → new criteria" is written in the Issue
- [ ] It is explicit whether old data is kept, annotated, or discarded
- [ ] If values change, the summary-table template description has been updated

## 6. Notes

- Any temporary compromises, uncovered scenarios, follow-up todos.
