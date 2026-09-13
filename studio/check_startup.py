"""Offline boot gate for the production image, independent of pytest/PYTHONPATH.

Copy only application code to a disposable layout matching /app/studio and
/app/pipeline. No runtime credentials, dotenv files, records or generated
media are inherited. Import the actual Uvicorn target and run its lifespan.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


CHECK = r'''
from pathlib import Path
from fastapi.testclient import TestClient
from uvicorn import Config

config = Config("app.main:app", lifespan="on")
config.load()
import serial
assert Path(serial.__file__).resolve().parent == Path.cwd().parent / "pipeline" / "serial"

with TestClient(config.loaded_app) as client:
    response = client.get("/healthz")
    assert response.status_code == 200, response.status_code
    health = response.json()
    assert health["ok"] is True
    assert health["mode"] == "mock"
    assert health["store"] == "local"
    assert health["paid_calls_enabled"] is False
    assert health["configuration_problems"] == 0
    assert client.get("/login").status_code == 200

print("Production startup check passed: Uvicorn import, lifespan, health and sign-in page.")
'''


def main() -> int:
    studio = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="studio-startup-") as tmp:
        layout = Path(tmp)
        for source, destination in (
            (studio / "app", layout / "studio" / "app"),
            (studio.parent / "pipeline" / "serial", layout / "pipeline" / "serial"),
        ):
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))
        result = subprocess.run(
            [sys.executable, "-c", CHECK],
            cwd=layout / "studio",
            # Deliberately do not copy os.environ: CI's PYTHONPATH can conceal
            # broken engine imports, and live settings must never reach a check.
            env={
                "STUDIO_STORE": "local",
                "STUDIO_BASE_PATH": "",
                "STUDIO_ALLOW_PAID": "false",
                "PIPELINE_ALLOW_PAID": "false",
                "STUDIO_ALLOW_CONNECTION_TESTS": "false",
                "STUDIO_SESSION_SECRET": "offline-startup-check",
                "STUDIO_ADMIN_EMAIL": "startup@example.test",
                "STUDIO_ADMIN_PASSWORD": "offline-startup-check",
                "PYTHONUNBUFFERED": "1",
            },
            timeout=30,
            check=False,
        )
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
