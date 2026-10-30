"""Gemini REST 호출 공통 함수

- API 키는 URL(?key=)이 아니라 헤더로 보낸다. 오류 메시지·로그에 키가 남지 않게 하기 위함.
- 모델은 .env의 GEMINI_MODEL로 바꿀 수 있다 (구글이 모델을 중단하면 여기만 바꾸면 됨).
- 실패하면 예외 대신 None을 돌려주고, 호출한 쪽이 기본 문구로 대체한다.
"""

import os
import time

import requests

DEFAULT_MODEL = "gemini-3.5-flash"
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def generate_text(prompt: str, temperature: float = 0.3, timeout: float = 30) -> str | None:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL
    try:
        for attempt in range(2):
            res = requests.post(
                API_URL.format(model=model),
                headers={"x-goog-api-key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": temperature},
                },
                timeout=timeout,
            )
            # 일시적 과부하(429/503)는 한 번만 잠깐 쉬고 재시도
            if res.status_code not in (429, 503) or attempt == 1:
                break
            time.sleep(3)
        if res.status_code != 200:
            try:
                message = res.json()["error"]["message"]
            except Exception:
                message = res.text
            print(f"⚠️ Gemini 응답 오류 ({model}, HTTP {res.status_code}): {message[:150]}")
            return None
        return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except requests.RequestException as e:
        print(f"⚠️ Gemini 호출 실패 ({model}): {type(e).__name__}")
        return None
    except (KeyError, IndexError, ValueError):
        print(f"⚠️ Gemini 응답 형식 오류 ({model})")
        return None
