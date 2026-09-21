# Approval shadow fixtures

These three streams are an **illustrative** evaluation set for
`jev/scripts/approval_shadow.py`. They exist so the documented commands run and
the report format is reproducible.

They are **not** an independent safety benchmark and they certify no threshold.
They are six hand-authored rows covering a handful of action categories, and the
labels are the author's own. Replace them with a versioned, independently
adjudicated dataset before interpreting accuracy or choosing thresholds, exactly
as `docs/EVALUATION.md` in `jev-codex-approval` requires.

- `jev-audit.jsonl` - typed JEV judgments (`candidate`, `decision`) plus the
  action class and scenario family; no command text.
- `guardian.jsonl` - the host's final decision per review id with the observed
  end-to-end `elapsed_ms`.
- `labels.jsonl` - independent `allow`/`deny` adjudications by `request_id`.

The set deliberately contains a false allow (`rev-105`), so the gate is *not*
permitted and enforcement stays disabled, which is the honest outcome for a
handful of rows.
