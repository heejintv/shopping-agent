"""일일 운영 보고서를 명령으로 만들기 (/daily-report). 관리자 화면과 같은 기능을 실행하고 결과 JSON을 출력한다.

실행: .venv/bin/python make_report.py   (끝 코드: 0 성공, 1 실패, 2 crewai 없음)
"""

import json
import sys

import report_crew


def main() -> int:
    if not report_crew.available():   # 앱(설정)을 불러오기 전에 멈춤
        print(report_crew.UNAVAILABLE)
        return 2
    import app
    app._make_report("cli")
    print(json.dumps(app._report, ensure_ascii=False, indent=1))
    return 0 if app._report["state"] == "draft" else 1


if __name__ == "__main__":
    sys.exit(main())
