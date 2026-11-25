# Pull Request

> Copy this into the PR description and fill it in. Do not tick verification you have not actually run; write unverified items under "Not verified".

## 1. Scope of changes

- What problem does this PR solve:
- Which modules/files changed:
- Explicitly out of scope (to avoid scope creep):

Related Issue: #

## 2. Risks and impact

- [ ] Affects failure/success judgement (exceptions, timeouts, pending, observation failure, lock conflict)
- [ ] Affects experiment criteria (task set/metrics/repeat count/judging criteria)
- [ ] Affects device lock or concurrency
- [ ] Affects evidence archiving (`runs/`, `results.jsonl`, `task_log.json`)
- [ ] Affects privacy/screenshot upload/Web authentication
- [ ] Docs/comments/tests only

Notes:

## 3. Verification

### Executed

```text
# Command + actual output (paste key lines)
pytest -q
ruff check .
python experiments/run_experiment.py --tasks experiments/tasks.json --dry-run
```

- CI:
- Real device: not run / run (device model, task, evidence path)
- Real API: not run / run (provider, model, task)
- Failure-path verification: list the failure scenarios actually constructed/observed

### Not verified

- [ ] Real-device tap/input/send
- [ ] Cross-resolution / cross-ROM
- [ ] Real model API stability
- [ ] Multi-app real planning output
- [ ] Baseline integration
- Other:

## 4. Impact on experiment criteria

- Whether `tasks.json` / `metrics.py` / `test.md` / judging criteria changed: yes / no
- If yes: related experiment Issue, two-person confirmation record, old criteria → new criteria, whether a full batch rerun is needed
- Whether metric values are affected:

## 5. Rollback

- Rollback method (revert / switch / config):
- What needs to be rerun after rollback:

## 6. Checklist

- [ ] One PR, one topic, no unrelated formatting dragged in
- [ ] No `.env`, keys, weights, `runs/`, `experiments/results/`, real screenshots committed
- [ ] New/modified behavior has tests, and failure paths are covered too
- [ ] No `importorskip` used to silently skip key tests
- [ ] README/docs agree with the code; inconsistencies have been fixed
- [ ] Experiment-criteria changes went through an Issue and are recorded
- [ ] Merge method is Squash; commit message follows Conventional Commits
