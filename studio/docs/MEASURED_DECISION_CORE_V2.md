# Measured Decision Core V2 in MarkeVita Studio

The first production integration is **Shadow Mode**. It reads the saved episode
brief, scenes, video takes, visual-QC evidence and cost ledger. It produces one
structured decision per scene and stores the report as
`core_v2.shadow_analysis_completed` in `generation_history`.

Shadow Mode has no execution authority:

- no provider or language-model calls;
- no paid retries;
- no take-selection changes;
- no approvals;
- no publication.

The report is content-addressed. Re-running it against unchanged evidence
returns the prior result instead of adding duplicate decisions.

## Rollout gates

1. **Shadow** — compare Core V2 decisions with producer decisions on completed
   and failed episodes.
2. **Supervised** — allow the Core to prepare exact repair requests, while a
   producer must approve every paid call or creative substitution.
3. **Autonomous** — allow bounded execution only when confidence, retry count,
   budget and continuity policies all pass. Publication remains separate.

Episode 4 of *The Wild Cat* is the first calibration case. A Shadow report can
be run from its Studio screen or from:

`POST /api/series/the_wild_cat/episodes/s01e04/core-v2/shadow`
