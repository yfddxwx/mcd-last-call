#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""完整流水线入口（技能用）。与仓库根目录的 `mcd_last_call.py` 等价。

为什么多这一层：技能被安装到 `~/.workbuddy/skills/mcd-last-call/` 后，Agent 的
当前工作目录通常在用户项目里，直接 `python mcd_last_call.py` 会找不到文件。
这个脚本按**自身位置**推导技能根目录，因此在任何 cwd 下都能跑。

用法（参数与 mcd_last_call.py 完全一致）：
    python <skill-dir>/scripts/plan.py --demo --scenario 1 --deadline 13:00
    python <skill-dir>/scripts/plan.py --city 深圳 --keyword 科技园 --minutes 40
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcd_last_call import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
