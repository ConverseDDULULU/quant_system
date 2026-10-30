import os
import json
import math
import time
import pytz
import requests
from datetime import datetime
from dotenv import load_dotenv

# 1. 환경변수 탐색 및 로드
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
    """키움증권 REST Open API 실서버 클라이언트 (미국주식 전문)"""

    def __init__(self):
        self.app_key = os.getenv("KIWOOM_APP_KEY", "").strip()
        self.app_secret = (
            os.getenv("KIWOOM_APP_SECRET") or os.getenv("KIWOOM_SECRET_KEY") or ""
        ).strip()
        self.account_no = os.getenv("KIWOOM_ACCOUNT_NO", "").strip()

        self.full_account_no = None  # 10자리 정규 계좌번호
        self.access_token = None
        self.token_expires_at = None  # KST 기준 만료 시각
        self.base_url = "https://api.kiwoom.com"
        self._us_stock_master_cache = {}
        self._us_stock_master_cache_time = {}
        self._us_rank_cache = {}
        self._us_rank_cache_time = {}
        self._us_minute_chart_cache = {}
        self._us_minute_chart_cache_time = {}

        self.authenticate()

    def authenticate(self):
        """OAuth 2.0 접근토큰 발급 (au10001) 및 계좌번호 확인 (ka00001)"""
        token_url = f"{self.base_url}/oauth2/token"
        headers = {"Content-Type": "application/json;charset=UTF-8"}
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
        self.token_expires_at = self._parse_token_expiry(data.get("expires_dt"))

        # 계좌번호 조회 (ka00001)
        acct_url = f"{self.base_url}/api/dostk/acnt"
        acct_headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": "ka00001",
        }

        try:
            res_acct = requests.post(acct_url, headers=acct_headers, json={}, timeout=10)
            acct_data = res_acct.json()
            fallback_acct = (
                self.account_no if len(self.account_no) == 10 else f"{self.account_no}10"
            )
            self.full_account_no = acct_data.get("acctNo", fallback_acct)
        except Exception:
            self.full_account_no = (
                self.account_no if len(self.account_no) == 10 else f"{self.account_no}10"
            )

        print(f"🔑 [KiwoomClient] 로그인 완료: 계좌번호={self.full_account_no}")

    @staticmethod
    def _parse_token_expiry(expires_dt):
        """au10001 expires_dt(YYYYMMDDHHMMSS, KST)를 aware datetime으로 변환"""
        try:
            naive = datetime.strptime(str(expires_dt).strip(), "%Y%m%d%H%M%S")
            return pytz.timezone("Asia/Seoul").localize(naive)
        except (TypeError, ValueError):
            return None

    def _ensure_token(self):
        """토큰이 없거나 만료 10분 전이면 재발급한다 (24시간 가동 대비)."""
        if not self.access_token:
            self.authenticate()
            return
        if self.token_expires_at is None:
            return
        now = datetime.now(pytz.timezone("Asia/Seoul"))
        if (self.token_expires_at - now).total_seconds() < 600:
            print("🔄 [KiwoomClient] 토큰 만료 임박, 재발급합니다.")
            self.authenticate()

    def get_us_holdings(self) -> list:
        """미국주식 원장잔고확인 (ust21070)"""
        self._ensure_token()
        url = f"{self.base_url}/api/us/acnt"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": "ust21070",
        }
        body = {"acctNo": self.full_account_no}

        try:
            res = requests.post(url, headers=headers, json=body, timeout=10)
            data = res.json() if res.status_code == 200 else {}
            if res.status_code != 200 or str(data.get("return_code", "0")) != "0":
                # 실패 시 빈 목록이 '보유 없음'으로 오인되므로 로그로 남긴다
                print(
                    f"⚠️ 미국주식 보유종목 조회 오류: HTTP {res.status_code} "
                    f"[{data.get('return_code')}] {data.get('return_msg', '응답 없음')}"
                )
                return []
            return data.get("result_list") or data.get("output1") or []
        except Exception as e:
            print(f"⚠️ 미국주식 보유종목 조회 실패: {e}")
            return []

    def get_us_cash_usd(self) -> float | None:
        """통화별 예수금(ust21120)의 USD 외화예수금. 실패 시 None

        ust21110의 fc_ord_alowa(주문가능금액)는 원화대용·미체결 주문에 따라 흔들려
        계좌 평가액으로 쓰면 일일 수익률이 왜곡된다. 평가액에는 이 값을 쓴다.
        """
        self._ensure_token()
        try:
            res = requests.post(
                f"{self.base_url}/api/us/acnt",
                headers={
                    "Content-Type": "application/json;charset=UTF-8",
                    "authorization": f"Bearer {self.access_token}",
                    "api-id": "ust21120",
                },
                json={"cmsn_incl_tp": "0", "exrt_tp": "0"},
                timeout=10,
            )
            data = res.json() if res.status_code == 200 else {}
            for item in data.get("result_list") or []:
                if isinstance(item, dict) and item.get("crnc_code") == "USD":
                    return safe_float(item.get("fx_entr"))
        except Exception as e:
            print(f"⚠️ 외화예수금(ust21120) 조회 실패: {e}")
        return None

    def get_us_balance(self) -> dict:
        """해외주식 예수금 조회 (ust21110) + 보유잔고 병합"""
        self._ensure_token()
        url = f"{self.base_url}/api/us/acnt"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": "ust21110",
        }
        body = {"acctNo": self.full_account_no}

        try:
            res = requests.post(url, headers=headers, json=body, timeout=10)
            data = res.json() if res.status_code == 200 else {}
        except Exception as e:
            print(f"⚠️ 예수금 조회 통신 실패: {e}")
            data = {}

        data["holdings_list"] = self.get_us_holdings()
        return data

    def get_balance(self) -> dict:
        """스케줄러 호환 잔고 리스트 반환"""
        return {"result_list": self.get_us_holdings()}

    def get_us_stock_master(
        self, exchange: str = "ND", max_pages: int = 10, refresh: bool = False
    ) -> list:
        """키움 미국주식 종목 마스터(usa10099)를 조회한다."""
        exchange = exchange.upper().strip()
        cached = self._us_stock_master_cache.get(exchange)
        cached_at = self._us_stock_master_cache_time.get(exchange, 0.0)
        if cached and not refresh and time.monotonic() - cached_at < 21600:
            return list(cached)

        self._ensure_token()

        url = f"{self.base_url}/api/us/stkinfo"
        rows = []
        cont_yn = "N"
        next_key = ""

        for _ in range(max_pages):
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {self.access_token}",
                "api-id": "usa10099",
            }
            if cont_yn == "Y" and next_key:
                headers["cont-yn"] = cont_yn
                headers["next-key"] = next_key

            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json={"stex_tp": exchange},
                    timeout=15,
                )
                data = response.json()
            except Exception as exc:
                print(f"⚠️ 키움 미국주식 마스터 조회 실패: {exc}")
                break

            if str(data.get("return_code", "0")) != "0":
                print(
                    "⚠️ 키움 미국주식 마스터 오류: "
                    f"[{data.get('return_code')}] {data.get('return_msg', '응답 없음')}"
                )
                break

            page_rows = data.get("list") or []
            if isinstance(page_rows, dict):
                page_rows = [page_rows]
            if not isinstance(page_rows, list) or not page_rows:
                break
            rows.extend(row for row in page_rows if isinstance(row, dict))

            cont_yn = response.headers.get("cont-yn", "N").upper()
            next_key = response.headers.get("next-key", "")
            if cont_yn != "Y" or not next_key:
                break

        if rows:
            self._us_stock_master_cache[exchange] = rows
            self._us_stock_master_cache_time[exchange] = time.monotonic()
        return rows

    def get_us_exchange_code(self, ticker: str) -> str:
        """종목 마스터에서 키움 미국 거래소 코드를 찾는다."""
        ticker = ticker.upper().strip()
        for exchange in ("ND", "NY", "NA"):
            for item in self.get_us_stock_master(exchange):
                if str(item.get("stk_cd", "")).upper().strip() == ticker:
                    return exchange
        return ""

    def get_us_stock_quote(self, ticker: str, exchange: str = "ND") -> dict:
        """키움 미국주식 현재가·누적거래량(usa20100)을 조회한다."""
        self._ensure_token()

        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": "usa20100",
        }
        try:
            response = requests.post(
                f"{self.base_url}/api/us/mrkcond",
                headers=headers,
                json={"stex_tp": exchange.upper(), "stk_cd": ticker.upper().strip()},
                timeout=10,
            )
            data = response.json()
        except Exception as exc:
            print(f"⚠️ {ticker} 키움 시세 조회 실패: {exc}")
            return {}

        if str(data.get("return_code", "0")) != "0":
            return {}
        return data

    def _get_us_rank_rows(
        self, api_id: str, body: dict, cache_key: tuple, max_pages: int, refresh: bool
    ) -> list:
        """/api/us/rkinfo 순위 조회 공통 (연속조회, 30초 캐시)"""
        cached = self._us_rank_cache.get(cache_key)
        cached_at = self._us_rank_cache_time.get(cache_key, 0.0)
        if cached and not refresh and time.monotonic() - cached_at < 30:
            return list(cached)

        self._ensure_token()

        rows = []
        cont_yn = "N"
        next_key = ""
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": api_id,
        }

        for _ in range(max_pages):
            page_headers = dict(headers)
            if cont_yn == "Y" and next_key:
                page_headers["cont-yn"] = cont_yn
                page_headers["next-key"] = next_key
            try:
                response = requests.post(
                    f"{self.base_url}/api/us/rkinfo",
                    headers=page_headers,
                    json=body,
                    timeout=15,
                )
                data = response.json()
            except Exception as exc:
                print(f"⚠️ 키움 미국 순위 조회 실패({api_id}): {exc}")
                break

            if str(data.get("return_code", "0")) != "0":
                print(
                    f"⚠️ 키움 미국 순위 오류({api_id}): "
                    f"[{data.get('return_code')}] {data.get('return_msg', '응답 없음')}"
                )
                break

            page_rows = data.get("result_list") or []
            if isinstance(page_rows, dict):
                page_rows = [page_rows]
            if not isinstance(page_rows, list) or not page_rows:
                break
            rows.extend(row for row in page_rows if isinstance(row, dict))

            cont_yn = response.headers.get("cont-yn", "N").upper()
            next_key = response.headers.get("next-key", "")
            if cont_yn != "Y" or not next_key:
                break
            time.sleep(0.2)

        if rows:
            self._us_rank_cache[cache_key] = rows
            self._us_rank_cache_time[cache_key] = time.monotonic()
        return rows

    def get_us_today_volume_top(
        self, exchange: str = "2", max_pages: int = 5, refresh: bool = False
    ) -> list:
        """키움 미국주식 당일 거래량 상위(usa20530)를 조회한다. 거래량 키: acc_trde_qty"""
        exchange = str(exchange)
        body = {
            "stex_tp": exchange,
            "inds_cd": "",
            "stk_tp": "1",
            "trde_qty_tp": "0",
            "qry_tp": "0",
            "stk_cnd": "0",
            "pric_cnd": "0",
            "trde_prica_cnd": "0",
        }
        return self._get_us_rank_rows(
            "usa20530", body, ("usa20530", exchange), max_pages, refresh
        )

    def get_us_top_gainers(
        self,
        exchange: str = "0",
        min_trade_value: str = "100",
        max_pages: int = 1,
        refresh: bool = False,
    ) -> list:
        """키움 미국주식 전일대비 등락률 상위(usa20910, 상승률순)를 조회한다.

        exchange: 0 전체, 1 NYSE, 2 NASDAQ, 3 AMEX
        min_trade_value: 거래대금 하한(USD 1만 단위, "100"=100만 달러 이상)
        응답의 거래량 키는 trde_qty (usa20530의 acc_trde_qty와 다름)
        """
        exchange = str(exchange)
        body = {
            "stex_tp": exchange,
            "inds_cd": "000",
            "inds_cls_tp": "0",
            "sort_tp": "1",
            "stk_tp": "1",
            "stk_cnd": "0",
            "pric_cnd": "0",
            "trde_prica_cnd": str(min_trade_value),
            "trde_qty_tp": "0",
        }
        return self._get_us_rank_rows(
            "usa20910", body, ("usa20910", exchange, str(min_trade_value)), max_pages, refresh
        )

    def _get_us_stock_chart_rows(
        self,
        ticker: str,
        exchange: str,
        api_id: str,
        body: dict,
        max_pages: int,
        cache_key: tuple,
    ) -> list:
        self._ensure_token()
        cached_rows = self._us_minute_chart_cache.get(cache_key)
        cached_at = self._us_minute_chart_cache_time.get(cache_key, 0.0)
        if cached_rows and time.monotonic() - cached_at < 10:
            return list(cached_rows)

        rows = []
        cont_yn = "N"
        next_key = ""
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": api_id,
        }

        for _ in range(max_pages):
            page_headers = dict(headers)
            if cont_yn == "Y" and next_key:
                page_headers["cont-yn"] = cont_yn
                page_headers["next-key"] = next_key
            try:
                response = requests.post(
                    f"{self.base_url}/api/us/chart",
                    headers=page_headers,
                    json=body,
                    timeout=15,
                )
                data = response.json()
            except Exception as exc:
                print(f"⚠️ {ticker} 키움 차트 조회 실패({api_id}): {exc}")
                break

            if str(data.get("return_code", "0")) != "0":
                print(
                    f"⚠️ {ticker} 키움 차트 오류({api_id}): "
                    f"[{data.get('return_code')}] {data.get('return_msg', '응답 없음')}"
                )
                break

            page_rows = data.get("result_list") or []
            if isinstance(page_rows, dict):
                page_rows = [page_rows]
            if not isinstance(page_rows, list) or not page_rows:
                break
            rows.extend(row for row in page_rows if isinstance(row, dict))

            cont_yn = response.headers.get("cont-yn", "N").upper()
            next_key = response.headers.get("next-key", "")
            if cont_yn != "Y" or not next_key:
                break
            time.sleep(0.2)

        rows.sort(
            key=lambda row: (
                str(row.get("bus_dt", row.get("dt", ""))),
                str(row.get("cntr_tm", "")),
            )
        )
        if rows:
            self._us_minute_chart_cache[cache_key] = rows
            self._us_minute_chart_cache_time[cache_key] = time.monotonic()
        return rows

    def get_us_stock_minute_chart(
        self,
        ticker: str,
        exchange: str = "ND",
        interval: str = "1",
        start_date: str | None = None,
        max_pages: int = 5,
    ) -> list:
        """키움 미국주식 분 차트(usa06011)를 조회한다."""
        if start_date is None:
            start_date = datetime.now(pytz.timezone("America/New_York")).strftime("%Y%m%d")
        body = {
            "stex_tp": exchange.upper(),
            "stk_cd": ticker.upper().strip(),
            "strt_dt": start_date,
            "tic_scope": str(interval),
            "upd_stkpc_tp": "0",
            "exrt_appl_tp": "1",
        }
        cache_key = (
            "usa06011", exchange.upper(), ticker.upper().strip(), str(interval), start_date
        )
        return self._get_us_stock_chart_rows(
            ticker, exchange, "usa06011", body, max_pages, cache_key
        )

    def get_us_stock_period_chart(
        self,
        ticker: str,
        exchange: str = "ND",
        period: str = "D",
        start_date: str | None = None,
        max_pages: int = 5,
    ) -> list:
        """키움 미국주식 일·주·월 차트(usa06012/13/14)를 조회한다."""
        period_code = period.upper()
        api_id_by_period = {"D": "usa06012", "W": "usa06013", "M": "usa06014"}
        api_id = api_id_by_period.get(period_code)
        if not api_id:
            raise ValueError(f"지원하지 않는 차트 주기입니다: {period}")
        if start_date is None:
            start_date = datetime.now(pytz.timezone("America/New_York")).strftime("%Y%m%d")

        body = {
            "stex_tp": exchange.upper(),
            "stk_cd": ticker.upper().strip(),
            "strt_dt": start_date,
            "upd_stkpc_tp": "1",
            "exrt_appl_tp": "0",
        }
        cache_key = (api_id, exchange.upper(), ticker.upper().strip(), start_date)
        return self._get_us_stock_chart_rows(
            ticker, exchange, api_id, body, max_pages, cache_key
        )

    def get_deposit(self) -> dict:
        """주문가능 외화(USD) 및 원화 예수금 추출"""
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

            return {"usd_deposit": usd, "krw_deposit": krw}
        except Exception as e:
            print(f"⚠️ 예수금 파싱 오류: {e}")
            return {"krw_deposit": 0.0, "usd_deposit": 0.0}

    def _get_current_price(self, ticker: str) -> float:
        """가격이 비어 있거나 0일 때 키움 현재가로 보정한다."""
        try:
            exchange = self.get_us_exchange_code(ticker)
            if not exchange:
                return 0.0
            quote = self.get_us_stock_quote(ticker, exchange)
            # usa20100 cur_prc는 등락 부호가 붙어 온다(예: "-0.0206")
            return abs(safe_float(quote.get("cur_prc")))
        except Exception:
            pass
        return 0.0

    def _normalize_order_price(self, ticker: str, price) -> float:
        """문자열/0값/NaN을 모두 float로 정리해 실제 주문가격으로 변환한다."""
        try:
            if price is None:
                return self._get_current_price(ticker)

            if isinstance(price, str):
                price = price.strip().replace(",", "")
                if price in ("", "-"):
                    return self._get_current_price(ticker)

            value = float(price)
            if not math.isfinite(value):
                return self._get_current_price(ticker)
            if value <= 0:
                return self._get_current_price(ticker)
            return value
        except (TypeError, ValueError):
            return self._get_current_price(ticker)

    def order_us_stock(
        self,
        ticker: str,
        order_type: str,
        qty: int,
        price: float = 0.0,
        exchange: str = "",
    ) -> dict:
        exchange_code = str(exchange or "").upper()
        is_buy = order_type.upper() == "BUY"

        if exchange_code not in {"ND", "NY", "NA"}:
            return {
                "return_code": -1,
                "return_msg": f"거래소 확인 불가로 주문을 차단했습니다: {ticker} ({exchange_code or 'UNKNOWN'})",
            }
        is_soxl_amex = ticker.upper().strip() == "SOXL" and exchange_code == "NA"
        if is_buy and exchange_code not in {"ND", "NY"} and not is_soxl_amex:
            return {
                "return_code": -1,
                "return_msg": f"NASDAQ·NYSE 또는 SOXL(NA)만 매수할 수 있습니다: {ticker} (거래소 {exchange_code})",
            }

        self._ensure_token()

        if qty <= 0:
            return {"return_code": -1, "return_msg": "주문 수량이 0 이하입니다."}

        normalized_price = self._normalize_order_price(ticker, price)
        if normalized_price <= 0:
            print(f"❌ [주문 오류] {ticker} 주문 단가({normalized_price})가 유효하지 않습니다. 현재가를 확인해주세요.")
            return {"return_code": -1, "return_msg": "유효하지 않은 주문 단가입니다."}

        price = float(normalized_price)
        tr_id = "ust20000" if is_buy else "ust20001"
        url = f"{self.base_url}/api/us/ordr"

        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.access_token}",
            "api-id": tr_id,
        }

        # 미국 호가 단위(Tick Size) 엄격 적용: $1 이상 소수점 2자리, $1 미만 4자리
        if price >= 1.0:
            formatted_price = f"{round(price, 2):.2f}"
        elif price > 0:
            formatted_price = f"{round(price, 4):.4f}"
        else:
            formatted_price = "0.00"

        body = {
            "stex_tp": exchange_code,
            "stk_cd": ticker.upper().strip(),  # 종목코드
            "ord_qty": str(int(qty)),  # 수량 문자열
            "ord_uv": formatted_price,  # 검증된 호가 단가 문자열
            "trde_tp": "00",  # 보통 지정가
        }

        if self.full_account_no:
            body["acntNo"] = self.full_account_no

        print(
            f"🚀 [키움 공식 실주문] {order_type} {ticker} {qty}주 @ ${formatted_price} (TR: {tr_id})"
        )

        try:
            res = requests.post(url, headers=headers, json=body, timeout=10)
            data = res.json()
            print(f"📦 [키움 주문 원본 응답]: {data}")
            return data
        except Exception as e:
            print(f"⚠️ 주문 전송 예외 발생: {e}")
            return {"return_code": -1, "return_msg": str(e)}


if __name__ == "__main__":
    client = KiwoomClient()
    deposit = client.get_deposit()
    print(f"• 계좌 예수금: 원화 {deposit['krw_deposit']:,}원 | 외화 ${deposit['usd_deposit']:.2f}")
    holdings = client.get_us_holdings()
    print(f"• 보유 종목 수: {len(holdings)}개")