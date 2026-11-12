"""临时校验脚本：语言包键是否一致、JSON 是否合法。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "src" / "kestrel" / "i18n" / "locales"


def main() -> int:
    catalogs = {}
    for path in sorted(LOCALES.glob("*.json")):
        catalogs[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        print(f"{path.stem}: {len(catalogs[path.stem])} keys")

    keys = {name: set(catalog) for name, catalog in catalogs.items()}
    base = keys.get("zh-CN", set())
    ok = True
    for name, present in keys.items():
        extra = present - base
        missing = base - present
        if extra or missing:
            ok = False
            print(f"{name}: extra={sorted(extra)[:10]} missing={sorted(missing)[:10]}")
    print("parity ok" if ok else "parity FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
