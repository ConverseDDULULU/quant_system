"""키움 미체결(대기) 주문 추적

매 사이클 키움 미체결 조회(client.get_us_open_orders)로 대기 주문을 다시 읽는다.
메모리에만 두지 않으므로 재시작해도 알아낸다.
- 대기 매수는 보유로 계산 (11/5 PSQ가 잔고에 안 보이자 SH까지 산 일 방지)
- 정해진 시간 안에 체결 안 된 매수는 취소, 장마감 청산 전에는 대기 매수를 모두 취소
- 대기 매도가 있으면 매도 주문을 다시 내지 않는다
- 조회 실패는 "대기 없음"이 아니다: 직전에 알던 목록을 유지하고 그 사이클의 신규 매수를 쉰다
- 취소 접수(return_code 0)는 취소 완료가 아니고, 거절(한도 초과 등 일시적일 수 있음)은 주문이 살아 있을 수 있다.
  그래서 어느 쪽이든 미체결 목록에서 사라질 때까지 대기 주문으로 계산한다
  (이미 체결된 주문은 잔량이 없어 다음 조회에서 저절로 빠진다)
"""

import time
from datetime import datetime, timedelta

import pytz

KST = pytz.timezone("Asia/Seoul")
SLOW_SELL_WARN_SECONDS = 120
SELL_BLOCK_SECONDS = 180  # 대기 매도가 재주문을 막는 최대 시간 (손절이 무기한 막히지 않게)
SESSION_MARGIN_SECONDS = 60
CANCEL_RETRY_SECONDS = 60  # 취소가 접수됐는데도 목록에 남아 있으면 다시 요청하는 간격

_known: dict[tuple, dict] = {}  # 직전에 알던 대기 주문
_cancel_requested: dict[tuple, float] = {}  # 취소를 접수한 주문 -> 접수 시각 (time.monotonic)
_cancel_failed_logged: set[tuple] = set()  # 취소 실패 로그를 이미 남긴 주문 (사이클마다 반복 출력 방지)
_block_lifted_logged: set[tuple] = set()  # 차단 해제 로그를 이미 남긴 대기 매도


def _key(order: dict) -> tuple:
    return (order.get("order_no"), order.get("ticker"))


def reset_state() -> None:
    _known.clear()
    _cancel_requested.clear()
    _cancel_failed_logged.clear()
    _block_lifted_logged.clear()


def known_orders() -> list[dict]:
    """직전에 알던 대기 주문 (조회 실패·오류 때 사용)"""
    return list(_known.values())


def order_age_seconds(order_time: str, now: datetime) -> float | None:
    """주문시간(KST HH:mm:ss)부터 지난 초. 지금보다 뒤 시각이면 어제 주문(한국 자정을 넘긴 장)으로 본다"""
    try:
        hour, minute, second = (int(part) for part in str(order_time).strip().split(":"))
        placed = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    except (TypeError, ValueError):
        return None
    if placed > now + timedelta(seconds=5):
        placed -= timedelta(days=1)
    return max(0.0, (now - placed).total_seconds())


def fetch(client, session_start: datetime | None = None, now: datetime | None = None) -> tuple[list[dict], bool]:
    """(대기 주문 목록, 조회 성공 여부). 실패하면 직전에 알던 목록을 그대로 돌려준다

    정규장이 한국 자정을 넘고 "오늘"의 기준이 불분명해서 자정 이후에는 어제 날짜도 함께 조회한다.
    이번 장 개장 전에 낸 주문은 지난 장의 기록으로 보고 뺀다.
    """
    now = now or datetime.now(KST)
    days = [now]
    if session_start is not None and session_start.date() != now.date():
        days.append(now - timedelta(days=1))

    rows = []
    for day in days:
        ord_dt = day.strftime("%Y%m%d")
        try:
            result = client.get_us_open_orders(ord_dt=ord_dt)
        except Exception as e:
            print(f"⚠️ [대기 주문 조회 실패] {type(e).__name__}: {e}")
            result = None
        if result is None:
            print("⚠️ [대기 주문 조회 실패] 직전 목록 유지, 이번 사이클 신규 매수 쉼")
            return known_orders(), False
        rows.extend(dict(row, query_date=ord_dt) for row in result)

    max_age = None
    if session_start is not None:
        max_age = (now - session_start).total_seconds() + SESSION_MARGIN_SECONDS

    current = {}
    for row in rows:
        order = dict(row)
        order["age_seconds"] = order_age_seconds(order.get("order_time"), now)
        if max_age is not None and order["age_seconds"] is not None and order["age_seconds"] > max_age:
            continue
        if _key(order) not in _known and len(days) > 1:
            # 미체결 조회 날짜 기준(한국/뉴욕) 확인용: 확인되면 날짜 하나만 조회해 호출을 절반으로 줄인다
            print(
                f"ℹ️ [대기 주문 조회일] {order.get('ticker')} {order.get('side')} 주문시간 {order.get('order_time')} "
                f"→ ord_dt={order.get('query_date')} 조회에서 나옴 (조회한 날짜: {', '.join(d.strftime('%Y%m%d') for d in days)})"
            )
        current[_key(order)] = order

    for key, order in _known.items():
        if key in current:
            continue
        cancel_requested = _cancel_requested.pop(key, None) is not None
        _cancel_failed_logged.discard(key)
        _block_lifted_logged.discard(key)
        if cancel_requested:
            print(f"⏱️ [대기 주문 취소 확인] {order['ticker']} {order['side']} {order['unfilled_qty']}주 미체결 목록에서 사라짐")
            continue
        age = order.get("age_seconds")
        age_text = f"약 {age:.0f}초" if age is not None else "시간 미상"
        print(f"✅ [대기 주문 체결 추정] {order['ticker']} {order['side']} {order['unfilled_qty']}주: 접수 후 {age_text}")

    _known.clear()
    _known.update(current)
    return list(current.values()), True


def buys(orders: list[dict]) -> list[dict]:
    return [order for order in orders if order["side"] == "BUY"]


def buy_tickers(orders: list[dict]) -> list[str]:
    return list(dict.fromkeys(order["ticker"] for order in buys(orders)))


def sell_tickers(orders: list[dict]) -> set[str]:
    """매도 주문을 다시 내지 않을 종목: 나이를 알고 SELL_BLOCK_SECONDS 안인 대기 매도만

    미체결 조회의 실제 동작을 믿고 무기한 막으면, 사라지지 않는 행 하나로 손절이 멈춘다.
    나이를 모르거나 한도를 넘긴 매도는 막지 않는다. 수량이 묶여 있으면 키움이 거절할 뿐 이중 매도는 나지 않는다.
    """
    blocked = set()
    for order in orders:
        if order["side"] != "SELL":
            continue
        age = order.get("age_seconds")
        if age is not None and age < SELL_BLOCK_SECONDS:
            blocked.add(order["ticker"])
        elif _key(order) not in _block_lifted_logged:
            _block_lifted_logged.add(_key(order))
            reason = "주문시간 미상" if age is None else f"{age:.0f}초 경과"
            print(f"⚠️ [대기 매도 차단 해제] {order['ticker']} {order.get('unfilled_qty')}주 {reason}: 손절·익절 판단을 다시 진행")
    return blocked


def reserved_cash(orders: list[dict]) -> float:
    """대기 매수에 묶일 금액"""
    return sum(order["unfilled_qty"] * order["order_price"] for order in buys(orders))


def _cancel(client, order: dict, exchange_of, reason: str) -> None:
    """미체결 수량 취소 요청. 결과는 다음 조회로 확인한다 (접수 ≠ 완료). 실패하면 다음 사이클에 다시 시도"""
    key = _key(order)
    requested_at = _cancel_requested.get(key)
    if requested_at is not None and time.monotonic() - requested_at < CANCEL_RETRY_SECONDS:
        return
    if requested_at is None and order.get("cancel_pending"):
        return
    exchange = order.get("exchange") or exchange_of(order["ticker"])
    result = client.cancel_us_order(order["order_no"], order["ticker"], exchange) or {}
    code = str(result.get("return_code", "")).strip()
    if code == "0":
        _cancel_requested[key] = time.monotonic()
        _cancel_failed_logged.discard(key)
        filled = f", 체결 {order['filled_qty']}주는 보유" if order.get("filled_qty") else ""
        print(f"⏱️ [대기 주문 취소] {order['ticker']} {order['side']} {order['unfilled_qty']}주 {reason}{filled}")
    elif key not in _cancel_failed_logged:
        _cancel_failed_logged.add(key)
        print(
            f"⚠️ [대기 주문 취소 실패] {order['ticker']} {order['side']} {reason}: "
            f"[{code}] {result.get('return_msg', '응답 없음')} (목록에 남아 있는 동안 대기로 계산, 매 사이클 재시도)"
        )


def cancel_expired(client, orders: list[dict], buy_timeout: float, sell_reprice: float, exchange_of) -> None:
    """오래된 대기 주문 처리

    매수: buy_timeout초 지나면 취소.
    매도: sell_reprice초(0이면 끔) 지나면 취소. 잔량이 목록에서 사라진 뒤에야 다음 사이클 손절·익절 판단이
    새 가격으로 다시 주문하므로(그 전까지는 대기 매도로 재주문 차단) 이중 매도가 나지 않는다.
    """
    for order in orders:
        age = order.get("age_seconds")
        if age is None:
            continue
        if order["side"] == "BUY":
            if age >= buy_timeout:
                _cancel(client, order, exchange_of, f"{age / 60:.1f}분 미체결")
        elif sell_reprice > 0 and age >= sell_reprice:
            _cancel(client, order, exchange_of, f"{age:.0f}초 미체결, 새 가격으로 재주문 예정")
        elif age >= SLOW_SELL_WARN_SECONDS and _key(order) not in _cancel_failed_logged:
            _cancel_failed_logged.add(_key(order))
            print(f"⚠️ [매도 대기 지연] {order['ticker']} {order['unfilled_qty']}주 {age / 60:.1f}분째 미체결 @ ${order['order_price']:.2f}")


def cancel_all_buys(client, orders: list[dict], exchange_of, reason: str) -> None:
    """대기 매수 전부에 취소를 요청한다"""
    for order in buys(orders):
        _cancel(client, order, exchange_of, reason)
