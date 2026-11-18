"""Short-lived import previews stay server-side; session cookies contain only a random id."""
import json
import re
import secrets
import time
from pathlib import Path

from flask import current_app, session

MAX_AGE = 1800


def _directory():
    path = Path(current_app.config['DATA_DIR']) / 'import_previews'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def save_preview(rows):
    directory = _directory()
    now = time.time()
    for path in directory.glob('*.json'):
        if now - path.stat().st_mtime > MAX_AGE:
            path.unlink(missing_ok=True)
    old = session.pop('people_import_preview', None)
    if isinstance(old, str) and re.fullmatch(r'[a-f0-9]{64}', old):
        (directory / (old + '.json')).unlink(missing_ok=True)
    key = secrets.token_hex(32)
    path = directory / (key + '.json')
    with path.open('x', encoding='utf-8') as stream:
        path.chmod(0o600)
        json.dump(rows, stream, ensure_ascii=False)
    session['people_import_preview'] = key


def consume_preview():
    key = session.pop('people_import_preview', None)
    if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key):
        return None
    path = _directory() / (key + '.json')
    try:
        if time.time() - path.stat().st_mtime > MAX_AGE:
            return None
        rows = json.loads(path.read_text(encoding='utf-8'))
        return rows if isinstance(rows, list) else None
    except (OSError, ValueError):
        return None
    finally:
        path.unlink(missing_ok=True)
