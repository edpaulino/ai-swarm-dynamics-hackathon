# Spot-check stage1: PASS

Generated 2026-10-04T11:26:14.

- Judge: `anthropic/claude-opus-5-5` (reasoning_effort=medium); JUDGE_PROMPT sha256 `1c86ba03f886aadf17bb491284d3376f1385c1ca076ee947005a09407341fea2`.
- Batch scope: 220 rows; the judge labeled 220 and could not label 0 (must_review, labeled by the author).
- Sample: 30 rows (requested 30, seed 20261004), stratified by the judge's confirmed_report: 30 labeled 1 and 0 labeled 0 (pool: 220 labeled 1, 0 labeled 0).
- Agreement on confirmed_report: 30/30 (100.0%); Cohen's kappa undefined.
- Rule: PASS iff agreement in at least 95% of sampled rows (29 of 30). Result: **PASS**.
- mentions_concealment: not checked by the author; the judge's labels are used.
- Judge cost: $2.06 for this batch's rows; $2.06 for all rows in llm_labels.csv.
- Written to hit_review.csv: 190 judge labels (`judge:opus-5-5`), 30 author labels (`author:spotcheck`); 0 rows already had an author answer and were left unchanged.

## confirmed_report: author (rows) by judge (columns), sampled rows

| author \ judge | 1 | 0 | total |
|---|---|---|---|
| 1 | 30 | 0 | 30 |
| 0 | 0 | 0 | 0 |
| total | 30 | 0 | 30 |
