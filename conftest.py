"""确保仓库根目录在 sys.path 上，使 `checkout` 包可被导入。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
