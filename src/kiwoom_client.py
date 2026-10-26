import os
import json
import requests
from dotenv import load_dotenv

# 1. config/.env 우선 로드, 없으면 프로젝트 루트의 .env 탐색
env_config_path = os.path.join(os.path.dirname(__file__), "../config/.env")
env_root_path = os.path.join(os.path.dirname(__file__), "../.env")

if os.path.exists(env_config_path):
    load_dotenv(dotenv_path=env_config_path, override=True)
elif os.path.exists(env_root_path):
    load_dotenv(dotenv_path=env_root_path, override=True)
else:
    load_dotenv(override=True)


def safe_float(val, default: float = 0.0) -> float:
    """문자열 숫자를 안전하게 float으로 변환 (콤마 제거 포함)"""
    try:
        if val is None:
            return default
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


class KiwoomClient:
    """키움증권 REST Open API 실서버 클라이언트 (미국주식 전용)"""

    def __init__(self):
        self.app_key = os.getenv("KIWOOM_APP_KEY", "").strip()
        self.app_secret = (
            os.getenv("KIWOOM_APP_SECRET") or os.getenv("KIWOOM_SECRET_KEY") or ""
        ).strip()
        self.account_no = os.getenv("KIWOOM_ACCOUNT_NO", "").strip()

        self.full_account_no = None  # 10자리 계좌번호
        self.access_token = None
        self.base_url = "https://api.kiwoom.com"

        self.authenticate()

    def authenticate(self):
        """OAuth 2.0 Bearer 토큰 발급 및 10자리 계좌번호 취득"""
        token_url = f"{self.base_url}/oauth2/token"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret,
        }

        try:
            res = requests.post(token_url, headers=headers, json=payload, timeout=10)
            data = res.json()
        except Exception as e:
            raise RuntimeError(f"키움 토큰 요청 통신 에러: {e}")

        self.access_token = data.get("token") or data.get("access_token")
        if not self.access_token:
            raise RuntimeError(f"키움 토큰 발급 실패: {data}")

        # 계좌번호 확인 (ka00001)
        acct_url = f"{self.base_url}/api/dostk/acnt"
        acct_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": "ka00001",
        }

        try:
            res_acct = requests.post(acct_url, headers=acct_headers, json={}, timeout=10)
            acct_data = res_acct.json()
            fallback_acct = self.account_no if len(self.account_no) == 10 else f"{self.account_no}10"
            self.full_account_no = acct_data.get("acctNo", fallback_acct)
        except Exception:
            self.full_account_no = self.account_no if len(self.account_no) == 10 else f"{self.account_no}10"

        print(f"🔑 [KiwoomClient] 로그인 완료: 계좌번호={self.full_account_no}")

    def get_us_holdings(self) -> list:
        """
        미국주식 보유종목 원장 상세 조회 (TR: ust21070 - 미국주식 원장잔고확인)
        """
        url = f"{self.base_url}/api/us/acnt"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": "ust21070",
        }
        body = {
            "acctNo": self.full_account_no,
        }
        try:
            res = requests.post(url, headers=headers, json=body, timeout=10)
            if res.status_code == 200:
                data = res.json()
                print("📦 [DEBUG ust21070 종목 원장 응답]:", json.dumps(data, indent=2, ensure_ascii=False))
                # 키움 응답 배열 키 탐색
                return data.get("output1") or data.get("output") or data.get("result_list") or []
            return []
        except Exception as e:
            print(f"⚠️ 보유종목 조회 통신 예외: {e}")
            return []

    def get_us_balance(self) -> dict:
        """
        미국주식 계좌 전체 통합 조회
        - 예수금: ust21110 (해외주식 예수금)
        - 보유종목: ust21070 (미국주식 원장잔고확인)
        """
        url = f"{self.base_url}/api/us/acnt"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": "ust21110",
        }
        body = {
            "acctNo": self.full_account_no,
        }
        try:
            res = requests.post(url, headers=headers, json=body, timeout=10)
            data = res.json() if res.status_code == 200 else {}
        except Exception as e:
            print(f"⚠️ 예수금 조회 통신 예외: {e}")
            data = {}

        # ust21070 종목 리스트 병합
        holdings = self.get_us_holdings()
        data["holdings_list"] = holdings
        return data

    def get_deposit(self) -> dict:
        """키움 외화/원화 예수금 조회 (ust21110 활용)"""
        try:
            balance = self.get_us_balance() or {}
            krw = safe_float(balance.get("krw_entra") or balance.get("krw_dncl_amt") or 0.0)

            usd = 0.0
            result_list = balance.get("result_list") or []
            if isinstance(result_list, dict):
                result_list = [result_list]

            for item in result_list:
                if isinstance(item, dict) and item.get("crnc_code") == "USD":
                    usd = safe_float(item.get("fc_ord_alowa") or item.get("fc_entra") or 0.0)
                    break

            return {
                "usd_deposit": usd,
                "krw_deposit": krw,
            }
        except Exception as e:
            print(f"⚠️ get_deposit 처리 오류: {e}")
            return {"krw_deposit": 0.0, "usd_deposit": 0.0}

    def order_us_stock(self, ticker: str, order_type: str, qty: int, price: float = 0.0) -> dict:
        """미국주식 주문 발송 (매수: ust20000, 매도: ust20001)"""
        tr_id = "ust20000" if order_type.upper() == "BUY" else "ust20001"
        url = f"{self.base_url}/api/us/order"

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": tr_id,
        }
        prc_tp = "00" if price > 0 else "01"

        body = {
            "acctNo": self.full_account_no,
            "stk_cd": ticker.upper(),
            "ord_qty": str(qty),
            "ord_pric": f"{price:.2f}" if price > 0 else "0",
            "prc_tp": prc_tp,
        }

        print(f"🚀 [키움 실주문 전송] {order_type} {ticker} {qty}주 (TR: {tr_id})")
        try:
            res = requests.post(url, headers=headers, json=body, timeout=10)
            return res.json()
        except Exception as e:
            print(f"⚠️ 주문 전송 예외 발생: {e}")
            return {"return_code": -1, "return_msg": str(e)}


if __name__ == "__main__":
    client = KiwoomClient()
    bal = client.get_us_balance()
    print("• 최종 통합 잔고 응답:")
    print(f"  - 예수금: 원화 {bal.get('krw_entra')}원")
    print(f"  - 보유종목 수: {len(bal.get('holdings_list', []))}개")