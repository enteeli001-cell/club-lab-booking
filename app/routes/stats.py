from datetime import timedelta
from io import BytesIO

from flask import Blueprint, flash, render_template, request, send_file
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from app import db
from app.decorators import stats_required
from app.models import Booking, Lab, User
from app.queries import booking_list_options
from app.utils import parse_date, today

stats_bp = Blueprint("stats", __name__, url_prefix="/stats")


def _date_range():
    default_end = today()
    default_start = default_end - timedelta(days=30)
    start = parse_date(request.values.get("start_date")) or default_start
    end = parse_date(request.values.get("end_date")) or default_end
    if start > end:
        start, end = end, start
    lab_id = request.values.get("lab_id", type=int)
    user_id = request.values.get("user_id", type=int)
    return start, end, lab_id, user_id


def _checkin_filter():
    value = request.values.get("checkin", "")
    return value if value in ("missing", "done") else ""


def _filtered_bookings(start, end, lab_id, user_id, status="approved", checkin=""):
    query = Booking.query.options(*booking_list_options(user=True)).filter(
        Booking.date >= start,
        Booking.date <= end,
        Booking.status == status,
    )
    if lab_id:
        query = query.filter(Booking.lab_id == lab_id)
    if user_id:
        query = query.filter(Booking.user_id == user_id)
    if checkin == "missing":
        query = query.filter(Booking.checked_in_at.is_(None))
    elif checkin == "done":
        query = query.filter(Booking.checked_in_at.isnot(None))
    return query.order_by(Booking.date.asc(), Booking.start_time.asc()).all()


def _summarize(bookings):
    by_lab = {}
    by_user = {}
    for booking in bookings:
        lab_row = by_lab.setdefault(
            booking.lab_id,
            {"name": booking.lab.name, "count": 0, "minutes": 0},
        )
        lab_row["count"] += 1
        lab_row["minutes"] += booking.duration_minutes

        user_row = by_user.setdefault(
            booking.user_id,
            {
                "name": booking.user.name,
                "student_id": booking.user.student_id,
                "count": 0,
                "minutes": 0,
            },
        )
        user_row["count"] += 1
        user_row["minutes"] += booking.duration_minutes

    lab_rows = sorted(by_lab.values(), key=lambda r: r["name"])
    user_rows = sorted(by_user.values(), key=lambda r: r["student_id"])
    totals = {
        "count": sum(r["count"] for r in lab_rows),
        "minutes": sum(r["minutes"] for r in lab_rows),
    }
    return lab_rows, user_rows, totals


def _minutes_label(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}小时{mins}分钟"
    if hours:
        return f"{hours}小时"
    return f"{mins}分钟"


@stats_bp.route("/")
@stats_required
def index():
    start, end, lab_id, user_id = _date_range()
    checkin = _checkin_filter()
    bookings = _filtered_bookings(start, end, lab_id, user_id, checkin=checkin)
    lab_rows, user_rows, totals = _summarize(bookings)
    return render_template(
        "stats/index.html",
        start=start,
        end=end,
        lab_id=lab_id or "",
        user_id=user_id or "",
        checkin=checkin,
        labs=Lab.query.order_by(Lab.name.asc()).all(),
        users=User.query.order_by(User.student_id.asc()).all(),
        lab_rows=lab_rows,
        user_rows=user_rows,
        totals=totals,
        bookings=bookings,
        minutes_label=_minutes_label,
    )


@stats_bp.route("/export")
@stats_required
def export_excel():
    start, end, lab_id, user_id = _date_range()
    checkin = _checkin_filter()
    bookings = _filtered_bookings(start, end, lab_id, user_id, checkin=checkin)
    lab_rows, user_rows, totals = _summarize(bookings)
    if not bookings and request.args.get("allow_empty") != "1":
        # Still allow empty export; just produce headers.
        pass

    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2F5D62")
    header_align = Alignment(horizontal="center")

    def style_header(ws):
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
        for col in range(1, ws.max_column + 1):
            ws.column_dimensions[get_column_letter(col)].width = 18

    ws1 = wb.active
    ws1.title = "按实验室"
    ws1.append(["实验室", "使用次数", "使用时长（分钟）", "使用时长"])
    for row in lab_rows:
        ws1.append([row["name"], row["count"], row["minutes"], _minutes_label(row["minutes"])])
    ws1.append(["合计", totals["count"], totals["minutes"], _minutes_label(totals["minutes"])])
    style_header(ws1)

    ws2 = wb.create_sheet("按成员")
    ws2.append(["姓名", "学号", "使用次数", "使用时长（分钟）", "使用时长"])
    for row in user_rows:
        ws2.append([row["name"], row["student_id"], row["count"], row["minutes"], _minutes_label(row["minutes"])])
    ws2.append(["合计", "", totals["count"], totals["minutes"], _minutes_label(totals["minutes"])])
    style_header(ws2)

    ws3 = wb.create_sheet("明细")
    ws3.append(["日期", "实验室", "姓名", "学号", "开始", "结束", "时长（分钟）", "人数", "用途", "状态", "队长联系方式", "同行人员", "签到状态", "签到时间"])
    for b in bookings:
        ws3.append([
            b.date.isoformat(),
            b.lab.name,
            b.user.name,
            b.user.student_id,
            b.start_time.strftime("%H:%M"),
            b.end_time.strftime("%H:%M"),
            b.duration_minutes,
            b.people_count,
            b.purpose,
            b.status_label,
            b.captain_phone or "",
            b.companions_label,
            (b.checkin or {}).get("short", ""),
            b.checked_in_at.strftime("%Y-%m-%d %H:%M:%S") if b.checked_in_at else "",
        ])
    style_header(ws3)

    ws4 = wb.create_sheet("筛选条件")
    ws4.append(["项目", "值"])
    ws4.append(["开始日期", start.isoformat()])
    ws4.append(["结束日期", end.isoformat()])
    lab_name = "全部"
    if lab_id:
        lab = db.session.get(Lab, lab_id)
        lab_name = lab.name if lab else str(lab_id)
    user_name = "全部"
    if user_id:
        user = db.session.get(User, user_id)
        user_name = f"{user.name}（{user.student_id}）" if user else str(user_id)
    ws4.append(["实验室", lab_name])
    ws4.append(["成员", user_name])
    ws4.append(["签到筛选", {"missing": "只看未签到", "done": "只看已签到"}.get(checkin, "全部")])
    style_header(ws4)

    buf = BytesIO()
    from app.utils import protect_excel_text
    protect_excel_text(wb)
    wb.save(buf)
    buf.seek(0)
    filename = f"lab-usage-{start.isoformat()}-to-{end.isoformat()}.xlsx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
