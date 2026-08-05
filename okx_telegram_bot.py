#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 선물 포지션 <-> 텔레그램 특정 토픽 연동 봇

- OKX 계좌의 현재 포지션을 조회해 직전 실행 시점(state.json)과 비교한다.
- 비교 결과를 [신규 진입 / 추가매수 / 부분청산 / 전체청산] 4가지 이벤트로 분류해
  텔레그램 그룹의 지정된 토픽(message_thread_id)에 기록을 남긴다.
- 토픽 상단에는 "현재 보유 포지션 요약" 메시지 1개를 고정해두고, 상태가 바뀔 때마다
  그 메시지 내용만 계속 수정(edit)한다. 개별 이벤트는 별도의 불변 로그 메시지로 쌓인다.
- 정확도를 속도보다 우선하는 설계다. 실시간 웹소켓 대신 주기적 폴링(권장: 5분 간격)으로
  전체 스냅샷을 매번 새로 받아 비교하므로, 연결 끊김으로 인한 이벤트 유실이 없다.

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


# ---------------------------------------------------------------------------
# 상태 저장 (직전 포지션 스냅샷)
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
    pos_id = p.get("posId")
    if pos_id:
        return pos_id
    return f"{p['instId']}|{p['posSide']}|{p['mgnMode']}"


def direction_label(p: dict) -> str:
    if p["posSide"] == "net":
        return "롱" if float(p["pos"]) > 0 else "숏"
    return "롱" if p["posSide"] == "long" else "숏"


def fmt_num(x) -> str:
    x = float(x)
    # 소수점 불필요한 0 제거
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"


# ---------------------------------------------------------------------------
# 이벤트 분류 & 메시지 포맷
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


def process_events(prev_positions: dict, curr_positions: dict) -> list[dict]:
    """이벤트 목록을 반환한다. 각 이벤트는 dict(type=..., key=..., ...)."""
    events = []
    all_keys = set(prev_positions) | set(curr_positions)

    for key in all_keys:
        prev = prev_positions.get(key)
        curr = curr_positions.get(key)

        if prev is None and curr is not None:
            events.append({"type": "entry", "key": key, "curr": curr})

        elif prev is not None and curr is None:
            events.append({"type": "full_close", "key": key, "prev": prev})

        elif prev is not None and curr is not None:
            prev_sz = abs(float(prev["pos"]))
            curr_sz = abs(float(curr["pos"]))
            size_changed = abs(curr_sz - prev_sz) > 1e-12
            lever_changed = str(prev.get("lever")) != str(curr.get("lever"))

            if not size_changed and not lever_changed:
                continue  # 수량·레버리지 모두 변화 없음 (마크가격만 갱신된 경우 등) - 이벤트 아님

            if not size_changed and lever_changed:
                # 수량은 그대로인데 레버리지만 바뀐 경우 (도중에 배율 조정)
                events.append({"type": "leverage_change", "key": key, "prev": prev, "curr": curr})
            elif curr_sz > prev_sz:
                events.append({"type": "add", "key": key, "prev": prev, "curr": curr, "lever_changed": lever_changed})
            else:
                events.append({"type": "partial_close", "key": key, "prev": prev, "curr": curr, "lever_changed": lever_changed})

    return events


def format_entry(p: dict) -> str:
    direction = direction_label(p)
    sz = fmt_num(p["pos"])
    return (
        f"🟢 <b>[신규 진입]</b>\n"
        f"{p['instId']}\n"
        f"방향: {direction}  |  레버리지: {p['lever']}배\n"
        f"진입가: {p['avgPx']}\n"
        f"수량: {sz}"
    )


def format_add(prev: dict, curr: dict) -> str:
    direction = direction_label(curr)
    prev_sz = abs(float(prev["pos"]))
    curr_sz = abs(float(curr["pos"]))
    add_sz = curr_sz - prev_sz
    add_pct = add_sz / prev_sz * 100 if prev_sz else 0
    return (
        f"🔵 <b>[추가매수]</b>\n"
        f"{curr['instId']}\n"
        f"방향: {direction}  |  레버리지: {curr['lever']}배\n"
        f"추가 수량: {fmt_num(add_sz)}  (기존 대비 +{add_pct:.1f}%)\n"
        f"갱신된 평단가: {curr['avgPx']}\n"
        f"현재 총 수량: {fmt_num(curr_sz)}"
    )


def format_partial_close(prev: dict, curr: dict) -> str:
    direction = direction_label(curr)
    prev_sz = abs(float(prev["pos"]))
    curr_sz = abs(float(curr["pos"]))
    closed_sz = prev_sz - curr_sz
    closed_pct = closed_sz / prev_sz * 100 if prev_sz else 0

    # 정확한 체결가 대신, 감지 시점의 mark price와 평단가를 비교한 근사치로 익절/손절을 라벨링한다.
    # (체결 단위 정밀도가 필요하면 trade/fills-history 연동으로 확장 가능 - 하단 "알려진 한계" 참고)
    mark_px = float(curr.get("markPx", curr["avgPx"]))
    entry_px = float(curr["avgPx"])
    is_profit = (mark_px >= entry_px) if direction == "롱" else (mark_px <= entry_px)
    label = "부분익절" if is_profit else "부분손절"

    return (
        f"🟡 <b>[{label}]</b>\n"
        f"{curr['instId']}\n"
        f"방향: {direction}  |  레버리지: {curr['lever']}배\n"
        f"청산 수량: {fmt_num(closed_sz)}  (보유 물량의 {closed_pct:.1f}%)\n"
        f"청산 시점 참고가: {curr.get('markPx', 'N/A')}\n"
        f"잔여 수량: {fmt_num(curr_sz)}\n"
        f"※ 정확한 체결가·실현손익은 계좌 체결내역에서 확인하세요"
    )


def format_full_close(prev: dict) -> str:
    direction = direction_label(prev)
    record = find_close_record(prev["instId"])

    if record:
        pnl_ratio = float(record.get("pnlRatio", 0)) * 100
        pnl = record.get("pnl", "N/A")
        open_px = record.get("openAvgPx", prev.get("avgPx", "N/A"))
        close_px = record.get("closeAvgPx", "N/A")
        hold_ms = int(record.get("uTime", 0)) - int(record.get("cTime", 0))
        hold_h = hold_ms / 1000 / 3600 if hold_ms > 0 else None
        hold_line = f"보유 시간: 약 {hold_h:.1f}시간\n" if hold_h else ""
        result_emoji = "✅" if float(record.get("pnl", 0)) >= 0 else "❌"
        return (
            f"🔴 <b>[전체청산]</b> {result_emoji}\n"
            f"{prev['instId']}\n"
            f"방향: {direction}  |  레버리지: {record.get('lever', prev.get('lever'))}배\n"
            f"진입가: {open_px}  →  청산가: {close_px}\n"
            f"{hold_line}"
            f"실현손익: {pnl}  ({pnl_ratio:+.2f}%)"
        )

    # positions-history에 아직 반영 안 된 경우의 폴백 (다음 실행에서는 조회 가능해짐)
    return (
        f"🔴 <b>[전체청산]</b>\n"
        f"{prev['instId']}\n"
        f"방향: {direction}  |  레버리지: {prev['lever']}배\n"
        f"진입가: {prev['avgPx']}\n"
        f"※ 실현손익 정산 데이터 반영 대기 중 (다음 갱신에서 계좌 정산 내역 직접 확인 권장)"
    )


def format_leverage_change(prev: dict, curr: dict) -> str:
    direction = direction_label(curr)
    return (
        f"⚙️ <b>[레버리지 변경]</b>\n"
        f"{curr['instId']}\n"
        f"방향: {direction}\n"
        f"레버리지: {prev.get('lever')}배 → {curr.get('lever')}배\n"
        f"수량: {fmt_num(curr['pos'])}  |  평단가: {curr['avgPx']}"
    )


def format_summary(curr_positions: dict) -> str:
    if not curr_positions:
        return "📋 <b>[현재 보유 포지션]</b>\n보유 중인 포지션 없음"
    lines = ["📋 <b>[현재 보유 포지션]</b>"]
    for p in curr_positions.values():
        direction = direction_label(p)
        upl_ratio = float(p.get("uplRatio", 0) or 0) * 100
        lines.append(
            f"\n{p['instId']}  ({direction} {p['lever']}배)\n"
            f"수량: {fmt_num(p['pos'])}  |  평단가: {p['avgPx']}  |  평가손익: {upl_ratio:+.2f}%"
        )
    lines.append(f"\n<i>갱신: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 메인 로직
# ---------------------------------------------------------------------------
def main():
    state = load_state()
    prev_positions = state.get("positions", {})
    thread_ids = state.get("thread_root_message_id", {})  # key -> 포지션 스레드의 첫 메시지 id

    curr_positions_list = get_positions()
    curr_positions = build_snapshot_map(curr_positions_list)

    # 최초 실행: 이미 보유 중인 포지션을 "신규 진입"으로 오인하지 않도록 베이스라인만 저장
    if not state.get("initialized"):
        state["positions"] = curr_positions
        state["initialized"] = True
        summary_id = tg_send(format_summary(curr_positions))
        tg_pin(summary_id)
        state["summary_message_id"] = summary_id
        state["thread_root_message_id"] = {}
        save_state(state)
        print("최초 실행: 베이스라인 저장 및 요약 메시지 고정 완료")
        return

    events = process_events(prev_positions, curr_positions)

    for ev in events:
        key = ev["key"]
        reply_to = thread_ids.get(key)

        if ev["type"] == "entry":
            msg = format_entry(ev["curr"])
            msg_id = tg_send(msg)
            thread_ids[key] = msg_id  # 이후 이 포지션의 후속 이벤트는 여기에 reply로 연결

        elif ev["type"] == "add":
            msg = format_add(ev["prev"], ev["curr"])
            if ev.get("lever_changed"):
                msg += f"\n⚙️ 레버리지 변경: {ev['prev'].get('lever')}배 → {ev['curr'].get('lever')}배"
            tg_send(msg, reply_to=reply_to)

        elif ev["type"] == "partial_close":
            msg = format_partial_close(ev["prev"], ev["curr"])
            if ev.get("lever_changed"):
                msg += f"\n⚙️ 레버리지 변경: {ev['prev'].get('lever')}배 → {ev['curr'].get('lever')}배"
            tg_send(msg, reply_to=reply_to)

        elif ev["type"] == "leverage_change":
            # 수량 변화 없이 레버리지만 조정한 경우 (도중에 배율만 올리거나 내린 경우)
            msg = format_leverage_change(ev["prev"], ev["curr"])
            tg_send(msg, reply_to=reply_to)

        elif ev["type"] == "full_close":
            msg = format_full_close(ev["prev"])
            tg_send(msg, reply_to=reply_to)
            thread_ids.pop(key, None)  # 스레드 종료

        time.sleep(0.5)  # 텔레그램 rate limit 여유

    # 상단 고정 요약 메시지는 이벤트 유무와 무관하게 항상 최신 상태로 갱신
    summary_id = state.get("summary_message_id")
    summary_text = format_summary(curr_positions)
    if summary_id:
        try:
            tg_edit(summary_id, summary_text)
        except Exception as e:
            print(f"요약 메시지 수정 실패, 새로 전송: {e}")
            summary_id = tg_send(summary_text)
            tg_pin(summary_id)
    else:
        summary_id = tg_send(summary_text)
        tg_pin(summary_id)

    state["positions"] = curr_positions
    state["summary_message_id"] = summary_id
    state["thread_root_message_id"] = thread_ids
    save_state(state)
    print(f"실행 완료: 이벤트 {len(events)}건 처리")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# 알려진 한계 (필요 시 직접 보완)
# ---------------------------------------------------------------------------
# 1. 방향 전환 미검출: 헤지 모드가 아닌 원웨이(net) 모드에서, 보유 수량보다 큰 반대
#    방향 주문으로 포지션이 롱->숏(혹은 그 반대)으로 즉시 뒤집히는 경우, 현재 로직은
#    이를 "전체청산 후 신규진입"으로 자연스럽게 처리하지만 두 이벤트가 별도 스레드로
#    잡힌다 (의도된 동작이지만, 하나로 묶고 싶다면 posId 변경 여부로 추가 판별 필요).
# 2. 부분청산의 정확한 체결가/실현손익: 현재는 감지 시점의 mark price로 익절/손절
#    여부만 근사 판정한다. 체결 단위의 정확한 가격이 필요하면 /api/v5/trade/fills-history
#    를 폴링 주기마다 조회해 reduce 방향 체결만 필터링하는 로직을 추가하면 된다.
# 3. 폴링 간격보다 짧은 시간 안에 여러 이벤트(예: 추가매수 후 바로 부분청산)가 발생하면
#    두 변화가 하나의 순수 증감으로 뭉뚱그려 보일 수 있다. 간격을 좁히면 완화된다.
