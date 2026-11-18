#!/usr/bin/env python3
"""Measure page response time on a throw-away database with realistic data volume.

Never touches data/app.db, production credentials, the tunnel or real mail.
All demo passwords are random and kept in memory only.

Usage:
  .venv/bin/python scripts/benchmark_pages.py --out docs/perf_before.json
  .venv/bin/python scripts/benchmark_pages.py --out docs/perf_after.json --browser
  .venv/bin/python scripts/benchmark_pages.py --compare docs/perf_before.json docs/perf_after.json

Two measurements per page:
  * server: waitress on 127.0.0.1, sequential keep-alive HTTP, median/p95 of N runs,
    SQL statements per request and HTML size (raw / gzip on the wire).
  * browser (--browser): Chromium with 150 ms simulated RTT (like the Cloudflare tunnel),
    repeated navigation to the same page with a warm cache; time until the load event.
"""
from __future__ import annotations

import argparse
import gzip
import http.client
import json
import os
import random
import re
import secrets
import statistics
import sys
import tempfile
import threading
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".playwright-browsers"))
os.environ["AUTO_REFRESH_SECONDS"] = "0"  # no background reloads while a script drives the browser
(ROOT / ".runtime-tests").mkdir(exist_ok=True)
os.environ["TMPDIR"] = str(ROOT / ".runtime-tests")
tempfile.tempdir = None

RUNS = 15
CAPTAIN_PAGES = [
    ("/captain", "队长首页"),
    ("/bookings/new", "测试预约表单"),
    ("/bookings", "我的预约"),
    ("/records", "申请记录（测试）"),
    ("/records?tab=tools", "申请记录（工具）"),
    ("/tools", "我的工具借用"),
    ("/tools/new", "工具借用登记"),
    ("/schedule", "占用视图（日）"),
    ("/schedule?view=week", "占用视图（周）"),
    ("/password", "修改密码"),
]
ADMIN_PAGES = [
    ("/", "管理员首页"),
    ("/review/", "申请审核（待审核）"),
    ("/review/?status=all", "申请审核（全部）"),
    ("/tools/admin", "工具借用记录"),
    ("/stats/", "使用统计"),
    ("/noshows/", "未签到统计"),
    ("/mail/", "邮件记录"),
    ("/labs/", "实验室管理"),
    ("/checkin-codes/", "签到二维码"),
]
SUPER_PAGES = [
    ("/", "总管理员首页"),
    ("/security/", "安全防护中心"),
    ("/people/", "人员信息管理"),
    ("/managers/", "管理员管理"),
    ("/activity/users", "普通人员操作详情"),
    ("/activity/admins", "管理员操作信息"),
    ("/users/", "账号管理"),
    ("/duties/", "值班安排"),
    ("/tools/catalog", "工具清单"),
]
PUBLIC_PAGES = [("/login", "队长登录页"), ("/admin/login", "管理员登录页")]
BROWSER_PAGES = [
    ("anon", "/login", "队长登录页"),
    ("captain", "/captain", "队长首页"),
    ("captain", "/bookings/new", "测试预约表单"),
    ("captain", "/records", "申请记录"),
    ("captain", "/records?tab=tools", "申请记录（工具）"),
    ("admin", "/", "管理员首页"),
    ("admin", "/review/?status=all", "申请审核（全部）"),
    ("admin", "/tools/admin", "工具借用记录"),
    ("super", "/", "总管理员首页"),
    ("super", "/security/", "安全防护中心"),
]


# --------------------------------------------------------------------------- seed

def build(directory: Path):
    from app import create_app, db
    from app.models import (AppSetting, AuditLog, Booking, BookingCompanion, DutyShift, Lab, LoginLog,
                            MailLog, SecurityEvent, TeamMember, ToolCatalog, ToolLoan, ToolLoanItem, User)
    from app.utils import local_now
    from werkzeug.security import generate_password_hash

    app = create_app({"TESTING": True, "DATA_DIR": str(directory / "data"), "LOG_DIR": str(directory / "logs"),
                      "SECRET_KEY": secrets.token_hex(32), "API_TOKEN": secrets.token_hex(32),
                      "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(directory / "bench.db")})
    rnd = random.Random(20261113)
    creds = {}
    with app.app_context():
        now = local_now().replace(second=0, microsecond=0)
        db.session.get(AppSetting, "checkin_enabled_at").value = now.isoformat(timespec="seconds")
        captain_pw = secrets.token_urlsafe(12) + "Aa!"
        captain_hash = generate_password_hash(captain_pw)
        creds["captain"] = ("BENCH001", captain_pw)
        staff = []
        for i, (sid, name, role) in enumerate([("BSUPER", "总管理员", "superadmin"), ("BADMIN1", "李老师", "admin"),
                                               ("BADMIN2", "王老师", "admin"), ("BADMIN3", "赵老师", "admin")]):
            pw = secrets.token_urlsafe(16) + "Aa!9"
            user = User(student_id=sid, name=name, role=role, contact_phone="1390000000%d" % i)
            user.set_password(pw, require_change=False)
            db.session.add(user)
            staff.append(user)
            creds[sid] = pw
        creds["admin"] = creds["BADMIN1"]
        creds["super"] = creds["BSUPER"]
        labs = [Lab(name=n, capacity=12, open_start=time(8, 0), open_end=time(22, 0), enabled=True,
                    contact_name="实验室老师", contact_phone="13911112222")
                for n in ("结构测试实验室", "材料力学实验室", "振动台实验室")]
        db.session.add_all(labs)
        tools = [ToolCatalog(name=n, total_quantity=q, is_major=True)
                 for n, q in (("电钻", 6), ("游标卡尺", 12), ("热熔胶枪", 10), ("美工刀", 20))]
        db.session.add_all(tools)
        db.session.flush()
        captains = []
        for i in range(1, 101):
            u = User(student_id=f"BENCH{i:03d}", name=f"队长{i:03d}", role="member", password_hash=captain_hash,
                     team_name=f"第{i}结构队", last_phone="13800138000", last_email=f"c{i}@example.com")
            db.session.add(u)
            captains.append(u)
        db.session.flush()
        members = {}
        for u in captains:
            members[u.id] = []
            for j in range(6):
                m = TeamMember(captain_id=u.id, name=f"队员{u.id}-{j}", student_id=f"M{u.id:03d}{j}", phone="13700000000")
                db.session.add(m)
                members[u.id].append(m)
        db.session.flush()
        statuses = ["pending"] * 3 + ["approved"] * 5 + ["rejected", "cancelled"]
        for k in range(3000):
            u = captains[0] if k % 75 == 0 else rnd.choice(captains)
            day = (now + timedelta(days=rnd.randint(-45, 30))).date()
            start = time(rnd.randint(8, 20), rnd.choice((0, 30)))
            end = (datetime.combine(day, start) + timedelta(minutes=60)).time()
            status = rnd.choice(statuses)
            b = Booking(lab=rnd.choice(labs), user=u, date=day, start_time=start, end_time=end, purpose="模型承载测试",
                        people_count=3, captain_phone="13800138000", captain_email="c@example.com", status=status,
                        created_at=now - timedelta(minutes=k),
                        reviewed_by_id=staff[1].id if status in ("approved", "rejected") else None,
                        reject_reason="时段冲突" if status == "rejected" else None,
                        checked_in_at=(datetime.combine(day, start) if status == "approved" and day < now.date() and rnd.random() < .7 else None))
            db.session.add(b)
            for m in rnd.sample(members[u.id], 2):
                db.session.add(BookingCompanion(booking=b, source="member", name=m.name, student_id=m.student_id,
                                                team_member_id=m.id))
        for k in range(800):
            u = captains[0] if k % 50 == 0 else rnd.choice(captains)
            at = now - timedelta(hours=rnd.randint(1, 1000))
            returned = rnd.random() < .8
            loan = ToolLoan(user=u, lab=rnd.choice(labs), team_name=u.team_name, contact="13800138000", borrow_at=at,
                            expected_return_at=at + timedelta(hours=4), actual_return_at=at + timedelta(hours=3) if returned else None,
                            borrow_photo=f"bench_photo_{k % 40}.png", return_photo=f"bench_photo_{(k + 7) % 40}.png" if returned else None,
                            status="returned" if returned else "borrowing")
            db.session.add(loan)
            for t in rnd.sample(tools, 2):
                db.session.add(ToolLoanItem(loan=loan, catalog_id=t.id, name=t.name, quantity=1))
        for k in range(8000):
            u = rnd.choice(captains + staff)
            db.session.add(LoginLog(created_at=now - timedelta(minutes=k * 3), user_id=u.id, student_id=u.student_id,
                                    role=u.role, ip=f"10.0.{k % 50}.{k % 200}", user_agent="Mozilla/5.0", device_id=f"dev{k % 300}",
                                    browser="Chrome", os_name="Android", success=rnd.random() < .9))
        for k in range(6000):
            u = rnd.choice(captains + staff)
            db.session.add(AuditLog(created_at=now - timedelta(minutes=k * 4), actor_id=u.id, actor_student_id=u.student_id,
                                    actor_role=u.role, ip=f"10.0.{k % 50}.{k % 200}", action="booking.create", summary="提交测试申请。"))
        for k in range(4000):
            db.session.add(SecurityEvent(created_at=now - timedelta(minutes=k * 5), event_type=rnd.choice(["crawl", "path_scan", "brute_force", "forbidden"]),
                                         severity=rnd.choice(["low", "medium", "high", "critical"]), ip=f"203.0.113.{k % 250}",
                                         path="/wp-admin", method="GET", read_at=None if k < 40 else now))
        for k in range(300):
            db.session.add(MailLog(created_at=now - timedelta(hours=k), recipient=f"c{k}@example.com", subject="预约未通过", body="……", status="local"))
        for k in range(30):
            db.session.add(DutyShift(lab=labs[k % 3], admin=staff[1 + k % 3], weekday=k % 7, start_time=time(8 + k % 6, 0), end_time=time(14 + k % 6, 0)))
        db.session.commit()
    uploads = directory / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    # 40 distinct ~300 KB phone-photo stand-ins (content is irrelevant, size matters).
    import base64
    tiny = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    for k in range(40):
        (uploads / f"bench_photo_{k}.png").write_bytes(tiny + os.urandom(300_000))
    return app, creds


# --------------------------------------------------------------------------- http

class Client:
    def __init__(self, port: int, gzip_ok: bool = True):
        self.port = port
        self.cookies: dict[str, str] = {}
        self.gzip_ok = gzip_ok
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)

    def request(self, method, path, body=None, headers=None):
        hdrs = {"User-Agent": "Mozilla/5.0 (bench)", "Accept": "text/html"}
        if self.gzip_ok:
            hdrs["Accept-Encoding"] = "gzip"
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        hdrs.update(headers or {})
        for attempt in range(2):
            try:
                self.conn.request(method, path, body=body, headers=hdrs)
                resp = self.conn.getresponse()
                data = resp.read()
                break
            except (http.client.HTTPException, ConnectionError):
                self.conn.close()
                self.conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
                if attempt:
                    raise
        for key, value in resp.getheaders():
            if key.lower() == "set-cookie":
                name, _, rest = value.partition("=")
                self.cookies[name.strip()] = rest.split(";", 1)[0]
        return resp, data

    def get(self, path):
        return self.request("GET", path)

    def post_form(self, path, fields):
        from urllib.parse import urlencode
        return self.request("POST", path, urlencode(fields), {"Content-Type": "application/x-www-form-urlencoded"})


def csrf_from(html: bytes) -> str:
    m = re.search(rb'name="csrf_token" value="([^"]+)"', html)
    return m.group(1).decode() if m else ""


def body_text(resp, data) -> bytes:
    if (resp.getheader("Content-Encoding") or "") == "gzip":
        return gzip.decompress(data)
    return data


def login(client: Client, role: str, creds):
    if role == "captain":
        resp, data = client.get("/login")
        sid, pw = creds["captain"]
        resp, _ = client.post_form("/login", {"csrf_token": csrf_from(body_text(resp, data)), "student_id": sid, "password": pw})
    else:
        resp, data = client.get("/admin/login")
        resp, _ = client.post_form("/admin/login", {"csrf_token": csrf_from(body_text(resp, data)), "password": creds[role]})
    assert resp.status in (302, 303), (role, resp.status)


class QueryCounter:
    def __init__(self, engine):
        from sqlalchemy import event
        self.count = 0
        self.lock = threading.Lock()
        event.listen(engine, "before_cursor_execute", self._hit)

    def _hit(self, *_a, **_k):
        with self.lock:
            self.count += 1


def measure(client: Client, path: str, counter: QueryCounter, runs: int = RUNS):
    times, queries = [], []
    resp, data = client.get(path)  # warm-up (template compile, caches)
    assert resp.status == 200, (path, resp.status)
    for _ in range(runs):
        before = counter.count
        t0 = _time.perf_counter()
        resp, data = client.get(path)
        times.append((_time.perf_counter() - t0) * 1000)
        queries.append(counter.count - before)
        assert resp.status == 200, (path, resp.status)
    raw = body_text(resp, data)
    times.sort()
    return {
        "median_ms": round(statistics.median(times), 2),
        "p95_ms": round(times[max(0, int(len(times) * .95) - 1)], 2),
        "queries": int(statistics.median(queries)),
        "html_bytes": len(raw),
        "wire_bytes": len(data),
        "content_encoding": resp.getheader("Content-Encoding") or "",
        "cache_control": resp.getheader("Cache-Control") or "",
    }


def asset_urls(html: bytes) -> list[str]:
    urls = re.findall(rb'<(?:link[^>]+href|script[^>]+src|img[^>]+src)="(/[^"]+)"', html)
    return sorted({u.decode().replace("&amp;", "&") for u in urls})


def run_server_bench(app, creds, port):
    from app import db
    with app.app_context():
        counter = QueryCounter(db.engine)
    results = {"pages": [], "assets": []}
    anon = Client(port)
    for path, label in PUBLIC_PAGES:
        results["pages"].append({"role": "anon", "path": path, "label": label, **measure(anon, path, counter)})
    _, html = anon.get("/login")
    for role, pages in (("captain", CAPTAIN_PAGES), ("admin", ADMIN_PAGES), ("super", SUPER_PAGES)):
        c = Client(port)
        login(c, role, creds)
        for path, label in pages:
            results["pages"].append({"role": role, "path": path, "label": label, **measure(c, path, counter)})
            print(f"  {role:8s} {path:22s} {results['pages'][-1]['median_ms']:8.2f} ms  q={results['pages'][-1]['queries']}")
        if role == "captain":
            captain_html = body_text(*c.get("/records?tab=tools"))
            assets = set(asset_urls(body_text(*anon.get("/login")))) | set(asset_urls(captain_html))
            static = [u for u in sorted(assets) if "/media/" not in u]
            photos = [u for u in sorted(assets) if "/media/" in u][:1]  # one sample photo is enough
            for url in static + photos:
                resp, data = c.get(url)
                results["assets"].append({"url": url.split("?")[0],
                                          "status": resp.status, "wire_bytes": len(data),
                                          "content_encoding": resp.getheader("Content-Encoding") or "",
                                          "cache_control": resp.getheader("Cache-Control") or "",
                                          "etag": bool(resp.getheader("ETag"))})
            results["photo_requests_on_tools_tab"] = len(re.findall(rb'<img[^>]+/media/', captain_html))
            results["photo_lazy"] = bool(re.search(rb'<img[^>]+loading="lazy"[^>]*/media/|<img[^>]+/media/[^>]+loading="lazy"', captain_html))
    # Captain name lookup (debounced XHR on the login page)
    t = []
    for _ in range(8):
        t0 = _time.perf_counter()
        resp, _ = anon.request("GET", "/login/captain-name?student_id=BENCH001", headers={"Accept": "application/json"})
        t.append((_time.perf_counter() - t0) * 1000)
    results["name_lookup_ms"] = round(statistics.median(t), 2)
    # Login POST (password hash check)
    for role in ("captain", "admin"):
        samples = []
        for _ in range(3):
            c = Client(port)
            t0 = _time.perf_counter()
            login(c, role, creds)
            samples.append((_time.perf_counter() - t0) * 1000)
        results[f"login_{role}_ms"] = round(statistics.median(samples), 2)
    return results


def run_browser_bench(port, creds, rtt_ms=150):
    from playwright.sync_api import sync_playwright
    base = f"http://127.0.0.1:{port}"
    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for role in ("anon", "captain", "admin", "super"):
            ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, locale="zh-CN")
            page = ctx.new_page()
            cdp = ctx.new_cdp_session(page)
            cdp.send("Network.enable")
            if role == "anon":
                pass
            elif role == "captain":
                page.goto(base + "/login")
                page.fill("#student_id", creds["captain"][0])
                page.fill("#password", creds["captain"][1])
            else:
                page.goto(base + "/admin/login")
                page.fill("#password", creds[role])
            if role != "anon":
                page.locator("button[type=submit]").first.click()
                page.wait_for_load_state("load")
            # ~150 ms RTT and 10 Mbit/s like a phone on the tunnel.
            cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": rtt_ms,
                                                           "downloadThroughput": 10_000_000 / 8, "uploadThroughput": 5_000_000 / 8})
            for r, path, label in BROWSER_PAGES:
                if r != role:
                    continue
                samples, reqs = [], []
                for i in range(6):
                    counter = {"n": 0}
                    handler = lambda _req, c=counter: c.__setitem__("n", c["n"] + 1)
                    page.on("request", handler)
                    t0 = _time.perf_counter()
                    page.goto(base + path, wait_until="load")
                    elapsed = (_time.perf_counter() - t0) * 1000
                    page.remove_listener("request", handler)
                    if i:  # first visit fills the cache; measure warm navigations
                        samples.append(elapsed)
                        reqs.append(counter["n"])
                out.append({"role": role, "path": path, "label": label,
                            "load_ms": round(statistics.median(samples), 1), "requests": int(statistics.median(reqs))})
                print(f"  browser {role:8s} {path:22s} {out[-1]['load_ms']:8.1f} ms  req={out[-1]['requests']}")
            ctx.close()
        browser.close()
    return out


def main_measure(args):
    from waitress.server import create_server
    with tempfile.TemporaryDirectory(prefix=".bench-", dir=ROOT / ".runtime-tests") as temp:
        app, creds = build(Path(temp))
        server = create_server(app, host="127.0.0.1", port=0, threads=8, ident="bench")
        port = server.effective_port
        threading.Thread(target=server.run, daemon=True).start()
        try:
            with patch("app.mailer.send_via_smtp", side_effect=AssertionError("no real mail")):
                print("server-side measurements …")
                results = run_server_bench(app, creds, port)
                if args.browser:
                    print("browser measurements (150 ms RTT) …")
                    results["browser"] = run_browser_bench(port, creds)
        finally:
            server.close()
            from app import db
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
    results["measured_at"] = datetime.now().isoformat(timespec="seconds")
    results["runs_per_page"] = RUNS
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written", args.out)


def main_compare(before_path, after_path):
    before = json.loads(Path(before_path).read_text(encoding="utf-8"))
    after = json.loads(Path(after_path).read_text(encoding="utf-8"))
    key = lambda p: (p["role"], p["path"])
    b = {key(p): p for p in before["pages"]}
    role_label = {"anon": "未登录", "captain": "队长", "admin": "管理员", "super": "总管理员"}
    lines = ["| 角色 | 页面 | 路径 | 优化前 中位数 ms | 优化后 中位数 ms | 变化 | SQL 次数 前→后 | 传输字节 前→后 |",
             "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for p in after["pages"]:
        o = b.get(key(p))
        if not o:
            continue
        change = (p["median_ms"] - o["median_ms"]) / o["median_ms"] * 100 if o["median_ms"] else 0
        lines.append(f"| {role_label[p['role']]} | {p['label']} | `{p['path']}` | {o['median_ms']:.1f} | {p['median_ms']:.1f} | "
                     f"{change:+.0f}% | {o['queries']}→{p['queries']} | {o['wire_bytes']:,}→{p['wire_bytes']:,} |")
    if before.get("browser") and after.get("browser"):
        bb = {key(p): p for p in before["browser"]}
        lines += ["", "| 角色 | 页面（浏览器，150 ms RTT，热缓存） | 优化前 ms | 优化后 ms | 变化 | 请求数 前→后 |",
                  "| --- | --- | ---: | ---: | ---: | ---: |"]
        for p in after["browser"]:
            o = bb.get(key(p))
            if not o:
                continue
            change = (p["load_ms"] - o["load_ms"]) / o["load_ms"] * 100 if o["load_ms"] else 0
            lines.append(f"| {role_label[p['role']]} | {p['label']} `{p['path']}` | {o['load_ms']:.0f} | {p['load_ms']:.0f} | {change:+.0f}% | {o['requests']}→{p['requests']} |")
    print("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--compare", nargs=2)
    args = parser.parse_args()
    if args.compare:
        main_compare(*args.compare)
    else:
        if not args.out:
            parser.error("--out is required")
        main_measure(args)
