# _template

Placeholder series package (no story content). Copy to `series/<series_id>/`, replace every placeholder, then validate:

    cd pipeline && python run_episode.py --validate ../series/<series_id>

Contract: `pipeline/docs/SERIES_PACKAGE.md`. This template intentionally has fewer than 12 scenes and lowered limits so it validates as-is; production packages must use the default limits.
