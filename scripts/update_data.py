"""한국 주식 RS 스크리너 데이터 업데이트 (고속 버전)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from pykrx import stock

PERIODS = [("1W", 7), ("1M", 31), ("3M", 93), ("6M", 186), ("12M", 372)]
RS_WEIGHTS = {"1M": 0.20, "3M": 0.20, "6M": 0.30, "12M": 0.30}

OUT_DIR = Path(__file__).resolve().parents[1] / "data"
OUT_FILE = OUT_DIR / "stocks.json"


def find_business_day(target: datetime, max_back: int = 10) -> str:
    for i in range(max_back):
        d = (target - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv(d, market="KOSPI")
            if df is not None and not df.empty and df["종가"].sum() > 0:
                return d
        except Exception:
            continue
    raise RuntimeError("영업일을 찾지 못했습니다")


def fetch_panel(date_str: str) -> pd.DataFrame:
    frames = []
    for market in ("KOSPI", "KOSDAQ"):
        df = stock.get_market_ohlcv(date_str, market=market)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["__market__"] = market
        frames.append(df)
    return pd.concat(frames) if frames else pd.DataFrame()


def main() -> int:
    today_dt = datetime.now()
    print("[1/5] 기준 영업일 탐색...", file=sys.stderr)
    today = find_business_day(today_dt)
    print(f"  -> {today}", file=sys.stderr)

    print("[2/5] 기준일 전 종목 시세...", file=sys.stderr)
    today_df = fetch_panel(today)
    print(f"  -> {len(today_df)}개", file=sys.stderr)
    if today_df.empty:
        return 1

    print("[3/5] 종목명/시총/필터링...", file=sys.stderr)
    names = {t: stock.get_market_ticker_name(t) for t in today_df.index}

    def keep(name: str) -> bool:
        if any(k in name for k in ("스팩", "우B", "우C", "리츠")):
            return False
        if name.endswith("우"):
            return False
        return True

    keep_idx = [t for t, n in names.items() if keep(n)]
    today_df = today_df.loc[keep_idx]

    try:
        cap = stock.get_market_cap(today, market="ALL")
        market_cap = cap["시가총액"].to_dict()
    except Exception as e:
        print(f"  시총 조회 실패: {e}", file=sys.stderr)
        market_cap = {}

    print("[4/5] 과거 시점별 종가 수집...", file=sys.stderr)
    past_closes: dict[str, dict] = {}
    for label, days_back in PERIODS:
        past_day = find_business_day(today_dt - timedelta(days=days_back))
        df_past = fetch_panel(past_day)
        past_closes[label] = df_past["종가"].to_dict() if not df_past.empty else {}
        print(f"  {label}: {past_day} -> {len(past_closes[label])}개", file=sys.stderr)

    print("[5/5] 계산 + JSON 작성...", file=sys.stderr)
    rows: list[dict] = []
    for t in today_df.index:
        last = float(today_df.at[t, "종가"])
        if last <= 0:
            continue
        rets = {}
        for label, _ in PERIODS:
            past = past_closes.get(label, {}).get(t)
            rets[label] = None if not past else round((last / past - 1.0) * 100.0, 2)
        rows.append({
            "ticker": t,
            "name": names.get(t, t),
            "market": today_df.at[t, "__market__"],
            "sector": "",
            "price": last,
            "market_cap": float(market_cap.get(t, 0)),
            "returns": rets,
            "rs": None,
        })

    df = pd.DataFrame({r["ticker"]: r["returns"] for r in rows}).T
    pct = df.rank(pct=True) * 100.0
    weights = pd.Series({k: v for k, v in RS_WEIGHTS.items() if k in pct.columns})
    weights = weights / weights.sum()
    rs = (pct[weights.index] * weights).sum(axis=1).round(1)
    rs_dict = rs.to_dict()
    for r in rows:
        v = rs_dict.get(r["ticker"])
        r["rs"] = None if pd.isna(v) else float(v)

    rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": today,
        "count": len(rows),
        "periods": [p for p, _ in PERIODS],
        "rs_weights": RS_WEIGHTS,
        "rows": rows,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"완료: {OUT_FILE} ({len(rows)}개)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
