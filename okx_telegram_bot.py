#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 선물 포지션 <-> 텔레그램 특정 토픽 연동 봇

- "매매할 때마다 빠짐없이 기록되는 것"을 최우선 목표로 삼는다. 이를 위해 포지션
  스냅샷의 순간적인 증감 비교가 아니라, OKX의 체결 내역(fills-history)을 직전
  체크포인트 이후로 전부 가져와 체결 하나하나를 순서대로 재생(replay)하며 이벤트를
  만든다. 같은 폴링 구간 안에 여러 번 매매해도, 서로 상쇄되는 매매를 해도 전부
  개별 이벤트로 남는다.
- 체결을 [신규 진입 / 추가매수 / 부분청산 / 전체청산] 4가지로 분류해 텔레그램
  그룹의 지정된 토픽(message_thread_id)에 기록을 남긴다. 레버리지만 바뀌고
  수량 변화가 없는 경우는 체결 기록에 남지 않으므로, 이 부분만 별도로 포지션
  스냅샷을 비교해 감지한다.
- 토픽 상단에는 "진행중인 포지션" 요약 메시지를 고정해두는데, 계속 같은 메시지를 수정하는
  대신 변동(체결 또는 레버리지 변경)이 있을 때만 새 메시지를 보내 그걸 새로 고정하고
  이전 고정은 해제한다. 개별 이벤트는 별도의 불변 로그 메시지로 쌓이며,
  같은 포지션에 속한 이벤트는 최초 진입 메시지에 답장(reply)으로 이어붙는다.
- GitHub Actions로 몇 분 간격 폴링하는 구조라 완전한 실시간은 아니지만, 폴링 사이에
  일어난 모든 체결을 하나도 빠짐없이 반영하는 것을 정확도의 핵심으로 삼는다.

사용 전 반드시 확인할 것
1) OKX API 키는 반드시 "읽기 전용" 권한으로 발급할 것 (출금/거래 권한 부여 금지).
2) 먼저 OKX 데모 트레이딩(모의투자) 환경에서 충분히 검증한 뒤 실계좌에 연결할 것.
   데모 트레이딩 사용 시 OKX_SIMULATED=1 환경변수를 추가하면 x-simulated-trading 헤더가 붙는다.
3) 이 스크립트는 알려진 한계가 있다 (파일 하단 "알려진 한계" 주석 참고).
"""

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# 환경 설정
# ---------------------------------------------------------------------------
OKX_BASE_URL = "https://www.okx.com"
OKX_API_KEY = os.environ["OKX_API_KEY"]
OKX_API_SECRET = os.environ["OKX_API_SECRET"]
OKX_API_PASSPHRASE = os.environ["OKX_API_PASSPHRASE"]
OKX_SIMULATED = os.environ.get("OKX_SIMULATED", "0")  # "1"이면 데모 트레이딩

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_TOPIC_ID = int(os.environ["TELEGRAM_TOPIC_ID"])

# 감시할 상품 종류. 선물(swap)만 본다면 SWAP만 두면 된다. 무기한/현물 등 필요에 맞게 조정.
INST_TYPES = os.environ.get("OKX_INST_TYPES", "SWAP").split(",")

STATE_PATH = os.environ.get("STATE_PATH", "state.json")


# ---------------------------------------------------------------------------
# OKX API 인증 & 요청
# ---------------------------------------------------------------------------
def _iso_timestamp() -> str:
    # OKX는 밀리초 단위 ISO8601 UTC 타임스탬프를 요구한다. 예: 2020-12-08T09:08:57.715Z
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def _sign(timestamp: str, method: str, request_path: str, body: str) -> str:
    # 사전해시 문자열 = timestamp + method(대문자) + requestPath(쿼리스트링 포함) + body
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    mac = hmac.new(OKX_API_SECRET.encode(), prehash.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def okx_request(method: str, path: str, params: dict | None = None) -> dict:
    """OKX v5 REST API에 인증된 요청을 보낸다. GET만 사용 (읽기 전용)."""
    query = ""
    if params:
        query = "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    request_path = f"{path}{query}"
    body = ""  # GET 요청은 바디 없음

    timestamp = _iso_timestamp()
    sign = _sign(timestamp, method, request_path, body)

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        "Content-Type": "application/json",
    }
    if OKX_SIMULATED == "1":
        headers["x-simulated-trading"] = "1"

    resp = requests.get(OKX_BASE_URL + request_path, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX API 오류: {data}")
    return data.get("data", [])


def get_positions() -> list[dict]:
    """현재 보유 중인 전체 포지션 스냅샷."""
    out = []
    for inst_type in INST_TYPES:
        out.extend(okx_request("GET", "/api/v5/account/positions", {"instType": inst_type}))
    # pos == 0 인 항목은 보유하지 않는 상태이므로 제외
    return [p for p in out if float(p.get("pos", "0") or "0") != 0]


def get_positions_history(inst_id: str, limit: int = 5) -> list[dict]:
    """완전히 청산된 포지션의 정산 기록 (실현손익 등 거래소가 확정한 값)."""
    return okx_request(
        "GET",
        "/api/v5/account/positions-history",
        {"instId": inst_id, "limit": limit},
    )


def get_fills_history(inst_type: str, before: str | None = None, limit: int = 100) -> list[dict]:
    """체결(거래 실행) 내역. before=billId를 주면 그 billId보다 새로운 체결만 반환."""
    params = {"instType": inst_type, "limit": str(limit)}
    if before:
        params["before"] = before
    return okx_request("GET", "/api/v5/trade/fills-history", params)


def get_new_fills(last_bill_id: str | None) -> list[dict]:
    """직전 체크포인트 이후 발생한 모든 체결을, 오래된 순서로 정렬해 반환한다.
    체크포인트가 없으면(비정상 복구 상황) 0으로 간주해 최근 체결을 전부 '새 것'으로
    취급하되, 한 번에 텔레그램으로 쏟아붓다 레이트리밋에 걸리는 걸 막기 위해
    조회량 자체를 30건으로 제한한다 (평상시 5분 간격 폴링에서는 이보다 훨씬 적게
    쌓이므로 정상 운영에는 지장 없다).

    OKX의 `before` 파라미터가 정확히 어느 방향을 반환하는지 100% 확신할 수 없어서
    (문서상 관례로는 "이 billId보다 새로운 것"이지만, 실제 이 파라미터에 의존하다가
    아무것도 안 잡히는 문제가 있었다) - 아예 그 파라미터 없이 최근 체결 목록을
    통째로 가져온 뒤, billId를 직접 숫자로 비교해서 새 것만 걸러낸다. 이러면
    페이지네이션 파라미터의 방향에 의존하지 않아도 된다."""
    last_bid = int(last_bill_id) if last_bill_id else 0
    all_fills = []
    for inst_type in INST_TYPES:
        try:
            fills = get_fills_history(inst_type, limit=30)  # before 없이 최신순 그대로
        except Exception as e:
            print(f"체결 내역 조회 실패 ({inst_type}): {e}")
            fills = []
        new_ones = [f for f in fills if int(f["billId"]) > last_bid]
        print(f"[fills] {inst_type}: 조회 {len(fills)}건 중 신규 {len(new_ones)}건 (체크포인트={last_bid})")
        all_fills.extend(new_ones)
    all_fills.sort(key=lambda f: int(f["billId"]))
    return all_fills


def get_latest_bill_id() -> str | None:
    """최초 실행 시 체크포인트를 잡기 위한, 가장 최근 체결의 billId."""
    latest = None
    for inst_type in INST_TYPES:
        try:
            fills = get_fills_history(inst_type, limit=1)
        except Exception:
            fills = []
        if fills:
            bid = fills[0]["billId"]
            if latest is None or int(bid) > int(latest):
                latest = bid
    return latest


FILL_MERGE_WINDOW_SECONDS = 60  # 이 시간 안에 연달아 체결되면 하나로 합친다


def merge_fills(fills: list[dict], window_seconds: int = FILL_MERGE_WINDOW_SECONDS) -> list[dict]:
    """짧은 시간 안에 같은 종목·같은 방향(posSide)·같은 매매(side)로 연달아 체결된 것들을
    하나로 합친다. 큰 주문 하나가 거래소에서 여러 체결로 쪼개져 잡히면서
    "한 번에 진입/청산했는데 여러 건으로 나뉘어 보이는" 문제를 막기 위함.
    합쳐진 체결가는 수량 가중평균, billId는 그룹의 마지막 체결 것을 쓴다
    (체크포인트가 묶인 원본 체결 전부를 지나가도록)."""
    if not fills:
        return []
    merged: list[dict] = []
    group: dict | None = None

    def flush():
        if group is None:
            return
        sz = group["_sz"]
        merged.append({
            "instId": group["instId"],
            "posSide": group["posSide"],
            "side": group["side"],
            "fillSz": str(sz),
            "fillPx": str(group["_notional"] / sz) if sz else "0",
            "billId": group["billId"],
            "ts": group["ts"],
        })

    for f in fills:
        key = (f["instId"], f["posSide"], f["side"])
        ts = int(f["ts"])
        sz = float(f["fillSz"])
        px = float(f["fillPx"])
        if group is not None and group["_key"] == key and (ts - group["_last_ts"]) <= window_seconds * 1000:
            group["_sz"] += sz
            group["_notional"] += sz * px
            group["_last_ts"] = ts
            group["billId"] = f["billId"]  # 마지막 체결의 billId로 갱신
            group["ts"] = f["ts"]
        else:
            flush()
            group = {
                "_key": key, "_sz": sz, "_notional": sz * px, "_last_ts": ts,
                "instId": f["instId"], "posSide": f["posSide"], "side": f["side"],
                "billId": f["billId"], "ts": f["ts"],
            }
    flush()

    if len(merged) != len(fills):
        print(f"[merge] 체결 {len(fills)}건 -> {window_seconds}초 이내 연속 체결 병합 후 {len(merged)}건")
    return merged


# ---------------------------------------------------------------------------
# 계약 수 -> 실제 수량 환산
# ---------------------------------------------------------------------------
# OKX의 `pos`/`fillSz`는 "계약(contract) 개수"이지, 실제 코인/주식 수량이 아니다.
# 계약 1개가 실제로 얼마만큼의 기초자산에 해당하는지는 상품마다 다른
# ctVal(계약 액면가) x ctMult(승수) 값을 곱해야 알 수 있다.
# 예: BTC-USDT-SWAP은 1계약 = 0.01 BTC인 식이라, 계약 수를 그대로 실제 수량인
# 것처럼 보여주면 실제보다 훨씬 크게(혹은 작게) 오인하기 쉽다.
_INSTRUMENT_CACHE: dict[str, dict] = {}


def _get_instrument_meta(inst_id: str, inst_type: str = "SWAP") -> dict:
    if inst_id in _INSTRUMENT_CACHE:
        return _INSTRUMENT_CACHE[inst_id]
    try:
        # 공개 엔드포인트라 서명 불필요
        resp = requests.get(
            f"{OKX_BASE_URL}/api/v5/public/instruments",
            params={"instType": inst_type, "instId": inst_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        meta = data["data"][0] if data.get("code") == "0" and data.get("data") else {}
    except Exception:
        meta = {}
    _INSTRUMENT_CACHE[inst_id] = meta
    return meta


def contracts_to_actual(inst_id: str, contracts) -> tuple[float | None, str]:
    """계약 개수를 실제 기초자산 수량으로 환산. (수량, 단위통화) 반환.
    상품 정보를 못 가져오면 (None, "") 반환 - 호출부에서 계약 수만 표기하도록 폴백."""
    meta = _get_instrument_meta(inst_id)
    ct_val = meta.get("ctVal")
    if not ct_val:
        return None, ""
    ct_mult = float(meta.get("ctMult") or 1)
    qty = abs(float(contracts)) * float(ct_val) * ct_mult
    return qty, meta.get("ctValCcy", "")


def qty_line(inst_id: str, contracts, label: str = "수량") -> str:
    """'수량: 6.18계약 (0.06 BTC)' 형태의 표시용 한 줄을 만든다."""
    n_contracts = fmt_num(contracts)
    actual, ccy = contracts_to_actual(inst_id, contracts)
    if actual is not None and ccy:
        return f"{label}: {n_contracts}계약 ({fmt_num(actual)} {ccy})"
    return f"{label}: {n_contracts}계약"


# ---------------------------------------------------------------------------
# 텔레그램 API
# ---------------------------------------------------------------------------
TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_SEND_DELAY = 1.2  # 메시지 사이 기본 대기시간(초) - 레이트리밋 예방


def tg_call(method: str, payload: dict, max_retries: int = 5) -> dict:
    """텔레그램 API 호출. 429(rate limit)를 받으면 크래시시키는 대신
    텔레그램이 알려주는 시간만큼 기다렸다가 자동으로 재시도한다."""
    for attempt in range(max_retries):
        resp = requests.post(f"{TG_BASE}/{method}", json=payload, timeout=15)
        if resp.status_code == 429:
            try:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                retry_after = 5
            print(f"[telegram] 429 rate limit - {retry_after}초 대기 후 재시도 ({attempt + 1}/{max_retries})")
            time.sleep(retry_after + 1)
            continue
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"텔레그램 API 오류: {data}")
        return data["result"]
    raise RuntimeError(f"텔레그램 API 반복 실패(429 rate limit 지속): {method}")


def tg_send(text: str, reply_to: int | None = None) -> int:
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_thread_id": TELEGRAM_TOPIC_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    result = tg_call("sendMessage", payload)
    return result["message_id"]


def tg_edit(message_id: int, text: str) -> None:
    tg_call(
        "editMessageText",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        },
    )


def tg_pin(message_id: int) -> None:
    tg_call(
        "pinChatMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "disable_notification": True,
        },
    )


def tg_unpin(message_id: int) -> None:
    tg_call("unpinChatMessage", {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id})


# ---------------------------------------------------------------------------
# 상태 저장 (직전 포지션 스냅샷 + 체결 체크포인트)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"initialized": False, "positions": {}, "summary_message_id": None}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def position_key(p: dict) -> str:
    # 체결(fills) 데이터에는 posId/mgnMode가 없기 때문에, 포지션과 체결을 같은
    # 키로 매칭할 수 있도록 일부러 posId를 쓰지 않고 instId+posSide만 사용한다.
    return f"{p['instId']}|{p['posSide']}"


def direction_label(p: dict) -> str:
    if p["posSide"] == "net":
        return "롱" if float(p["pos"]) > 0 else "숏"
    return "롱" if p["posSide"] == "long" else "숏"


def _leading_zero_count(ax: float) -> int:
    """0 < ax < 1 일 때, 소수점 이후 첫 유효숫자가 나오기 전까지의 0 개수."""
    s = f"{ax:.15f}"
    frac = s.split(".")[1]
    count = 0
    for ch in frac:
        if ch == "0":
            count += 1
        else:
            break
    return count


def fmt_num(x, decimals: int = 3) -> str:
    """숫자를 보기 좋게 반올림한다. 기본은 소수점 3자리.
    다만 도지코인처럼 값이 작아서 3자리로도 유효숫자가 1개뿐인 경우
    (예: 0.008 -> '8' 하나만 보임) 유효숫자가 2개 이상 보일 때까지
    자릿수를 자동으로 늘린다 (예: 0.008234 -> 0.0082, 4자리)."""
    x = float(x)
    if x == 0:
        return "0"
    ax = abs(x)
    if ax >= 1:
        # 1 이상인 값은 불필요한 끝자리 0을 지워도 유효숫자 손실 걱정이 없다
        s = f"{x:.{decimals}f}".rstrip("0").rstrip(".")
        return s if s else "0"
    # 1 미만인 값은 늘린 자릿수(d)가 "최소 유효숫자 2개 보장"을 위한 것이므로,
    # 끝자리가 우연히 0이어도 지우지 않는다 (지우면 그 보장이 깨진다 - 예: 0.070 -> 0.07)
    d = min(max(decimals, _leading_zero_count(ax) + 2), 10)  # 10자리는 안전 상한
    return f"{x:.{d}f}"


def ticker(inst_id: str) -> str:
    """'BTC-USDT-SWAP' -> 'BTC' 처럼 짧은 표시용 심볼."""
    return inst_id.split("-")[0].upper()


def direction_word(p: dict) -> str:
    """'LONG' / 'SHORT' 표시용 문자열."""
    return "LONG" if direction_label(p) == "롱" else "SHORT"


# ---------------------------------------------------------------------------
# 체결 -> 이벤트 분류
# ---------------------------------------------------------------------------
def build_snapshot_map(positions: list[dict]) -> dict:
    return {position_key(p): p for p in positions}


def find_close_record(inst_id: str) -> dict | None:
    """positions-history에서 "방금" 청산된 것에 해당하는 정산 기록을 찾는다.
    OKX가 정산 기록을 만드는 데 약간의 지연이 있을 수 있어 짧게 재시도하고,
    가장 최근 것이라 해도 너무 오래된(=지금 이 청산과 무관한 예전) 기록이면
    무시한다 - 이게 없으면 예전에 같은 종목을 청산했던 엉뚱한 기록을
    "방금 청산 결과"인 것처럼 잘못 보여줄 수 있다."""
    now_ms = int(time.time() * 1000)
    for attempt in range(3):
        try:
            records = get_positions_history(inst_id, limit=5)
        except Exception:
            records = []
        if records:
            latest = max(records, key=lambda r: int(r.get("uTime", "0")))
            if now_ms - int(latest.get("uTime", "0")) <= 10 * 60 * 1000:  # 10분 이내 것만 신뢰
                return latest
        if attempt < 2:
            time.sleep(3)
    return None


_ZERO_EPS = 1e-8  # 이 값 이하는 "0"으로 취급 (부동소수점 오차로 완전청산이 부분청산으로 오인되는 것 방지)


def _classify(old_mag: float, new_mag: float, direction: str, common: dict) -> list[dict]:
    """포지션 크기(항상 0 이상)의 전/후 비교만으로 이벤트 하나를 만든다.
    0과의 비교는 정확히 == 0이 아니라 아주 작은 오차범위(_ZERO_EPS) 이내인지로 판단한다.
    체결 수량들을 누적해서 더하고 빼다 보면 0.00000000003 같은 부동소수점 오차가
    남을 수 있는데, 이걸 정확히 0이 아니라고 판단해버리면 완전청산이 부분청산으로
    잘못 인식된다."""
    if abs(new_mag - old_mag) < 1e-12:
        return []
    if old_mag <= _ZERO_EPS and new_mag > _ZERO_EPS:
        return [{**common, "type": "entry", "direction": direction, "size_before": 0.0, "size_after": new_mag}]
    if new_mag <= _ZERO_EPS and old_mag > _ZERO_EPS:
        return [{**common, "type": "full_close", "direction": direction, "size_before": old_mag, "size_after": 0.0}]
    if new_mag > old_mag:
        return [{**common, "type": "add", "direction": direction, "size_before": old_mag, "size_after": new_mag}]
    return [{**common, "type": "partial_close", "direction": direction, "size_before": old_mag, "size_after": new_mag}]


def process_fills(fills: list[dict], prev_positions: dict, curr_positions: dict) -> list[tuple[str, list[dict]]]:
    """체결들을 오래된 순서 그대로 재생하며, 체결 하나당 (billId, 이벤트목록) 튜플을 만든다.
    체결 단위로 묶어서 반환하는 이유는, 텔레그램 전송 도중 실패하더라도 "이 체결까지는
    확실히 처리됨"을 체결 단위로 체크포인트에 반영할 수 있게 하기 위해서다.
    체결 하나하나를 다 처리하므로, 같은 폴링 구간 안에 여러 매매가 있어도
    (심지어 서로 상쇄되어도) 전부 개별 이벤트로 남는다."""
    # 원웨이(net) 모드는 부호 있는 값으로, 헤지 모드(long/short 버킷)는 0 이상 값으로 추적
    ledger: dict[str, float] = {}
    for key, p in prev_positions.items():
        ledger[key] = float(p["pos"]) if p["posSide"] == "net" else abs(float(p["pos"]))

    groups: list[tuple[str, list[dict]]] = []

    for f in fills:
        inst_id = f["instId"]
        pos_side = f["posSide"]
        side = f["side"]
        fill_sz = float(f["fillSz"])
        fill_px = float(f["fillPx"])
        key = f"{inst_id}|{pos_side}"
        old_signed = ledger.get(key, 0.0)

        if pos_side == "short":
            delta = fill_sz if side == "sell" else -fill_sz
        else:  # long 또는 net: 매수가 증가 방향
            delta = fill_sz if side == "buy" else -fill_sz

        new_signed = old_signed + delta
        if pos_side in ("long", "short"):
            new_signed = max(new_signed, 0.0)  # 버킷 내에서는 0 미만으로 내려갈 수 없음

        ctx = curr_positions.get(key) or prev_positions.get(key) or {}
        common = {
            "instId": inst_id,
            "key": key,
            "fill_px": fill_px,
            "lever": ctx.get("lever"),
            "entry_px": float(ctx["avgPx"]) if ctx.get("avgPx") else None,
        }

        events: list[dict] = []
        if pos_side in ("long", "short"):
            direction = "롱" if pos_side == "long" else "숏"
            events.extend(_classify(old_signed, new_signed, direction, common))
        else:
            if abs(old_signed) > _ZERO_EPS and abs(new_signed) > _ZERO_EPS and (old_signed > 0) != (new_signed > 0):
                # 한 체결 안에서 방향이 뒤집힌 경우 (반대매매 초과 주문) - 전체청산 + 신규진입으로 분리
                old_dir = "롱" if old_signed > 0 else "숏"
                new_dir = "롱" if new_signed > 0 else "숏"
                events.append({**common, "type": "full_close", "direction": old_dir,
                               "size_before": abs(old_signed), "size_after": 0.0})
                events.append({**common, "type": "entry", "direction": new_dir,
                               "size_before": 0.0, "size_after": abs(new_signed)})
            else:
                ref = new_signed if abs(new_signed) > _ZERO_EPS else old_signed
                direction = "롱" if ref > 0 else "숏"
                events.extend(_classify(abs(old_signed), abs(new_signed), direction, common))

        ledger[key] = new_signed
        groups.append((f["billId"], events))

    return groups


def detect_leverage_changes(prev_positions: dict, curr_positions: dict) -> list[dict]:
    """수량 변화 없이 레버리지만 바뀐 경우. 레버리지 변경은 체결 기록에 남지 않으므로
    스냅샷 비교로만 잡아낼 수 있다."""
    events = []
    for key, curr in curr_positions.items():
        prev = prev_positions.get(key)
        if prev and str(prev.get("lever")) != str(curr.get("lever")):
            events.append({"type": "leverage_change", "key": key, "prev": prev, "curr": curr})
    return events


# ---------------------------------------------------------------------------
# 메시지 포맷
# ---------------------------------------------------------------------------
def format_fill_event(ev: dict) -> str:
    inst_id = ev["instId"]
    direction = ev["direction"]
    lever = ev.get("lever") or "?"

    if ev["type"] == "entry":
        return (
            f"🟢 <b>[신규 진입]</b>\n"
            f"{inst_id}\n"
            f"방향: {direction}  |  레버리지: {lever}배\n"
            f"체결가: {fmt_num(ev['fill_px'])}\n"
            f"{qty_line(inst_id, ev['size_after'])}"
        )

    if ev["type"] == "add":
        added = ev["size_after"] - ev["size_before"]
        add_pct = added / ev["size_before"] * 100 if ev["size_before"] else 0
        return (
            f"🔵 <b>[추가매수]</b>\n"
            f"{inst_id}\n"
            f"방향: {direction}  |  레버리지: {lever}배\n"
            f"체결가: {fmt_num(ev['fill_px'])}\n"
            f"{qty_line(inst_id, added, label='추가 수량')}  (기존 대비 +{add_pct:.1f}%)\n"
            f"{qty_line(inst_id, ev['size_after'], label='현재 총 수량')}"
        )

    if ev["type"] == "partial_close":
        closed = ev["size_before"] - ev["size_after"]
        closed_pct = closed / ev["size_before"] * 100 if ev["size_before"] else 0
        entry_px = ev.get("entry_px")
        label = "부분청산"
        pnl_line = ""
        if entry_px:
            sign = 1 if direction == "롱" else -1
            price_return_pct = (ev["fill_px"] - entry_px) / entry_px * 100 * sign
            label = "부분익절" if price_return_pct >= 0 else "부분손절"

            actual_qty, _ccy = contracts_to_actual(inst_id, closed)
            if actual_qty is not None:
                pnl_amount = (ev["fill_px"] - entry_px) * actual_qty * sign
                try:
                    lev = float(ev.get("lever") or 1)
                except (TypeError, ValueError):
                    lev = 1.0
                pnl_pct_leveraged = price_return_pct * lev
                quote_ccy = inst_id.split("-")[1] if "-" in inst_id else ""
                sign_str = "+" if pnl_amount >= 0 else ""
                pnl_line = f"수익금: {sign_str}{fmt_num(pnl_amount)} {quote_ccy}  (수익률 {pnl_pct_leveraged:+.2f}%)\n"

        return (
            f"🟡 <b>[{label}]</b>\n"
            f"{inst_id}\n"
            f"방향: {direction}  |  레버리지: {lever}배\n"
            f"{qty_line(inst_id, closed, label='청산 수량')}  (보유 물량의 {closed_pct:.1f}%)\n"
            f"체결가: {fmt_num(ev['fill_px'])}\n"
            f"{pnl_line}"
            f"{qty_line(inst_id, ev['size_after'], label='잔여 수량')}"
        )

    if ev["type"] == "full_close":
        record = find_close_record(inst_id)
        if record:
            pnl_ratio = float(record.get("pnlRatio", 0)) * 100
            pnl = fmt_num(record.get("pnl", 0))
            open_px = fmt_num(record.get("openAvgPx", ev.get("entry_px") or 0))
            close_px = fmt_num(record.get("closeAvgPx", ev["fill_px"]))
            hold_ms = int(record.get("uTime", 0)) - int(record.get("cTime", 0))
            hold_h = hold_ms / 1000 / 3600 if hold_ms > 0 else None
            hold_line = f"보유 시간: 약 {hold_h:.1f}시간\n" if hold_h else ""
            result_emoji = "✅" if float(record.get("pnl", 0)) >= 0 else "❌"
            return (
                f"🔴 <b>[전체청산]</b> {result_emoji}\n"
                f"{inst_id}\n"
                f"방향: {direction}  |  레버리지: {record.get('lever', lever)}배\n"
                f"진입가: {open_px}  →  청산가: {close_px}\n"
                f"{hold_line}"
                f"실현손익: {pnl}  ({pnl_ratio:+.2f}%)"
            )
        # positions-history에 아직 반영 안 된 경우의 폴백 (다음 실행에서는 조회 가능해짐)
        return (
            f"🔴 <b>[전체청산]</b>\n"
            f"{inst_id}\n"
            f"방향: {direction}  |  레버리지: {lever}배\n"
            f"체결가: {fmt_num(ev['fill_px'])}\n"
            f"※ 실현손익 정산 데이터 반영 대기 중 (다음 갱신에서 계좌 정산 내역 직접 확인 권장)"
        )

    return ""


def format_leverage_change(prev: dict, curr: dict) -> str:
    direction = direction_label(curr)
    return (
        f"⚙️ <b>[레버리지 변경]</b>\n"
        f"{curr['instId']}\n"
        f"방향: {direction}\n"
        f"레버리지: {prev.get('lever')}배 → {curr.get('lever')}배\n"
        f"{qty_line(curr['instId'], curr['pos'])}  |  평단가: {fmt_num(curr['avgPx'])}"
    )


def format_summary(curr_positions: dict) -> str:
    if not curr_positions:
        return "📌 <b>진행중인 포지션</b> 📌\n\n보유 중인 포지션 없음"

    rows = []
    for p in curr_positions.values():
        emoji = "📈" if direction_label(p) == "롱" else "📉"
        pnl_pct = float(p.get("uplRatio", 0) or 0) * 100
        rows.append((emoji, ticker(p["instId"]), direction_word(p), f"{p['lever']}x", fmt_num(p["avgPx"]), f"{pnl_pct:+.2f}%"))

    # 종목마다 글자 수가 달라서, 열 너비를 데이터에 맞춰 자동으로 맞춘다
    # (참고: <pre>/<code>는 텔레그램이 "복사" 버튼을 자동으로 붙이는 코드블록 UI라
    #  일부러 안 쓰고, 일반 텍스트 + 공백 패딩으로 정렬한다. 고정폭 글꼴이 아니라서
    #  <pre>만큼 완벽히 맞진 않지만 슬래시로 나열하던 것보단 훨씬 정돈되어 보인다.)
    tick_w = max(len(r[1]) for r in rows) + 1
    dir_w = max(len(r[2]) for r in rows) + 1
    lev_w = max(len(r[3]) for r in rows) + 1
    px_w = max(len(r[4]) for r in rows) + 1
    pnl_w = max(len(r[5]) for r in rows) + 1

    lines = ["📌 <b>진행중인 포지션</b> 📌", ""]
    for emoji, tk, dw, lv, px, pnl in rows:
        lines.append(f"{emoji} {tk.ljust(tick_w)} {dw.ljust(dir_w)} {lv.ljust(lev_w)} {px.rjust(px_w)}  {pnl.rjust(pnl_w)}")
    lines.append("")
    lines.append(f"<i>갱신: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 메인 로직
# ---------------------------------------------------------------------------
def main():
    state = load_state()
    prev_positions = state.get("positions", {})
    thread_ids = state.get("thread_root_message_id", {})  # key -> 포지션 스레드의 첫 메시지 id
    last_bill_id = state.get("last_bill_id")
    print(f"[state] initialized={state.get('initialized')}, last_bill_id={last_bill_id}, 저장된 포지션 수={len(prev_positions)}")

    curr_positions_list = get_positions()
    curr_positions = build_snapshot_map(curr_positions_list)

    # 최초 실행: 이미 보유 중인 포지션을 "신규 진입"으로 오인하지 않도록 베이스라인만 저장하고,
    # 이후 체결을 빠짐없이 조회하기 위한 체크포인트(가장 최근 체결의 billId)를 확보한다.
    if not state.get("initialized"):
        state["positions"] = curr_positions
        state["initialized"] = True
        state["last_bill_id"] = get_latest_bill_id()
        print(f"[init] 확보한 체크포인트 last_bill_id={state['last_bill_id']}")
        summary_id = tg_send(format_summary(curr_positions))
        tg_pin(summary_id)
        state["summary_message_id"] = summary_id
        state["thread_root_message_id"] = {}
        save_state(state)
        print("최초 실행: 베이스라인 저장 및 요약 메시지 고정 완료")
        return

    # last_bill_id가 비어있어도(초기화 당시 조회 실패 등) get_new_fills가 0으로 간주해
    # 최근 체결을 전부 "새 것"으로 처리하므로, 별도 분기 없이 그대로 호출하면 된다.
    fills = get_new_fills(last_bill_id)
    fills = merge_fills(fills)  # 1분 이내 연속 체결은 하나로 합쳐서 이벤트를 만든다
    fill_groups = process_fills(fills, prev_positions, curr_positions)
    lever_events = detect_leverage_changes(prev_positions, curr_positions)
    total_events = 0

    # 체결 단위로 처리 + 저장을 묶는다: 텔레그램 전송 도중 실패해도, 그때까지 확실히
    # 보낸 체결까지는 체크포인트가 전진해있어서 다음 실행이 처음부터 다시 쏟아붓지 않는다.
    for bill_id, events in fill_groups:
        for ev in events:
            key = ev["key"]
            if ev["type"] == "entry":
                msg_id = tg_send(format_fill_event(ev))  # 신규 진입은 항상 새 스레드로 시작
                thread_ids[key] = msg_id
            else:
                tg_send(format_fill_event(ev), reply_to=thread_ids.get(key))
                if ev["type"] == "full_close":
                    thread_ids.pop(key, None)
            total_events += 1
            time.sleep(TELEGRAM_SEND_DELAY)
        # 이 체결(및 여기서 파생된 이벤트 전부)까지는 처리 완료 - 체크포인트 전진 후 즉시 저장
        state["last_bill_id"] = bill_id
        state["thread_root_message_id"] = thread_ids
        save_state(state)

    for ev in lever_events:
        tg_send(format_leverage_change(ev["prev"], ev["curr"]), reply_to=thread_ids.get(ev["key"]))
        time.sleep(TELEGRAM_SEND_DELAY)

    # 요약은 변동(체결 이벤트 또는 레버리지 변경)이 있을 때만 새 메시지로 다시 보내고,
    # 그걸 새로 고정한 뒤 이전 고정은 해제한다.
    summary_id = state.get("summary_message_id")
    if total_events or lever_events:
        old_summary_id = summary_id
        summary_id = tg_send(format_summary(curr_positions))
        tg_pin(summary_id)
        if old_summary_id:
            try:
                tg_unpin(old_summary_id)
            except Exception as e:
                print(f"이전 요약 메시지 고정 해제 실패 (무시하고 진행): {e}")
    elif not summary_id:
        summary_id = tg_send(format_summary(curr_positions))
        tg_pin(summary_id)

    state["positions"] = curr_positions
    state["summary_message_id"] = summary_id
    state["thread_root_message_id"] = thread_ids
    save_state(state)
    print(f"실행 완료: 체결 {len(fills)}건 -> 이벤트 {total_events + len(lever_events)}건 처리")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# 알려진 한계 (필요 시 직접 보완)
# ---------------------------------------------------------------------------
# 1. 체크포인트 유실 시 최대 30건까지만 복구: last_bill_id가 없어졌을 때 종목당 최근
#    30건까지만 "새 것"으로 처리한다. 그 이상 쌓여있었다면 일부는 놓친다 (반대로 30건을
#    한꺼번에 텔레그램으로 쏟아붓다 레이트리밋에 걸려 무한 실패하는 것보다는 낫다는 절충).
# 2. 청산/ADL 등 특수 체결: 강제청산(liquidation)이나 자동감산(ADL)으로 발생한 체결도
#    일반 체결과 동일하게 처리한다 (오히려 이런 경우일수록 놓치면 안 되는 이벤트라 의도된 동작).
# 3. 레버리지 변경은 체결 기록에 남지 않아 여전히 "5분 간격 스냅샷 비교"로만 감지된다
#    (수량 변화와 동시에 일어난 경우가 아니라 순수 레버리지 조정만 있었던 경우).
# 4. GitHub Actions 워크플로우의 "상태 파일 커밋" 스텝에 if: always()가 걸려있어야,
#    파이썬 스크립트가 도중에 실패해도 그때까지 진행된 state.json 변경이 커밋된다.
#    (제공한 워크플로우 파일에는 이미 반영되어 있음 - 직접 워크플로우를 고친 경우 확인 필요)
