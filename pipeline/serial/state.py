import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

STATUS_FLOW = ["draft", "validated", "references_pending", "references_review", "video_pending", "voice_pending",
               "lipsync_pending", "assembly_pending", "qa_pending", "complete", "published"]
STATUS_EXCEPTIONAL = ["blocked_open_question", "failed_provider", "failed_qa", "needs_budget_override", "cancelled"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State:
    """episodes/<ep>/state.json: статус, сцены, референсы, все takes с provenance, расходы, approvals."""

    def __init__(self, episode_dir: Path):
        self.path = episode_dir / "state.json"
        self.data = {
            "status": "draft", "stages": {}, "scenes": {}, "references": {}, "takes": {},
            "spent_usd": 0.0, "reserved_usd": 0.0, "cost_log": [], "approvals": {}, "overrides": [],
        }
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self):
        self.data["updated_at"] = now()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def set_status(self, status: str):
        self.data["status"] = status
        self.save()

    def scene(self, scene_id: str) -> dict:
        return self.data["scenes"].setdefault(scene_id, {})

    def stage_done(self, name: str) -> bool:
        return self.data["stages"].get(name) == "done"

    def mark_stage(self, name: str, status: str = "done"):
        self.data["stages"][name] = status
        self.save()

    def take(self, take_id: str) -> dict:
        return self.data["takes"].setdefault(take_id, {"take_id": take_id, "created_at": now()})

    def approved(self, what: str) -> bool:
        return bool(self.data["approvals"].get(what, {}).get("approved"))
