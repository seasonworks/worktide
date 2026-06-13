"""Phase 6.5B · Mention Enhancement 冒烟（**dry-run / 0 真发**）。

    server/.venv/Scripts/python.exe server/scripts/smoke_mention.py

规则：Idle Enter=@员工+@管理员 · Break Timeout=@员工 · Idle Exit/Break Switch=不@
mention 优先级（2026-06 改）：user_id 优先（不可变，改名仍 @ 得到）→ 退回 @username。
覆盖：
    1  员工有 user_id（即便也有 username）→ tg://user?id= 链接（按不可变 id @，改名仍有效）
    2  员工仅 user_id（无 username）→ tg://user?id= 链接 + parse_mode=HTML + fallback
    2b 员工仅 username（无 user_id）→ 退回 @username（无需 HTML）
    3  员工未绑定 → 降级纯文本名字（不构成 mention），管理员仍 @
    4  Idle Enter 同时 @员工 + @管理员（均用 tg://user?id= 链接）
    5  Break Timeout 仅 @员工（tg://user?id=），不 @管理员
    6  Idle Exit / Break Switch 完全不 @
    7  parse_mode 异常 → 自动降级纯文本重发（不丢失，真 _send 路径）
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ORIGINS", "[]")
os.environ.setdefault("TELEGRAM_ENABLED", "true")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-100123")

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

# ---- in-memory DB ----
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from app.database import Base  # noqa: E402
import app.models as models  # noqa: E402

test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(test_engine)
TestSession = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)

import app.notifier as notifier_mod  # noqa: E402
import app.services.settings_service as ss_mod  # noqa: E402
notifier_mod.SessionLocal = TestSession  # type: ignore[assignment]

from app.notifier import notifier  # noqa: E402

# admin ids 用可控替身
_admin_ids = {"v": ""}
_orig_get = ss_mod.settings_service.get


def _fake_get(key):
    if key == "admin_telegram_user_ids":
        return _admin_ids["v"]
    return _orig_get(key)


ss_mod.settings_service.get = _fake_get

PASS = 0
FAIL: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL.append(label)
        print(f"  ❌ {label}   {detail}")


# 捕获 _send 调用（content + parse_mode）
_calls: list[dict] = []


def _capture_send(self, text, parse_mode=None, fallback_text=None):  # noqa: ANN001
    _calls.append({"text": text, "parse_mode": parse_mode, "fallback_text": fallback_text})


_orig_send = notifier_mod.TelegramNotifier._send
notifier_mod.TelegramNotifier._send = _capture_send


def _bind(emp_id, name, tg_id, username):
    with TestSession() as db:
        if db.get(models.Employee, emp_id) is None:
            db.add(models.Employee(id=emp_id, machine_id=f"m{emp_id}", name=name,
                                   hostname=f"h{emp_id}", status="online",
                                   idle_seconds=0, is_active=True))
            db.commit()
        if tg_id is not None:
            db.add(models.TelegramUser(employee_id=emp_id, telegram_user_id=tg_id,
                                       telegram_username=username))
            db.commit()


def _reset():
    notifier.reset_cooldowns()
    _calls.clear()


print("[1] Idle Enter · 员工有 user_id（也有 username）→ 用 tg://user 链接（按不可变 id @）")
_admin_ids["v"] = ""
_bind(101, "Jordan", 5550101, "jordan_tg")
_reset()
notifier.notify_idle_enter(employee_id=101, employee_name="Jordan", idle_seconds=900)
c = _calls[-1]
ok("发了 1 次", len(_calls) == 1)
ok("用 tg://user?id=5550101（按 id，不用 @username）", "tg://user?id=5550101" in c["text"], c["text"][:60])
ok("不含 @jordan_tg（改名会失效，改用 id）", "@jordan_tg" not in c["text"], c["text"][:60])
ok("parse_mode=HTML", c["parse_mode"] == "HTML", str(c["parse_mode"]))
ok("有 fallback 纯文本含名字", c["fallback_text"] is not None and "Jordan" in c["fallback_text"])

print("[2] Idle Enter · 员工仅 user_id（无 username）→ HTML tg://user 链接 + fallback")
_admin_ids["v"] = ""
_bind(102, "Morgan", 5550102, None)
_reset()
notifier.notify_idle_enter(employee_id=102, employee_name="Morgan", idle_seconds=900)
c = _calls[-1]
ok("parse_mode=HTML", c["parse_mode"] == "HTML", str(c["parse_mode"]))
ok("含 tg://user?id=5550102", "tg://user?id=5550102" in c["text"], c["text"][:60])
ok("有 fallback 纯文本", c["fallback_text"] is not None and "Morgan" in c["fallback_text"])
ok("fallback 不含 HTML 标签", "<a" not in (c["fallback_text"] or ""))

print("[2b] _build_mention_line 单测 · 仅 username（无 user_id）→ 退回 @username（无 HTML）")
# 注：telegram_users.telegram_user_id 是 NOT NULL，DB 永远不会产出"只有用户名"的行，
# 故该退回分支只可能由手工构造的 target 触发，这里直接单测函数本身。
h, p, needs = notifier_mod._build_mention_line([("NOID", "noid_tg", None)])
ok("html 含 @noid_tg（无 id 时退回用户名）", "@noid_tg" in h, h)
ok("plain 含 @noid_tg", "@noid_tg" in p, p)
ok("needs_html=False（username 不需 HTML）", needs is False, str(needs))
# 同时校验 uid 优先：同时有 username+uid 时只走链接、不出现 @username
h2, _, n2 = notifier_mod._build_mention_line([("BOTH", "both_tg", 777222)])
ok("uid 优先：含 tg://user?id=777222", "tg://user?id=777222" in h2, h2)
ok("uid 优先：不含 @both_tg", "@both_tg" not in h2, h2)
ok("uid 优先：needs_html=True", n2 is True, str(n2))

print("[3] Idle Enter · 员工未绑定 → 纯文本名字（不 @），管理员仍 @")
_admin_ids["v"] = "999001"
_reset()
notifier.notify_idle_enter(employee_id=777, employee_name="未绑定员工", idle_seconds=900)
c = _calls[-1]
ok("员工名出现（纯文本）", "未绑定员工" in c["text"])
ok("员工无 @（无 username/id）", "@未绑定员工" not in c["text"])
ok("管理员被 @（tg://user?id=999001）", "tg://user?id=999001" in c["text"], c["text"][:80])
ok("parse_mode=HTML（admin 为 id-only）", c["parse_mode"] == "HTML")

print("[4] Idle Enter · 同时 @员工 + @管理员（均用 tg://user?id= 链接）")
_admin_ids["v"] = "999002"
_bind(104, "Taylor", 5550104, "taylor_tg")
_reset()
notifier.notify_idle_enter(employee_id=104, employee_name="Taylor", idle_seconds=900)
c = _calls[-1]
ok("含员工 tg://user?id=5550104（按 id，不用 @username）", "tg://user?id=5550104" in c["text"])
ok("不含 @taylor_tg", "@taylor_tg" not in c["text"])
ok("含管理员 tg://user?id=999002", "tg://user?id=999002" in c["text"])

print("[5] Break Timeout · 仅 @员工（tg://user?id=），不 @管理员")
_admin_ids["v"] = "999003"  # 即便配置了管理员
_bind(105, "Sam", 5550105, "sam_tg")
_reset()
notifier.notify_break_timeout(employee_id=105, employee_name="Sam",
                              break_type="smoke", duration_seconds=900)
c = _calls[-1]
ok("含员工 tg://user?id=5550105", "tg://user?id=5550105" in c["text"])
ok("不含 @sam_tg", "@sam_tg" not in c["text"])
ok("不含管理员 id 999003", "999003" not in c["text"], c["text"][:80])

print("[6] Idle Exit · 完全不 @")
_admin_ids["v"] = "999004"
_bind(106, "Riley", 5550106, "riley_tg")
_reset()
notifier.notify_idle_exit(employee_id=106, employee_name="Riley", idle_duration_seconds=900)
c = _calls[-1]
ok("无 @riley_tg", "@riley_tg" not in c["text"])
ok("无管理员 id", "999004" not in c["text"])
ok("无 parse_mode", c["parse_mode"] is None)

print("[7] Break Switch · 完全不 @")
_reset()
notifier.notify_break_switch(employee_id=106, employee_name="Riley",
                             from_break="meal", to_break="smoke")
c = _calls[-1]
ok("无 @", "@riley_tg" not in c["text"] and "999004" not in c["text"])
ok("无 parse_mode", c["parse_mode"] is None)

# ---- [8] 真 _send fallback：parse_mode 400 → 降级纯文本重发 ----
print("[8] parse_mode 异常 → 自动降级纯文本（不丢失）")
notifier_mod.TelegramNotifier._send = _orig_send  # 还原真 _send

_sent_payloads: list[dict] = []


def _fake_urlopen(req, timeout=None):  # noqa: ANN001
    import json as _json
    body = _json.loads(req.data.decode("utf-8"))
    _sent_payloads.append(body)
    if body.get("parse_mode"):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request: can't parse entities",
                                     hdrs=None, fp=None)

    class _R:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return _R()


notifier_mod.urllib.request.urlopen = _fake_urlopen
_admin_ids["v"] = ""
_bind(108, "Casey", 5550108, None)  # id-only → 触发 HTML
notifier.reset_cooldowns()
_sent_payloads.clear()
notifier.notify_idle_enter(employee_id=108, employee_name="Casey", idle_seconds=900)
ok("共 2 次发送尝试（HTML 失败 + 纯文本降级）", len(_sent_payloads) == 2, str(len(_sent_payloads)))
ok("第1次带 parse_mode=HTML", _sent_payloads[0].get("parse_mode") == "HTML")
ok("第2次纯文本（无 parse_mode）", "parse_mode" not in _sent_payloads[1])
ok("降级文本仍含员工名（通知未丢失）", "Casey" in _sent_payloads[1]["text"])
ok("降级文本无 HTML 标签", "<a" not in _sent_payloads[1]["text"])

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("Mention 全部通过（0 真发；fallback 不丢失验证通过）")
