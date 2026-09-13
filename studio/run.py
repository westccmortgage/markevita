#!/usr/bin/env python3
"""Start the MarkeVita AI Series Studio.

    python run.py            # http://127.0.0.1:8800
    python run.py --reload   # auto-reload while developing
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=settings.host)
    ap.add_argument("--port", type=int, default=settings.port)
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args()

    if settings.allow_paid:
        sys.exit(
            "Refusing to start: STUDIO_ALLOW_PAID is true.\n"
            "This build is mock-only. Set STUDIO_ALLOW_PAID=false in studio/.env."
        )

    print(f"MarkeVita AI Series Studio  ·  mode={settings.mode}  store={settings.store_driver}")
    print(f"Admin panel: http://{a.host}:{a.port}")

    import uvicorn
    uvicorn.run("app.main:app", host=a.host, port=a.port, reload=a.reload)


if __name__ == "__main__":
    main()
