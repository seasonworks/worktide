"""一键跑全部 smoke_*.py 回归（#5）。

    server/.venv/Scripts/python.exe server/scripts/run_all_smokes.py
    # 或从任意目录： python <repo>/server/scripts/run_all_smokes.py

要点：
- 自动发现本目录下所有 smoke_*.py（本文件除外）。
- 用**当前解释器**（建议用 server/.venv 的 python）逐个子进程跑，**cwd 固定为 server/**——
  根治"从仓库根跑时 ./data 相对路径开不了库"的坑（smoke_notifier / smoke_mention 等需要）。
- 每个脚本以退出码判定通过/失败；末行（通常 "PASS=.. FAIL=.." 或总结）回显。
- 任一失败 → 整体退出码 1，便于 pre-push hook / CI 拦截。

可选参数：传若干子串只跑匹配的（如 `run_all_smokes.py idle mention`）。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve()
_SCRIPTS = _HERE.parent
_SERVER = _HERE.parents[1]
PER_SCRIPT_TIMEOUT = 180  # 秒

# 需要"已启动的 live server"的集成测试，不属于离线回归，默认跳过。
# 仍可显式跑：run_all_smokes.py settings_api（用子串过滤会绕过排除）
_NEEDS_LIVE_SERVER = {"smoke_settings_api.py"}


def _discover(filters: list[str]) -> list[Path]:
    files = sorted(p for p in _SCRIPTS.glob("smoke_*.py"))
    if filters:
        return [p for p in files if any(f.lower() in p.name.lower() for f in filters)]
    skipped = [p.name for p in files if p.name in _NEEDS_LIVE_SERVER]
    if skipped:
        print(f"（跳过需 live server 的集成测试：{', '.join(skipped)}）")
    return [p for p in files if p.name not in _NEEDS_LIVE_SERVER]


def _last_meaningful_line(text: str) -> str:
    for line in reversed(text.strip().splitlines()):
        s = line.strip()
        if s:
            return s
    return "(no output)"


def main() -> int:
    filters = sys.argv[1:]
    scripts = _discover(filters)
    if not scripts:
        print("没有匹配的 smoke 脚本")
        return 1

    print(f"== 跑 {len(scripts)} 个 smoke（cwd={_SERVER}，python={sys.executable}） ==\n")
    results: list[tuple[str, bool, str, float]] = []
    for p in scripts:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, str(p)],
                cwd=str(_SERVER),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PER_SCRIPT_TIMEOUT,
            )
            ok = proc.returncode == 0
            tail = _last_meaningful_line(proc.stdout) if ok else (
                _last_meaningful_line(proc.stdout + "\n" + proc.stderr)
            )
        except subprocess.TimeoutExpired:
            ok, tail = False, f"TIMEOUT >{PER_SCRIPT_TIMEOUT}s"
        dt = time.monotonic() - t0
        results.append((p.name, ok, tail, dt))
        mark = "✅" if ok else "❌"
        print(f"  {mark} {p.name:<28} ({dt:4.1f}s)  {tail}")

    passed = sum(1 for _, ok, _, _ in results if ok)
    failed = [name for name, ok, _, _ in results if not ok]
    print("\n" + "=" * 64)
    print(f"总计：{passed}/{len(results)} 通过")
    if failed:
        print("失败：")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("全部 smoke 通过 🎉")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
