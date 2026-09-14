#!/usr/bin/env python3
"""Запуск серии из series package (см. docs/SERIES_PACKAGE.md). По умолчанию mock: платные вызовы заменены заглушками.

  python run_episode.py --series ../series/<id> --episode s01e01                       # mock, все стадии до deliver
  python run_episode.py --series ../series/<id> --episode s01e01 --stages intake,direction
  python run_episode.py --series ../series/<id> --approve references --by "Anatoliy"
  python run_episode.py --series ../series/<id> --episode s01e01 --approve publish --by "Anatoliy"
  python run_episode.py --series ../series/<id> --episode s01e01 --stages publish
  python run_episode.py --series ../series/<id> --episode s01e01 --live                 # ПЛАТНО: нужны ключи, PIPELINE_ALLOW_PAID=true, series.approval=approved
  python run_episode.py --series ../series/<id> --episode s01e01 --force sc07           # переделать одну сцену
  python run_episode.py --series ../series/<id> --episode s01e01 --override MAX_EPISODE_BUDGET_USD=65 --reason "..." --by "..."
  python run_episode.py --validate ../series/<id>                                       # только проверить пакет и все серии
"""
import argparse
import os
import sys
from pathlib import Path

from serial.config import Config
from serial.package import PackageError, SeriesPackage, validate_episode
from serial.pipeline import Pipeline, STAGES, SeriesState
from serial.state import State, now

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"


def validate_package(series_dir: Path, cfg) -> int:
    try:
        pkg = SeriesPackage(series_dir)
    except PackageError as e:
        print(f"PACKAGE INVALID: {e}"); return 1
    print(f"package OK: {pkg.series['series_id']} bible {pkg.bible_version}, characters {len(pkg.characters)}, locations {len(pkg.locations)}, "
          f"secrets {len(pkg.secrets)}, relationships {len(pkg.relationships)}")
    rc, prev_end = 0, None
    for season_id, ep_id in pkg.episode_order():
        if not (pkg.episode_dir(ep_id) / "brief.json").exists():
            print(f"  {ep_id}: brief.json отсутствует (ещё не написан)"); prev_end = None; continue
        try:
            ep = pkg.load_episode(ep_id)
            norm = validate_episode(pkg, ep, prev_end, cfg)
            pp = pkg.load_production_prompts(ep_id)
            print(f"  {ep_id}: OK {len(norm['scenes'])} клипов {norm['total_seconds']}s, ledger {len(norm['ledger'])}, prompts {'package' if pp else 'llm'}"
                  + (f", warnings: {norm['warnings']}" if norm["warnings"] else ""))
            prev_end = norm["end_state"]
        except PackageError as e:
            print(f"  {ep_id}: INVALID\n{e}"); rc = 1; prev_end = None
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", help="папка series package (содержит series.json)")
    ap.add_argument("--episode", help="episode_id, напр. s01e01")
    ap.add_argument("--validate", help="только валидация пакета по этому пути")
    ap.add_argument("--stages", default="auto", help="через запятую: " + ",".join(STAGES) + ". auto = всё кроме publish")
    ap.add_argument("--force", default="", help="стадии и/или id сцен/персонажей/локаций для переделки")
    ap.add_argument("--live", action="store_true", help="реальные провайдеры (платно). Без флага все платные вызовы mock")
    ap.add_argument("--accept-weak", action="store_true")
    ap.add_argument("--captions", choices=["srt", "burned", "both", "none"], default=None)
    ap.add_argument("--approve", choices=["references", "publish"])
    ap.add_argument("--by", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--override", default="", help="KEY=VALUE, напр. MAX_EPISODE_BUDGET_USD=65")
    ap.add_argument("--reason", default="")
    a = ap.parse_args()

    cfg = Config.load(HERE, live=a.live)
    if a.validate:
        sys.exit(validate_package(Path(a.validate), cfg))
    if not a.series:
        sys.exit("нужен --series <папка пакета> (или --validate)")
    try:
        pkg = SeriesPackage(Path(a.series))
    except PackageError as e:
        sys.exit(f"PACKAGE INVALID: {e}")
    series_dir = RUNS / pkg.series["series_id"]

    if a.approve == "references":
        if not a.by:
            sys.exit("--approve требует --by")
        ss = SeriesState(series_dir / "series_state.json")
        if ss.data.get("bible_version") != pkg.bible_version or not ss.data["references"]["characters"]:
            sys.exit(f"референсы для bible {pkg.bible_version} ещё не сгенерированы (--stages references)")
        ss.data["approvals"]["references"] = {"approved": True, "bible_version": pkg.bible_version, "by": a.by, "at": now(), "note": a.note}
        for group in ss.data["references"].values():
            for pack in group.values():
                for rec in (pack.values() if "path" not in pack else [pack]):
                    rec["approval"] = "approved"
        ss.save(); print(f"references approved for bible {pkg.bible_version} by {a.by}"); return

    if not a.episode:
        sys.exit("нужен --episode <id>")
    if a.episode not in [e for _, e in pkg.episode_order()]:
        sys.exit(f"{a.episode} не числится в series.json")
    ep_dir = series_dir / a.episode; ep_dir.mkdir(parents=True, exist_ok=True)

    if a.approve == "publish":
        if not a.by:
            sys.exit("--approve требует --by")
        st = State(ep_dir)
        if st.data.get("status") not in ("complete",) and not st.data.get("delivered"):
            sys.exit(f"серия не в статусе complete/delivered (сейчас {st.data.get('status')}); публиковать нечего")
        st.data["approvals"]["publish"] = {"approved": True, "by": a.by, "at": now(), "note": a.note}
        st.save(); print(f"publish approved by {a.by}"); return

    if a.override:
        key, _, val = a.override.partition("=")
        if not a.reason or not a.by:
            sys.exit("--override требует --reason и --by")
        st = State(ep_dir)
        st.data["overrides"].append({"key": key, "old": os.environ.get(key), "new": val, "reason": a.reason, "by": a.by, "at": now()})
        if st.data["status"] == "needs_budget_override":
            st.data["status"] = "validated"
        st.save(); os.environ[key] = val
        cfg = Config.load(HERE, live=a.live)
        print(f"override {key}={val} записан")

    st = State(ep_dir)
    for o in st.data.get("overrides", []):
        if o["key"] == "MAX_EPISODE_BUDGET_USD":
            cfg.max_episode_budget_usd = float(o["new"])
    stages = [s for s in STAGES if s != "publish"] if a.stages == "auto" else [s.strip() for s in a.stages.split(",")]
    bad = [s for s in stages if s not in STAGES]
    if bad:
        sys.exit(f"неизвестные стадии: {bad}")
    missing = cfg.missing_for(stages)
    if missing:
        sys.exit("в .env не хватает: " + ", ".join(sorted(set(missing))))
    force = {s.strip() for s in a.force.split(",") if s.strip()}
    Pipeline(cfg, pkg, a.episode, RUNS, force=force, accept_weak=a.accept_weak, captions=a.captions).run(stages)


if __name__ == "__main__":
    main()
