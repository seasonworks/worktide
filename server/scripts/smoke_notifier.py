"""Phase 6.5A Sprint B · notifier 双语模板 + cooldown 冒烟（**严格 dry-run**）。

跑法（项目根目录）：

    .venv\\Scripts\\python.exe server\\scripts\\smoke_notifier.py

输出：
    - 6 种模板渲染样例（打印到 stdout，可贴回评审）
    - cooldown 行为校验
    - enabled 开关校验
    - **零** Telegram 发送（dry_run=True / 不触 _send；同时无 env token 也不会真发）

设计取舍：
- 不依赖 pytest；零外部依赖（stdlib only）。
- 每个用例独立可读；失败定位靠脚本里的断言行号。
- 即便 settings.telegram_enabled=True，本脚本永不路过 dry_run=False；这是安全冗余。
"""
from __future__ import annotations

import sys

# Windows 控制台 GBK 编码不认 emoji，强制 UTF-8（与 smoke_settings_api.py 同款）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

# 让脚本可在项目根 / server 子目录下都能跑
_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[2]
_SERVER = _HERE.parents[1]
for p in (_SERVER, _REPO):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# 强行清空既有内存 cooldown（防止重复跑脚本拿到脏状态）
from app.notifier import (  # noqa: E402
    NotifyType,
    TelegramNotifier,
    build_break_switch,
    build_break_timeout,
    build_clock_in_reminder,
    build_clock_out_reminder,
    build_idle_enter,
    build_idle_exit,
    notifier as global_notifier,
)

# -----------------------------------------------------------------------------
# 安全护栏 · 物理切断 _send → Telegram API 的网络路径
#   dev 机 .env 里 telegram_enabled=true + 真实 token + 真实 chat_id，
#   一旦脚本里有任何 dry_run=False 路径走到 _send，就会真发到生产群。
#   本段在 import 后立刻**全局**替换 TelegramNotifier._send 为计数器，
#   再加 monkey-patch urllib.request.urlopen 兜底；双保险。
# -----------------------------------------------------------------------------
import urllib.request  # noqa: E402

_real_send_call_count = {"n": 0, "payloads": []}


def _fake_send(self, text: str) -> None:  # noqa: ANN001
    _real_send_call_count["n"] += 1
    _real_send_call_count["payloads"].append(text)
    # 不打全文，避免输出污染；只记 head 用于断言定位
    print(f"    [intercepted _send] head={text.split(chr(10), 1)[0]!r}")


TelegramNotifier._send = _fake_send  # type: ignore[assignment]


def _block_urlopen(*a, **kw):  # noqa: ANN001
    raise RuntimeError("BLOCKED · smoke 脚本禁止任何 urllib.urlopen 出网")


urllib.request.urlopen = _block_urlopen  # type: ignore[assignment]

# 同时清掉单例的 cooldown（防止跨测试干扰）
global_notifier.reset_cooldowns()

_pass = 0
_fail = 0
_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ✅ {name}")
    else:
        _fail += 1
        msg = f"  ❌ {name}" + (f" -- {detail}" if detail else "")
        print(msg)
        _failures.append(msg)


# ---------------------------------------------------------------------------
# 1) 渲染 6 种模板，打印样例供评审
# ---------------------------------------------------------------------------


def section_render_templates() -> None:
    print("\n" + "=" * 70)
    print('[1] 模板渲染样例（员工 Alex / Jordan 等假数据；UTC+8 时间已固化）')
    print("=" * 70)

    samples = [
        ("GS-3 BREAK TIMEOUT (smoke 10min auto-resume)",
         build_break_timeout("Alex", "smoke", 600)),
        ("GS-7B BREAK SWITCH (meal → smoke)",
         build_break_switch("Jordan", "meal", "smoke")),
        ("GS-4 IDLE ENTER (10min)",
         build_idle_enter("Jordan", 600)),
        ("GS-5 IDLE EXIT (18min)",
         build_idle_exit("Jordan", 1080)),
        ("GS-6 CLOCK-IN REMINDER",
         build_clock_in_reminder("LILY")),
        ("GS-7 CLOCK-OUT REMINDER",
         build_clock_out_reminder("LILY")),
    ]
    for title, text in samples:
        print(f"\n--- {title} ---")
        print(text)
        print("---")


# ---------------------------------------------------------------------------
# 2) 模板结构断言（review 要求：员工姓名第一行 + 双语 + UTC+8 时间）
# ---------------------------------------------------------------------------


def section_template_assertions() -> None:
    print("\n" + "=" * 70)
    print("[2] 模板结构断言（review 要求）")
    print("=" * 70)

    # 所有非 timeout/switch 类的模板，首行必须就是 emoji + 员工字段（员工姓名第一行原则）
    idle_in = build_idle_enter("Jordan", 600)
    _check("IDLE_ENTER 首行 emoji + 员工字段",
           idle_in.split("\n")[0].startswith("🟡 员工 / Employee"))
    _check("IDLE_ENTER 第二行就是姓名",
           idle_in.split("\n")[1] == "Jordan")

    idle_out = build_idle_exit("Jordan", 1080)
    _check("IDLE_EXIT 首行 emoji + 员工字段",
           idle_out.split("\n")[0].startswith("🟢 员工 / Employee"))
    _check("IDLE_EXIT 第二行就是姓名",
           idle_out.split("\n")[1] == "Jordan")

    # 带标题的类（break_timeout / break_switch / clock_*）：title 在首行，员工字段紧随
    bt = build_break_timeout("Alex", "smoke", 600)
    lines = bt.split("\n")
    _check("BREAK_TIMEOUT 首行有 ⏰ + Break Timeout / 休息超时", "Break Timeout" in lines[0] and "休息超时" in lines[0])
    _check("BREAK_TIMEOUT 含'员工 / Employee:'", any("员工 / Employee:" in ln for ln in lines))
    _check("BREAK_TIMEOUT 含 break_type 双语", "Smoke Break / 抽烟" in bt)
    _check("BREAK_TIMEOUT 含'10 minutes / 10 分钟'", "10 minutes / 10 分钟" in bt)
    _check("BREAK_TIMEOUT 含 auto-return 双语 (V2 文案)",
           "System automatically returned employee to work" in bt
           and "系统已自动返回工作岗位" in bt)

    bs = build_break_switch("Jordan", "meal", "smoke")
    _check("BREAK_SWITCH 含 EN 切换 'Lunch Break → Smoke Break'", "Lunch Break → Smoke Break" in bs)
    _check("BREAK_SWITCH 含 ZH 切换 '吃饭 → 抽烟'", "吃饭 → 抽烟" in bs)

    ci = build_clock_in_reminder("LILY")
    _check("CLOCK_IN 首行 ⏰ + Clock-In Reminder / 上班提醒",
           "Clock-In Reminder" in ci.split("\n")[0] and "上班提醒" in ci.split("\n")[0])
    _check("CLOCK_IN 含'Please clock in.' (V2 文案)", "Please clock in." in ci)
    _check("CLOCK_IN 含'请及时上班打卡。' (V2 文案)", "请及时上班打卡。" in ci)

    co = build_clock_out_reminder("LILY")
    _check("CLOCK_OUT 首行 🏁 + Clock-Out Reminder", "Clock-Out Reminder" in co.split("\n")[0])
    _check("CLOCK_OUT 含'Please clock out.' (V2 文案)", "Please clock out." in co)
    _check("CLOCK_OUT 含 ZH '请及时下班打卡。' (V2 文案)", "请及时下班打卡。" in co)

    # 所有模板尾部"时间 / Time:"出现一次
    all_texts = [bt, bs, idle_in, idle_out, ci, co]
    for label, t in zip(["BT", "BS", "IDLE_IN", "IDLE_OUT", "CI", "CO"], all_texts):
        _check(f"{label} 末尾有'时间 / Time:' 标签", "时间 / Time:" in t)
    # UTC+8 时间格式（YYYY-MM-DD HH:MM；不验具体值，只验形状）
    import re
    ts_re = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
    for label, t in zip(["BT", "BS", "IDLE_IN", "IDLE_OUT", "CI", "CO"], all_texts):
        _check(f"{label} 含 'YYYY-MM-DD HH:MM' 时间戳", bool(ts_re.search(t)))


# ---------------------------------------------------------------------------
# 3) 未知 break_type 兜底（不抛异常）
# ---------------------------------------------------------------------------


def section_unknown_break_type() -> None:
    print("\n" + "=" * 70)
    print("[3] 未知 break_type 兜底（DB 加新类型时不应崩）")
    print("=" * 70)
    try:
        t1 = build_break_timeout("某员工", "weird_unknown", 300)
        _check("未知 break_type 不抛", True)
        _check("未知 break_type 原样回填", "weird_unknown / weird_unknown" in t1)
    except Exception as e:
        _check("未知 break_type 不抛", False, str(e))


# ---------------------------------------------------------------------------
# 4) dry_run 路径：不触发 _send，永远返回模板
# ---------------------------------------------------------------------------


def section_dry_run_behavior() -> None:
    print("\n" + "=" * 70)
    print("[4] dry_run=True：跳过 enabled / cooldown，永远返回模板")
    print("=" * 70)
    # 用全新实例确保 cooldown 干净
    n = TelegramNotifier()

    # 即便 configured=False（dev 机大概率没设 token），dry_run 也照样返回模板
    r1 = n.notify_break_timeout(10, "Alex", "smoke", 600, dry_run=True)
    _check("dry_run BREAK_TIMEOUT 返回 str", isinstance(r1, str))
    _check("dry_run 内容含'Alex'", r1 and "Alex" in r1)

    # 连续调用 2 次，dry_run 都返回（不被 cooldown 拦截）
    r2 = n.notify_break_timeout(10, "Alex", "smoke", 600, dry_run=True)
    _check("dry_run 重复调用仍返回 str（不消耗 cooldown）", isinstance(r2, str))

    # 所有 6 种都跑一遍
    cases = [
        ("BREAK_TIMEOUT", lambda: n.notify_break_timeout(1, "A", "smoke", 600, dry_run=True)),
        ("BREAK_SWITCH",  lambda: n.notify_break_switch(2, "B", "meal", "smoke", dry_run=True)),
        ("IDLE_ENTER",    lambda: n.notify_idle_enter(3, "C", 900, dry_run=True)),
        ("IDLE_EXIT",     lambda: n.notify_idle_exit(4, "D", 1080, dry_run=True)),
        ("CLOCK_IN",      lambda: n.notify_clock_in_reminder(5, "E", dry_run=True)),
        ("CLOCK_OUT",     lambda: n.notify_clock_out_reminder(6, "F", dry_run=True)),
    ]
    for label, fn in cases:
        out = fn()
        _check(f"dry_run {label} 返回模板", isinstance(out, str) and len(out) > 20)


# ---------------------------------------------------------------------------
# 5) cooldown 行为校验（非 dry-run，但 telegram 未配置 → _send 不会真发）
# ---------------------------------------------------------------------------


def section_cooldown_behavior() -> None:
    print("\n" + "=" * 70)
    print("[5] cooldown 行为（不发真消息，靠 enabled=False 兜底；同时使用 fresh 实例）")
    print("=" * 70)
    # fresh 实例，cooldown 干净
    n = TelegramNotifier()

    # 由于 dev 机 telegram_enabled 默认 False，_dispatch 在 dry_run=False 时第一关
    # 就被 _enabled() 拦掉，返回 None。下面单独验证 _claim_cooldown 行为（隔离）
    # IDLE_ENTER 已取消时间 cooldown（频控改由 activity 层 bucket 逐阈值段去重）→ 每次都 True
    ok1 = n._claim_cooldown(NotifyType.IDLE_ENTER, employee_id=99)
    _check("IDLE_ENTER 无 cooldown：第一次 True", ok1 is True)
    okr = n._claim_cooldown(NotifyType.IDLE_ENTER, employee_id=99)
    _check("IDLE_ENTER 无 cooldown：紧接再调仍 True", okr is True)
    # cooldown 机制本身用 IDLE_EXIT(10min) 验证：同 emp 同类型二次被拦
    ok2a = n._claim_cooldown(NotifyType.IDLE_EXIT, employee_id=99)
    _check("第一次 IDLE_EXIT 占用成功", ok2a is True)
    ok2 = n._claim_cooldown(NotifyType.IDLE_EXIT, employee_id=99)
    _check("第二次同 emp 同类型立刻被冷却拦截", ok2 is False)
    ok3 = n._claim_cooldown(NotifyType.IDLE_EXIT, employee_id=100)
    _check("第二次另一 emp 不受影响", ok3 is True)
    ok4 = n._claim_cooldown(NotifyType.IDLE_ENTER, employee_id=99)
    _check("同 emp 不同类型不互相串扰", ok4 is True)
    ok5 = n._claim_cooldown(NotifyType.BREAK_TIMEOUT, employee_id=99)
    _check("BREAK_TIMEOUT cooldown=0 永远 True", ok5 is True)
    ok6 = n._claim_cooldown(NotifyType.BREAK_TIMEOUT, employee_id=99)
    _check("BREAK_TIMEOUT 再调一次仍 True", ok6 is True)


# ---------------------------------------------------------------------------
# 6) enabled 开关：runtime 关闭后 dispatch 直接返回 None
# ---------------------------------------------------------------------------


def section_enabled_gate() -> None:
    print("\n" + "=" * 70)
    print("[6] enabled 开关（runtime + env 双闸；_send 已被拦截，0 真发）")
    print("=" * 70)
    from app.database import SessionLocal
    from app.services.settings_service import settings_service

    n = TelegramNotifier()
    n.reset_cooldowns()
    print(f"  → notifier.configured = {n.configured}")
    baseline = _real_send_call_count["n"]

    # 备份原值，准备用 settings_service 关掉 idle_enter
    db = SessionLocal()
    try:
        # 1) runtime=True（默认）: 应走完整链路 → _send 被拦截调用一次
        out_on = n.notify_idle_enter(101, "GhostEmployee_A", 600, dry_run=False)
        sends_after_on = _real_send_call_count["n"]
        _check("runtime enabled=True 时返回模板字符串",
               isinstance(out_on, str) and "GhostEmployee_A" in (out_on or ""))
        _check("runtime enabled=True 触发 _send 拦截器（+1）",
               sends_after_on == baseline + 1,
               f"baseline={baseline} after={sends_after_on}")

        # 2) 关掉 runtime 开关 → 同 emp 不同时间窗（reset cooldown） → 应被拦截返回 None
        settings_service.update(db, "notify_idle_enter_enabled", False)
        n.reset_cooldowns()
        out_off = n.notify_idle_enter(102, "GhostEmployee_B", 600, dry_run=False)
        sends_after_off = _real_send_call_count["n"]
        _check("runtime enabled=False 时返回 None", out_off is None)
        _check("runtime enabled=False 不触发 _send（计数不变）",
               sends_after_off == sends_after_on,
               f"after_on={sends_after_on} after_off={sends_after_off}")
    finally:
        # 务必恢复 settings（不留垃圾）
        settings_service.reset(db, "notify_idle_enter_enabled")
        db.close()

    # 3) dry_run=True 永远返回模板，与 configured / enabled 无关
    out_dry = n.notify_idle_enter(103, "GhostEmployee_C", 600, dry_run=True)
    _check("dry_run=True 始终返回模板", isinstance(out_dry, str))
    _check("dry_run=True 不触发 _send",
           _real_send_call_count["n"] == sends_after_off)


# ---------------------------------------------------------------------------
# 7) 全局单例还在 + notify_idle 向后兼容（Sprint C 前 activity.py 仍调它）
# ---------------------------------------------------------------------------


def section_singleton_and_compat() -> None:
    print("\n" + "=" * 70)
    print("[7] 全局单例 + notify_idle 别名已移除（Sprint E）")
    print("=" * 70)
    _check("全局 notifier 存在", global_notifier is not None)
    _check("notify_idle_enter 方法存在（新名）",
           hasattr(global_notifier, "notify_idle_enter"))
    # Sprint E：notify_idle 别名已移除；如果还有人调它会 AttributeError
    _check("notify_idle 别名已移除（Sprint E cleanup）",
           not hasattr(global_notifier, "notify_idle"))

    # 用 notify_idle_enter 走完整链路（_send 已 fake 拦截）
    global_notifier.reset_cooldowns()
    before = _real_send_call_count["n"]
    try:
        result = global_notifier.notify_idle_enter(
            employee_id=999, employee_name="EnterPath",
            idle_seconds=900,
        )
        _check("notify_idle_enter(...) 不抛 + 返回 str（实链）",
               isinstance(result, str))
        _check("notify_idle_enter 触发 _send 拦截器",
               _real_send_call_count["n"] == before + 1,
               f"before={before} after={_real_send_call_count['n']}")
    except Exception as e:
        _check("notify_idle_enter(...) 不抛", False, str(e))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    section_render_templates()
    section_template_assertions()
    section_unknown_break_type()
    section_dry_run_behavior()
    section_cooldown_behavior()
    section_enabled_gate()
    section_singleton_and_compat()

    # 终极断言：所有 _send 都被 fake 拦截器接住（urlopen 已被禁用，理论上无法出网）
    # 拦截器计数 = 业务路径里"走到 _send 这一行"的次数；不等于"真发到 Telegram"
    intercepted = _real_send_call_count["n"]
    print("\n" + "=" * 70)
    print(f"=== 结果：{_pass} pass / {_fail} fail ===")
    print(f"=== _send 被拦截 {intercepted} 次（这些原本会发到生产群，已物理截断）===")
    print("=== 真实 urllib.urlopen 被替换为 raise，0 次实际出网 ===")
    if _failures:
        print("\n失败用例：")
        for m in _failures:
            print(m)
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
