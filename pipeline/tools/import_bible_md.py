#!/usr/bin/env python3
"""Опционально: markdown-библия -> JSON-файлы пакета v2 через Anthropic (не генерация, но платный LLM-вызов; нужен --live).
  python tools/import_bible_md.py path/to/BIBLE.md ../series/<series_id>
Результат надо проверить глазами и дополнить series.json/episodes вручную."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serial.config import Config  # noqa: E402
from serial.llm import LLM  # noqa: E402

if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    cfg = Config.load(Path(__file__).resolve().parents[1], live=True)
    out = LLM(cfg, print).import_bible_markdown(src.read_text(encoding="utf-8"))
    (dst / "bible").mkdir(parents=True, exist_ok=True)
    for name in ("characters", "locations", "props", "relationships", "secrets", "style"):
        (dst / "bible" / f"{name}.json").write_text(json.dumps(out.get(name, [] if name != "style" else {}), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written to {dst}/bible/. Now create series.json and episodes/, then: python run_episode.py --validate {dst}")
