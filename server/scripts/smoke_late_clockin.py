"""迟到打卡显示冒烟（Clock In 晚于设置的上班时刻 → 回执显示迟到时长）。

    server/.venv/Scripts/python.exe server/scripts/smoke_late_clockin.py

覆盖（上班时刻 22:00、tz=UTC+7，对应用户真实夜班配置）：
    1  复现 Alex：本地 22:02 上班 → 迟到 2 分钟
    2  准时（22:00 整）→ 不显示迟到
    3  早到（21:50）→ 不显示迟到
    4  迟到 90 分钟 → 1小时30分钟 / 1h30m
    5  跨午夜：本地 00:30 上班（22:00 夜班）→ 约 2.5 小时迟到
    6  _build_action_chat_message：clock_in 迟到时含「迟到」行
    7  其它动作（吃饭/回座）永不显示迟到行
    8  clock_in 准时：回执不含「迟到」行
    9  上班时刻格式非法 → 安全返回 None（不崩）
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ORIGINS", "[]")
os.environ.setdefault("TELEGRAM_ENABLED", "false")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-100123")

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from app.services import telegram_handlers as th  # noqa: E402

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


# 注入可控设置：上班时刻 22:00、tz=UTC+7
_settings = {"clock_in_reminder_time": "22:00", "reminders_timezone_offset_hours": 7}
th.settings_service.get = lambda k: _settings.get(k)

UTC = timezone.utc


def _at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


print("[1] 复现 Alex：本地 22:02（UTC 15:02）→ 迟到 2 分钟")
line = th._late_clock_in_line(now=_at(2026, 6, 9, 15, 2))  # +7 = 22:02
ok("有迟到行", line is not None, str(line))
ok("显示 2 分钟", line and "2 分钟 / 2 min" in line, str(line))

print("[2] 准时 22:00（UTC 15:00）→ 无迟到")
ok("None", th._late_clock_in_line(now=_at(2026, 6, 9, 15, 0)) is None)

print("[3] 早到 21:50（UTC 14:50）→ 无迟到")
ok("None", th._late_clock_in_line(now=_at(2026, 6, 9, 14, 50)) is None)

print("[4] 迟到 90 分钟（本地 23:30 / UTC 16:30）→ 1小时30分钟")
line = th._late_clock_in_line(now=_at(2026, 6, 9, 16, 30))
ok("含 1小时30分钟 / 1h30m", line and "1小时30分钟 / 1h30m" in line, str(line))

print("[5] 跨午夜：本地 00:30（UTC 17:30）→ 约 2.5 小时迟到")
line = th._late_clock_in_line(now=_at(2026, 6, 10, 17, 30))  # +7 = 次日 00:30
ok("有迟到行", line is not None, str(line))
ok("约 2小时30分钟", line and "2小时30分钟" in line, str(line))

print("[6] _build_action_chat_message · clock_in 迟到含迟到行")
# 用真实 now 不可控，这里改注入一个一定迟到的设置（上班时刻设成 00:00 必迟到）
_settings["clock_in_reminder_time"] = "00:01"
msg = th._build_action_chat_message("ws:clock_in", "ALEX")
ok("clock_in 回执含『迟到 / Late』", msg and "迟到 / Late" in msg, str(msg))
ok("仍含状态/操作/时间", msg and "Clock In" in msg and "时间：" in msg)

print("[7] 其它动作永不显示迟到行")
meal = th._build_action_chat_message("ws:break:meal", "ALEX")
back = th._build_action_chat_message("ws:back", "ALEX")
ok("吃饭 不含迟到", meal and "迟到" not in meal)
ok("回座 不含迟到", back and "迟到" not in back)

print("[8] clock_in 准时不含迟到行")
# 上班时刻设成 23:59 且当前几乎不可能晚于它 12h 内 → 多数时刻为早到
_settings["clock_in_reminder_time"] = "23:58"
# 直接用 helper 在一个确定早到的时刻验证（本地 23:00 < 23:58）
ok("准时/早到 helper=None",
   th._late_clock_in_line(now=_at(2026, 6, 9, 16, 0)) is None)  # +7=23:00 < 23:58

print("[9] 上班时刻格式非法 → None（不崩）")
_settings["clock_in_reminder_time"] = "garbage"
ok("非法 HH:MM → None", th._late_clock_in_line(now=_at(2026, 6, 9, 15, 2)) is None)
_settings["clock_in_reminder_time"] = None
ok("None 值 → None", th._late_clock_in_line(now=_at(2026, 6, 9, 15, 2)) is None)

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("迟到打卡显示全部通过")
