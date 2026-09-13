"""Initialize isolated test settings before any app module is imported."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'pipeline'), str(ROOT / 'studio')]
os.environ.update(STUDIO_STORE='local', STUDIO_ALLOW_PAID='false', PIPELINE_ALLOW_PAID='false',
                  STUDIO_SESSION_SECRET='offline-test-session-secret',
                  STUDIO_ADMIN_EMAIL='admin@example.test', STUDIO_ADMIN_PASSWORD='test-password')
