"""Shared configuration for persistent demo data, isolated from the production database."""
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEMO_DATA = ROOT / 'data' / 'testing'
DEMO_LOGS = ROOT / 'logs' / 'testing'


def create_demo_app():
    from app import create_app
    return create_app({'TESTING':True, 'DATA_DIR':str(DEMO_DATA), 'LOG_DIR':str(DEMO_LOGS),
                       'SECRET_KEY':secrets.token_hex(32), 'API_TOKEN':secrets.token_hex(32)})
