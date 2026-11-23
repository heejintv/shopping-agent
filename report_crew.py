"""일일 운영 보고서의 Crew (CrewAI): 작성 Agent + 검토 Agent. Gemini만 쓰고 보고서 하나에 최대 5번 부른다.

흐름: 작성 → 검토 → (틀리면) 수정 → 검토 … 수정은 최대 2번
검토: Python 대조(daily_report.check_numbers)에서 틀린 숫자가 있으면 바로 수정 요청 (Gemini를 아낌),
      다 맞으면 검토 Agent가 숫자의 뜻(항목·문맥)까지 대조. 둘 다 통과하면 "검토 통과"
"수정 필요"는 Python 숫자 대조가 하나라도 틀릴 때만. 숫자는 다 맞는데 검토 Agent가 마지막 본을 못 봤으면(호출 5번을 다 씀 등)
"숫자 검증 통과 (AI 최종 검토 생략)", 숫자 말고 다른 지적이 남았으면 "숫자 검증 통과 (남은 지적 있음)".

crewai는 무거워서(메모리 약 100MB) 처음 보고서를 만들 때만 불러온다.
"""

import importlib.util
import json
import os
import re
from pathlib import Path

import daily_report
import gemini
import stats
from gemini import EngineError

MAX_CALLS = 5
MAX_FIXES = 2
TIMEOUT = 60   # 초. Gemini 호출 하나
UNAVAILABLE = "이 환경에서는 보고서 기능을 쓸 수 없어요 (Python 3.12 필요)"
PASS, NEEDS_FIX = "검토 통과", "수정 필요"
PASS_NO_AI, PASS_NOTES = "숫자 검증 통과 (AI 최종 검토 생략)", "숫자 검증 통과 (남은 지적 있음)"

RULES_FILE = Path(__file__).resolve().parent / "report-rules.md"
RULE_SECTIONS = ("작성", "근거 표시", "사용 없음", "검토")


def load_rules() -> dict[str, str]:
    """report-rules.md → {제목: 내용}. 보고서를 만들 때마다 읽음. 없거나 제목이 빠졌으면 EngineError."""
    try:
        parts = re.split(r"^## +(.+?) *$", RULES_FILE.read_text(encoding="utf-8"), flags=re.M)
    except OSError:
        raise EngineError("report-rules.md를 읽을 수 없어요.") from None
    rules = {name: body.strip() for name, body in zip(parts[1::2], parts[2::2])}
    missing = [n for n in RULE_SECTIONS if not rules.get(n)]
    if missing:
        raise EngineError(f"report-rules.md에 빠진 규칙이 있어요: {', '.join(missing)}")
    return rules


class Stop(Exception):
    """Gemini 호출 한도를 다 썼거나 호출이 실패해서 멈춤."""


def available() -> bool:
    """crewai가 설치돼 있는지 (불러오지는 않음)."""
    return importlib.util.find_spec("crewai") is not None


def _load_crewai():
    os.environ.update({"CREWAI_DISABLE_TELEMETRY": "true", "CREWAI_DISABLE_TRACKING": "true",
                       "OTEL_SDK_DISABLED": "true", "CREWAI_TRACING_ENABLED": "false",
                       "CREWAI_TESTING": "true",            # 첫 실행 때 기록 보내기를 묻지 않게
                       "PYTHON_DOTENV_DISABLED": "true"})   # crewai가 .env를 따로 읽지 않게
    try:
        import crewai
    except ImportError:
        raise EngineError(UNAVAILABLE) from None
    return crewai


def _make_llm(budget: dict):
    from crewai import BaseLLM

    class GeminiLLM(BaseLLM):
        """CrewAI → 기존 gemini.chat_gemini. 호출 수를 세고, 한 번 실패하면 재시도 없이 멈춤."""

        def call(self, messages, tools=None, callbacks=None, available_functions=None,
                 from_task=None, from_agent=None, response_model=None):
            if budget["error"] is not None or budget["calls"] >= MAX_CALLS:
                raise Stop()
            budget["calls"] += 1
            if isinstance(messages, str):
                messages = [{"role": "user", "content": messages}]
            part = lambda system: "\n\n".join(str(m.get("content") or "") for m in messages   # noqa: E731
                                              if (m.get("role") == "system") == system)
            usage = {}
            try:
                text = gemini.chat_gemini(part(True), part(False), TIMEOUT, usage, json_mode=True)
            except Exception as e:
                budget["error"] = e   # CrewAI가 429를 자동 재시도하지 않게 Stop으로 바꿈
                stats.record_engine("gemini", False, getattr(e, "code", None))
                raise Stop() from None
            stats.record_engine("gemini", True)
            budget["tokens"] += usage.get("tokens") or 0
            return text

    return GeminiLLM(model=gemini.model_name(), provider="gemini")


def _ask(crewai, llm, agent_kw: dict, description: str, expected: str) -> str:
    """Agent 하나 + Task 하나짜리 Crew를 돌려 답변 텍스트를 받는다 (도구·기억·계획 없음)."""
    agent = crewai.Agent(llm=llm, verbose=False, allow_delegation=False, max_iter=1, max_retry_limit=0, **agent_kw)
    task = crewai.Task(description=description, expected_output=expected, agent=agent)
    crew = crewai.Crew(agents=[agent], tasks=[task], process=crewai.Process.sequential,
                       memory=False, planning=False, cache=False, verbose=False, tracing=False)
    return crew.kickoff().raw


def _ask_json(budget: dict, *args) -> dict:
    """_ask의 답을 JSON으로. 파싱에 실패하면 같은 요청을 한 번만 다시 (호출 한도 안에서). 또 실패하면 EngineError."""
    try:
        return gemini.extract_json(_ask(*args))
    except EngineError:
        if budget["calls"] >= MAX_CALLS:
            raise
    return gemini.extract_json(_ask(*args))


WRITER = {"role": "운영 보고서 작성자", "goal": "집계값만 근거로 사용량·성능 중심의 짧고 정확한 일일 운영 보고서를 쓴다",
          "backstory": "쇼핑 가격비교 서비스 Pickit의 운영 담당자. 모든 숫자에 근거를 남긴다. 한국어로 쓴다."}
REVIEWER = {"role": "숫자 검토자", "goal": "보고서의 모든 숫자를 aggregates와 대조해 틀린 곳을 찾는다",
            "backstory": "꼼꼼한 감사 담당자. 숫자가 맞아도 다른 항목의 값을 끌어다 썼으면 틀린 것으로 본다."}


def _write(crewai, llm, budget, rules, agg_json: str, draft=None, problems=None, no_usage=False) -> dict:
    parts = [rules["사용 없음"]] if no_usage else []
    text = "\n".join(parts + [rules["작성"], "규칙:\n" + rules["근거 표시"]]) + f"""

<aggregates>
{agg_json}
</aggregates>"""
    if problems:
        text += (f"\n\n검토에서 아래 지적을 받았다 (지적은 검토자의 문장 그대로, 여러 번이면 모두). "
                 f"하나도 빠짐없이 고치고 같은 실수를 반복하지 말고 전체를 다시 써라.\n"
                 f"<초안>\n{json.dumps(draft, ensure_ascii=False)}\n</초안>\n<검토 지적>\n" + "\n".join(problems) + "\n</검토 지적>")
    return _ask_json(budget, crewai, llm, WRITER, text,
                     "설명 없이 JSON 하나만: " + (json.dumps(daily_report.NO_USAGE_DRAFT, ensure_ascii=False) if no_usage
                                           else '{"summary": ["", "", ""], "anomalies": [""], "suggestions": [""]}'))


def _review(crewai, llm, budget, rules, agg_json: str, draft: dict) -> list[str] | None:
    """검토 Agent → 틀린 곳 목록 (빈 목록이면 통과). 답을 읽을 수 없으면 None."""
    prompt = rules["검토"] + f"""

<aggregates>
{agg_json}
</aggregates>
<초안>
{json.dumps(draft, ensure_ascii=False)}
</초안>"""
    try:
        data = _ask_json(budget, crewai, llm, REVIEWER, prompt,
                         '설명 없이 JSON 하나만: {"pass": true 또는 false, "problems": ["틀린 곳과 올바른 값"]}')
    except EngineError:
        return None
    problems = [str(p)[:300] for p in data["problems"]] if isinstance(data.get("problems"), list) else []
    if data.get("pass") is True and not problems:
        return []
    return problems[:20] or (["검토 Agent가 통과시키지 않음 (이유 없음)"] if data.get("pass") is False else None)


def run(agg: dict) -> dict:
    """aggregates → {draft, checks, verdict, problems, fixes, calls, tokens, reviewed}. 초안도 못 만들면 EngineError."""
    crewai = _load_crewai()
    rules = load_rules()
    budget = {"calls": 0, "tokens": 0, "error": None}
    llm = _make_llm(budget)
    agg_json = json.dumps({k: agg[k] for k in ("date", "label", "no_usage", "metrics", "anomalies", "top_anomaly", "notes")}, ensure_ascii=False)
    no_usage = bool(agg.get("no_usage"))
    history = []   # 지금까지 받은 지적 전부 (수정할 때마다 같이 보냄)
    draft, checks, problems, fixes, reviewed, passed, skipped = None, [], [], 0, False, False, False

    while True:
        data, format_error = None, None
        try:
            data = _write(crewai, llm, budget, rules, agg_json, draft, history, no_usage)
        except EngineError as e:   # 다시 해도 JSON이 아님
            format_error = e
        except Exception as e:
            _check_stop(e, budget, draft)
            problems.append("수정본을 만들지 못해 이전 초안을 보여줌")
            break
        try:
            if format_error:
                raise format_error
            draft = daily_report.clean_draft(data, no_usage)
            reviewed = False
            checks = daily_report.check_numbers(draft, agg)
            wrong = [f"{c['section']} {c['line']}번째 줄 {c['number'] or c['id']}: {c['reason']}"
                        for c in checks if not c["ok"]]
            problems = wrong + (daily_report.no_usage_problems(draft) if no_usage else
                                daily_report.bad_summary(draft, agg) + daily_report.missing_anomalies(draft, agg)
                                + daily_report.bad_suggestions(draft))
        except (EngineError, ValueError) as e:
            problems = [f"초안 형식이 잘못됨: {e}"] + (problems if draft else [])   # 앞선 초안이 있으면 그대로 둠
        if not problems:
            if budget["calls"] >= MAX_CALLS:
                problems = [f"Gemini 호출 {MAX_CALLS}번을 다 써서 마지막 수정본은 Python 대조만 통과함 (검토 Agent 미확인)"]
                skipped = True
                break
            try:
                found = _review(crewai, llm, budget, rules, agg_json, draft)
            except Exception as e:
                _check_stop(e, budget, draft)
                problems = ["검토 Agent 호출에 실패해 Python 대조만 통과함"]
                skipped = True
                break
            if found is None:
                problems = ["검토 Agent의 답을 읽을 수 없어 Python 대조만 통과함"]
                skipped = True
                break
            reviewed = True
            problems = found
            if not problems:
                passed = True
                break
        if fixes >= MAX_FIXES or budget["calls"] >= MAX_CALLS:
            break
        fixes += 1
        history += [f"{fixes}차 검토: {p}" for p in problems]

    if draft is None:
        raise EngineError("AI가 보고서 형식에 맞게 쓰지 못했어요. 다시 만들어 주세요.")
    verdict = (PASS if passed else NEEDS_FIX if any(not c["ok"] for c in checks)
               else PASS_NO_AI if skipped else PASS_NOTES)
    return {"draft": draft, "checks": checks, "verdict": verdict, "problems": problems, "fixes": fixes,
            "calls": budget["calls"], "tokens": budget["tokens"], "reviewed": reviewed}


def _check_stop(e: Exception, budget: dict, draft) -> None:
    """초안이 있고 Gemini 호출 때문에 멈춘 거면 그대로 진행, 아니면 오류로."""
    if draft is not None:
        return
    cause = budget["error"]
    if isinstance(cause, EngineError):
        raise cause
    raise EngineError("보고서를 만들지 못했어요. 잠시 후 다시 시도해 주세요.") from e
