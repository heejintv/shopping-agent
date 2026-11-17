"""cache/에 저장된 검색 결과를 demo/ 폴더로 복사한다 (GitHub Pages 데모 모드용).

사용법: python make_demo.py
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
DEMO_DIR = BASE_DIR / "demo"

# 공개 저장소에 올리지 않을 내부 정보
DROP_KEYS = {"cost_usd", "cached", "elapsed_sec"}


def main() -> None:
    DEMO_DIR.mkdir(exist_ok=True)
    manifest = []
    for src in sorted(CACHE_DIR.glob("*.json")):
        data = json.loads(src.read_text(encoding="utf-8"))
        if not data.get("query") or not data.get("products"):
            continue
        for k in DROP_KEYS:
            data.pop(k, None)
        (DEMO_DIR / src.name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest.append({
            "query": data["query"],
            "file": src.name,
            "searched_at": data.get("searched_at"),
            "count": len(data["products"]),
        })
    (DEMO_DIR / "index.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"demo/에 검색 결과 {len(manifest)}개를 저장했어요:")
    for m in manifest:
        print(f"  - {m['query']} ({m['count']}개 상품, {m['searched_at']})")


if __name__ == "__main__":
    main()
