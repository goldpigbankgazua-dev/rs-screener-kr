"""한국 주식 RS 스크리너 데이터 업데이트.

FinanceDataReader(종목 리스트, 네이버 기반) + yfinance(가격, 야후 기반).
KRX 직접 호출을 피해서 GitHub Actions IP 차단을 우회한다.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import FinanceDataReader as fdr
import yfinance as yf

PERIODS = [("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126), ("12M", 252)]
RS_WEIGHTS = {"1M": 0.20, "3M": 0.20, "6M": 0.30, "12M": 0.30}
OUT_DIR = Path(__file__).resolve().parents[1] / "data"
OUT_FILE = OUT_DIR / "stocks.json"


def fetch_listing() -> pd.DataFrame:
    kospi = fdr.StockListing("KOSPI")
    kosdaq = fdr.StockListing("KOSDAQ")
    kospi["Market"] = "KOSPI"
    kosdaq["Market"] = "KOSDAQ"
    df = pd.concat([kospi, kosdaq], ignore_index=True)
    if "Code" not in df.columns and "Symbol" in df.columns:
        df["Code"] = df["Symbol"]
    df["YF"] = df.apply(
        lambda r: f"{r['Code']}.{'KS' if r['Market'] == 'KOSPI' else 'KQ'}", axis=1
    )

    def keep(name) -> bool:
        if not isinstance(name, str): return False
        if any(k in name for k in ("스팩", "우B", "우C", "리츠")): return False
        if name.endswith("우"): return False
        return True

    df = df[df["Name"].apply(keep)].reset_index(drop=True)
    return df


def fetch_prices(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    closes: dict[str, pd.Series] = {}
    batch_size = 200
    total = len(tickers)
    for i in range(0, total, batch_size):
        batch = tickers[i:i + batch_size]
        print(f"  batch {i + 1}-{i + len(batch)}/{total}", file=sys.stderr)
        try:
            data = yf.download(
                batch, start=start, end=end,
                group_by="ticker", threads=True, progress=False, auto_adjust=False,
            )
        except Exception as e:
            print(f"  batch error: {e}", file=sys.stderr)
            continue
        for t in batch:
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    if t in data.columns.get_level_values(0):
                        s = data[t]["Close"].dropna()
                        if not s.empty:
                            closes[t] = s
                else:
                    s = data["Close"].dropna()
                    if not s.empty:
                        closes[t] = s
            except Exception:
                pass
        time.sleep(0.5)
    if not closes:
        return pd.DataFrame()
    return pd.DataFrame(closes).sort_index()


def main() -> int:
    print("[1/4] 종목 리스트 (FinanceDataReader)...", file=sys.stderr)
    listing = fetch_listing()
    print(f"  -> {len(listing)}개", file=sys.stderr)

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=420)
    start = start_dt.strftime("%Y-%m-%d")
    end = end_dt.strftime("%Y-%m-%d")

    print(f"[2/4] 가격 다운로드 (yfinance, {start} ~ {end})...", file=sys.stderr)
    yf_tickers = listing["YF"].tolist()
    prices = fetch_prices(yf_tickers, start, end)
    if prices.empty:
        print("가격 데이터 비어있음", file=sys.stderr)
        return 1
    print(f"  -> {prices.shape[1]}개 종목 시세 확보", file=sys.stderr)

    print("[3/4] 기간별 수익률 계산...", file=sys.stderr)
    prices = prices.ffill()
    last = prices.iloc[-1]
    returns_by_period: dict[str, pd.Series] = {}
    for label, days in PERIODS:
        if len(prices) <= days:
            continue
        past = prices.iloc[-1 - days]
        returns_by_period[label] = (last / past - 1.0) * 100.0
    rets_df = pd.DataFrame(returns_by_period)

    pct = rets_df.rank(pct=True) * 100.0
    weights = pd.Series({k: v for k, v in RS_WEIGHTS.items() if k in pct.columns})
    weights = weights / weights.sum()
    rs = (pct[weights.index] * weights).sum(axis=1).round(1)

    print("[4/4] JSON 작성...", file=sys.stderr)
    yf_to_meta = listing.set_index("YF")
    today_str = end_dt.strftime("%Y%m%d")

    rows: list[dict] = []
    for yf_t, price in last.items():
        if pd.isna(price) or price <= 0:
            continue
        if yf_t not in yf_to_meta.index:
            continue
        meta = yf_to_meta.loc[yf_t]
        if isinstance(meta, pd.DataFrame):  # 중복 ticker 방어
            meta = meta.iloc[0]
        rets = {}
        for label, _ in PERIODS:
            s = returns_by_period.get(label)
            if s is None or yf_t not in s.index:
                rets[label] = None
            else:
                v = s[yf_t]
                rets[label] = None if pd.isna(v) else round(float(v), 2)
        market_cap = 0.0
        if "Marcap" in meta.index:
            try:
                m = meta["Marcap"]
                market_cap = float(m) if pd.notna(m) else 0.0
            except Exception:
                market_cap = 0.0
        rs_val = rs.get(yf_t, float("nan"))
        rows.append({
            "ticker": str(meta["Code"]),
            "name": str(meta["Name"]),
            "market": str(meta["Market"]),
            "sector": "",
            "price": float(price),
            "market_cap": market_cap,
            "returns": rets,
            "rs": None if pd.isna(rs_val) else float(rs_val),
        })

    rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": today_str,
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
