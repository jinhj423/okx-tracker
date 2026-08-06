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
- 토픽에는 "진행중인 포지션" 요약을, 변동(체결 또는 레버리지 변경)이 있을 때만
  새 메시지로 보낸다 (고정/수정 없이 매번 새 채팅으로 쌓인다). 개별 이벤트는
  별도의 불변 로그 메시지로 쌓이며, 같은 포지션에 속한 이벤트는 최초 진입
  메시지에 답장(reply)으로 이어붙는다.
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
    체크포인트가 없으면(비정상 복구 상황) 0으로 간주해 최근 체결(최대 100건)을 전부
    '새 것'으로 취급한다 - 트레이드를 조용히 누락시키는 것보다는 낫다는 판단.

    OKX의 `before` 파라미터가 정확히 어느 방향을 반환하는지 100% 확신할 수 없어서
    (문서상 관례로는 "이 billId보다 새로운 것"이지만, 실제 이 파라미터에 의존하다가
    아무것도 안 잡히는 문제가 있었다) - 아예 그 파라미터 없이 최근 체결 목록을
    통째로 가져온 뒤, billId를 직접 숫자로 비교해서 새 것만 걸러낸다. 이러면
    페이지네이션 파라미터의 방향에 의존하지 않아도 된다."""
    last_bid = int(last_bill_id) if last_bill_id else 0
    all_fills = []
    for inst_type in INST_TYPES:
        try:
            fills = get_fills_history(inst_type, limit=100)  # before 없이 최신순 그대로
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


def tg_call(method: str, payload: dict) -> dict:
    resp = requests.post(f"{TG_BASE}/{method}", json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"텔레그램 API 오류: {data}")
    return data["result"]


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
        d = decimals
    else:
        d = min(max(decimals, _leading_zero_count(ax) + 2), 10)  # 10자리는 안전 상한
    s = f"{x:.{d}f}".rstrip("0").rstrip(".")
    return s if s else "0"


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
    """positions-history에서 가장 최근 정산 기록을 찾는다 (거래소가 확정한 realized pnl)."""
    try:
        records = get_positions_history(inst_id, limit=5)
    except Exception:
        return None
    if not records:
        return None
    # uTime(정산 시각) 기준 가장 최근 것
    return max(records, key=lambda r: int(r.get("uTime", "0")))


def _classify(old_mag: float, new_mag: float, direction: str, common: dict) -> list[dict]:
    """포지션 크기(항상 0 이상)의 전/후 비교만으로 이벤트 하나를 만든다."""
    if abs(new_mag - old_mag) < 1e-12:
        return []
    if old_mag == 0 and new_mag > 0:
        return [{**common, "type": "entry", "direction": direction, "size_before": 0.0, "size_after": new_mag}]
    if new_mag == 0 and old_mag > 0:
        return [{**common, "type": "full_close", "direction": direction, "size_before": old_mag, "size_after": 0.0}]
    if new_mag > old_mag:
        return [{**common, "type": "add", "direction": direction, "size_before": old_mag, "size_after": new_mag}]
    return [{**common, "type": "partial_close", "direction": direction, "size_before": old_mag, "size_after": new_mag}]


def process_fills(fills: list[dict], prev_positions: dict, curr_positions: dict) -> list[dict]:
    """체결들을 오래된 순서 그대로 재생하며 이벤트 목록을 만든다.
    체결 하나하나를 다 처리하므로, 같은 폴링 구간 안에 여러 매매가 있어도
    (심지어 서로 상쇄되어도) 전부 개별 이벤트로 남는다."""
    # 원웨이(net) 모드는 부호 있는 값으로, 헤지 모드(long/short 버킷)는 0 이상 값으로 추적
    ledger: dict[str, float] = {}
    for key, p in prev_positions.items():
        ledger[key] = float(p["pos"]) if p["posSide"] == "net" else abs(float(p["pos"]))

    events: list[dict] = []

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

        if pos_side in ("long", "short"):
            direction = "롱" if pos_side == "long" else "숏"
            events.extend(_classify(old_signed, new_signed, direction, common))
        else:
            if old_signed != 0 and new_signed != 0 and (old_signed > 0) != (new_signed > 0):
                # 한 체결 안에서 방향이 뒤집힌 경우 (반대매매 초과 주문) - 전체청산 + 신규진입으로 분리
                old_dir = "롱" if old_signed > 0 else "숏"
                new_dir = "롱" if new_signed > 0 else "숏"
                events.append({**common, "type": "full_close", "direction": old_dir,
                               "size_before": abs(old_signed), "size_after": 0.0})
                events.append({**common, "type": "entry", "direction": new_dir,
                               "size_before": 0.0, "size_after": abs(new_signed)})
            else:
                ref = new_signed if new_signed != 0 else old_signed
                direction = "롱" if ref > 0 else "숏"
                events.extend(_classify(abs(old_signed), abs(new_signed), direction, common))

        ledger[key] = new_signed

    return events


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
        if entry_px:
            is_profit = (ev["fill_px"] >= entry_px) if direction == "롱" else (ev["fill_px"] <= entry_px)
            label = "부분익절" if is_profit else "부분손절"
        return (
            f"🟡 <b>[{label}]</b>\n"
            f"{inst_id}\n"
            f"방향: {direction}  |  레버리지: {lever}배\n"
            f"{qty_line(inst_id, closed, label='청산 수량')}  (보유 물량의 {closed_pct:.1f}%)\n"
            f"체결가: {fmt_num(ev['fill_px'])}\n"
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
        rows.append((emoji, ticker(p["instId"]), direction_word(p), f"{p['lever']}x", fmt_num(p["avgPx"])))

    # 종목마다 글자 수가 달라서, 열 너비를 데이터에 맞춰 자동으로 맞춘다
    # (참고: <pre>/<code>는 텔레그램이 "복사" 버튼을 자동으로 붙이는 코드블록 UI라
    #  일부러 안 쓰고, 일반 텍스트 + 공백 패딩으로 정렬한다. 고정폭 글꼴이 아니라서
    #  <pre>만큼 완벽히 맞진 않지만 슬래시로 나열하던 것보단 훨씬 정돈되어 보인다.)
    tick_w = max(len(r[1]) for r in rows) + 1
    dir_w = max(len(r[2]) for r in rows) + 1
    lev_w = max(len(r[3]) for r in rows) + 1

    lines = ["📌 <b>진행중인 포지션</b> 📌", ""]
    for emoji, tk, dw, lv, px in rows:
        lines.append(f"{emoji} {tk.ljust(tick_w)} {dw.ljust(dir_w)} {lv.ljust(lev_w)} {px.rjust(9)}")
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
        tg_send(format_summary(curr_positions))
        state["thread_root_message_id"] = {}
        save_state(state)
        print("최초 실행: 베이스라인 저장 및 요약 메시지 전송 완료")
        return

    # last_bill_id가 비어있어도(초기화 당시 조회 실패 등) get_new_fills가 0으로 간주해
    # 최근 체결을 전부 "새 것"으로 처리하므로, 별도 분기 없이 그대로 호출하면 된다.
    fills = get_new_fills(last_bill_id)
    fill_events = process_fills(fills, prev_positions, curr_positions)
    lever_events = detect_leverage_changes(prev_positions, curr_positions)

    for ev in fill_events:
        key = ev["key"]
        if ev["type"] == "entry":
            msg_id = tg_send(format_fill_event(ev))  # 신규 진입은 항상 새 스레드로 시작
            thread_ids[key] = msg_id
        else:
            tg_send(format_fill_event(ev), reply_to=thread_ids.get(key))
            if ev["type"] == "full_close":
                thread_ids.pop(key, None)
        time.sleep(0.5)  # 텔레그램 rate limit 여유

    for ev in lever_events:
        tg_send(format_leverage_change(ev["prev"], ev["curr"]), reply_to=thread_ids.get(ev["key"]))
        time.sleep(0.5)

    # 요약은 계속 고정해두고 수정하는 대신, 변동(체결 이벤트 또는 레버리지 변경)이
    # 있을 때만 새 메시지로 보낸다. 고정(pin) 기능은 쓰지 않는다.
    if fill_events or lever_events:
        tg_send(format_summary(curr_positions))

    if fills:
        state["last_bill_id"] = fills[-1]["billId"]
    state["positions"] = curr_positions
    state["thread_root_message_id"] = thread_ids
    save_state(state)
    print(f"실행 완료: 체결 {len(fills)}건 -> 이벤트 {len(fill_events) + len(lever_events)}건 처리")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# 알려진 한계 (필요 시 직접 보완)
# ---------------------------------------------------------------------------
# 1. before 파라미터 방향 가정: OKX v5의 일반적인 페이지네이션 관례상 `before=<billId>`는
#    그 billId보다 "새로운"(더 큰) 기록을 요청하는 파라미터라고 가정하고 구현했다.
#    실제로 처음 실행해보고 이벤트가 중복되거나 전혀 안 잡히면 이 가정이 틀린 것이니 알려달라.
# 2. 청산/ADL 등 특수 체결: 강제청산(liquidation)이나 자동감산(ADL)으로 발생한 체결도
#    일반 체결과 동일하게 처리한다 (오히려 이런 경우일수록 놓치면 안 되는 이벤트라 의도된 동작).
# 3. 레버리지 변경은 체결 기록에 남지 않아 여전히 "5분 간격 스냅샷 비교"로만 감지된다
#    (수량 변화와 동시에 일어난 경우가 아니라 순수 레버리지 조정만 있었던 경우).
# 4. 체크포인트(last_bill_id) 유실: state.json이 어떤 이유로든 소실되면 그 이전 체결들은
#    다시 조회되지 않는다. GitHub Actions가 매번 state.json을 커밋하므로 일반적인 상황에서는
#    문제 없지만, 저장소를 통째로 밀어버리는 등의 사고에는 대비가 안 되어 있다.
