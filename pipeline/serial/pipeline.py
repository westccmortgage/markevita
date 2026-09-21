"""Оркестратор. Контент приходит из series package (package.py); пайплайн не знает сюжета.
Runtime: runs/<series_id>/series_state.json (референсы, approvals, end-state серий) и runs/<series_id>/<episode_id>/ (state, work, out)."""

import json
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import media, package as pkgmod, prompts, providers, reference_reuse
from .costs import Budget, BudgetExceeded
from .llm import LLM
from .state import State, now, sha256
from .storage import R2, Keys

STAGES = ["intake", "direction", "references", "keyframes", "video", "voice", "lipsync", "assemble", "qa", "deliver", "publish"]
PAID_STAGES = {"direction", "references", "keyframes", "video", "voice", "lipsync"}


def capped_video_route(cfg, episode: dict) -> tuple[str, ...]:
    """Return the approved route without silently buying every fallback."""
    route = tuple(getattr(cfg, "video_model_route", ()) or (cfg.fal_video_model,))
    cap = int(episode.get("max_video_route_attempts") or len(route))
    return route[:max(1, cap)]
# Defined in package.py so validation and production cannot drift apart.
LEAD_IN = pkgmod.LEAD_IN
GAP = pkgmod.GAP
MAX_TEMPO = pkgmod.MAX_TEMPO


def _overrun(scene_id: str, spoken: float, limit: float) -> str:
    """How far a scene's speech exceeds its clip, in numbers that survive.

    One decimal place rounded both sides to the same value: a 0.09s overrun
    printed as "3.8s > 3.8s", which reads as a broken comparison and tells an
    operator nothing about how much to cut. Two decimals plus the difference
    make the edit obvious and stay readable in either interface language.
    """
    return f"{scene_id} {spoken:.2f}s > {limit:.2f}s (+{spoken - limit:.2f}s)"


class SceneFailed(RuntimeError):
    pass


class SeriesState:
    def __init__(self, path: Path):
        self.on_save = None
        self.path = path
        self.data = {"bible_version": None, "references": {"characters": {}, "locations": {}, "props": {}}, "approvals": {}, "episodes": {}}
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        if self.on_save:
            self.on_save()


class Pipeline:
    def __init__(self, cfg, pkg: pkgmod.SeriesPackage, episode_id: str, runs_root: Path, force: set[str] | None = None,
                 accept_weak: bool = False, captions: str | None = None):
        self.cfg, self.pkg, self.episode_id = cfg, pkg, episode_id
        self.series_dir = runs_root / pkg.series["series_id"]
        self.ep = self.series_dir / episode_id
        self.work, self.out = self.ep / "work", self.ep / "out"
        self.work.mkdir(parents=True, exist_ok=True); self.out.mkdir(parents=True, exist_ok=True)
        self.force = force or set()
        self.accept_weak = accept_weak
        self.captions_override = captions
        self.logf = open(self.ep / "log.txt", "a", encoding="utf-8")
        self.state = State(self.ep)
        self.sstate = SeriesState(self.series_dir / "series_state.json")
        self.budget = Budget(pkg.limits(cfg)["budget"], self.state)
        self.regen = pkg.limits(cfg)["regen"]
        self.llm = LLM(cfg, self.log)
        self.r2 = R2(cfg, self.log)
        self.keys = Keys(pkg.series["series_id"], episode_id)
        inputs = providers.InputPublisher(cfg, self.log, self.r2, f"{self.keys.ep}/_transport")
        self.fal = providers.Fal(cfg, self.log, self.state, self.budget, inputs)
        self.bible = self._bible_for_llm()

    # ---------- utils ----------

    def log(self, msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True); self.logf.write(line + "\n"); self.logf.flush()

    def _bible_for_llm(self) -> dict:
        p = self.pkg
        return {"characters": list(p.characters.values()), "locations": list(p.locations.values()), "props": list(p.props.values()),
                "relationships": list(p.relationships.values()), "secrets": list(p.secrets.values()),
                "style_sentence": p.style["style_sentence"], "camera_rules": p.style.get("camera_rules", ""),
                "color_rules": p.style.get("color_rules", ""), "negative_image": p.style.get("negative_image", ""),
                "negative_video": p.style.get("negative_video", "")}

    def _done(self, stage: str) -> bool:
        if stage in self.force or not self.state.stage_done(stage):
            return False
        items = self.force - set(STAGES)
        if not items:
            return True
        if stage in ("intake", "direction"):
            return True
        if stage == "references":
            R = self.sstate.data["references"]
            return not any(i in R["characters"] or i in R["locations"] or i in R["props"] for i in items)
        return False

    def _redo(self, stage: str, item_id: str) -> bool:
        return stage in self.force or item_id in self.force

    @property
    def episode(self) -> dict:
        return self.state.data["episode"]

    @property
    def scenes(self) -> list[dict]:
        return self.episode["scenes"]

    def _take_id(self, scene_id: str, stage: str, attempt: int) -> str:
        return f"{self.episode_id}_{scene_id}_{stage}_{attempt:02d}"

    def _attempt_base(self, prefix: str, forced: bool) -> int:
        """Форсированная переделка = новые takes после уже существующих (takes неизменяемы), с пометкой forced_by_operator."""
        if not forced:
            return 0
        n = sum(1 for t in self.state.data["takes"] if t.startswith(prefix))
        if n:
            self.state.data["overrides"].append({"key": "forced_regeneration", "take_prefix": prefix, "previous_takes": n, "at": now()})
        return n

    def _char(self, cid: str) -> dict:
        return self.pkg.characters[cid]

    def _loc(self, lid: str) -> dict:
        return self.pkg.locations[lid]

    def _qc_verdict(self, qc) -> tuple[bool, bool, float]:
        """Kept, close enough to keep, and the score — against the settings.

        The check's own pass flag was taken at its word, and the threshold was
        a number written into its instructions. Both are settings now, because
        every miss is a fully paid regeneration: a shot scoring six against a
        seven costs a whole second clip to try for the point.
        """
        try:
            score = float(qc.get("score"))
        except (TypeError, ValueError):
            return bool(qc.get("pass")), False, 0.0
        want = float(getattr(self.cfg, "qc_pass_score", 7.0))
        near = float(getattr(self.cfg, "qc_close_enough", 1.0))
        return score >= want, score >= want - near, score

    def _decided_refusal(self, exc) -> bool:
        """Did the provider decide about this one shot, or fail us generally?

        A model that will not make this particular picture has decided, and
        asking again changes nothing — so the scene is set aside and the rest
        of the episode goes on. A connection or an account failing has decided
        nothing, and burning every remaining scene's attempts against it helps
        nobody, so that still stops the run where it stands.
        """
        return bool(getattr(exc, "decided", False))

    def _weak_is_allowed(self, scene_id: str) -> bool:
        """May the best attempt stand for this scene, though QC marked it down?

        A judgement, and the producer's to make: the material exists and was
        paid for, and nobody but them can say whether it is good enough for
        this shot. The engine offered this as a command-line flag, so from the
        studio the only way past a scene QC kept failing was to pay for it
        again and hope — which is not a way past, it is a loop.
        """
        return bool(self.accept_weak) or scene_id in set(getattr(self.cfg, "accepted_weak", ()) or ())

    def _guard_live(self, stage: str):
        if self.cfg.dry_run or stage not in PAID_STAGES:
            return
        if not self.cfg.allow_paid_env:
            raise RuntimeError("live mode: PIPELINE_ALLOW_PAID=true не установлен в .env")
        if self.pkg.series.get("approval", {}).get("status") != "approved":
            raise RuntimeError("live mode: series.json approval.status != approved (creative package not approved)")

    def _require_references_approval(self):
        ap = self.sstate.data["approvals"].get("references")
        bv = self.pkg.reference_version
        if not ap or ap.get("bible_version") != bv:
            self.state.set_status("blocked_open_question")
            raise RuntimeError(f"нужен approval референсов для bible {bv}: run_episode.py --series ... --approve references --by \"...\"")

    # ---------- stage: intake ----------

    def stage_intake(self):
        if self._done("intake"):
            self.log("intake: уже сделано"); return
        ep = self.pkg.load_episode(self.episode_id)
        prev = self.pkg.previous_episode(self.episode_id)
        prev_end = self.sstate.data["episodes"].get(prev, {}).get("end_state") if prev else None
        norm = pkgmod.validate_episode(self.pkg, ep, prev_end, self.cfg)
        for w in norm["warnings"]:
            self.log(f"intake: warning: {w}")
        pp = self.pkg.load_production_prompts(self.episode_id)
        missing = pkgmod.attach_prompts(norm, pp)
        norm["prompts_missing"] = missing
        refs_needed = self.sstate.data.get("bible_version") != self.pkg.reference_version or not self.sstate.data["references"]["characters"]
        est = pkgmod.estimate_first_pass(norm, self.pkg, self.cfg, refs_needed)
        if est["total_first_pass"] > est["budget_cap"] - self.budget.spent:
            self.state.set_status("needs_budget_override")
            raise BudgetExceeded(f"projected first pass ${est['total_first_pass']:.2f} > remaining ${est['budget_cap'] - self.budget.spent:.2f}")
        self.state.data["episode"] = norm
        self.state.data["cost_projection"] = est
        self.state.data["package_checksums"] = self.pkg.checksums
        (self.work / "normalized.json").write_text(json.dumps(norm, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.work / "ledger.json").write_text(json.dumps({"ledger": norm["ledger"], "end_state": norm["end_state"]}, ensure_ascii=False, indent=2), encoding="utf-8")
        self.sstate.data["episodes"][self.episode_id] = {"end_state": norm["end_state"], "status": "planned", "brief_sha256": norm["brief_sha256"], "at": now()}
        self.sstate.save()
        splits = [s["scene_id"] for s in norm["scenes"] if s["split"]]
        self.log(f"intake: OK. {len(norm['scenes'])} клипов ({norm['total_seconds']}s), под-кадры: {splits or 'нет'}; "
                 f"knowledge/relationship events: {len(norm['ledger'])}; промпты из пакета: {len(norm['scenes']) - len(missing)}/{len(norm['scenes'])}")
        self.log(f"intake: проекция 1 прогона ${est['total_first_pass']:.2f} / cap ${est['budget_cap']:.2f} "
                 f"(video {est['video']}, refs {est['references']}, kf {est['keyframes']}, lipsync {est['lipsync']}, llm {est['llm']})")
        self.state.set_status("validated")
        self.state.mark_stage("intake")

    # ---------- stage: direction ----------

    def stage_direction(self):
        self._guard_live("direction")
        if self._done("direction"):
            self.log("direction: уже сделано"); return
        need = [s for s in self.scenes if not s.get("keyframe_prompt")]
        if need:
            self.log(f"direction: промпты для {len(need)} сцен через LLM (в пакете нет production_prompts)")
            d = self.llm.direct(self.bible, need)
            by_id = {x["scene_id"]: x for x in d["scenes"]}
            miss = [s["scene_id"] for s in need if s["scene_id"] not in by_id]
            if miss:
                raise RuntimeError(f"direction: не вернул сцены {miss}")
            for s in need:
                x = by_id[s["scene_id"]]
                for k in ("lens", "time_of_day", "continuity", "keyframe_prompt", "video_prompt", "negative", "keyframe_expected", "video_expected"):
                    if x.get(k):
                        s[k] = x[k]
                s["prompts_source"] = "llm"
        else:
            self.log("direction: все промпты пришли из пакета, LLM не нужен")
        for s in self.scenes:
            s.setdefault("keyframe_expected", s["action"]); s.setdefault("video_expected", s["action"])
        self.state.save()
        (self.ep / "direction.json").write_text(json.dumps(self.scenes, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_screenplay_md()
        self.state.mark_stage("direction")
        self.log(f"direction: готово -> {self.ep / 'screenplay.md'}")

    def _write_screenplay_md(self):
        e = self.episode
        L = [f"# {e['series_title']} — {e['season_id']}/{e['episode_id']} «{e['title']}»", "", e.get("logline", ""), "",
             f"{len(self.scenes)} клипов, {e['total_seconds']} с, {e['aspect_ratio']}, язык {e['language']}, bible {e['bible_version']}", "",
             f"Cliffhanger: {e['cliffhanger']['scene_id']} — {e['cliffhanger']['hook']}", ""]
        for s in self.scenes:
            dl = "; ".join(f"**{d['speaker']}**: {d['text']}" + (" (VO)" if d.get("voice_over") else "") for d in s["dialogue"]) or "—"
            L += [f"## {s['scene_id']} · {s['duration']}s · {s['location']}{' / ' + s['lighting_state'] if s.get('lighting_state') else ''} · {s.get('lens') or ''}",
                  f"В кадре: {', '.join(f'{c} [{v}]' for c, v in s['wardrobe'].items()) or '—'}. Lipsync: {s.get('lipsync_speaker') or 'нет'}",
                  f"Действие: {s['action']}", f"Реплики: {dl}", f"Кадр: {s.get('keyframe_prompt','')}", f"Движение: {s.get('video_prompt','')}", ""]
        if e["ledger"]:
            L += ["## Ledger (знания и отношения)", ""] + [f"- {x['scene_id']}: " + (f"{x['character']} узнаёт {x['secret']} ({x.get('how','')})" if x['type'] == 'knowledge' else f"{x['id']}: {x['from']} → {x['to']}") for x in e["ledger"]]
        (self.ep / "screenplay.md").write_text("\n".join(L), encoding="utf-8")

    # ---------- stage: references (series-level, per bible version) ----------

    def _gen_ref(self, take_id: str, prompt: str, dest: Path, refs: list[Path], aspect: str, pro: bool, what: str,
                 qc_refs: list[tuple[str, Path]], expected: str, forced: bool = False) -> Path:
        hint, last = "", None
        base = self._attempt_base(f"{take_id}_", forced)
        for attempt in range(base, base + self.regen + 1):
            path, take = providers.gen_image(self.fal, f"{take_id}_{attempt:02d}", prompt + (f" Correction: {hint}" if hint else ""),
                                             dest.with_name(f"{dest.stem}_v{attempt}{dest.suffix}"), refs, aspect, pro, what)
            take["forced_by_operator"] = bool(base)
            qc = self.llm.qc_image(qc_refs, path, expected) if qc_refs else {"pass": True, "score": 10, "issues": []}
            take["qa"] = qc; self.state.save()
            last = path
            kept, near, score = self._qc_verdict(qc)
            if kept:
                return path
            if near:
                # A reference image one point short is not worth another
                # reference image. Twenty-seven of these were paid for twice.
                self.log(f"    QC {score}: принято как достаточно близкое")
                return path
            hint = qc.get("fix_hint") or "; ".join(qc.get("issues", []))
            self.log(f"    QC {qc.get('score')}: {qc.get('issues')}")
        if self.accept_weak:
            self.log(f"    беру слабый вариант (--accept-weak): {last.name}"); return last
        raise SceneFailed(f"{what}: не прошёл QC за {self.regen + 1} попытки")

    def _ref_record(self, path: Path, key: str) -> dict:
        rec = {"path": str(path), "checksum": sha256(path), "r2_key": key, "bible_version": self.pkg.reference_version, "approval": "pending", "created_at": now()}
        if self.r2.enabled:
            self.r2.put(path, key)
        return rec

    def stage_references(self):
        self._guard_live("references")
        bv = self.pkg.reference_version
        R = self.sstate.data["references"]
        if (self.sstate.data.get("bible_version") != bv
                or self.sstate.data.get("reference_pack_complete") != bv):
            reused = reference_reuse.find_reusable(self.pkg, self.series_dir, self.sstate.data)
            if reused is not None and not self.force:
                refs, source_version, approval = reused
                for kind, group in refs.items():
                    for pack in group.values():
                        for rec in ([pack] if kind == "props" else pack.values()):
                            rec.update(bible_version=bv, source_bible_version=rec.get("source_bible_version", source_version),
                                       approval="approved" if approval else "pending")
                self.sstate.data.update(references=refs, bible_version=bv, reference_pack_complete=bv,
                                        reference_inputs_fingerprint=reference_reuse.fingerprint(self.pkg),
                                        reference_inputs_parts=reference_reuse.fingerprint_parts(self.pkg))
                if approval:
                    approval.update(bible_version=bv, source_bible_version=source_version)
                    self.sstate.data["approvals"]["references"] = approval
                else:
                    self.sstate.data["approvals"].pop("references", None)
                self.sstate.save()
                self.state.mark_stage("references")
                self.state.set_status("references_review")
                self.log(f"references: reused verified images from {source_version}; no image generation")
                if not approval:
                    self.log("references: recovered approved images from a delivered episode; review and approve this pack to continue")
                return
        if self.sstate.data.get("bible_version") != bv:
            if self.sstate.data.get("bible_version"):
                moved = reference_reuse.changed_parts(self.pkg, self.sstate.data.get("reference_inputs_parts"))
                # Without this the log says a version changed and nothing more,
                # which is not something a producer can act on: they regenerate
                # the pack and watch the target move again for a reason nobody
                # ever names.
                why = ("; изменилось: " + ", ".join(moved)) if moved else (
                    "; что именно изменилось — не записано: прежний пакет собран до того, "
                    "как это стало записываться")
                self.log(f"references: bible изменился ({self.sstate.data['bible_version']} -> {bv}); "
                         f"пакет референсов генерируется заново, approval сброшен{why}")
            kept = reference_reuse.keep_unchanged(
                self.pkg, R, self.sstate.data.get("reference_inputs_parts"), bv)
            if kept:
                self.log(f"references: {kept} изобр. не затронуты правкой и переиспользуются")
            self.sstate.data["approvals"].pop("references", None)
            self.sstate.data.pop("reference_pack_complete", None)
            self.sstate.data["bible_version"] = bv
            self.sstate.data["reference_inputs_fingerprint"] = reference_reuse.fingerprint(self.pkg)
            self.sstate.save()
        elif (self.sstate.data.get("reference_pack_complete") == bv
              and self._done("references") and R["characters"]):
            wanted = reference_reuse.expected(self.pkg)
            missing = [(kind, owner, name) for kind, owners in wanted.items()
                       for owner, names in owners.items() for name in names
                       if owner not in R[kind] or (kind != "props" and name not in R[kind][owner])]
            if not missing:
                self.log("references: уже сделано для этой версии bible"); return
            self.log(f"references: в зафиксированном пакете нет {len(missing)} обязательных кадров; достраиваю")
            self.sstate.data["approvals"].pop("references", None)
            self.sstate.data.pop("reference_pack_complete", None)
            self.sstate.save()
        elif self._done("references") and R["characters"]:
            # The stage was marked done under an earlier bible and that mark is
            # never cleared. When the bible moved, the pack was emptied and
            # regeneration began; if that run died part-way, the next one read
            # the old mark, saw the few images it had managed, and declared the
            # pack finished. The stage then says "already done" while approval
            # says "generate the pack for the current settings first", and
            # both are telling the truth. Carry on and finish it instead.
            # Counted image by image: a character holds a pack of them, so
            # counting the owners said "1 изобр." for eight pictures.
            made = sum(1 for group in R.values() for pack in group.values()
                       for _ in ([pack] if "path" in pack else pack.values()))
            self.log(f"references: пакет для {bv} собран не полностью ({made} изобр.); достраиваю")
        self.state.set_status("references_pending")
        style = self.pkg.style["style_sentence"]
        rdir = self.series_dir / "references" / bv
        backdrop = prompts.CHARACTER_PACK_BACKDROP

        for c in self.pkg.characters.values():
            if not c["visual"]:
                continue
            cid = c["id"]; pack = R["characters"].setdefault(cid, {}); fz = self._redo("references", cid)
            seeds = [self.pkg.root / a for a in c.get("seed_assets", [])]
            wdef = c["wardrobe"]["variants"][c["wardrobe"]["default"]]["description"]
            character_pack = prompts.character_pack(c)
            key, tmpl = character_pack[0]
            is_feline = character_pack is prompts.WILD_FELINE_CHARACTER_PACK
            same_identity = ("Same animal as the reference: identical coat markings, face, eyes, ear notch, build and tail"
                             if is_feline else "Same person as the reference: identical face and hair")
            if key not in pack or self._redo("references", cid):
                self.log(f"references: {cid} identity ({'from seed' if seeds else 'text-to-image pro'})")
                desc = f"{c['name']}, {c.get('age','')}. {c['appearance']} Wearing: {wdef}. {backdrop}. {style}"
                if seeds:
                    seed_identity = ("Same animal as the reference image(s): identical coat markings, face, eyes, ear notch, build and tail"
                                     if is_feline else "Same person as the reference image(s): identical face, hair, build")
                    p = self._gen_ref(f"ref_{bv}_{cid}_{key}", f"{seed_identity}. {tmpl}. {desc}",
                                      rdir / "characters" / cid / f"{key}.png", seeds, "3:4", False, f"ref {cid}/{key}", [(f"{c['name']} seed", seeds[0])], f"{tmpl}; same person as seed", forced=fz)
                else:
                    p = self._gen_ref(f"ref_{bv}_{cid}_{key}", f"{tmpl}. {desc}", rdir / "characters" / cid / f"{key}.png", [], "3:4", True, f"ref {cid}/{key}", [], "", forced=fz)
                pack[key] = self._ref_record(p, self.keys.bible_char(cid, bv, f"{key}.png")); self.sstate.save()
            identity = Path(pack[key]["path"])
            for key, tmpl in character_pack[1:]:
                if key.startswith("fullbody"):
                    continue
                if key in pack and not self._redo("references", cid):
                    continue
                self.log(f"references: {cid} {key}")
                p = self._gen_ref(f"ref_{bv}_{cid}_{key}", f"{same_identity}. {tmpl}. Wearing: {wdef}. {backdrop}. {style}",
                                  rdir / "characters" / cid / f"{key}.png", [identity], "3:4", False, f"ref {cid}/{key}", [(f"{c['name']} identity", identity)], f"{tmpl}; same person", forced=fz)
                pack[key] = self._ref_record(p, self.keys.bible_char(cid, bv, f"{key}.png")); self.sstate.save()
            for vid_, variant in c["wardrobe"]["variants"].items():
                for key, tmpl in [k for k in character_pack if k[0].startswith("fullbody")]:
                    vkey = f"{key}__{vid_}"
                    if vkey in pack and not self._redo("references", cid):
                        continue
                    self.log(f"references: {cid} {vkey}")
                    p = self._gen_ref(f"ref_{bv}_{cid}_{vkey}", f"{same_identity}. {tmpl}. Wearing: {variant['description']}. {backdrop}. {style}",
                                      rdir / "characters" / cid / f"{vkey}.png", [identity] + seeds[:1], "9:16", False, f"ref {cid}/{vkey}", [(f"{c['name']} identity", identity)], f"{tmpl}; same person; wardrobe: {variant['description'][:80]}", forced=fz)
                    pack[vkey] = self._ref_record(p, self.keys.bible_char(cid, bv, f"{vkey}.png")); self.sstate.save()

        for l in self.pkg.locations.values():
            lid = l["id"]; pack = R["locations"].setdefault(lid, {}); fz = self._redo("references", lid)
            seeds = [self.pkg.root / a for a in l.get("seed_assets", [])]
            if seeds and seeds[0].suffix.lower() == ".webp":
                from PIL import Image
                png = rdir / "locations" / lid / "seed.png"; png.parent.mkdir(parents=True, exist_ok=True)
                Image.open(seeds[0]).convert("RGB").save(png); seeds = [png]
            light = (l.get("lighting_states") or {}).get("default", "")
            desc = f"{l['name']}: {l['description']} Lighting: {light}. Empty of people. {style}"
            key, tmpl = prompts.LOCATION_PACK[0]
            if key not in pack or self._redo("references", lid):
                self.log(f"references: {lid} wide ({'from seed' if seeds else 'text-to-image pro'})")
                if seeds:
                    p = self._gen_ref(f"ref_{bv}_{lid}_{key}", f"Same place as the reference image: identical architecture, materials, fixtures. {tmpl}. {desc}",
                                      rdir / "locations" / lid / f"{key}.png", seeds, self.episode["aspect_ratio"], False, f"ref {lid}/{key}", [(f"{l['name']} seed", seeds[0])], f"{tmpl}; same place", forced=fz)
                else:
                    p = self._gen_ref(f"ref_{bv}_{lid}_{key}", f"{tmpl}. {desc}", rdir / "locations" / lid / f"{key}.png", [], self.episode["aspect_ratio"], True, f"ref {lid}/{key}", [], "", forced=fz)
                pack[key] = self._ref_record(p, self.keys.bible_loc(lid, bv, f"{key}.png")); self.sstate.save()
            wide = Path(pack[key]["path"])
            for key, tmpl in prompts.LOCATION_PACK[1:]:
                if key in pack and not self._redo("references", lid):
                    continue
                self.log(f"references: {lid} {key}")
                p = self._gen_ref(f"ref_{bv}_{lid}_{key}", f"Same place as the reference: identical geometry, materials, fixtures, lighting. {tmpl}. {desc}",
                                  rdir / "locations" / lid / f"{key}.png", [wide] + seeds[:1], self.episode["aspect_ratio"], False, f"ref {lid}/{key}", [(f"{l['name']} wide", wide)], f"{tmpl}; same place", forced=fz)
                pack[key] = self._ref_record(p, self.keys.bible_loc(lid, bv, f"{key}.png")); self.sstate.save()

        for pr in self.pkg.props.values():
            pid = pr["id"]
            if pid in R["props"] and not self._redo("references", pid):
                continue
            self.log(f"references: prop {pid}")
            p = self._gen_ref(f"ref_{bv}_prop_{pid}", prompts.PROP_TEMPLATE.format(desc=pr["description"]) + f" {style}",
                              rdir / "props" / f"{pid}.png", [], "1:1", False, f"ref prop/{pid}", [], "")
            R["props"][pid] = self._ref_record(p, self.keys.bible_prop(pid, bv, f"{pid}.png")); self.sstate.save()

        self.sstate.data["reference_pack_complete"] = self.pkg.reference_version
        self.sstate.data["reference_inputs_fingerprint"] = reference_reuse.fingerprint(self.pkg)
        self.sstate.data["reference_inputs_parts"] = reference_reuse.fingerprint_parts(self.pkg)
        self.sstate.save()
        self.state.mark_stage("references"); self.state.set_status("references_review")
        self.log(f"references: пакет в {rdir}. Утвердить: --approve references --by \"...\"")

    # ---------- reference selection ----------

    def _scene_refs(self, s: dict, prev_kf: Path | None) -> tuple[list[tuple[str, Path]], list[str]]:
        R = self.sstate.data["references"]
        refs, legend = [], []
        cont = (s.get("continuity") or {}).get("characters", {})
        for cid in s["characters_in_frame"]:
            pack = R["characters"].get(cid, {}); c = self._char(cid)
            variant = s["wardrobe"].get(cid, c["wardrobe"]["default"])
            expr = (cont.get(cid) or {}).get("expression", "neutral")
            keys = ["front_headshot", "three_quarter_left", f"fullbody_front__{variant}"]
            if f"expr_{expr}" in pack:
                keys.append(f"expr_{expr}")
            action = " ".join((s.get("action") or "", s.get("keyframe_prompt") or "",
                               s.get("video_prompt") or "")).lower()
            if "expr_feminine_gaze" in pack and any(x in action for x in ("gaze", "look", "eyes", "watch")):
                keys.append("expr_feminine_gaze")
            if "expr_feline_half_smirk" in pack and any(x in action for x in ("half-smirk", "half smirk", "playful", "defian")):
                keys.append("expr_feline_half_smirk")
            walking = f"fullbody_walking__{variant}"
            if walking in pack and any(x in action for x in ("walk", "follow", "pad", "step")):
                keys.append(walking)
            start = len(refs) + 1
            for k in keys:
                if k in pack:
                    refs.append((f"{c['name']} {k}", Path(pack[k]["path"])))
            legend.append(f"images {start}-{len(refs)} = {c['name'].upper()} (wardrobe: {variant})")
        lpack = R["locations"].get(s["location"], {}); lname = self._loc(s["location"])["name"]
        act = (s.get("action") or "").lower()
        lkeys = ["wide"] + (["entrance"] if ("enter" in act or "arriv" in act or "door" in act) else ["medium"])
        start = len(refs) + 1
        for k in lkeys:
            if k in lpack:
                refs.append((f"{lname} {k}", Path(lpack[k]["path"])))
        legend.append(f"images {start}-{len(refs)} = the location {lname.upper()}: keep geometry and lighting")
        for pid in {p["prop_id"] for p in (s.get("props") or [])}:
            if pid in R["props"]:
                refs.append((f"prop {pid}", Path(R["props"][pid]["path"]))); legend.append(f"image {len(refs)} = prop {pid.replace('_',' ')}")
        if prev_kf and len(refs) < 14:
            refs.append(("previous shot", prev_kf)); legend.append(f"image {len(refs)} = previous shot in the same location: keep set, lighting, positions, props")
        return refs[:14], legend

    # ---------- stage: keyframes ----------

    def stage_keyframes(self):
        if self._done("keyframes"):
            self.log("keyframes: уже сделано"); return
        self._guard_live("keyframes"); self._require_references_approval()
        self.state.set_status("video_pending")
        style = self.pkg.style["style_sentence"]
        neg_extra = self.pkg.style.get("negative_image", "")
        prev_kf, prev_loc, failed = None, None, []
        for s in self.scenes:
            st = self.state.scene(s["scene_id"])
            if st.get("keyframe") and not self._redo("keyframes", s["scene_id"]):
                prev_kf, prev_loc = Path(st["keyframe"]), s["location"]; continue
            refs, legend = self._scene_refs(s, prev_kf if prev_loc == s["location"] else None)
            loc = self._loc(s["location"])
            light = (loc.get("lighting_states") or {}).get(s.get("lighting_state") or "default", "")
            prompt = (f"Reference legend: {'; '.join(legend)}. Keep every referenced person's face, hair, body, wardrobe and jewelry exactly as in their references. "
                      f"Location: {loc['description']} Lighting: {light}. Shot: {s.get('shot_type','')}, {s.get('lens') or ''}. "
                      f"One cinematic film still, the first frame of the shot: {s['keyframe_prompt']} "
                      f"Full-bleed {self.episode['aspect_ratio']} composition; keep important action away from "
                      f"the extreme edges, with no borders, mattes, letterbox bars or blank bands. {style} "
                      f"Avoid: {s.get('negative','')}, {neg_extra}, {prompts.NEGATIVE_IMAGE}.")
            hint, ok, path, tid = "", None, None, None
            base = self._attempt_base(f"{self.episode_id}_{s['scene_id']}_kf_", self._redo("keyframes", s["scene_id"]))
            if base:
                previous = self.state.data["takes"].get(
                    self._take_id(s["scene_id"], "kf", base - 1), {})
                previous_qc = previous.get("qa") or {}
                hint = previous_qc.get("fix_hint") or "; ".join(previous_qc.get("issues") or [])
            refused, softened = None, None
            for attempt in range(base, base + self.regen + 1):
                tid = self._take_id(s["scene_id"], "kf", attempt)
                self.log(f"keyframes: {s['scene_id']} попытка {attempt+1}")
                try:
                    path, take = providers.gen_image(self.fal, tid, prompt + (f" Correction: {hint}" if hint else ""), self.work / "keyframes" / f"{s['scene_id']}_v{attempt}.png",
                                                     [p for _, p in refs], self.episode["aspect_ratio"], False, f"keyframe {s['scene_id']}")
                except Exception as exc:
                    if not self._decided_refusal(exc):
                        raise
                    if not softened:
                        softened = self._soften(f"keyframe {s['scene_id']}", prompt, str(exc))
                        if softened:
                            prompt = softened
                            continue
                    self.log(f"keyframes: {s['scene_id']} провайдер отказался это создавать; сцена отложена")
                    refused = str(exc); break
                take["scene_id"] = s["scene_id"]; take["attempt"] = attempt; take["forced_by_operator"] = bool(base)
                take["regen_cap"] = self.regen
                qc = self.llm.qc_image(refs, path, s["keyframe_expected"]); take["qa"] = qc; self.state.save()
                kept, near, score = self._qc_verdict(qc)
                self.log(f"keyframes: {s['scene_id']} QC {qc.get('score')} {'OK' if kept else 'FAIL'} {qc.get('issues') or ''}")
                if kept:
                    ok = (path, tid); break
                if near:
                    # Close enough to keep. Paying for another picture to
                    # argue about one point is how a scene costs three times
                    # what it is worth.
                    self.log(f"keyframes: {s['scene_id']} принято как достаточно близкое ({score})")
                    ok = (path, tid); st["keyframe_close"] = score; break
                hint = qc.get("fix_hint") or "; ".join(qc.get("issues", []))
            if refused:
                # Nothing to hold this shot on: the first frame is what this
                # stage makes, and it does not exist. The scene needs a person.
                st["status"] = "refused"; st["refusal"] = refused
                failed.append(s["scene_id"]); self.state.save(); continue
            if not ok and self._weak_is_allowed(s["scene_id"]):
                ok = (path, tid); st["keyframe_weak"] = True
            if not ok:
                st["status"] = "failed_qa"; failed.append(s["scene_id"]); self.state.save(); continue
            st["keyframe"], st["keyframe_take"], st["status"] = str(ok[0]), ok[1], "keyframe_ok"; self.state.save()
            prev_kf, prev_loc = ok[0], s["location"]
        if failed:
            self.state.set_status("failed_qa")
            raise SceneFailed(f"keyframes не прошли QC: {failed}. Поправь production_prompts.json и --force <scene_id>, либо прими лучший дубль")
        self.state.mark_stage("keyframes")

    # ---------- stage: video ----------

    def stage_video(self):
        if self._done("video"):
            self.log("video: уже сделано"); return
        self._guard_live("video"); self._require_references_approval()
        style, cam = self.pkg.style["style_sentence"], self.pkg.style.get("camera_rules", "")
        neg_extra = self.pkg.style.get("negative_video", "")
        failed = []
        for s in self.scenes:
            st = self.state.scene(s["scene_id"])
            if st.get("video") and not self._redo("video", s["scene_id"]):
                continue
            refs, _ = self._scene_refs(s, None)
            speaks = bool(s.get("lipsync_speaker"))
            prompt = (f"{s['video_prompt']} Camera: {s.get('camera_motion','locked tripod')}, {s.get('lens') or ''}. "
                      f"{'The speaking character talks with natural lip movement; there is NO audible voice.' if speaks else 'Nobody speaks.'} "
                      f"Keep every person identical to the first frame for the whole clip: same face, hair, wardrobe, jewelry. {cam} {style}")
            if getattr(self.cfg, "native_dialogue", False):
                dialogue = " ".join(f"{self._char(d['speaker'])['name']} ({d.get('delivery', '')}): {json.dumps(d['text'], ensure_ascii=False)}" for d in s['dialogue'])
                prompt = (f"{s['video_prompt']} Camera: {s.get('camera_motion','locked tripod')}, {s.get('lens') or ''}. "
                          f"Keep the exact faces, wardrobe and location from the starting image. {cam} {style} "
                          f"Natural ambient audio. Language: {self.episode['language']}. "
                          + (f"Speak these lines exactly once with synchronized lips; all other people remain silent: {dialogue}" if dialogue else "Nobody speaks."))
            negative = ", ".join(x for x in (s.get("negative", ""), neg_extra) if x)
            hint, ok, path, tid = "", None, None, None
            route = capped_video_route(self.cfg, self.episode)
            if len(route) > 1:
                refusals = []
                for route_index, endpoint in enumerate(route):
                    tid = self._take_id(s["scene_id"], f"vid_r{route_index}", 0)
                    self.log(f"video: {s['scene_id']} {s['duration']}s route {route_index + 1}/{len(route)}: {endpoint}")
                    try:
                        path, take = providers.gen_video(
                            self.fal, tid, Path(st["keyframe"]),
                            prompt + (f" Correction from prior QC: {hint}" if hint else ""), negative,
                            s["duration"], self.work / "video" / f"{s['scene_id']}_r{route_index}.mp4",
                            self.episode["aspect_ratio"], f"video {s['scene_id']}", endpoint=endpoint)
                    except Exception as exc:
                        if not self._decided_refusal(exc):
                            raise
                        refusals.append(str(exc))
                        self.log(f"video: {s['scene_id']} {endpoint} refused; advancing to next route engine")
                        continue
                    take.update(scene_id=s["scene_id"], attempt=route_index,
                                route_index=route_index, route_endpoint=endpoint,
                                parent_take=st.get("keyframe_take"), forced_by_operator=False,
                                regen_cap=0)
                    frames = media.sample_frames(path, self.work / "frames" / f"{s['scene_id']}_r{route_index}")
                    qc = self.llm.qc_video(refs, frames, s["video_expected"])
                    take["qa"] = qc; self.state.save()
                    kept, near, score = self._qc_verdict(qc)
                    self.log(f"video: {s['scene_id']} {endpoint} QC {qc.get('score')} {'OK' if kept else 'FAIL'} {qc.get('issues') or ''}")
                    if kept:
                        ok = (path, tid); break
                    if near:
                        self.log(f"video: {s['scene_id']} принято как достаточно близкое ({score})")
                        ok = (path, tid); st["video_close"] = score; break
                    hint = qc.get("fix_hint") or "; ".join(qc.get("issues", []))
                if not ok and len(refusals) == len(route):
                    held = media.hold_from_still(Path(st["keyframe"]), s["duration"],
                                                 self.work / "video" / f"{s['scene_id']}_hold.mp4",
                                                 self.episode["width"], self.episode["height"])
                    st["video"], st["video_take"] = str(held), None
                    st["video_held"], st["refusal"], st["status"] = True, refusals[-1], "video_ok"
                    self.log(f"video: {s['scene_id']} all route engines refused; holding frame {s['duration']}s")
                    self.state.save(); continue
                if not ok and self._weak_is_allowed(s["scene_id"]):
                    ok = (path, tid); st["video_weak"] = True
                if not ok:
                    st["status"] = "failed_qa"; failed.append(s["scene_id"]); self.state.save(); continue
                st["video"], st["video_take"], st["status"] = str(ok[0]), ok[1], "video_ok"
                self.state.save(); continue
            base = self._attempt_base(f"{self.episode_id}_{s['scene_id']}_vid_", self._redo("video", s["scene_id"]))
            refused, softened = None, None
            for attempt in range(base, base + self.regen + 1):
                tid = self._take_id(s["scene_id"], "vid", attempt)
                self.log(f"video: {s['scene_id']} {s['duration']}s попытка {attempt+1}")
                try:
                    path, take = providers.gen_video(self.fal, tid, Path(st["keyframe"]), prompt + (f" Correction: {hint}" if hint else ""), negative,
                                                     s["duration"], self.work / "video" / f"{s['scene_id']}_v{attempt}.mp4", self.episode["aspect_ratio"], f"video {s['scene_id']}")
                except Exception as exc:
                    if not self._decided_refusal(exc):
                        raise
                    # Most refusals are the wording, not the beat. Saying it
                    # another way costs one Anthropic call and saves the scene;
                    # a refused scene used to cost the producer an evening.
                    if not softened:
                        softened = self._soften(f"video {s['scene_id']}", prompt, str(exc))
                        if softened:
                            prompt = softened
                            continue
                    self.log(f"video: {s['scene_id']} провайдер отказался это создавать")
                    refused = str(exc); break
                take["scene_id"] = s["scene_id"]; take["attempt"] = attempt; take["parent_take"] = st.get("keyframe_take"); take["forced_by_operator"] = bool(base)
                take["regen_cap"] = self.regen
                frames = media.sample_frames(path, self.work / "frames" / f"{s['scene_id']}_v{attempt}")
                qc = self.llm.qc_video(refs, frames, s["video_expected"]); take["qa"] = qc; self.state.save()
                kept, near, score = self._qc_verdict(qc)
                self.log(f"video: {s['scene_id']} QC {qc.get('score')} {'OK' if kept else 'FAIL'} {qc.get('issues') or ''}")
                if kept:
                    ok = (path, tid); break
                if near:
                    self.log(f"video: {s['scene_id']} принято как достаточно близкое ({score})")
                    ok = (path, tid); st["video_close"] = score; break
                hint = qc.get("fix_hint") or "; ".join(qc.get("issues", []))
            if not ok and softened and not refused:
                # The refusal came on the last attempt, so there was no room
                # left to try the softened wording. Holding the frame is still
                # better than stopping the episode.
                refused = 'The provider refused this shot and no attempt was left to say it another way.'
            if refused:
                # Said another way and still refused. The first frame of this
                # scene exists and is paid for, so the shot is held on it: an
                # ordinary thing in drama, and the episode finishes.
                held = media.hold_from_still(Path(st["keyframe"]), s["duration"],
                                             self.work / "video" / f"{s['scene_id']}_hold.mp4",
                                             self.episode["width"], self.episode["height"])
                st["video"], st["video_take"] = str(held), None
                st["video_held"], st["refusal"], st["status"] = True, refused, "video_ok"
                self.log(f"video: {s['scene_id']} провайдер отказался и после смягчения; "
                         f"держим кадр {s['duration']}s")
                self.state.save(); continue
            if not ok and self._weak_is_allowed(s["scene_id"]):
                ok = (path, tid); st["video_weak"] = True
            if not ok:
                st["status"] = "failed_qa"; failed.append(s["scene_id"]); self.state.save(); continue
            st["video"], st["video_take"], st["status"] = str(ok[0]), ok[1], "video_ok"; self.state.save()
        if failed:
            self.state.set_status("failed_qa")
            raise SceneFailed(f"video не прошло QC: {failed}. Поправь описание сцены и --force <scene_id>, либо прими лучший дубль")
        self.state.mark_stage("video"); self.state.set_status("voice_pending")

    # ---------- stage: voice ----------

    def _voice_id(self, cid: str) -> str:
        v = self._char(cid).get("voice") or {}
        return os.environ.get(v.get("voice_env", ""), "") or self.cfg.voice_ids.get(cid, "")

    def stage_voice(self):
        if self._done("voice"):
            self.log("voice: уже сделано"); return
        self._guard_live("voice")
        if getattr(self.cfg, "native_dialogue", False):
            for scene in self.scenes:
                cues = []
                lines = scene["dialogue"]
                available = scene["duration"] - 0.8
                for i, line in enumerate(lines):
                    start = 0.4 + available * i / len(lines)
                    end = 0.4 + available * (i + 1) / len(lines)
                    cues.append({"start": start, "end": end, "text": line["text"], "speaker": line["speaker"]})
                self.state.scene(scene["scene_id"])["voice"] = {"cues": cues, "timing_source": "estimated_native_audio"}
            self.state.mark_stage("voice")
            return
        speakers = {d["speaker"] for s in self.scenes for d in s["dialogue"]}
        missing = [c for c in speakers if not self._voice_id(c)] if not self.cfg.dry_run else []
        if missing:
            raise RuntimeError("нет voice id в .env для: " + ", ".join(f"{c} ({(self._char(c).get('voice') or {}).get('voice_env','?')})" for c in missing))
        too_long = []
        for s in self.scenes:
            st = self.state.scene(s["scene_id"])
            if not s["dialogue"]:
                continue
            if st.get("voice") and not self._redo("voice", s["scene_id"]):
                # A failed length check also saved its audio. Resume must reuse
                # it AND enforce the same check, not mark the stage successful.
                end = max((c['end'] for c in st['voice'].get('cues', [])), default=0)
                limit = s['duration'] - pkgmod.TAIL
                if end > limit + 0.05:
                    too_long.append(_overrun(s["scene_id"], end, limit))
                continue
            vdir = self.work / "voice" / s["scene_id"]
            t, sync_lines, vo_lines, cues = LEAD_IN, [], [], []
            limit = s["duration"] - pkgmod.TAIL
            for i, d in enumerate(s["dialogue"]):
                line_id = f"{s['scene_id']}_l{i:02d}"
                cvoice = self._char(d["speaker"]).get("voice") or {}
                def make_voice():
                    return providers.tts(self.cfg, self.log, d["text"], d.get("delivery", ""), self._voice_id(d["speaker"]), vdir / f"{line_id}_raw.mp3",
                                         model_id=cvoice.get("model_id") or self.cfg.elevenlabs_model_id, settings=cvoice.get("settings"), language_code=self.episode["language"])
                guard = getattr(self.cfg, "paid_calls", None)
                if guard:
                    from .costs import PRICE
                    params = {"line": line_id, "text": d["text"], "delivery": d.get("delivery", ""),
                              "voice_id": self._voice_id(d["speaker"]), "voice": cvoice,
                              # Audio generated while stage direction was being
                              # read aloud is not reusable.
                              "tag_policy": 2}
                    # Preserve operation IDs of existing English productions.
                    # Other languages must not reuse an English voice result.
                    if not self.episode["language"].lower().startswith("en"):
                        params["language_code"] = self.episode["language"]
                    amount = (len(d["text"]) + len(d.get("delivery", "")) + 3) / 1000 * PRICE["elevenlabs_per_1k_chars_estimate"]
                    prov = dict(guard.once("elevenlabs", params, amount, make_voice))
                else:
                    prov = make_voice()
                cur = Path(prov["local_path"])
                if cvoice.get("phone_fx") or (d.get("voice_over") and "phone" in d.get("delivery", "").lower()):
                    cur = media.phone_fx(cur, vdir / f"{line_id}_phone.mp3"); prov["phone_fx"] = True
                dur = media.duration(cur)
                remaining = limit - t
                if dur > remaining:
                    factor = min(MAX_TEMPO, dur / max(remaining, 0.1))
                    cur = media.speed_up(cur, vdir / f"{line_id}_tempo.mp3", factor); prov["atempo"] = round(factor, 3); dur = media.duration(cur)
                prov.update({"line_id": line_id, "speaker": d["speaker"], "start": round(t, 3), "duration": round(dur, 3),
                             "r2_key": self.keys.audio(d["speaker"], line_id, cur.name), "final_path": str(cur)})
                self.state.data.setdefault("audio", {})[line_id] = prov
                is_vo = d.get("voice_over") or d["speaker"] != s.get("lipsync_speaker")
                (vo_lines if is_vo else sync_lines).append((cur, t))
                cues.append({"start": round(t, 3), "end": round(t + dur, 3), "text": d["text"], "speaker": d["speaker"]})
                t += dur + GAP
            if t - GAP > limit + 0.05:
                too_long.append(_overrun(s["scene_id"], t - GAP, limit))
            if sync_lines:
                media.build_timeline(sync_lines, s["duration"], vdir / "sync_track.mp3")
            if vo_lines:
                media.build_timeline(vo_lines, s["duration"], vdir / "vo_track.mp3")
            st["voice"] = {"sync_track": str(vdir / "sync_track.mp3") if sync_lines else None, "vo_track": str(vdir / "vo_track.mp3") if vo_lines else None, "cues": cues}
            self.state.save()
            self.log(f"voice: {s['scene_id']} {len(s['dialogue'])} реплик, конец {t - GAP:.1f}s / {s['duration']}s")
        if too_long:
            raise RuntimeError(f"реплики не помещаются даже с atempo {MAX_TEMPO}: {too_long}. Сократи текст в brief.json")
        self.state.mark_stage("voice"); self.state.set_status("lipsync_pending")

    # ---------- stage: lipsync ----------

    def stage_lipsync(self):
        if self._done("lipsync"):
            self.log("lipsync: уже сделано"); return
        self._guard_live("lipsync")
        for s in self.scenes:
            st = self.state.scene(s["scene_id"])
            if st.get("final") and not self._redo("lipsync", s["scene_id"]):
                continue
            video, v = Path(st["video"]), st.get("voice") or {}
            final = self.work / "final" / f"{s['scene_id']}.mp4"; final.parent.mkdir(parents=True, exist_ok=True)
            if not v:
                shutil.copy(video, final); st["final"] = str(final); self.state.save(); continue
            cur = video
            if v.get("sync_track") and st.get("video_held"):
                # Nothing moves in a held frame, so there are no lips to sync.
                # Paying to animate a still would buy an uncanny mouth on an
                # otherwise deliberate shot; the speech is mixed over it.
                self.log(f"lipsync: {s['scene_id']} кадр удержан, губы не синхронизируем")
                cur = media.mix_audio_into(cur, Path(v["sync_track"]),
                                           self.work / "lipsync" / f"{s['scene_id']}_held.mp4")
            elif v.get("sync_track"):
                base = self._attempt_base(f"{self.episode_id}_{s['scene_id']}_ls_", self._redo("lipsync", s["scene_id"]))
                tid = self._take_id(s["scene_id"], "ls", base)
                self.log(f"lipsync: {s['scene_id']} ({s.get('lipsync_speaker')})")
                cur, take = providers.lipsync(self.fal, tid, video, Path(v["sync_track"]), s["duration"], self.work / "lipsync" / f"{s['scene_id']}.mp4", f"lipsync {s['scene_id']}")
                take["scene_id"] = s["scene_id"]; take["parent_take"] = st.get("video_take"); take["speaker"] = s.get("lipsync_speaker"); take["forced_by_operator"] = bool(base)
                st["lipsync_take"] = tid
            if v.get("vo_track"):
                cur = media.mix_audio_into(cur, Path(v["vo_track"]), self.work / "lipsync" / f"{s['scene_id']}_vo.mp4")
            shutil.copy(cur, final); st["final"] = str(final); self.state.save()
        self.state.mark_stage("lipsync"); self.state.set_status("assembly_pending")

    def _soften(self, what: str, prompt: str, refusal: str) -> str | None:
        """Ask for the same beat in words the provider will make.

        Returns None if the rewrite itself fails, so a refusal is never made
        worse by the attempt to work around it.
        """
        try:
            said = self.llm.soften_shot(prompt, refusal)
        except Exception as exc:
            self.log(f"{what}: смягчить описание не удалось ({type(exc).__name__})")
            return None
        text = (said or {}).get("prompt")
        if not text:
            return None
        self.log(f"{what}: провайдер отказал; говорим иначе — {said.get('changed', '')}")
        return text

    # ---------- music ----------

    MUSIC_LEVELS = {1: "calm", 2: "uneasy", 3: "taut"}
    MUSIC_PROMPTS = {
        1: "Calm instrumental underscore, sparse and unhurried, soft sustained strings and gentle piano, "
           "no drums, no vocals, no melody that pulls attention, loopable background bed.",
        2: "Uneasy instrumental underscore, low pulsing drone with a quiet irregular heartbeat, muted strings, "
           "restrained and tense but never loud, no vocals, loopable background bed.",
        3: "Taut instrumental underscore, insistent low ostinato and rising strings, dread building without release, "
           "no vocals, no melody, loopable background bed.",
    }
    # Loud enough to feel under an empty room, quiet enough to stay under a line.
    MUSIC_DB = {1: -26.0, 2: -24.0, 3: -22.0}
    MUSIC_DUCK_DB = 4.0
    MUSIC_BED_SECONDS = 180

    def _tension(self, scene: dict) -> int:
        """How tight this scene is, from the script if it says so.

        Episodes written before the script carried the field still need an
        answer, and the facts that make a scene tense are already recorded:
        a cliffhanger, a secret changing hands, a relationship moving.
        """
        stated = scene.get("tension")
        if stated in (1, 2, 3):
            return int(stated)
        if scene.get("is_cliffhanger"):
            return 3
        if scene.get("knowledge_gained") or scene.get("relationship_changes"):
            return 2
        return 1

    def _score_plan(self, spans: list[tuple[dict, float]]) -> list[dict]:
        plan = []
        for scene, seconds in spans:
            level = self._tension(scene)
            db = self.MUSIC_DB[level]
            if scene.get("dialogue"):
                db -= self.MUSIC_DUCK_DB
            plan.append({"scene_id": scene["scene_id"], "level": level, "seconds": seconds, "db": db})
        return plan

    def _music_beds(self, levels: set[int]) -> dict[int, Path]:
        """One bed per level in use. Files the producer supplied, or generated once."""
        mode = self.episode.get("music", "off")
        assets = self.pkg.root / "assets"
        beds: dict[int, Path] = {}
        for level in sorted(levels):
            name = self.MUSIC_LEVELS[level]
            supplied = next((p for p in assets.glob(f"music_{name}.*")), None) or next((p for p in assets.glob("music.*")), None)
            if mode == "files":
                if not supplied:
                    raise RuntimeError(
                        f"The series is set to use your own music, but no bed for '{name}' was uploaded. "
                        "Upload one on the series page, or switch the series to generated music.")
                beds[level] = supplied
            else:
                if supplied:
                    beds[level] = supplied; continue
                dest = self.work / "music" / f"{name}.wav"
                if not dest.exists():
                    tid = self._take_id("episode", "music", level)
                    self.log(f"assemble: music {name} генерируется")
                    providers.gen_music(self.fal, tid, self.MUSIC_PROMPTS[level],
                                        self.MUSIC_BED_SECONDS, dest, f"music {name}")
                beds[level] = dest
        return beds

    # ---------- stage: assemble ----------

    def _finishing_changed(self, finishing: dict) -> bool:
        """Has a packaging setting moved since this master was cut?

        `finishing` is the series format as the package reads it now. The
        episode dict cannot answer this: it is a snapshot taken when the
        script was taken in, so an episode started days ago was assembled to
        settings the producer had since changed twice, with no way to tell
        because the screen showed the new ones.

        Re-cutting calls no provider and costs nothing, so a master carrying
        subtitles the producer has since turned off is worth cutting again
        rather than shipping. Only these settings qualify: everything else
        that could differ would contradict footage already shot.
        """
        mdir = self.state.data.get("master_dir")
        if not mdir:
            return False
        meta = Path(mdir) / "metadata.json"
        if not meta.exists():
            return False
        try:
            was = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        # Against the settings this run would actually use, overrides and
        # all — comparing the package alone re-cut the master on every rerun
        # whenever subtitles were being overridden on the command line.
        wants = {"captions": self.captions_override or finishing.get("captions", "srt"),
                 "music": finishing.get("music", "off")}
        return any(key in was and was[key] != want for key, want in wants.items())

    def stage_assemble(self):
        finishing = self.pkg.series.get("format") or {}
        if self._done("assemble"):
            if not self._finishing_changed(finishing):
                self.log("assemble: уже сделано"); return
            # Nothing generated is discarded: the scenes are already cut and
            # the master is simply put together again from them.
            self.log("assemble: субтитры или музыка изменились — собираем заново")
            for stage in ("assemble", "qa"):
                self.state.data.get("stages", {}).pop(stage, None)
        e = self.episode; w, h = e["width"], e["height"]
        norm, cues, spans, t = [], [], [], 0.0
        for s in self.scenes:
            st = self.state.scene(s["scene_id"])
            dst = media.normalize_clip(Path(st["final"]), self.work / "norm" / f"{s['scene_id']}.mp4", w, h)
            d = media.duration(dst)
            for c in (st.get("voice") or {}).get("cues", []):
                cues.append({"start": round(t + c["start"], 3), "end": round(min(t + c["end"], t + d - 0.05), 3), "text": c["text"]})
            norm.append(dst); spans.append((s, d)); t += d
        ver = f"v{int(self.state.data.get('master_version', 0)) + 1}"
        mdir = self.out / "masters" / ver; mdir.mkdir(parents=True, exist_ok=True)
        cur = media.concat(norm, self.work / "episode_cut.mp4")
        room = next((p for p in (self.pkg.root / "assets").glob("room_tone.*")), None)
        if room:
            cur = media.add_bed(cur, room, self.work / "episode_room_tone.mp4", -28.0)
            self.log("assemble: room_tone подложен (-28.0 dB)")
        music_mode = finishing.get("music", e.get("music", "off"))
        if music_mode in ("files", "generate") and spans:
            plan = self._score_plan(spans)
            beds = self._music_beds({p["level"] for p in plan})
            track = media.score_track([{"bed": beds[p["level"]], "seconds": p["seconds"], "db": p["db"]} for p in plan],
                                      self.work / "music" / "episode_score.wav")
            cur = media.mix_track(cur, track, self.work / "episode_music.mp4")
            counts = {name: sum(1 for p in plan if self.MUSIC_LEVELS[p["level"]] == name) for name in self.MUSIC_LEVELS.values()}
            self.log("assemble: музыка подложена — " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
        elif (bed := next((p for p in (self.pkg.root / "assets").glob("music.*")), None)):
            cur = media.add_bed(cur, bed, self.work / "episode_music.mp4", -22.0)
            self.log("assemble: music подложен (-22.0 dB)")
        # Read from the package as it reads now, not from the copy frozen when
        # the script was taken in. Subtitles and music decide only how the
        # finished episode is packaged, and an episode started days ago was
        # assembled to settings the producer had since changed twice — with no
        # way to tell, because the screen showed the new ones.
        captions = self.captions_override or finishing.get("captions", e.get("captions", "srt"))
        # "none": ни поверх картинки, ни отдельным файлом. Реплики всё равно
        # считаются — иначе проверка "субтитры совпадают с диалогом" молча
        # отключилась бы вместе с субтитрами.
        srt = media.write_srt(cues, (mdir if captions != "none" else self.work) / "episode.srt")
        if captions in ("burned", "both"):
            cur = media.burn_subtitles(cur, srt, self.work / "episode_subs.mp4"); self.log("assemble: captions вшиты")
        elif captions == "none":
            self.log("assemble: субтитры отключены для этого сериала")
        cur = media.loudnorm(cur, self.work / "episode_loud.mp4")
        master = media.encode_master(cur, mdir / "episode.mp4", w, h)
        media.poster_frame(master, 1.0, mdir / "poster.jpg")
        meta = {"series_id": e["series_id"], "season_id": e["season_id"], "episode_id": e["episode_id"], "number": e["number"], "title": e["title"],
                "series_title": e["series_title"], "language": e["language"], "duration_seconds": round(media.duration(master), 2),
                "aspect_ratio": e["aspect_ratio"], "captions": captions, "subtitle_cues": len(cues), "music": music_mode,
                "cliffhanger": e["cliffhanger"],
                "caption_text": f"{e['series_title']} · {e['season_id'].upper()}{e['episode_id'].upper()} «{e['title']}»\n{e.get('logline','')}",
                "hashtags": [], "master_version": ver, "created_at": now()}
        (mdir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest = {"master_version": ver, "master_sha256": sha256(master), "brief_sha256": e["brief_sha256"], "bible_version": e["bible_version"],
                    "package_checksums": self.state.data.get("package_checksums"), "spent_usd": self.budget.spent, "created_at": now(),
                    "scenes": [{"scene_id": s["scene_id"], "duration": s["duration"], "keyframe_take": self.state.scene(s["scene_id"]).get("keyframe_take"),
                                "video_take": self.state.scene(s["scene_id"]).get("video_take"), "lipsync_take": self.state.scene(s["scene_id"]).get("lipsync_take"),
                                "final_sha256": sha256(Path(self.state.scene(s["scene_id"])["final"]))} for s in self.scenes]}
        (mdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.state.data["master_version"] = int(ver[1:]); self.state.data["master_dir"] = str(mdir)
        self.state.mark_stage("assemble"); self.state.set_status("qa_pending")
        self.log(f"assemble: {ver} -> {master} ({meta['duration_seconds']}s)")

    # ---------- stage: qa ----------

    def _repair_master_audio(self, mdir: Path, loudness: dict) -> bool:
        """Create one audio-corrected version; retain the original and all takes."""
        manifest = json.loads((mdir / "manifest.json").read_text(encoding="utf-8"))
        # A completed correction is never looped, including after Resume.
        if manifest.get("audio_repair") or self.state.stage_done("deliver"):
            return False
        versions = [int(p.name[1:]) for p in mdir.parent.iterdir()
                    if p.is_dir() and p.name.startswith("v") and p.name[1:].isdigit()]
        ver = f"v{max(versions) + 1}"
        destination = mdir.parent / ver
        self.log(f"qa: correcting soundtrack in {mdir.name} -> {ver}; saved video and takes are reused")
        with tempfile.TemporaryDirectory(prefix="audio-repair-", dir=self.work) as tmp:
            staging = Path(tmp)
            master = media.loudnorm(mdir / "episode.mp4", staging / "episode.mp4")
            for name in ("episode.srt", "poster.jpg"):
                if (mdir / name).exists():
                    shutil.copy2(mdir / name, staging / name)
            metadata = json.loads((mdir / "metadata.json").read_text(encoding="utf-8"))
            metadata.update(master_version=ver, duration_seconds=round(media.duration(master), 2), created_at=now())
            repair = {"method": "loudnorm_two_pass", "source_master_version": mdir.name,
                      "source_master_sha256": sha256(mdir / "episode.mp4"), "before": loudness, "created_at": now()}
            manifest.update(master_version=ver, master_sha256=sha256(master), audio_repair=repair, created_at=now())
            (staging / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            staging.rename(destination)
        self.state.data.update(master_version=int(ver[1:]), master_dir=str(destination))
        self.state.save()
        return True

    def stage_qa(self, _repair_audio: bool = True):
        if self._done("qa"):
            self.log("qa: уже сделано"); return
        e = self.episode; L = e["limits"]
        mdir = Path(self.state.data["master_dir"]); master = mdir / "episode.mp4"
        p = media.probe(master); checks = []
        def chk(name, ok, detail=""):
            checks.append({"check": name, "pass": bool(ok), "detail": detail}); self.log(f"qa: {'OK  ' if ok else 'FAIL'} {name} {detail}")
        # Final mux/encode may add or remove a fraction of a second through
        # frame/audio padding. Judge that technical drift with the same one-second
        # tolerance already used against the planned duration; otherwise a 120s
        # episode can be rejected at 120.23s even though it matches its plan.
        duration_tolerance = 1.0
        chk("duration_range",
            L["min_sec"] - duration_tolerance <= p["duration"] <= L["max_sec"] + duration_tolerance,
            f"{p['duration']:.2f}s (encode tolerance ±{duration_tolerance:.0f}s)")
        chk("duration_matches_plan", abs(p["duration"] - e["total_seconds"]) <= duration_tolerance,
            f"plan {e['total_seconds']}s")
        chk("resolution", (p["width"], p["height"]) == (e["width"], e["height"]), f"{p['width']}x{p['height']}")
        chk("decodes_clean", media.decode_check(master))
        blacks = [b for b in media.black_segments(master) if b["end"] < p["duration"] - 1.0]
        chk("no_black_segments", not blacks, str(blacks) if blacks else "")
        ld = media.loudness(master)
        chk("loudness_target", ld["integrated_lufs"] is not None and -16.5 <= ld["integrated_lufs"] <= -11.5, f"{ld['integrated_lufs']} LUFS")
        chk("true_peak", ld["true_peak_dbtp"] is not None and ld["true_peak_dbtp"] <= -0.5, f"{ld['true_peak_dbtp']} dBTP")
        weak = [s["scene_id"] for s in self.scenes
                if self.state.scene(s["scene_id"]).get("keyframe_weak")
                or self.state.scene(s["scene_id"]).get("video_weak")]
        # A scene the producer looked at and accepted is not a fault to report
        # back at them. Offering the decision and then refusing the episode
        # over it left the only way past a marked-down scene leading nowhere.
        decided = [sid for sid in weak if self._weak_is_allowed(sid)]
        unreviewed = [sid for sid in weak if sid not in decided]
        chk("scene_qc_all_passed", not unreviewed,
            (f"weak: {unreviewed}" if unreviewed else "")
            + (f" (accepted by the producer: {decided})" if decided else ""))
        held = [s["scene_id"] for s in self.scenes if self.state.scene(s["scene_id"]).get("video_held")]
        if held:
            self.log(f"qa: кадр удержан вместо видео в сценах {held}")
        # Takes are immutable and survive every resume, so a lifetime count
        # against the cap in force today condemns every scene shot under a
        # larger one — lowering the setting made already-paid-for work a
        # permanent failure. Only takes that recorded the cap they were made
        # under can be judged; the rest are not this run's to answer for.
        over = []
        route = tuple(getattr(self.cfg, "video_model_route", ()) or (self.cfg.fal_video_model,))
        for scene in self.scenes:
            made = [t for t in self.state.data["takes"].values()
                    if t.get("scene_id") == scene["scene_id"]
                    and t.get("endpoint") in route
                    and not t.get("forced_by_operator")
                    and t.get("regen_cap") is not None]
            if len(route) > 1:
                endpoints = [t.get("endpoint") for t in made]
                exceeded = len(endpoints) != len(set(endpoints)) or len(endpoints) > len(route)
            else:
                exceeded = bool(made and len(made) > min(int(t["regen_cap"]) for t in made) + 1)
            if exceeded:
                over.append(scene["scene_id"])
        chk("retry_limit", not over, str(over) if over else "")
        # Intake freezes the episode shape, including the budget that was in
        # force when production first started.  A later, explicitly approved
        # budget increase updates the live Budget object, but must not rewrite
        # that old creative snapshot.  Judge the finished run against the
        # effective approved cap or recovery can spend within the new limit
        # and then be rejected by QA against the stale one.
        chk("budget", self.budget.spent <= self.budget.cap,
            f"${self.budget.spent:.2f} / ${self.budget.cap:.2f}")
        prov_missing = [tid for tid, t in self.state.data["takes"].items() if t.get("status") == "succeeded" and not all(k in t for k in ("endpoint", "request_id", "checksum", "estimated_cost"))]
        chk("provenance_complete", not prov_missing, str(prov_missing[:5]) if prov_missing else "")
        srt_file = mdir / "episode.srt"
        srt_cues = (srt_file.read_text(encoding="utf-8").count("-->") if srt_file.exists()
                    else int(json.loads((mdir / "metadata.json").read_text(encoding="utf-8"))
                             .get("subtitle_cues", -1)))
        n_lines = sum(len(s["dialogue"]) for s in self.scenes)
        chk("subtitles_match_dialogue", srt_cues == n_lines, f"{srt_cues}/{n_lines}")
        last = self.scenes[-1]
        chk("cliffhanger_is_last", last["source_scene_id"] == e["cliffhanger"]["scene_id"], last["scene_id"])
        passed = all(c["pass"] for c in checks)
        qdir = self.out / "qa" / mdir.name; qdir.mkdir(parents=True, exist_ok=True)
        (qdir / "report.json").write_text(json.dumps({"master_version": mdir.name, "pass": passed, "checks": checks, "loudness": ld, "probe": p, "created_at": now()}, ensure_ascii=False, indent=2), encoding="utf-8")
        self.state.data["qa_dir"] = str(qdir)
        if not passed:
            failed = {c["check"] for c in checks if not c["pass"]}
            self.state.set_status("failed_qa")
            if _repair_audio and failed <= {"loudness_target", "true_peak"} and self._repair_master_audio(mdir, ld):
                # All checks run again on the encoded correction. No thresholds
                # are relaxed and no image/video/voice providers are called.
                return self.stage_qa(_repair_audio=False)
            raise RuntimeError(f"QA failed ({', '.join(sorted(failed))}), отчёт {qdir / 'report.json'}")
        self.state.mark_stage("qa")
        self.state.set_status("complete"); self.log(f"qa: PASS -> {qdir / 'report.json'}")

    # ---------- stage: deliver ----------

    def stage_deliver(self):
        if self._done("deliver"):
            self.log("deliver: уже сделано"); return
        if not self.state.stage_done("qa"):
            raise RuntimeError("Pass QA before delivering an episode.")
        k = self.keys; mdir = Path(self.state.data["master_dir"]); ver = mdir.name
        self.r2.put(self.pkg.episode_dir(self.episode_id) / "brief.json", k.brief())
        self.r2.put(self.ep / "direction.json", f"{k.ep}/direction.json")
        self.r2.put(self.work / "ledger.json", f"{k.ep}/ledger.json")
        for name in ("episode.mp4", "episode.srt", "poster.jpg", "metadata.json", "manifest.json"):
            if (mdir / name).exists():
                self.r2.put(mdir / name, k.master(ver, name))
        self.r2.put(Path(self.state.data["qa_dir"]) / "report.json", k.qa(ver, "report.json"))
        for tid, t in self.state.data["takes"].items():
            if t.get("local_path") and Path(t["local_path"]).exists() and t.get("scene_id"):
                t["r2_key"] = self.r2.put(Path(t["local_path"]), k.take(t["scene_id"], tid, Path(t["local_path"]).name))
        for lid, a in self.state.data.get("audio", {}).items():
            fp = Path(a.get("final_path") or a["local_path"])
            if fp.exists():
                self.r2.put(fp, k.audio(a["speaker"], lid, fp.name))
        prov = {"takes": self.state.data["takes"], "audio": self.state.data.get("audio", {}), "cost_log": self.state.data["cost_log"],
                "references": self.sstate.data["references"], "overrides": self.state.data["overrides"], "bible_version": self.pkg.bible_version,
                "reference_version": self.pkg.reference_version}
        pp = mdir / "provenance.json"; pp.write_text(json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")
        self.r2.put(pp, k.qa(ver, "provenance.json"))
        self.state.data["delivered"] = {"master_version": ver, "at": now()}
        self.sstate.data["episodes"][self.episode_id].update({"status": "delivered", "master_version": ver, "end_state": self.episode["end_state"]})
        self.sstate.save(); self.state.mark_stage("deliver")
        self.log(f"deliver: master {ver} в R2 (private): {k.master(ver, 'episode.mp4')}. Далее: --approve publish --by \"...\" и --stages publish")

    # ---------- stage: publish ----------

    def stage_publish(self):
        if not self.state.approved("publish"):
            self.state.set_status("blocked_open_question")
            raise RuntimeError("нужен approval 'publish': run_episode.py ... --approve publish --by \"...\"")
        k = self.keys; mdir = Path(self.state.data["master_dir"]); ver = mdir.name; pub = {}
        for name in ("episode.mp4", "poster.jpg", "episode.srt", "metadata.json"):
            if (mdir / name).exists():
                pub[name] = self.r2.public_url(self.r2.copy(k.master(ver, name), k.public(name)))
        self.state.data["public"] = pub; self.state.set_status("published"); self.state.mark_stage("publish")
        self.sstate.data["episodes"][self.episode_id]["status"] = "published"; self.sstate.save()
        self.log(f"publish: промотировано в /public/: {pub['episode.mp4']}")
        if not self.cfg.instagram_publish_enabled:
            self.log("publish: Instagram выключен (INSTAGRAM_PUBLISH_ENABLED=false). Готово для ручной публикации."); return
        raise NotImplementedError("Instagram Graph API publish: нужны INSTAGRAM_USER_ID/META_* и решение по автопубликации")

    # ---------- runner ----------

    def run(self, stages: list[str]):
        t0 = time.time()
        try:
            for s in stages:
                getattr(self, f"stage_{s}")()
        finally:
            self.log(f"итого: mode={self.cfg.mode}, потрачено ${self.budget.spent:.2f} из ${self.budget.cap:.2f}, статус {self.state.data['status']}, {(time.time()-t0)/60:.1f} мин")
            self.logf.close()
