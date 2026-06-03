"""한국 주식 RS 스크리너 데이터 업데이트 스크립트.

산출 지표:
- RS 점수 (1~99): 1M·3M·6M·12M 가중 백분위 (추세추종 틸트 10/36/32/22), 1W·YTD 제외
- 품질 (0~1): 6개월 월간 샘플 7점에 대한 log가격 추세 직선성 (R²)
- 가속 (%p): 최근 3M 수익률 − 직전 3M 수익률
- 기간별 수익률: 1W, 1M, 3M, 6M, 12M, YTD

산출물: data/stocks.json
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from pykrx import stock

# RS 가중치 — 추세추종 틸트 (1W·YTD 제외)
RS_WEIGHTS = {"1M": 0.10, "3M": 0.36, "6M": 0.32, "12M": 0.22}

# (라벨, 며칠 전) — 표시용 기간 수익률
RETURN_PERIODS = [
    ("1D", 1),
    ("1W", 7),
    ("1M", 31),
    ("3M", 93),
    ("6M", 186),
    ("12M", 372),
]

# 품질(R²)용 6개월 월간 샘플 — 0, 31, 62, 93, 124, 155, 186일 전
QUALITY_SAMPLE_DAYS = [0, 31, 62, 93, 124, 155, 186]

OUT_DIR = Path(__file__).resolve().parents[1] / "data"
OUT_FILE = OUT_DIR / "stocks.json"


def find_business_day(target: datetime, max_back: int = 15) -> str:
    """target 또는 그 이전 가장 가까운 영업일 YYYYMMDD."""
    import pykrx
    print(f"  pykrx 버전: {getattr(pykrx, '__version__', 'unknown')}", file=sys.stderr)
    for i in range(max_back):
        d = (target - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv(d, market="KOSPI")
            n = 0 if df is None or df.empty else len(df)
            close_sum = 0 if df is None or df.empty else float(df["종가"].sum())
            print(f"  [{d}] rows={n} close_sum={close_sum:.0f}", file=sys.stderr)
            if df is not None and not df.empty and close_sum > 0:
                return d
        except Exception as e:
            print(f"  [{d}] 예외: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(0.5)  # KRX 호출 사이 여유
    raise RuntimeError("영업일을 찾지 못했습니다")


def fetch_panel(date_str: str) -> pd.DataFrame:
    """해당 날짜 KOSPI+KOSDAQ 전 종목 OHLCV. index=ticker."""
    frames = []
    for market in ("KOSPI", "KOSDAQ"):
        df = stock.get_market_ohlcv(date_str, market=market)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["__market__"] = market
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames)


def r_squared(prices: list) -> float | None:
    """로그가격의 시간 회귀 R². prices는 과거→현재 시간순.
    None 또는 0 이하 가격은 제외. 5개 미만이면 None.
    """
    clean = [p for p in prices if p is not None and p > 0]
    if len(clean) < 5:
        return None
    y = np.log(np.array(clean, dtype=float))
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


def main() -> int:
    today_dt = datetime.now()
    print("[1/6] 기준 영업일 탐색...", file=sys.stderr)
    today = find_business_day(today_dt)
    print(f"  -> {today}", file=sys.stderr)

    # 필요한 모든 영업일 결정
    return_dates: dict[str, str] = {"today": today}
    for label, days_back in RETURN_PERIODS:
        return_dates[label] = find_business_day(today_dt - timedelta(days=days_back))

    # YTD: 작년 마지막 영업일
    last_year_end = datetime(today_dt.year - 1, 12, 31)
    return_dates["YTD"] = find_business_day(last_year_end)

    # 품질용 월간 샘플 (오름차순으로 정렬: 과거→현재)
    quality_dates: list[str] = []
    seen_q: set[str] = set()
    for days in sorted(QUALITY_SAMPLE_DAYS, reverse=True):  # 186, 155, ..., 0
        bd = find_business_day(today_dt - timedelta(days=days))
        if bd not in seen_q:
            quality_dates.append(bd)
            seen_q.add(bd)
    # quality_dates 는 과거→현재 순

    # 한 번씩만 fetch
    all_dates = set(return_dates.values()) | set(quality_dates)
    print(f"[2/6] 패널 수집 ({len(all_dates)}개 날짜)...", file=sys.stderr)
    panels: dict[str, pd.DataFrame] = {}
    for i, d in enumerate(sorted(all_dates, reverse=True), 1):
        panels[d] = fetch_panel(d)
        print(f"  {i}/{len(all_dates)} {d}: {len(panels[d])}개", file=sys.stderr)

    today_df = panels[today]
    if today_df.empty:
        print("기준일 데이터 비어있음", file=sys.stderr)
        return 1

    # 종목명 + 우선주/스팩/리츠 필터
    print("[3/6] 종목명/필터링...", file=sys.stderr)
    names: dict[str, str] = {}
    for t in today_df.index:
        try:
            names[t] = stock.get_market_ticker_name(t)
        except Exception:
            names[t] = t
        time.sleep(0.005)

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

    # 지표 계산
    print("[4/6] 지표 계산...", file=sys.stderr)

    def close_of(date_str: str, ticker: str):
        df = panels.get(date_str)
        if df is None or df.empty or ticker not in df.index:
            return None
        v = df.at[ticker, "종가"]
        if v is None or pd.isna(v) or v <= 0:
            return None
        return float(v)

    rows: list[dict] = []
    for t in today_df.index:
        last = close_of(today, t)
        if last is None:
            continue

        # 기간 수익률 (표시용)
        rets: dict[str, float | None] = {}
        for label, _ in RETURN_PERIODS:
            past = close_of(return_dates[label], t)
            rets[label] = None if past is None else round((last / past - 1.0) * 100.0, 2)
        ytd_past = close_of(return_dates["YTD"], t)
        rets["YTD"] = None if ytd_past is None else round((last / ytd_past - 1.0) * 100.0, 2)

        # 가속 = (P0/P3 - 1) - (P3/P6 - 1), %p
        p3 = close_of(return_dates["3M"], t)
        p6 = close_of(return_dates["6M"], t)
        accel = None
        if p3 and p6:
            r3 = last / p3 - 1.0
            prior_r3 = p3 / p6 - 1.0
            accel = round((r3 - prior_r3) * 100.0, 2)

        # 품질 R²
        q_prices = [close_of(d, t) for d in quality_dates]
        q_val = r_squared(q_prices)
        quality = None if q_val is None else round(q_val, 4)

        rows.append({
            "ticker": t,
            "name": names.get(t, t),
            "market": today_df.at[t, "__market__"],
            "sector": "",
            "price": last,
            "market_cap": float(market_cap.get(t, 0)),
            "returns": rets,
            "rs": None,
            "quality": quality,
            "quality_pct": None,
            "acceleration": accel,
            "acceleration_pct": None,
            "return_pct": {},
        })

    # 백분위 계산 (시장 내 순위)
    print("[5/6] RS/백분위 계산...", file=sys.stderr)
    df_ret = pd.DataFrame({r["ticker"]: r["returns"] for r in rows}).T
    # 기간별 0~100 백분위
    pct100 = df_ret.rank(pct=True) * 100.0

    # RS: 가중평균 백분위 → 1~99 스케일
    weights = pd.Series({k: v for k, v in RS_WEIGHTS.items() if k in pct100.columns})
    weights = weights / weights.sum()
    rs_raw = (pct100[weights.index] * weights).sum(axis=1, min_count=1)  # 0~100
    rs_score = (1.0 + 98.0 * rs_raw / 100.0).round(1)

    # 품질·가속 백분위 (1~99)
    qualities = pd.Series({r["ticker"]: r["quality"] for r in rows})
    qpct = (1.0 + 98.0 * qualities.rank(pct=True)).round(1)

    accels = pd.Series({r["ticker"]: r["acceleration"] for r in rows})
    apct = (1.0 + 98.0 * accels.rank(pct=True)).round(1)

    # 기간별 백분위 (1~99) — 클라이언트 기간 다중선택 정렬용
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

    print("[6/6] JSON 작성...", file=sys.stderr)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": today,
        "count": len(rows),
        "periods": ["1D", "1W", "1M", "3M", "6M", "12M", "YTD"],
        "rs_weights": RS_WEIGHTS,
        "quality_sample_dates": quality_dates,
        "rows": rows,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"완료: {OUT_FILE} ({len(rows)}개 종목)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
