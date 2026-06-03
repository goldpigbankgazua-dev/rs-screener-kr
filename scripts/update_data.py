"""한국 주식 RS 스크리너 - 네이버 파이낸스 기반.

FinanceDataReader 라이브러리가 한국 종목 일봉을 네이버 파이낸스에서 받아옵니다.
종목 리스트는 data/tickers.json에서 로드.

산출 지표:
- RS 점수 (1~99): 1M·3M·6M·12M 가중 백분위 (10/36/32/22), 1W·YTD 제외
- 품질 (0~1): 최근 6개월 일봉 log가격 추세 직선성 R²
- 가속 (%p): 최근 3M − 직전 3M 수익률
- 기간 수익률: 1D, 1W, 1M, 3M, 6M, 12M, YTD
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import FinanceDataReader as fdr

RS_WEIGHTS = {"1M": 0.10, "3M": 0.36, "6M": 0.32, "12M": 0.22}

PERIOD_DAYS = {"1D": 1, "1W": 7, "1M": 31, "3M": 93, "6M": 186, "12M": 372}

OUT_DIR = Path(__file__).resolve().parents[1] / "data"
OUT_FILE = OUT_DIR / "stocks.json"
TICKERS_FILE = OUT_DIR / "tickers.json"


def r_squared(prices) -> float | None:
    arr = np.array([p for p in prices if p is not None and not pd.isna(p) and p > 0], dtype=float)
    if len(arr) < 5:
        return None
    y = np.log(arr)
    x = np.arange(len(y), dtype=float)
    if np.var(y) == 0:
        return None
    a, b = np.polyfit(x, y, 1)
    y_pred = a * x + b
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    if ss_tot == 0:
        return None
    return max(0.0, min(1.0, 1.0 - ss_res / ss_tot))


def price_at_or_before(close: pd.Series, target: datetime) -> float | None:
    eligible = close[close.index <= pd.Timestamp(target)]
    return float(eligible.iloc[-1]) if not eligible.empty else None


def main() -> int:
    today_dt = datetime.now()

    print("[1/4] 종목 리스트 로드...", file=sys.stderr)
    if not TICKERS_FILE.exists():
        print(f"  ✗ {TICKERS_FILE} 없음", file=sys.stderr)
        return 1
    universe = json.loads(TICKERS_FILE.read_text(encoding="utf-8"))
    # 중복 제거 (ticker 기준)
    seen = set()
    unique = []
    for u in universe:
        if u["ticker"] not in seen:
            seen.add(u["ticker"])
            unique.append(u)
    universe = unique
    print(f"  -> {len(universe)}개 종목", file=sys.stderr)

    start_date = (today_dt - timedelta(days=400)).strftime("%Y-%m-%d")
    end_date = today_dt.strftime("%Y-%m-%d")
    print(f"[2/4] 네이버 파이낸스 일봉 다운로드 (기간 {start_date} ~ {end_date})...", file=sys.stderr)

    close_dict: dict[str, pd.Series] = {}
    fail = 0
    for i, info in enumerate(universe, 1):
        ticker = info["ticker"]
        try:
            df = fdr.DataReader(ticker, start_date, end_date)
            if df is None or df.empty or "Close" not in df.columns:
                fail += 1
                if fail <= 3 or i % 20 == 0:
                    print(f"  [{i}/{len(universe)}] {ticker} 빈 응답", file=sys.stderr)
                continue
            close = df["Close"].dropna()
            if close.empty:
                fail += 1
                continue
            close_dict[ticker] = close
            if i % 20 == 0:
                print(f"  [{i}/{len(universe)}] OK (실패 누적 {fail})", file=sys.stderr)
        except Exception as e:
            fail += 1
            if fail <= 3:
                print(f"  [{i}/{len(universe)}] {ticker} 예외: {type(e).__name__}: {str(e)[:100]}", file=sys.stderr)
        time.sleep(0.05)

    print(f"  -> 성공 {len(close_dict)}개 / 실패 {fail}개", file=sys.stderr)
    if not close_dict:
        print("  ✗ 데이터 없음", file=sys.stderr)
        return 1

    latest_idx = max(s.index[-1] for s in close_dict.values())
    as_of = latest_idx.strftime("%Y%m%d")
    print(f"  -> 기준일: {as_of}", file=sys.stderr)

    ytd_target = datetime(today_dt.year - 1, 12, 31)

    print("[3/4] 지표 계산...", file=sys.stderr)
    info_map = {u["ticker"]: u for u in universe}
    rows: list[dict] = []
    for ticker, close in close_dict.items():
        info = info_map[ticker]
        try:
            last = float(close.iloc[-1])
            if last <= 0:
                continue

            rets: dict[str, float | None] = {}
            for label, days in PERIOD_DAYS.items():
                past = price_at_or_before(close, today_dt - timedelta(days=days))
                rets[label] = None if past is None else round((last / past - 1.0) * 100.0, 2)
            ytd_past = price_at_or_before(close, ytd_target)
            rets["YTD"] = None if ytd_past is None else round((last / ytd_past - 1.0) * 100.0, 2)

            p3 = price_at_or_before(close, today_dt - timedelta(days=PERIOD_DAYS["3M"]))
            p6 = price_at_or_before(close, today_dt - timedelta(days=PERIOD_DAYS["6M"]))
            accel = None
            if p3 and p6:
                accel = round(((last / p3 - 1.0) - (p3 / p6 - 1.0)) * 100.0, 2)

            six_mo = close[close.index >= (close.index[-1] - pd.Timedelta(days=186))]
            quality_val = r_squared(six_mo.tolist())
            quality = None if quality_val is None else round(quality_val, 4)

            rows.append({
                "ticker": ticker,
                "name": info["name"],
                "market": info["market"],
                "sector": info.get("sector", ""),
                "price": last,
                "market_cap": 0,
                "returns": rets,
                "rs": None,
                "quality": quality,
                "quality_pct": None,
                "acceleration": accel,
                "acceleration_pct": None,
                "return_pct": {},
            })
        except Exception as e:
            print(f"  {ticker} 계산 실패: {type(e).__name__}: {e}", file=sys.stderr)
            continue

    if not rows:
        print("  ✗ 유효한 종목 없음", file=sys.stderr)
        return 1

    print(f"[4/4] RS/백분위 ({len(rows)}개)...", file=sys.stderr)
    df_ret = pd.DataFrame({r["ticker"]: r["returns"] for r in rows}).T
    pct100 = df_ret.rank(pct=True) * 100.0

    weights = pd.Series({k: v for k, v in RS_WEIGHTS.items() if k in pct100.columns})
    weights = weights / weights.sum()
    rs_raw = (pct100[weights.index] * weights).sum(axis=1, min_count=1)
    rs_score = (1.0 + 98.0 * rs_raw / 100.0).round(1)

    qualities = pd.Series({r["ticker"]: r["quality"] for r in rows})
    qpct = (1.0 + 98.0 * qualities.rank(pct=True)).round(1)

    accels = pd.Series({r["ticker"]: r["acceleration"] for r in rows})
    apct = (1.0 + 98.0 * accels.rank(pct=True)).round(1)

    return_pct_df = (1.0 + 98.0 * df_ret.rank(pct=True)).round(1)

    for r in rows:
        t = r["ticker"]
        v = rs_score.get(t)
        r["rs"] = None if v is None or pd.isna(v) else float(v)
        v = qpct.get(t)
        r["quality_pct"] = None if v is None or pd.isna(v) else float(v)
        v = apct.get(t)
        r["acceleration_pct"] = None if v is None or pd.isna(v) else float(v)
        r["return_pct"] = {}
        for p in return_pct_df.columns:
            v = return_pct_df[p].get(t)
            r["return_pct"][p] = None if v is None or pd.isna(v) else float(v)

    rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": as_of,
        "count": len(rows),
        "periods": ["1D", "1W", "1M", "3M", "6M", "12M", "YTD"],
        "rs_weights": RS_WEIGHTS,
        "source": "Naver Finance (FinanceDataReader)",
        "rows": rows,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"완료: {OUT_FILE} ({len(rows)}개)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
