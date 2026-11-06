"""키움 미체결(대기) 주문 추적

매 사이클 키움 미체결 조회(client.get_us_open_orders)로 대기 주문을 다시 읽는다.
메모리에만 두지 않으므로 재시작해도 알아낸다.
- 대기 매수는 보유로 계산 (11/5 PSQ가 잔고에 안 보이자 SH까지 산 일 방지)
- 정해진 시간 안에 체결 안 된 매수는 취소, 장마감 청산 전에는 대기 매수를 모두 취소
- 대기 매도가 있으면 매도 주문을 다시 내지 않는다
- 조회 실패는 "대기 없음"이 아니다: 직전에 알던 목록을 유지하고 그 사이클의 신규 매수를 쉰다
- 취소 접수(return_code 0)는 취소 완료가 아니다: 다음 조회에서 사라질 때까지 대기 주문으로 계산한다
"""

from datetime import datetime, timedelta

import pytz

KST = pytz.timezone("Asia/Seoul")
SLOW_SELL_WARN_SECONDS = 120
SESSION_MARGIN_SECONDS = 60

_known: dict[tuple, dict] = {}  # 직전에 알던 대기 주문
_cancel_requested: set[tuple] = set()  # 이 프로세스가 취소를 접수한 주문 (체결 시간 로그에서 제외)
_rejected: set[tuple] = set()  # 키움이 취소를 거절한 주문 (이미 체결 등). 대기 주문으로 보지 않음


def _key(order: dict) -> tuple:
    return (order.get("order_no"), order.get("ticker"))


def reset_state() -> None:
    _known.clear()
    _cancel_requested.clear()
    _rejected.clear()


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
        try:
            result = client.get_us_open_orders(ord_dt=day.strftime("%Y%m%d"))
        except Exception as e:
            print(f"⚠️ [대기 주문 조회 실패] {type(e).__name__}: {e}")
            result = None
        if result is None:
            print("⚠️ [대기 주문 조회 실패] 직전 목록 유지, 이번 사이클 신규 매수 쉼")
            return list(_known.values()), False
        rows.extend(result)

    max_age = None
    if session_start is not None:
        max_age = (now - session_start).total_seconds() + SESSION_MARGIN_SECONDS

    current = {}
    for row in rows:
        order = dict(row)
        order["age_seconds"] = order_age_seconds(order.get("order_time"), now)
        key = _key(order)
        if key in _rejected:
            continue
        if max_age is not None and order["age_seconds"] is not None and order["age_seconds"] > max_age:
            continue
        current[key] = order

    for key, order in _known.items():
        if key in current:
            continue
        if key in _cancel_requested:
            _cancel_requested.discard(key)
            print(f"⏱️ [대기 주문 취소 확인] {order['ticker']} {order['unfilled_qty']}주 미체결 목록에서 사라짐")
            continue
        if key in _rejected:
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
    return {order["ticker"] for order in orders if order["side"] == "SELL"}


def reserved_cash(orders: list[dict]) -> float:
    """대기 매수에 묶일 금액"""
    return sum(order["unfilled_qty"] * order["order_price"] for order in buys(orders))


def _cancel(client, order: dict, exchange_of, reason: str) -> None:
    """미체결 수량 취소 요청. 결과는 다음 조회로 확인한다 (접수 ≠ 완료, 거절 = 이미 체결 등)"""
    if order.get("cancel_pending") or _key(order) in _cancel_requested:
        return
    exchange = order.get("exchange") or exchange_of(order["ticker"])
    result = client.cancel_us_order(order["order_no"], order["ticker"], exchange)
    code = int(result.get("return_code", -2))
    filled = f", 체결 {order['filled_qty']}주는 보유" if order.get("filled_qty") else ""
    message = result.get("return_msg", "응답 없음")
    if code == 0:
        _cancel_requested.add(_key(order))
        print(f"⏱️ [대기 주문 취소] {order['ticker']} {order['unfilled_qty']}주 {reason}{filled}")
    elif code > 0:
        # 이미 체결 등으로 키움이 거절: 체결됐다면 잔고에 보유로 나타난다
        _rejected.add(_key(order))
        print(f"⚠️ [대기 주문 취소 거절] {order['ticker']} {reason}: [{code}] {message} (체결됐으면 보유로 처리)")
    else:
        print(f"⚠️ [대기 주문 취소 불명] {order['ticker']} {reason}: [{code}] {message} (다음 조회로 확인)")


def cancel_expired_buys(client, orders: list[dict], timeout_seconds: float, exchange_of) -> list[dict]:
    """시간 초과 매수에 취소를 요청하고, 거절된 주문을 뺀 대기 주문 목록을 돌려준다"""
    for order in orders:
        age = order.get("age_seconds")
        if age is None:
            continue
        if order["side"] == "BUY" and age >= timeout_seconds:
            _cancel(client, order, exchange_of, f"{age / 60:.1f}분 미체결")
        elif order["side"] == "SELL" and age >= SLOW_SELL_WARN_SECONDS:
            print(f"⚠️ [매도 대기 지연] {order['ticker']} {order['unfilled_qty']}주 {age / 60:.1f}분째 미체결 @ ${order['order_price']:.2f}")
    return [order for order in orders if _key(order) not in _rejected]


def cancel_all_buys(client, orders: list[dict], exchange_of, reason: str) -> list[dict]:
    """대기 매수 전부에 취소를 요청하고, 거절된 주문을 뺀 대기 주문 목록을 돌려준다"""
    for order in buys(orders):
        _cancel(client, order, exchange_of, reason)
    return [order for order in orders if _key(order) not in _rejected]
