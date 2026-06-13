"""仅跑场景 J，跳过 A–I（A–I 已确认通过）。"""
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.integration.harness import IsolatedServer, pick_free_port  # noqa: E402
from tests.integration.verify_step5 import (  # noqa: E402
    FAILURES, REPORT, scenario_J_compressed,
)


def main():
    seconds = 180
    for a in sys.argv[1:]:
        if a.startswith("--soak="):
            seconds = int(a.split("=", 1)[1])
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        port = pick_free_port(9110)
        db = tmp_path / "j.db"
        with IsolatedServer(db, port) as srv:
            scenario_J_compressed(srv, tmp_path, seconds)
    print(f"\n汇总：{len(REPORT)} 个断言，失败 {len(FAILURES)}")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
