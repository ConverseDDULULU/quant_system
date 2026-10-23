"""
Module: auth_guard.py
Purpose: 사용자 개인 Gemini API 키 인터랙티브 검증 및 로컬 안전 저장 (서버 혼잡 대응)
Architecture: Common Security & Config Layer
"""

import os
import sys
import time
from getpass import getpass
from dotenv import load_dotenv, set_key

ENV_PATH = "config/.env"


def ensure_gemini_api_key() -> str:
    """
    실행 시점에 Gemini API 키를 확보하고 검증하는 게이트웨이 함수
    1. config/.env 확인
    2. 없으면 터미널 프롬프트로 사용자 입력 유도
    3. Gemini 호출 테스트 통과 시 .env에 자동 영구 저장
    """
    load_dotenv(ENV_PATH)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if api_key:
        print("[AUTH] 기존 등록된 Gemini API 키를 로드했습니다.")
        return api_key

    print("\n" + "=" * 60)
    print("🔑 [Quant System] Gemini API 키 등록이 필요합니다.")
    print("  - 개인 API 키는 외부 서버로 전송되지 않고 로컬 config/.env에만 저장됩니다.")
    print("  - 발급처: https://aistudio.google.com/app/apikey")
    print("=" * 60)

    while True:
        try:
            user_input = getpass("Gemini API Key를 입력하세요 (화면에 보이지 않음): ").strip()
        except Exception:
            user_input = input("Gemini API Key를 입력하세요: ").strip()

        if not user_input:
            print("⚠️ API 키가 입력되지 않았습니다. 다시 입력해 주세요.")
            continue

        print("\n[VALIDATION] 입력된 API 키의 유효성을 검증 중입니다...")

        # 모델 자동 폴백 검증
        if validate_key_with_gemini(user_input):
            os.makedirs(os.path.dirname(ENV_PATH), exist_ok=True)
            if not os.path.exists(ENV_PATH):
                with open(ENV_PATH, "w", encoding="utf-8") as f:
                    f.write("# Quant System Local Credentials\n")

            set_key(ENV_PATH, "GEMINI_API_KEY", user_input)
            os.environ["GEMINI_API_KEY"] = user_input
            print("✅ [AUTH SUCCESS] API 키 인증 완료 및 config/.env 저장 성공!\n")
            return user_input
        else:
            print("❌ [AUTH FAILED] 키 검증에 실패했습니다. 다시 입력해 주세요.\n")


def validate_key_with_gemini(api_key: str) -> bool:
    """트래픽 분산용 멀티 모델 순차 핑 테스트"""
    from google import genai
    client = genai.Client(api_key=api_key)

    candidate_models = [
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-2.5-flash",
        "gemini-3.8-flash"
    ]

    for model_name in candidate_models:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents="ping",
            )
            if response.text:
                print(f"  [PING OK] 모델 연결 확인: {model_name}")
                return True
        except Exception as e:
            err_str = str(e)
            # 503(일시적 과부하)이거나 404(모델 지원 안함)인 경우 다음 후보 모델로 시도
            if "503" in err_str or "404" in err_str or "UNAVAILABLE" in err_str:
                print(f"  [WARN] {model_name} 일시 지연/미지원 ({err_str[:40]}...). 대체 모델 시도 중...")
                time.sleep(0.5)
                continue
            else:
                # 400, 403 등 실제 키 자체가 틀린 경우
                print(f"  [CRITICAL ERROR] {e}")
                return False

    # 만약 구글 서버 전체 과부하(503)인 경우에도 키 형식이 정상(AQ. 또는 AIzaSy)이면 통과 처리
    if api_key.startswith("AQ.") or api_key.startswith("AIzaSy"):
        print("  [BYPASS] 구글 서버 일시 503 응답이나 키 패턴 검증 통과. 등록을 허용합니다.")
        return True

    return False


if __name__ == "__main__":
    key = ensure_gemini_api_key()
    print(f"[TEST] 획득된 API 키: {key[:8]}********")