from datetime import date, time
from pathlib import Path

import pytest

from app import create_app, db
from app.models import Booking, Lab, User

FUTURE_DAY = date(2099, 6, 15)
API_TOKEN = "test-api-token"
STRONG_PASSWORD = "Valid#1a"
SUPER_PASSWORD = "SuperAdmin#12x"
ADMIN_PASSWORD = "LabAdmin#12yz"
STAFF_NEW_PASSWORD = "NewStaff#12ab"
CAPTAIN_PHONE = "13800138000"
CAPTAIN_EMAIL = "captain@example.com"


@pytest.fixture
def app(tmp_path):
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(data_dir),
            "LOG_DIR": str(log_dir),
            "DB_PATH": str(tmp_path / "test.db"),
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
            "SECRET_KEY": "test-secret-key",
            "API_TOKEN": API_TOKEN,
            "WTF_CSRF_ENABLED": False,
            # Legacy tests log captains in directly; tests/test_profile_setup.py turns the gate on.
            "PROFILE_COMPLETION_REQUIRED": False,
        }
    )
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _functional_tests_use_simplified_chinese(monkeypatch):
    """Keep existing behavior tests on Simplified Chinese.

    English is the visitor default. Language tests delete this cookie and cover that path.
    Mail sent with no request still uses English. This must not request the app fixture,
    or tests that own their temp directory would see it already populated.
    """
    from flask.testing import FlaskClient

    original = FlaskClient.__init__

    def wrapped(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.set_cookie("lang", "zh-Hans")

    monkeypatch.setattr(FlaskClient, "__init__", wrapped)


def create_user(student_id, name, role, password="password123"):
    user = User(student_id=student_id, name=name, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def create_lab(name="创新实验室", capacity=10, open_start=time(8, 0), open_end=time(22, 0), enabled=True):
    lab = Lab(name=name, capacity=capacity, open_start=open_start, open_end=open_end, enabled=enabled)
    db.session.add(lab)
    db.session.commit()
    return lab


def login(client, student_id, password=None, *, staff=False):
    if staff:
        if password is None:
            password = SUPER_PASSWORD if student_id == "admin" else ADMIN_PASSWORD
        return client.post(
            "/admin/login",
            data={"password": password},
            follow_redirects=True,
        )
    if password is None:
        password = "password123"
    return client.post(
        "/login",
        data={"student_id": student_id, "password": password},
        follow_redirects=True,
    )


def add_booking(
    lab,
    user,
    day=FUTURE_DAY,
    start=time(10, 0),
    end=time(11, 0),
    purpose="活动筹备",
    people_count=2,
    status="pending",
):
    booking = Booking(
        lab=lab,
        user=user,
        date=day,
        start_time=start,
        end_time=end,
        purpose=purpose,
        people_count=people_count,
        captain_phone=CAPTAIN_PHONE,
        captain_email=CAPTAIN_EMAIL,
        status=status,
    )
    db.session.add(booking)
    db.session.commit()
    return booking


@pytest.fixture
def seed(app):
    """Create a standard admin / officer / member and one lab. Returns ids only."""
    with app.app_context():
        admin = create_user("admin", "总管理员", "superadmin", SUPER_PASSWORD)
        officer = create_user("officer01", "李管理员", "admin", ADMIN_PASSWORD)
        member = create_user("2021001", "张三", "member")
        other = create_user("2021002", "王五", "member")
        lab = create_lab()
        return {
            "admin_id": admin.id,
            "officer_id": officer.id,
            "member_id": member.id,
            "other_id": other.id,
            "lab_id": lab.id,
        }


def api_headers(student_id, token=API_TOKEN):
    return {
        "Authorization": f"Bearer {token}",
        "X-Student-Id": student_id,
        "Content-Type": "application/json",
    }


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def booking_form(lab_id, **overrides):
    data = {
        "lab_id": str(lab_id),
        "date": FUTURE_DAY.isoformat(),
        "start_time": "10:00",
        "end_time": "11:00",
        "purpose": "测试预约",
        "people_count": "1",
        "captain_phone": CAPTAIN_PHONE,
        "captain_email": CAPTAIN_EMAIL,
    }
    for key, value in overrides.items():
        data[key] = value
    return data
