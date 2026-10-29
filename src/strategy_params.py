"""매매 조건 파라미터 저장소 (자동 조정 대상)

- 현재 값: data/strategy_params.json (없으면 기본값)
- 변경 이력: data/strategy_params_history.jsonl (한 줄 = 한 번의 변경, 롤백 기준)
- 모든 값은 [min, max] 범위로 고정되고, 한 번에 step 이상 바뀌지 않는다.
- 서킷브레이커(일일 손실 한도·손절 횟수)는 안전장치라 자동 조정 대상에서 뺀다.

사용법:
  python src/strategy_params.py show        현재 값과 허용 범위
  python src/strategy_params.py history     최근 변경 이력
  python src/strategy_params.py rollback    직전 변경 되돌리기
"""

import json
import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PARAMS_PATH = os.path.join(PROJECT_ROOT, "data", "strategy_params.json")
HISTORY_PATH = os.path.join(PROJECT_ROOT, "data", "strategy_params_history.jsonl")

# name: (기본값, 최소, 최대, 1회 최대 변경폭, 설명)
PARAM_SPECS = {
    "stop_loss_pct": (-1.8, -3.0, -1.0, 0.2, "손절 기준 수익률(%)"),
    "take_profit_pct": (3.5, 2.0, 6.0, 0.5, "익절 대기 시작 수익률(%)"),
    "max_take_profit_pct": (7.0, 5.0, 12.0, 1.0, "무조건 익절 수익률(%)"),
    "max_buy_qty": (5, 1, 10, 1, "1회 최대 매수 수량(주)"),
    "max_holdings": (3, 1, 5, 1, "동시 보유 종목 수"),
    "hold_min_profit_pct": (1.0, 0.5, 3.0, 0.5, "오버나잇 홀딩 최소 수익률(%)"),
    "max_overnight_holds": (2, 0, 3, 1, "오버나잇 홀딩 최대 종목 수"),
}


def defaults() -> dict:
    return {name: spec[0] for name, spec in PARAM_SPECS.items()}


def _coerce(name: str, value) -> float | int:
    default, low, high, _, _ = PARAM_SPECS[name]
    value = min(max(float(value), low), high)
    return int(round(value)) if isinstance(default, int) else round(value, 2)


def _enforce_relations(params: dict) -> dict:
    # 무조건 익절선은 익절 대기선보다 최소 1%p 위
    if params["max_take_profit_pct"] < params["take_profit_pct"] + 1.0:
        params["max_take_profit_pct"] = _coerce(
            "max_take_profit_pct", params["take_profit_pct"] + 1.0
        )
    return params


def load_params() -> dict:
    params = defaults()
    try:
        with open(PARAMS_PATH, encoding="utf-8") as f:
            stored = json.load(f)
        for name, value in stored.items():
            if name in PARAM_SPECS:
                params[name] = _coerce(name, value)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        print(f"⚠️ 파라미터 파일 손상, 기본값 사용: {e}")
    return _enforce_relations(params)


def _save_params(params: dict) -> None:
    os.makedirs(os.path.dirname(PARAMS_PATH), exist_ok=True)
    tmp_path = PARAMS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, PARAMS_PATH)


def _append_history(entry: dict) -> None:
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_history(limit: int = 20) -> list[dict]:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines[-limit:]]


def clamp_changes(current: dict, proposed: dict) -> dict:
    """제안값을 허용 범위·1회 변경폭으로 잘라 실제로 바뀌는 항목만 반환"""
    result = {}
    for name, target in proposed.items():
        if name not in PARAM_SPECS:
            continue
        step = PARAM_SPECS[name][3]
        old = current[name]
        bounded = min(max(float(target), old - step), old + step)
        new = _coerce(name, bounded)
        if new != old:
            result[name] = new
    return result


def update_params(proposed: dict, reason: str, source: str = "daily_review") -> dict:
    """범위 제한 후 저장하고 이력을 남긴다. 실제 변경 {name: (old, new)} 반환"""
    current = load_params()
    changes = clamp_changes(current, proposed)
    if not changes:
        return {}
    updated = _enforce_relations({**current, **changes})
    diff = {k: (current[k], updated[k]) for k in updated if updated[k] != current[k]}
    if not diff:
        return {}
    _save_params(updated)
    _append_history(
        {
            "at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "reason": reason,
            "before": current,
            "after": updated,
        }
    )
    return diff


def rollback() -> dict | None:
    """직전 변경 이전 값으로 되돌린다 (되돌린 것도 이력에 남김)"""
    history = [h for h in read_history(limit=1000) if h.get("source") != "rollback"]
    if not history:
        return None
    last = history[-1]
    current = load_params()
    restored = _enforce_relations({**defaults(), **last["before"]})
    _save_params(restored)
    _append_history(
        {
            "at": datetime.now().isoformat(timespec="seconds"),
            "source": "rollback",
            "reason": f"{last['at']} 변경 되돌림",
            "before": current,
            "after": restored,
        }
    )
    return restored


def apply_to_engine() -> dict:
    """파일 값을 엔진 모듈 상수에 반영한다. 거래일이 바뀔 때마다 호출"""
    params = load_params()
    import overnight_hold
    import trading_safety

    trading_safety.STOP_LOSS_PCT = params["stop_loss_pct"]
    trading_safety.TAKE_PROFIT_PCT = params["take_profit_pct"]
    trading_safety.MAX_TAKE_PROFIT_PCT = params["max_take_profit_pct"]
    overnight_hold.HOLD_MIN_PROFIT_PCT = params["hold_min_profit_pct"]
    overnight_hold.MAX_OVERNIGHT_HOLDS = params["max_overnight_holds"]
    return params


def _print_params() -> None:
    params = load_params()
    for name, (default, low, high, step, desc) in PARAM_SPECS.items():
        print(f"{name:22s} {params[name]:>6}  (기본 {default}, 범위 {low}~{high}, 1회 ±{step})  {desc}")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "show"
    if command == "show":
        _print_params()
    elif command == "history":
        for h in read_history():
            changed = {k: f"{h['before'].get(k)}→{v}" for k, v in h["after"].items() if h["before"].get(k) != v}
            print(f"{h['at']} [{h['source']}] {changed} | {h['reason']}")
    elif command == "rollback":
        restored = rollback()
        print("되돌릴 변경 이력이 없습니다." if restored is None else f"되돌림 완료: {restored}")
    else:
        print(__doc__)
