"""Site-wide explicit interface translations, selected by the lang cookie.

Source strings in code and templates are Simplified Chinese. English is the
default for new visitors, mail sent outside a request, and missing translations.
Users can switch language; the choice is stored in the lang cookie.

Translate only system-owned strings. User names, announcements, lab/tool names,
rejection reasons and other stored user content must never be passed to t().
"""

import re
import json
from pathlib import Path
from urllib.parse import urlencode
from string import Formatter

from flask import has_request_context, request, url_for

DEFAULT_LANG = "en"
SOURCE_LANG = "zh-Hans"
LANGUAGES = (
    ("en", "English"),
    ("zh-Hans", "简体中文"),
    ("zh-Hant", "繁體中文"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
)
LANG_CODES = tuple(code for code, _label in LANGUAGES)
HTML_LANG = {
    "en": "en",
    "zh-Hans": "zh-CN",
    "zh-Hant": "zh-Hant",
    "ja": "ja",
    "ko": "ko",
    "es": "es",
    "fr": "fr",
    "de": "de",
}
EXTRA_LANGS = ("ja", "ko", "es", "fr", "de")
LANG_COOKIE = "lang"
LANG_COOKIE_MAX_AGE = 365 * 24 * 3600


# source (简体) -> (繁體, English)
CATALOG = {
    # shared chrome
    "社团实验室预约管理系统": ("社團實驗室預約管理系統", "Club Lab Booking System"),
    "实验室预约与工具管理": ("實驗室預約與工具管理", "Lab booking & tool management"),
    "跳到主要内容": ("跳到主要內容", "Skip to main content"),
    "语言": ("語言", "Language"),
    "北京时间": ("北京時間", "Beijing time (UTC+8)"),
    "页面所有时间均为": ("頁面所有時間均為", "All times on this page are in "),
    "本预约系统对所有个人信息严格保密，仅用于本次竞赛的测试预约与签到管理，不会外传。所有个人信息将在正式比赛结束后统一销毁。": (
        "本預約系統對所有個人資訊嚴格保密，僅用於本次競賽的測試預約與簽到管理，不會外傳。所有個人資訊將在正式比賽結束後統一銷毀。",
        "All personal information is kept strictly confidential and used only for test booking and check-in for this competition. It will not be shared and will be destroyed after the competition ends.",
    ),
    # captain login page
    "平台介绍": ("平台介紹", "About the platform"),
    "社团实验室 · 预约服务": ("社團實驗室 · 預約服務", "Club Lab · Booking Services"),
    "专注结构探索": ("專注結構探索", "Focus on structural exploration"),
    "有序开展每次测试": ("有序開展每次測試", "Run every test in good order"),
    "预约时段、登记工具、跟进申请。": ("預約時段、登記工具、跟進申請。", "Book time slots, register tools, follow your applications."),
    "让实验室协作更清晰，让准备更从容。": ("讓實驗室協作更清晰，讓準備更從容。", "Clearer lab collaboration, calmer preparation."),
    "选择测试时段": ("選擇測試時段", "Pick a test slot"),
    "完善同行信息": ("完善同行資訊", "Add your teammates"),
    "等待管理员审核": ("等待管理員審核", "Wait for admin review"),
    "到场扫码签到": ("到場掃碼簽到", "Scan the QR code on site"),
    "单次测试不超过 1 小时 · 预约开始前请确认审核状态": ("單次測試不超過 1 小時 · 預約開始前請確認審核狀態", "Max 1 hour per test · Check the review status before your slot starts"),
    "登录后将自动返回": ("登入後將自動返回", "After login you will return to "),
    "扫码签到页面": ("掃碼簽到頁面", "the QR check-in page"),
    "刚才访问的页面": ("剛才造訪的頁面", "the page you were visiting"),
    "，首次登录需先修改初始密码，改完同样会回到签到页面": ("，首次登入需先修改初始密碼，改完同樣會回到簽到頁面", "; on first login you must change the initial password first, then you will be taken back to check-in"),
    "。": ("。", "."),
    "注意事项": ("注意事項", "Please note"),
    "仅允许参赛队长进行预约，队员请联系本队队长。": ("僅允許參賽隊長進行預約，隊員請聯絡本隊隊長。", "Only team captains may book; team members should contact their captain."),
    "请勿对本网站进行网络攻击、恶意扫描或刷接口，违者将取消预约资格并追究责任。": ("請勿對本網站進行網路攻擊、惡意掃描或刷介面，違者將取消預約資格並追究責任。", "Do not attack, scan or flood this site; offenders lose booking rights and will be held accountable."),
    "请妥善保管账号密码，勿借给他人。": ("請妥善保管帳號密碼，勿借給他人。", "Keep your account and password safe; do not lend them to others."),
    "预约需经管理员审核，请按时使用、如实填写。": ("預約需經管理員審核，請按時使用、如實填寫。", "Bookings need admin approval; be on time and fill in truthful information."),
    "正在登录…": ("正在登入…", "Logging in…"),
    "学号": ("學號", "Student ID"),
    "请输入本人学号": ("請輸入本人學號", "Enter your own student ID"),
    "密码": ("密碼", "Password"),
    "初始密码为学号": ("初始密碼為學號", "Initial password is your student ID"),
    "显示密码": ("顯示密碼", "Show password"),
    "隐藏密码": ("隱藏密碼", "Hide password"),
    "显示": ("顯示", "Show"),
    "隐藏": ("隱藏", "Hide"),
    "大写锁定已开启": ("大寫鎖定已開啟", "Caps Lock is on"),
    "正在查询…": ("正在查詢…", "Looking up…"),
    "参赛队长登录": ("參賽隊長登入", "Captain login"),
    "是否忘记密码？": ("是否忘記密碼？", "Forgot your password?"),
    "该学号已连续多次输入错误。如确系本人，可通过队伍信息验证后重置密码。": ("該學號已連續多次輸入錯誤。如確係本人，可透過隊伍資訊驗證後重設密碼。", "This student ID has had several failed attempts. If it is yours, you can reset the password after verifying your team information."),
    "忘记密码": ("忘記密碼", "Forgot password"),
    "管理员登录": ("管理員登入", "Admin login"),
    "账号由管理员添加或批量导入，不开放自助注册。未指定密码的队长，初始密码等于学号，首次登录须修改。": ("帳號由管理員新增或批次匯入，不開放自助註冊。未指定密碼的隊長，初始密碼等於學號，首次登入須修改。", "Accounts are created or imported by admins; there is no self-registration. Unless a password was assigned, a captain's initial password is the student ID and must be changed at first login."),
    "未找到该队长账号": ("未找到該隊長帳號", "No captain account found"),
    "查询过于频繁，请稍后再试。": ("查詢過於頻繁，請稍後再試。", "Too many lookups, please try again later."),
    "仅允许本人登录和预约，请勿代他人操作。": ("僅允許本人登入和預約，請勿代他人操作。", "Log in and book only for yourself; do not act on behalf of others."),
    # admin login page
    "管理员入口": ("管理員入口", "Admin entrance"),
    "总管理员与管理员均使用此入口，只需输入由总管理员下发的密码，无需学号。系统不提供忘记密码自助找回，如遗失请联系总管理员重置。": ("總管理員與管理員均使用此入口，只需輸入由總管理員下發的密碼，無需學號。系統不提供忘記密碼自助找回，如遺失請聯絡總管理員重設。", "Super admins and admins both use this entrance: enter only the password issued by the super admin, no student ID. There is no self-service recovery; if lost, ask the super admin to reset it."),
    "该 IP 已被暂时冻结": ("該 IP 已被暫時凍結", "This IP is temporarily locked"),
    "剩余冻结时间：": ("剩餘凍結時間：", "Time remaining: "),
    " 秒。冻结期间无法通过管理员入口登录，参赛队长入口不受影响。": (" 秒。凍結期間無法透過管理員入口登入，參賽隊長入口不受影響。", " s. Admin login is unavailable while locked; captain login is not affected."),
    "正在验证…": ("正在驗證…", "Verifying…"),
    "进入管理后台": ("進入管理後台", "Enter admin console"),
    "返回参赛队长登录": ("返回參賽隊長登入", "Back to captain login"),
    # messages shown on the login pages
    "学号或密码不正确。": ("學號或密碼不正確。", "Incorrect student ID or password."),
    "密码不正确。": ("密碼不正確。", "Incorrect password."),
    "该账号是管理员或总管理员，请使用下方的「管理员登录」入口。": ("該帳號是管理員或總管理員，請使用下方的「管理員登入」入口。", "This is an admin or super admin account; please use the “Admin login” link below."),
    "尝试过于频繁，请稍后再试。": ("嘗試過於頻繁，請稍後再試。", "Too many attempts, please try again later."),
    "已退出登录。": ("已登出。", "You have been logged out."),
    "请先登录": ("請先登入", "Please log in first"),
    "请先登录，登录后会自动回到签到页面。": ("請先登入，登入後會自動回到簽到頁面。", "Please log in first; you will be taken back to the check-in page afterwards."),
    "该账号当前不可用。": ("該帳號目前無法使用。", "This account is currently unavailable."),
    "队长登录：该功能已被管理员暂时关闭。": ("隊長登入：該功能已被管理員暫時關閉。", "Captain login: this feature has been temporarily disabled by the administrator."),
    "队长登录功能已被管理员暂时关闭，你已退出登录。": ("隊長登入功能已被管理員暫時關閉，你已登出。", "Captain login has been temporarily disabled by the administrator; you have been logged out."),
    "网站正在升级维护，暂时只有总管理员可以登录。": ("網站正在升級維護，暫時只有總管理員可以登入。", "The site is being upgraded; only the super admin can log in for now."),
}

# Keep the reviewed login translations; extend the same catalog across the site.
for _source, _pair in json.loads(Path(__file__).with_name("i18n_catalog.json").read_text(encoding="utf-8")).items():
    CATALOG.setdefault(_source, tuple(_pair))
# 材料领取与购买模块的界面文字。
for _source, _pair in json.loads(Path(__file__).with_name("i18n_materials.json").read_text(encoding="utf-8")).items():
    CATALOG.setdefault(_source, tuple(_pair))


def _as_lang_map(value) -> dict:
    if isinstance(value, dict):
        return {key: text for key, text in value.items() if isinstance(text, str) and text}
    hant, english = value
    return {"zh-Hant": hant, "en": english}


for _source, _pair in list(CATALOG.items()):
    CATALOG[_source] = _as_lang_map(_pair)


def _overlay_catalog(filename: str) -> None:
    path = Path(__file__).with_name(filename)
    if not path.exists():
        return
    for source, mapping in json.loads(path.read_text(encoding="utf-8")).items():
        entry = CATALOG.setdefault(source, {})
        if not isinstance(entry, dict):
            entry = _as_lang_map(entry)
            CATALOG[source] = entry
        for lang, text in _as_lang_map(mapping).items():
            entry.setdefault(lang, text)


_overlay_catalog("i18n_extra.json")
_overlay_catalog("i18n_locales.json")

# (pattern on the source text, 繁體 template, English template)
PATTERNS = (
    (re.compile(r"^登录失败次数过多，请 (\d+) 秒后再试。$"), "登入失敗次數過多，請 {0} 秒後再試。", "Too many failed logins, please try again in {0} seconds."),
    (re.compile(r"^(.+) 同学，您好$"), "{0} 同學，您好", "Hello, {0}"),
)

PATTERNS += (
    (re.compile(r"^使用人数不能超过实验室容量（(\d+) 人）。$"), "使用人數不能超過實驗室容量（{0} 人）。", "The number of people must not exceed the lab capacity ({0})."),
    (re.compile(r"^预约时间须在实验室开放时间内（(.+)）。$"), "預約時間須在實驗室開放時間內（{0}）。", "Bookings must be within lab opening hours ({0})."),
    (re.compile(r"^该时段值班管理员：(.+)$"), "該時段值班管理員：{0}", "Duty administrator for this slot: {0}"),
    (re.compile(r"^“(.+)”剩余可借 (\d+) 件，申请数量 (\d+) 超出库存，请调整后再提交。$"), "「{0}」剩餘可借 {1} 件，申請數量 {2} 超出庫存，請調整後再提交。", "{0}: {1} available; requested {2}. Reduce the quantity and try again."),
    (re.compile(r"^已通过 (.+) 的预约（(.+)）。$"), "已通過 {0} 的預約（{1}）。", "Approved {0}'s booking ({1})."),
    (re.compile(r"^已拒绝 (.+) 的预约。$"), "已拒絕 {0} 的預約。", "Rejected {0}'s booking."),
    (re.compile(r"^该特批账号已超过有效期。$"), "該特批帳號已超過有效期。", "This approved account has expired."),
)

PATTERNS += (
    (re.compile(r"^未到签到时间（(.+) 起可签到）$"), "未到簽到時間（{0} 起可簽到）", "Check-in opens at {0}"),
    (re.compile(r"^你在 (.+) 已经完成本次预约的签到，无需重复操作。$"), "你在 {0} 已經完成本次預約的簽到，無需重複操作。", "You checked in at {0}. No further action is needed."),
    (re.compile(r"^你今天在本实验室的预约是 (.+)，请在 (.+) 之后再扫码签到。$"), "你今天在本實驗室的預約是 {0}，請在 {1} 之後再掃碼簽到。", "Your booking here is {0}. Please scan after {1}."),
    (re.compile(r"^你今天 (.+) 的预约签到时间已结束，不能再补签。如有疑问请联系实验室管理员。$"), "你今天 {0} 的預約簽到時間已結束，不能再補簽。如有疑問請聯絡實驗室管理員。", "Check-in for your {0} booking today has closed. Contact the lab administrator if needed."),
    (re.compile(r"^你今天在「(.+)」没有已通过的预约。请先在系统中预约并等待管理员审核通过。$"), "你今天在「{0}」沒有已通過的預約。請先在系統中預約並等待管理員審核通過。", "You have no approved booking at {0} today. Submit a booking and wait for approval first."),
)

PATTERNS += (
    (re.compile(r"^已签到 (.+)$"), "已簽到 {0}", "Checked in at {0}"),
    (re.compile(r"^你的位置距离实验室约 (\d+) 米，超出签到范围（(\d+) 米）。请到实验室现场签到。$"), "你的位置距離實驗室約 {0} 米，超出簽到範圍（{1} 米）。請到實驗室現場簽到。", "You are about {0} m from the lab, outside the {1} m check-in radius. Check in on site."),
    (re.compile(r"^提示文案不能超过 (\d+) 个字。$"), "提示文案不能超過 {0} 個字。", "The message must not exceed {0} characters."),
)

# Complete system-message templates, never arbitrary word replacement in page data.
_FORMAT_ENUM_FIELDS = {
    "「{p0}」已经是{p1}状态。": {"p0": "interface", "p1": "state"},
    "已{p0}「{p1}」。": {"p0": "state", "p1": "interface"},
    "维护模式已经是{p0}状态。": {"p0": "state"},
    "学号 {p0} 已是「{p1}」账号，不能作为队长导入。": {"p1": "interface"},
    "当前状态为「{p0}」，无法撤销审核。": {"p0": "interface"},
    "队长登录：{p0}。": {"p0": "interface"},
    "{p0} 值班或可联系管理员：{p1}": {"p0": "interface"},
    "选择“其他”时，其他人员的姓名、学号和联系方式须全部填写。请填写 {p0}。": {"p0": "interface_list"},
}
_FORMAT_MESSAGES = []
for _source, _pair in CATALOG.items():
    if not re.search(r"\{p\d+\}", _source):
        continue
    _parts = []
    for _literal, _field, _spec, _conversion in Formatter().parse(_source):
        _parts.append(re.escape(_literal))
        if _field is not None:
            _parts.append("(?P<" + _field + ">.*?)")
    _FORMAT_MESSAGES.append((_source, re.compile("".join(_parts), re.DOTALL), _pair))

# 材料领取与购买：带数字/名称的提示。
PATTERNS += (
    (re.compile(r"^「(.+)」库存不足（剩余 ([0-9]+(?:/[0-9]+)?)(.*)，每队 ([0-9]+(?:/[0-9]+)?)(.*)）。$"), "「{0}」庫存不足（剩餘 {1}{2}，每隊 {3}{4}）。", "{0}: not enough stock ({1} {2} left, {3} {4} per team)."),
    (re.compile(r"^「(.+)」库存不足，暂时不能领取，请联系管理员。$"), "「{0}」庫存不足，暫時不能領取，請聯絡管理員。", "{0} is out of stock, so you cannot collect now. Please contact an administrator."),
    (re.compile(r"^「(.+)」的数量须为非负整数。$"), "「{0}」的數量須為非負整數。", "The quantity of {0} must be a whole number of 0 or more."),
    (re.compile(r"^「(.+)」已售罄，不能选择。$"), "「{0}」已售罄，不能選擇。", "{0} is sold out and cannot be chosen."),
    (re.compile(r"^「(.+)」库存仅剩 ([0-9]+(?:/[0-9]+)?)(.*)，购买数量不能超过库存。$"), "「{0}」庫存僅剩 {1}{2}，購買數量不能超過庫存。", "Only {1} {2} of {0} left; you cannot buy more than the stock."),
    (re.compile(r"^「(.+)」每次最多购买 ([0-9]+(?:/[0-9]+)?)(.*)。$"), "「{0}」每次最多購買 {1}{2}。", "You can buy at most {1} {2} of {0} per purchase."),
    (re.compile(r"^「(.+)」已下架，请重新选择。$"), "「{0}」已下架，請重新選擇。", "{0} is no longer available. Please choose again."),
    (re.compile(r"^「(.+)」的单价已调整为 (.+) 元，请重新提交。$"), "「{0}」的單價已調整為 {1} 元，請重新提交。", "The price of {0} has changed to {1} yuan. Please submit again."),
    (re.compile(r"^「(.+)」库存不足（剩余 ([0-9]+(?:/[0-9]+)?)(.*)），请重新选择。$"), "「{0}」庫存不足（剩餘 {1}{2}），請重新選擇。", "Not enough {0} in stock ({1} {2} left). Please choose again."),
    (re.compile(r"^「(.+)」库存不足，不能增加 ([0-9]+(?:/[0-9]+)?)(.*)。$"), "「{0}」庫存不足，不能增加 {1}{2}。", "Not enough {0} in stock to add {1} {2}."),
    (re.compile(r"^验证码已发送到 (.+)，5 分钟内有效。$"), "驗證碼已發送到 {0}，5 分鐘內有效。", "A code was sent to {0}. It is valid for 5 minutes."),
    (re.compile(r"^新的验证码已发送到 (.+)，旧验证码作废。$"), "新的驗證碼已發送到 {0}，舊驗證碼作廢。", "A new code was sent to {0}; the old code no longer works."),
    (re.compile(r"^验证码错误，还可以再试 ([0-9]+(?:/[0-9]+)?) 次。$"), "驗證碼錯誤，還可以再試 {0} 次。", "Wrong code. {0} attempts left."),
    (re.compile(r"^请 ([0-9]+(?:/[0-9]+)?) 秒后再重新发送。$"), "請 {0} 秒後再重新發送。", "Please wait {0} seconds before resending."),
    (re.compile(r"^购买成功！合计 (.+) 元，比赛后统一结算。$"), "購買成功！合計 {0} 元，比賽後統一結算。", "Purchase confirmed! Total {0} yuan, settled after the competition."),
    (re.compile(r"^已将 (.+) 的 ([0-9]+(?:/[0-9]+)?) 笔购买（(.+) 元）标记为已结算。$"), "已將 {0} 的 {1} 筆購買（{2} 元）標記為已結算。", "Marked {1} purchases by {0} ({2} yuan) as settled."),
)

# Strings needed by the shared scripts and the lab location form.
JS_KEYS = ('二维码识别组件未能加载，请刷新页面。', '位置权限被拒绝。本实验室要求位置验证，请在浏览器设置里允许获取位置后重试。', '共 {count} 人（含队长）', '关闭菜单', '单次时长 {minutes} 分钟，不得超过 1 小时，且须在实验室开放时间内。与他人已通过预约重叠不超过 30 分钟不算冲突。', '单次时长不得超过 1 小时（当前 {minutes} 分钟）。', '大写锁定已开启', '姓名查询暂时不可用，请稍后重试。', '已满足 {done} / {total} 项', '已获取位置（误差约 {meters} 米）。', '已识别二维码，正在签到…', '当前浏览器不支持定位，无法在需要位置验证的实验室签到。', '当前浏览器无法打开相机（需要 HTTPS 或本机地址）。请点下方按钮拍摄二维码照片，或用手机自带相机扫码。', '所选时段超出该实验室开放时间（{start}–{end}）。', '提交中…', '无法获取位置，请确认已开启手机定位后重试。', '显示', '显示密码', '有 {count} 项格式不正确，请检查后再提交', '服务器返回了无法识别的结果（{status}）。', '未找到该队长账号', '正在打开相机…', '正在查询…', '正在获取你的位置…', '正在识别照片中的二维码…', '此项', '没有在照片里识别到二维码，请靠近一些、保持清晰后重拍。', '照片预览', '相机无法使用或权限被拒绝。请允许相机权限，或点下方按钮拍摄二维码照片；也可以用手机自带相机扫码。', '签到失败。', '签到失败，请重试。', '结束时间必须晚于开始时间。', '获取位置超时，请到窗边或门口重试。', '菜单', '请填写 ', '请填写正确的 ', '请把镜头对准实验室门口的签到二维码。', '请联系：', '请选择 ', '超出允许范围，请检查', '还有 {count} 项未填写', '还有 {missing} 项未填写，另有 {invalid} 项格式不正确', '还有规则未满足，请按上方提示调整后再保存。', '这不是实验室签到二维码，请对准门口张贴的签到二维码。', '这张图片无法读取，请重新拍摄。', '长度不符合要求', '隐藏', '隐藏密码', '（超过 5MB，请重新拍摄或压缩）')
JS_KEYS += ("已填入当前位置（误差约 {meters} 米）。请确认后保存。",)
JS_KEYS += ("验证码已过期，请重新发送。", "数量格式无效，请填写大于 0 的小数或分数，如 0.5、1/3、1 1/2。", "数量超过允许上限。")


def _normalize_patterns() -> None:
    global PATTERNS
    normalized = []
    for item in PATTERNS:
        if len(item) == 3 and not isinstance(item[1], dict):
            regex, hant, english = item
            normalized.append((regex, {"zh-Hant": hant, "en": english}))
        else:
            normalized.append(item)
    path = Path(__file__).with_name("i18n_patterns.json")
    overlay = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for regex, mapping in normalized:
        for lang, text in overlay.get(regex.pattern, {}).items():
            if isinstance(text, str) and text:
                mapping.setdefault(lang, text)
    PATTERNS = tuple(normalized)


_normalize_patterns()

STATE_WORDS = {
    "zh-Hant": {"开启": "開啟", "关闭": "關閉"},
    "en": {"开启": "enabled", "关闭": "disabled"},
    "ja": {"开启": "有効", "关闭": "無効"},
    "ko": {"开启": "사용", "关闭": "사용 중지"},
    "es": {"开启": "activada", "关闭": "desactivada"},
    "fr": {"开启": "activée", "关闭": "désactivée"},
    "de": {"开启": "aktiviert", "关闭": "deaktiviert"},
}
WEEKDAYS = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "es": ("lun", "mar", "mié", "jue", "vie", "sáb", "dom"),
    "fr": ("lun", "mar", "mer", "jeu", "ven", "sam", "dim"),
    "de": ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"),
    "ja": ("月", "火", "水", "木", "金", "土", "日"),
    "ko": ("월", "화", "수", "목", "금", "토", "일"),
}


def current_lang() -> str:
    if not has_request_context():
        return DEFAULT_LANG
    chosen = request.args.get("lang")
    if chosen in LANG_CODES:
        return chosen
    chosen = request.cookies.get(LANG_COOKIE)
    return chosen if chosen in LANG_CODES else DEFAULT_LANG


def _apply(template: str, args=(), values=None):
    if values:
        return template.format(**values)
    if args:
        return template.format(*args)
    return template


def _pick(translations, lang: str):
    if not isinstance(translations, dict):
        return None
    text = translations.get(lang)
    if text:
        return text
    if lang not in (SOURCE_LANG, "en"):
        return translations.get("en") or None
    return None


def translate(text, lang: str, values=None):
    """Translate one Simplified Chinese source string into lang."""
    values = values or {}
    if not isinstance(text, str):
        return text
    if lang == SOURCE_LANG:
        return _apply(text, values=values)
    chosen = _pick(CATALOG.get(text), lang)
    if chosen is not None:
        return _apply(chosen, values=values)
    for source, pattern, translations in _FORMAT_MESSAGES:
        match = pattern.fullmatch(text)
        if not match:
            continue
        fields = match.groupdict()
        for field, kind in _FORMAT_ENUM_FIELDS.get(source, {}).items():
            if kind == "state":
                state = fields[field]
                fields[field] = STATE_WORDS.get(lang, STATE_WORDS["en"]).get(state, state)
            elif kind == "interface_list":
                separator = "、" if lang in ("zh-Hans", "zh-Hant", "ja") else ", "
                fields[field] = separator.join(translate(part, lang) for part in fields[field].split("、"))
            else:
                fields[field] = translate(fields[field], lang)
        chosen = _pick(translations, lang)
        if chosen is not None:
            return chosen.format(**fields)
    for pattern, translations in PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        chosen = _pick(translations, lang)
        if chosen is not None:
            return chosen.format(*match.groups())
    if lang != "en":
        return translate(text, "en", values)
    return _apply(text, values=values)


def t(text, **values):
    """Translate a Simplified Chinese source string for the active language."""
    return translate(text, current_lang(), values)


def language_url(code):
    """Keep the current resource, filters, pagination and login return target."""
    path = url_for(request.endpoint, **(request.view_args or {})) if request.endpoint else "/"
    pairs = [("lang", code)]
    pairs.extend((key, value) for key, value in request.args.items(multi=True) if key != "lang")
    return path + "?" + urlencode(pairs)


def js_catalog() -> dict:
    """source -> translation for app.js. Simplified Chinese keeps the source strings."""
    if current_lang() == SOURCE_LANG:
        return {}
    return {key: t(key) for key in JS_KEYS}


def html_lang() -> str:
    return HTML_LANG.get(current_lang(), current_lang())


def remember_choice(response):
    """Remember an explicit language choice on every page, including maintenance/errors."""
    if request.endpoint != "static" and request.args.get("lang") in LANG_CODES:
        from app.activity import request_is_https
        response.set_cookie(LANG_COOKIE, request.args["lang"], max_age=LANG_COOKIE_MAX_AGE,
                            samesite="Lax", httponly=True, secure=request_is_https())
    if response.mimetype in {"text/html", "application/json"}:
        response.headers["Content-Language"] = current_lang()
        response.vary.add("Cookie")
    return response


def cjk_punctuation(lang: str | None = None) -> bool:
    return (lang or current_lang()) in {"zh-Hans", "zh-Hant"}


def weekday_label(day):
    lang = current_lang()
    if lang in {"zh-Hans", "zh-Hant"}:
        return "星期" + ("一", "二", "三", "四", "五", "六", "日")[day.weekday()]
    return WEEKDAYS.get(lang, WEEKDAYS["en"])[day.weekday()]


def calendar_day(day):
    lang = current_lang()
    if lang == "ko":
        return f"{day.month}월 {day.day}일"
    if lang == "de":
        return day.strftime("%d.%m.")
    if lang in {"es", "fr"}:
        return day.strftime("%d/%m")
    if lang in {"zh-Hans", "zh-Hant", "ja"}:
        return day.strftime("%m月%d日")
    return day.strftime("%m-%d")


def duration_label(minutes):
    """Present numeric durations without changing exported or stored records."""
    hours, remaining = divmod(int(minutes), 60)
    lang = current_lang()
    if lang == "zh-Hans":
        return (f"{hours}小时" if hours else "") + (f"{remaining}分钟" if remaining or not hours else "")
    if lang == "zh-Hant":
        return (f"{hours}小時" if hours else "") + (f"{remaining}分鐘" if remaining or not hours else "")
    if lang == "ja":
        return (f"{hours}時間" if hours else "") + (f"{remaining}分" if remaining or not hours else "")
    if lang == "ko":
        parts = []
        if hours:
            parts.append(f"{hours}시간")
        if remaining or not hours:
            parts.append(f"{remaining}분")
        return " ".join(parts)
    if lang == "de":
        parts = []
        if hours:
            parts.append(f"{hours} Std.")
        if remaining or not hours:
            parts.append(f"{remaining} Min.")
        return " ".join(parts)
    if lang in {"es", "fr"}:
        parts = []
        if hours:
            parts.append(f"{hours} h")
        if remaining or not hours:
            parts.append(f"{remaining} min")
        return " ".join(parts)
    return " ".join(part for part in (f"{hours}h" if hours else "", f"{remaining}m" if remaining or not hours else "") if part)
