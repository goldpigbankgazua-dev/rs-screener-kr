"""한국 주식 RS 스크리너 - 네이버 파이낸스 기반.

KOSPI/KOSDAQ 보통주 전 종목을 자동으로 수집:
- 종목 리스트: FinanceDataReader.StockListing() (한국거래소 공시 데이터)
- 일봉 가격: fdr.DataReader() — 내부적으로 네이버 파이낸스 API 사용
- 우선주/스팩/리츠/ETF/ETN 제외, 보통주만

산출 지표: RS, 품질(R²), 가속, 1D/1W/1M/3M/6M/12M/YTD 수익률
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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
WICS_FILE = OUT_DIR / "wics_map.json"  # ticker -> WICS 중분류(업종). 1순위 섹터 출처.

# 보통주만: 다음 패턴 제외
EXCLUDE_NAME_KEYWORDS = ("스팩", "리츠", "ETF", "ETN")
# 우선주 패턴: ~우, ~우B, ~우C, ~2우B, ~3우B 등
EXCLUDE_PREFERRED_REGEX = re.compile(r"\d?우[A-Z]?$")


def is_common_stock(name: str) -> bool:
    """보통주 True. 우선주/스팩/리츠/ETF/ETN/SPAC은 False."""
    if not name:
        return False
    name = name.strip()
    if any(k in name for k in EXCLUDE_NAME_KEYWORDS):
        return False
    if EXCLUDE_PREFERRED_REGEX.search(name):
        return False
    return True


def load_sector_overrides() -> dict[str, str]:
    """tickers.json의 섹터 정보를 백업 매핑으로 로드."""
    if not TICKERS_FILE.exists():
        return {}
    try:
        data = json.loads(TICKERS_FILE.read_text(encoding="utf-8"))
        return {d["ticker"]: d.get("sector", "") for d in data if d.get("sector")}
    except Exception:
        return {}


def load_wics_map() -> dict[str, str]:
    """wics_map.json (ticker -> WICS 중분류). 섹터 1순위 출처."""
    if not WICS_FILE.exists():
        return {}
    try:
        return {k: v for k, v in json.loads(WICS_FILE.read_text(encoding="utf-8")).items() if v}
    except Exception:
        return {}


# FDR이 KOSDAQ에 대해 섹터 컬럼에 넣어주는 '소속부'(섹터 아님) — 섹터로 쓰면 안 됨.
SOSOKBU = {"우량기업부", "중견기업부", "벤처기업부", "기술성장기업부"}


def _is_garbage_sector(s: str) -> bool:
    s = (s or "").strip()
    return (not s) or (s in SOSOKBU) or ("소속부" in s)


def fetch_universe_from_fdr() -> list[dict]:
    """FinanceDataReader로 KOSPI+KOSDAQ 보통주 리스트 (시가총액 + 섹터 포함)."""
    sector_overrides = load_sector_overrides()
    wics_map = load_wics_map()
    universe = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df = fdr.StockListing(market)
        except Exception as e:
            print(f"  {market} 리스팅 실패: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if df is None or df.empty:
            print(f"  {market} 빈 리스팅", file=sys.stderr)
            continue
        print(f"  {market} 컬럼: {list(df.columns)}", file=sys.stderr)
        code_col = next((c for c in ["Code", "Symbol"] if c in df.columns), None)
        name_col = next((c for c in ["Name"] if c in df.columns), None)
        marcap_col = next((c for c in ["Marcap", "MarketCap"] if c in df.columns), None)
        # 섹터: 실제 업종 컬럼만. Department/Dept는 KOSDAQ '소속부'라 제외.
        sector_cols = [c for c in ["Sector", "Industry", "업종"] if c in df.columns]
        if not code_col or not name_col:
            print(f"  {market} 코드/이름 컬럼 인식 실패", file=sys.stderr)
            continue
        cnt_total, cnt_kept = len(df), 0
        for _, row in df.iterrows():
            code_raw = row[code_col]
            name = str(row[name_col]).strip() if pd.notna(row[name_col]) else ""
            if not name or pd.isna(code_raw):
                continue
            code = str(code_raw).zfill(6)
            if not is_common_stock(name):
                continue
            marcap = 0
            if marcap_col and pd.notna(row.get(marcap_col)):
                try:
                    marcap = float(row[marcap_col])
                except (ValueError, TypeError):
                    marcap = 0
            # 섹터 우선순위: WICS 맵(1순위) → FDR 업종(소속부 아닌 것) → tickers.json 백업
            sector = wics_map.get(code, "")
            if not sector:
                for sc in sector_cols:
                    v = row.get(sc)
                    if v is not None and pd.notna(v) and not _is_garbage_sector(str(v)):
                        sector = str(v).strip()
                        break
            if not sector and code in sector_overrides:
                sector = sector_overrides[code]
            universe.append({
                "ticker": code,
                "name": name,
                "market": market,
                "sector": sector,
                "market_cap": marcap,
            })
            cnt_kept += 1
        with_sector = sum(1 for u in universe if u["market"] == market and u["sector"])
        print(f"  {market}: {cnt_total} → {cnt_kept} (보통주, 섹터 보유 {with_sector})", file=sys.stderr)
    return universe


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


def fetch_one_price(info: dict, start_date: str, end_date: str):
    ticker = info["ticker"]
    try:
        df = fdr.DataReader(ticker, start_date, end_date)
        if df is None or df.empty or "Close" not in df.columns:
            return ticker, None
        close = df["Close"].dropna()
        if close.empty:
            return ticker, None
        return ticker, close
    except Exception:
        return ticker, None


def main() -> int:
    today_dt = datetime.now()

    print("[1/4] 종목 리스트 수집...", file=sys.stderr)
    universe = []
    try:
        universe = fetch_universe_from_fdr()
        print(f"  -> FDR에서 {len(universe)}개 보통주", file=sys.stderr)
    except Exception as e:
        print(f"  FDR 전체 실패: {e}", file=sys.stderr)

    if not universe and TICKERS_FILE.exists():
        print("  -> data/tickers.json 폴백 사용", file=sys.stderr)
        universe = json.loads(TICKERS_FILE.read_text(encoding="utf-8"))
        # 폴백 데이터에도 보통주 필터 적용
        universe = [u for u in universe if is_common_stock(u.get("name", ""))]

    if not universe:
        print("  ✗ 종목 리스트를 얻지 못함", file=sys.stderr)
        return 1

    # 중복 제거
    seen = set()
    uniq = []
    for u in universe:
        if u["ticker"] not in seen:
            seen.add(u["ticker"])
            uniq.append(u)
    universe = uniq
    print(f"  -> 중복 제거 후 {len(universe)}개", file=sys.stderr)

    start_date = (today_dt - timedelta(days=400)).strftime("%Y-%m-%d")
    end_date = today_dt.strftime("%Y-%m-%d")
    print(f"[2/4] 네이버 일봉 다운로드 (병렬, {start_date} ~ {end_date})...", file=sys.stderr)

    close_dict: dict[str, pd.Series] = {}
    fail = 0
    total = len(universe)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_one_price, u, start_date, end_date): u for u in universe}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                ticker, close = fut.result(timeout=30)
            except Exception:
                fail += 1
                continue
            if close is not None and not close.empty:
                close_dict[ticker] = close
            else:
                fail += 1
            if i % 200 == 0 or i == total:
                print(f"  진행 {i}/{total} (성공 {len(close_dict)}, 실패 {fail})", file=sys.stderr)

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
        info = info_map.get(ticker, {})
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
                "name": info.get("name", ticker),
                "market": info.get("market", ""),
                "sector": info.get("sector", ""),
                "price": last,
                "market_cap": info.get("market_cap", 0),
                "returns": rets,
                "rs": None,
                "quality": quality,
                "quality_pct": None,
                "acceleration": accel,
                "acceleration_pct": None,
                "return_pct": {},
            })
        except Exception:
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
