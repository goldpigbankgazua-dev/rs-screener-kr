"""한국 주식 RS(상대강도) 스크리너 데이터 업데이트 스크립트.

pykrx로 KOSPI/KOSDAQ 전 종목의 가격을 받아와서 기간별 수익률과
RS 점수(여러 기간 백분위의 가중 평균)를 계산해 data/stocks.json으로
저장한다. GitHub Actions가 매일 장 마감 후에 실행한다.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from pykrx import stock

# 기간별 거래일 수 (대략) — 한국 시장은 연 약 245 거래일
PERIODS = {
    "1W": 5,
    "1M": 21,
    "3M": 63,
    "6M": 126,
    "12M": 252,
}

# RS 점수를 계산할 때 각 기간에 부여할 가중치 (합이 1이 아니어도 됨)
RS_WEIGHTS = {
    "1M": 0.20,
    "3M": 0.20,
    "6M": 0.30,
    "12M": 0.30,
}

OUT_DIR = Path(__file__).resolve().parents[1] / "data"
OUT_FILE = OUT_DIR / "stocks.json"


@dataclass
class Row:
    ticker: str
    name: str
    market: str  # "KOSPI" or "KOSDAQ"
    sector: str
    price: float
    market_cap: float  # 시가총액 (원)
    returns: dict      # {"1W": 1.23, ...} (단위: %)


def fetch_universe(today: str) -> list[tuple[str, str]]:
    """(ticker, market) 리스트 반환. 우선주/스팩/리츠 등은 제외."""
    rows: list[tuple[str, str]] = []
    for market in ("KOSPI", "KOSDAQ"):
        tickers = stock.get_market_ticker_list(today, market=market)
        for t in tickers:
            name = stock.get_market_ticker_name(t)
            # 간단한 필터 (우선주/스팩 제외)
            if any(k in name for k in ("스팩", "우B", "우C", "리츠")):
                continue
            if name.endswith("우"):
                continue
            rows.append((t, market))
    return rows


def fetch_price_panel(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """여러 종목의 종가를 한 DataFrame(index=날짜, columns=ticker)으로 받는다.

    pykrx의 get_market_ohlcv_by_date는 종목별 호출이라 느리므로
    get_market_cap_by_ticker처럼 일자별 단면 호출을 활용하지 않고,
    종목 수가 많으므로 가격은 종목별로 받되 빠른 종가만 사용한다.
    """
    series_list = []
    total = len(tickers)
    for i, t in enumerate(tickers, 1):
        try:
            df = stock.get_market_ohlcv_by_date(start, end, t)
            if df is None or df.empty:
                continue
            s = df["종가"].rename(t)
            series_list.append(s)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {t}: {e}", file=sys.stderr)
        if i % 50 == 0:
            print(f"  fetched {i}/{total}", file=sys.stderr)
        # KRX rate-limit 회피
        time.sleep(0.05)
    if not series_list:
        return pd.DataFrame()
    return pd.concat(series_list, axis=1).sort_index()


def compute_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """columns=ticker, index=period_key, values=수익률(%)."""
    out = {}
    last = prices.ffill().iloc[-1]
    for key, days in PERIODS.items():
        if len(prices) <= days:
            continue
        past = prices.ffill().iloc[-1 - days]
        out[key] = (last / past - 1.0) * 100.0
    return pd.DataFrame(out)


def compute_rs_score(returns: pd.DataFrame) -> pd.Series:
    """각 기간의 백분위 순위를 가중 평균해 0~100 점수로 만든다."""
    pct = returns.rank(pct=True) * 100.0
    weights = pd.Series(RS_WEIGHTS)
    weights = weights[weights.index.intersection(pct.columns)]
    if weights.empty:
        return pd.Series(dtype=float)
    weights = weights / weights.sum()
    score = (pct[weights.index] * weights).sum(axis=1)
    return score.round(1)


def main() -> int:
    today = datetime.now().strftime("%Y%m%d")
    # 1년 + 여유 30거래일 정도 받자
    start_dt = datetime.now() - timedelta(days=400)
    start = start_dt.strftime("%Y%m%d")

    print(f"[1/4] 종목 유니버스 수집 ({today})...", file=sys.stderr)
    universe = fetch_universe(today)
    print(f"  -> {len(universe)}개", file=sys.stderr)
    if not universe:
        print("유니버스가 비었습니다.", file=sys.stderr)
        return 1

    ticker_market = dict(universe)
    tickers = [t for t, _ in universe]

    print(f"[2/4] 가격 데이터 수집 ({start} ~ {today})...", file=sys.stderr)
    prices = fetch_price_panel(tickers, start, today)
    if prices.empty:
        print("가격 데이터가 비었습니다.", file=sys.stderr)
        return 1

    print(f"[3/4] 기간 수익률 및 RS 점수 계산...", file=sys.stderr)
    rets = compute_returns(prices)  # rows=ticker, cols=period
    rs = compute_rs_score(rets)

    # 시가총액 (가장 최근 영업일 기준)
    try:
        cap_df = stock.get_market_cap_by_ticker(today, market="ALL")
    except Exception:
        cap_df = pd.DataFrame()

    print(f"[4/4] JSON 작성...", file=sys.stderr)
    rows: list[dict] = []
    last_close = prices.ffill().iloc[-1]
    for t in tickers:
        if t not in rets.index or pd.isna(last_close.get(t)):
            continue
        name = stock.get_market_ticker_name(t)
        try:
            sector = stock.get_market_sector_name(today, t) or ""
        except Exception:
            sector = ""
        market_cap = float(cap_df.loc[t, "시가총액"]) if t in cap_df.index else 0.0
        r = rets.loc[t].to_dict()
        rows.append({
            "ticker": t,
            "name": name,
            "market": ticker_market[t],
            "sector": sector,
            "price": float(last_close[t]),
            "market_cap": market_cap,
            "returns": {k: (None if pd.isna(v) else round(float(v), 2)) for k, v in r.items()},
            "rs": None if pd.isna(rs.get(t, float("nan"))) else float(rs[t]),
        })

    rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": today,
        "count": len(rows),
        "periods": list(PERIODS.keys()),
        "rs_weights": RS_WEIGHTS,
        "rows": rows,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"완료: {OUT_FILE} ({len(rows)}개 종목)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
