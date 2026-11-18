"""All runtime paths stay inside the project directory."""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
LOG_DIR = ROOT_DIR / "logs"
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
OUTBOX_DIR = DATA_DIR / "outbox"
MAIL_CONFIG_FILE = DATA_DIR / "mail_config"
DB_PATH = DATA_DIR / "app.db"
SECRET_KEY_FILE = DATA_DIR / "secret_key"
ADMIN_PASSWORD_FILE = DATA_DIR / "admin_initial_password.txt"
API_TOKEN_FILE = DATA_DIR / "api_token.txt"
PID_FILE = DATA_DIR / "server.pid"
LOG_FILE = LOG_DIR / "app.log"


def ensure_runtime_dirs(data_dir: Path | None = None, log_dir: Path | None = None) -> None:
    data = data_dir or DATA_DIR
    logs = log_dir or LOG_DIR
    data.mkdir(parents=True, exist_ok=True)
    (data / "uploads").mkdir(parents=True, exist_ok=True)
    (data / "exports").mkdir(parents=True, exist_ok=True)
    (data / "outbox").mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
