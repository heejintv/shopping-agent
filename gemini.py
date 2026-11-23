"""Gemini API 검색 엔진 (SEARCH_ENGINE=gemini 일 때 사용).

Google 검색 연동(grounding) + URL 읽기 도구를 켜고 Gemini에게 JSON으로 답하게 한 뒤,
app.py의 JSON 스키마에 맞게 값을 정리해서 claude -p 결과와 똑같은 모양으로 돌려준다.
(검색 도구와 응답 스키마 강제를 같이 못 쓰는 모델이 있어서, 텍스트로 받은 JSON을 직접 검사·정리함)

환경변수: GEMINI_API_KEY (필수), GEMINI_MODEL (기본 gemini-3.5-flash-lite)
상담 챗봇(chat.py)은 검색 도구 없이 답변만 받는 chat_gemini()를 쓴다.
"""

import json
import os
import re

DEFAULT_MODEL = "gemini-3.5-flash-lite"  # 2.5 계열은 새 사용자에게 막혀 있음(404)


class EngineError(Exception):
    """화면에 그대로 보여줄 수 있는 오류 메시지. code는 API 상태 코드(429, 503 등, 모르면 None)."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL


# ---------- 응답 JSON 추출·정리 ----------

def extract_json(text: str) -> dict:
    """답변 텍스트에서 JSON 객체를 꺼낸다 (```json 코드 블록이나 앞뒤 설명 문장 허용)."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            data = json.loads(c)
            if isinstance(data, dict):
                return data
        except ValueError:
            continue
    raise EngineError("Gemini 응답에서 JSON을 찾지 못했어요. 다시 시도해 주세요.")


def _types(schema: dict) -> list[str]:
    t = schema.get("type", [])
    return t if isinstance(t, list) else [t]


def coerce(value, schema: dict):
    """스키마 모양으로 값을 맞춘다. 맞출 수 없으면 null(허용 시) 또는 빈 값."""
    types_ = _types(schema)
    nullable = "null" in types_
    if value is None and nullable:
        return None
    if "object" in types_:
        if not isinstance(value, dict):
            return None if nullable else coerce({}, schema)
        props = schema.get("properties", {})
        out = {k: coerce(value.get(k), sub) for k, sub in props.items()}
        if nullable and any(out.get(k) in (None, "") for k in schema.get("required", [])
                            if "string" in _types(props.get(k, {}))):
            return None  # 추천(pick)처럼 필수 글자 값이 비면 통째로 null
        return out
    if "array" in types_:
        items = value if isinstance(value, list) else []
        items = [coerce(v, schema.get("items", {})) for v in items]
        items = [v for v in items if v not in (None, "")]
        return items[: schema.get("maxItems", len(items))]
    if "number" in types_ or "integer" in types_:
        if isinstance(value, bool):
            value = None
        elif isinstance(value, str):  # "129,000원" → 129000
            digits = re.sub(r"[^0-9.]", "", value)
            value = float(digits) if digits.replace(".", "", 1).isdigit() else None
        if isinstance(value, (int, float)):
            if "integer" in types_ or float(value).is_integer():
                return int(value)
            return value
        return None if nullable else 0
    if "string" in types_:
        if value is None or isinstance(value, (dict, list)):
            text = None
        else:
            text = str(value).strip() or None
        if text is not None and "enum" in schema and text not in schema["enum"]:
            text = None
        if text is None:
            return None if nullable else (schema["enum"][len(schema["enum"]) // 2] if "enum" in schema else "")
        return text
    return value


# ---------- 호출 ----------

JSON_INSTRUCTION = """

답변은 설명 없이 아래 JSON 스키마에 맞는 JSON 객체 하나만 출력해. 모르는 값은 null.
{schema}
"""


def _friendly_error(e: Exception) -> EngineError:
    err = _friendly_message(e)
    err.code = getattr(e, "code", None)
    return err


def _friendly_message(e: Exception) -> EngineError:
    from google.genai import errors
    if isinstance(e, errors.APIError):
        code = e.code
        if code == 429:
            return EngineError("Gemini API 사용 한도를 초과했어요. 잠시 후(또는 내일) 다시 시도하세요.")
        if code in (401, 403):
            return EngineError("Gemini API 키가 올바르지 않거나 권한이 없어요. GEMINI_API_KEY를 확인하세요.")
        if code == 400:
            return EngineError(f"Gemini 요청이 거절됐어요: {e.message or '잘못된 요청'}")
        if code == 404:
            return EngineError(f"Gemini 모델 '{model_name()}'을(를) 찾을 수 없어요. GEMINI_MODEL을 확인하세요.")
        if code and code >= 500:
            return EngineError("Gemini 서버에 일시적인 문제가 있어요. 잠시 후 다시 시도하세요.")
        return EngineError(f"Gemini API 오류 ({code}): {e.message or ''}".strip())
    name = type(e).__name__.lower()
    if "timeout" in name:
        return EngineError("Gemini 응답 시간이 너무 오래 걸렸어요. 다시 시도해 주세요.")
    return EngineError(f"Gemini 호출 실패: {type(e).__name__}")


def _client(timeout: int):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise EngineError("GEMINI_API_KEY 환경변수가 설정되지 않았어요.")
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise EngineError("google-genai 패키지가 없어요. pip install -r requirements.txt 를 실행하세요.")
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=timeout * 1000)), types


def chat_gemini(system: str, prompt: str, timeout: int, usage: dict | None = None) -> str:
    """도구 없이(웹 검색 안 함) 답변 텍스트만 받는다. 실패하면 EngineError(code 포함).
    usage에 dict를 넘기면 사용 토큰 수를 usage["tokens"]에 넣어 준다 (응답에 있을 때만)."""
    client, types = _client(timeout)
    config = types.GenerateContentConfig(system_instruction=system, temperature=0.4)
    try:
        resp = client.models.generate_content(model=model_name(), contents=prompt, config=config)
    except Exception as e:
        raise _friendly_error(e) from e
    text = (resp.text or "").strip()
    if not text:
        raise EngineError("Gemini가 빈 답변을 보냈어요. 다시 시도해 주세요.")
    if usage is not None:
        meta = getattr(resp, "usage_metadata", None)
        usage["tokens"] = getattr(meta, "total_token_count", None) if meta else None
    return text


def call_gemini(prompt: str, schema: dict, timeout: int) -> dict:
    """Gemini + Google 검색으로 답을 받아 schema 모양의 dict로 돌려준다. 실패하면 EngineError."""
    client, types = _client(timeout)
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch()),   # Google 검색 연동
               types.Tool(url_context=types.UrlContext())],      # 웹페이지 읽기
        temperature=0.2,
    )
    full_prompt = prompt + JSON_INSTRUCTION.format(schema=json.dumps(schema, ensure_ascii=False))
    try:
        resp = client.models.generate_content(model=model_name(), contents=full_prompt, config=config)
    except Exception as e:
        raise _friendly_error(e) from e

    text = resp.text or ""
    if not text.strip():
        reason = resp.candidates[0].finish_reason if resp.candidates else "응답 없음"
        raise EngineError(f"Gemini가 빈 답변을 보냈어요 ({reason}). 다시 시도해 주세요.")
    data = coerce(extract_json(text), schema)
    usage = resp.usage_metadata
    data["cost_usd"] = None  # 토큰 단가가 모델마다 달라 계산하지 않음 (토큰 수만 기록)
    data["tokens"] = getattr(usage, "total_token_count", None) if usage else None
    return data



def plan_json(system: str, prompt: str, schema: dict, timeout: int, usage: dict | None = None) -> dict:
    """도구 없이 한 번 호출해서 schema 모양의 JSON(dict)만 받는다 (응답 스키마 강제). 실패하면 EngineError."""
    client, types = _client(timeout)
    config = types.GenerateContentConfig(system_instruction=system, temperature=0,
                                         response_mime_type="application/json", response_json_schema=schema)
    try:
        resp = client.models.generate_content(model=model_name(), contents=prompt, config=config)
    except Exception as e:
        raise _friendly_error(e) from e
    if usage is not None:
        meta = getattr(resp, "usage_metadata", None)
        usage["tokens"] = getattr(meta, "total_token_count", None) if meta else None
    return extract_json(resp.text or "")
