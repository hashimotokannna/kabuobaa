#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kabuobaa 夜間スクリーニングバッチ
================================

毎晩、日本の全上場銘柄（東証プライム/スタンダード/グロース）を対象に:

  1. JPX公式の上場銘柄一覧を取得
  2. 各銘柄の日足1年分をYahoo Financeから取得
  3. 「終わった株」（ピーク価格から下がり続けている銘柄）を除外
  4. 「いつもより安い」度合いで上位100銘柄を選定
  5. 業種カテゴリ別の帳簿型Webページ(docs/index.html)とdocs/data.jsonを生成

使い方:
  python screener.py           # 本番実行（約40〜60分）
  python screener.py --demo    # ダミーデータでページ生成だけ試す（数秒）

判断の閾値はすべて下のCONFIGにまとまっています。
"""

import argparse
import base64
import html
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"

# ------------------------------------------------------------
# 設定（味付けを変えたいときはここだけ触ればOK）
# ------------------------------------------------------------
CONFIG = {
    # 選定
    "TOP_N": 100,            # ピックアップする銘柄数
    "RECENT_DAYS": 20,       # 「普段の値段」とみなす直近営業日数
    "CHEAP_PCT": 5.0,        # ◎かなり安い: 直近高値からこの%以上下落
    "MILD_PCT": 3.0,         # ○安め: 直近高値からこの%以上下落

    # 対象外（そもそも土俵に上げない）
    "MIN_RECORDS": 120,      # 上場から日が浅くデータがこれ未満なら対象外
    "MIN_PRICE": 100,        # 終値がこの円未満は対象外（低位株はノイズが多い）
    "MIN_TURNOVER": 50_000_000,  # 直近20日の平均売買代金がこの円未満は対象外（流動性不足）

    # 「終わった株」除外（ピクセラ型: ピークから下がり続けている銘柄）
    "DEAD_DRAWDOWN": 0.40,       # 1年高値からの下落率がこれ以上 → 除外
    "DEAD_BELOW_MA_RATIO": 0.90, # 直近60営業日のうちこの割合以上で200日平均線を下回る → 除外

    # 通信
    "WORKERS": 6,            # 同時に取得する並列数（上げすぎるとブロックされる）
    "THROTTLE_SEC": 0.1,     # 各作業員が1銘柄ごとに入れる待ち時間（礼儀）
    "RETRIES": 3,
}

# 東証以外（札幌・名古屋・福岡の単独上場銘柄）を対象に加えたい場合はここに追記。
# Yahoo Financeのサフィックス: 札証=.S / 名証=.N / 福証=.F
# 例: {"code": "3544", "name": "サツドラHD", "sector": "小売業", "market": "札証", "suffix": ".S"},
EXTRA_TICKERS = []

# JPX公式・全上場銘柄一覧（東証）
JPX_LIST_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"

# 33業種 → 表示用カテゴリ（案A帳簿型のセクション）
SECTOR_GROUPS = {
    "輸送用機器": "自動車・輸送機",
    "電気機器": "電機・精密", "精密機器": "電機・精密",
    "機械": "機械・素材", "鉄鋼": "機械・素材", "非鉄金属": "機械・素材",
    "金属製品": "機械・素材", "ガラス・土石製品": "機械・素材",
    "化学": "化学・医薬", "医薬品": "化学・医薬",
    "食料品": "食品・小売", "小売業": "食品・小売", "水産・農林業": "食品・小売",
    "鉱業": "エネルギー・資源", "石油・石炭製品": "エネルギー・資源", "電気・ガス業": "エネルギー・資源",
    "建設業": "建設・不動産", "不動産業": "建設・不動産",
    "銀行業": "金融", "証券、商品先物取引業": "金融", "保険業": "金融", "その他金融業": "金融",
    "情報・通信業": "情報通信・サービス", "サービス業": "情報通信・サービス",
    "陸運業": "運輸・卸売", "海運業": "運輸・卸売", "空運業": "運輸・卸売",
    "倉庫・運輸関連業": "運輸・卸売", "卸売業": "運輸・卸売",
    "繊維製品": "生活・その他製品", "パルプ・紙": "生活・その他製品",
    "ゴム製品": "生活・その他製品", "その他製品": "生活・その他製品",
}
DEFAULT_GROUP = "生活・その他製品"


# ------------------------------------------------------------
# 1. 上場銘柄一覧の取得（JPX公式Excel）
# ------------------------------------------------------------
def fetch_universe():
    """JPXの上場銘柄一覧から (code, name, sector, market, suffix) のリストを作る"""
    import io
    import requests
    import pandas as pd

    resp = requests.get(JPX_LIST_URL, timeout=60,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    df = pd.read_excel(io.BytesIO(resp.content))

    wanted_markets = {
        "プライム（内国株式）": "プライム",
        "スタンダード（内国株式）": "スタンダード",
        "グロース（内国株式）": "グロース",
    }
    universe = []
    for _, row in df.iterrows():
        market_raw = str(row.get("市場・商品区分", ""))
        if market_raw not in wanted_markets:
            continue  # ETF・REIT・外国株・PRO Marketなどは対象外
        code = str(row.get("コード", "")).strip()
        if code.endswith(".0"):
            code = code[:-2]
        if not code:
            continue
        universe.append({
            "code": code,
            "name": str(row.get("銘柄名", "")).strip(),
            "sector": str(row.get("33業種区分", "")).strip(),
            "market": wanted_markets[market_raw],
            "suffix": ".T",
        })
    universe.extend(EXTRA_TICKERS)
    return universe


# ------------------------------------------------------------
# 2. 日足データの取得（Yahoo Finance chart API）
# ------------------------------------------------------------
def fetch_daily(session, code, suffix=".T"):
    """日足1年分を [{date, open, high, low, close, volume}] (古い順) で返す"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}"
    params = {"range": "1y", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    last_err = None
    for attempt in range(CONFIG["RETRIES"]):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code == 429:  # レート制限: 長めに待って再試行
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            payload = resp.json()
            result = (payload.get("chart", {}).get("result") or [None])[0]
            if not result:
                return None
            ts = result.get("timestamp") or []
            quote = (result.get("indicators", {}).get("quote") or [{}])[0]
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            vols = quote.get("volume") or []
            days = []
            for i, t in enumerate(ts):
                try:
                    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                    v = vols[i] if i < len(vols) else None
                except IndexError:
                    continue
                if None in (o, h, l, c):
                    continue
                d = datetime.fromtimestamp(t, JST).date()
                days.append({"date": d.isoformat(), "open": o, "high": h,
                             "low": l, "close": c, "volume": v or 0})
            # 同一日の重複（当日ザラ場中の行）は後勝ちで1本化
            dedup = {}
            for day in days:
                dedup[day["date"]] = day

            # 当日分の補完:
            # 長期レンジの日足配列は最新営業日の反映が遅れることがある。
            # 同じ応答のmeta（Yahooサイトの画面と同じ「現在の気配」）から
            # 最新営業日の四本値を拾い、日足配列に無ければ追加する。
            meta = result.get("meta") or {}
            mtime = meta.get("regularMarketTime")
            mclose = meta.get("regularMarketPrice")
            if mtime and mclose is not None:
                mdate = datetime.fromtimestamp(mtime, JST).date().isoformat()
                if mdate not in dedup:
                    mopen = meta.get("regularMarketOpen")
                    mhigh = meta.get("regularMarketDayHigh")
                    mlow = meta.get("regularMarketDayLow")
                    dedup[mdate] = {
                        "date": mdate,
                        "open": mopen if mopen is not None else mclose,
                        "high": mhigh if mhigh is not None else mclose,
                        "low": mlow if mlow is not None else mclose,
                        "close": mclose,
                        "volume": meta.get("regularMarketVolume") or 0,
                    }
            return [dedup[k] for k in sorted(dedup)]
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1 + attempt)
    print(f"  ! {code}: 取得失敗 ({last_err})", file=sys.stderr)
    return None


# ------------------------------------------------------------
# 3. 指標計算と除外判定
# ------------------------------------------------------------
def compute_metrics(days):
    """日足リスト（古い順）から判定用の指標を計算する"""
    n = CONFIG["RECENT_DAYS"]
    closes = [d["close"] for d in days]
    latest = days[-1]

    recent = days[-n:]
    usual = sum(d["close"] for d in recent) / len(recent)
    high20 = max(d["high"] for d in recent)
    drop_yen = high20 - latest["close"]
    drop_pct = (drop_yen / high20 * 100) if high20 > 0 else 0.0

    high1y = max(d["high"] for d in days)
    low1y = min(d["low"] for d in days)
    drawdown_1y = (high1y - latest["close"]) / high1y if high1y > 0 else 0.0

    # 200日平均線と、直近60営業日でそれを下回っていた割合
    # （簡略化: 直近200日の単純平均を基準線とする）
    below_ratio = 0.0
    if len(closes) >= 200:
        ma200 = sum(closes[-200:]) / 200
        last60 = closes[-60:]
        below_ratio = sum(1 for c in last60 if c < ma200) / len(last60)

    turnover = sum(d["close"] * d["volume"] for d in days[-n:]) / len(recent)

    return {
        "open": latest["open"], "high": latest["high"],
        "low": latest["low"], "close": latest["close"],
        "date": latest["date"],
        "usual": usual, "high20": high20,
        "drop_yen": drop_yen, "drop_pct": drop_pct,
        "high1y": high1y, "low1y": low1y, "drawdown_1y": drawdown_1y,
        "below_ma_ratio": below_ratio,
        "turnover": turnover,
        "records": len(days),
    }


def classify(metrics):
    """対象外/除外/対象 の判定。返り値: ('ok'|'dead'|'skip', 理由)"""
    c = CONFIG
    if metrics["records"] < c["MIN_RECORDS"]:
        return "skip", "上場間もなくデータ不足"
    if metrics["close"] < c["MIN_PRICE"]:
        return "skip", "低位株"
    if metrics["turnover"] < c["MIN_TURNOVER"]:
        return "skip", "流動性不足"
    if metrics["drawdown_1y"] >= c["DEAD_DRAWDOWN"]:
        return "dead", f"1年高値から{metrics['drawdown_1y']*100:.0f}%下落"
    if metrics["below_ma_ratio"] >= c["DEAD_BELOW_MA_RATIO"]:
        return "dead", "長期の下落トレンド継続中"
    return "ok", ""


def level_of(drop_pct):
    if drop_pct >= CONFIG["CHEAP_PCT"]:
        return "cheap"   # ◎ かなり安い
    if drop_pct >= CONFIG["MILD_PCT"]:
        return "mild"    # ○ 安め
    return "normal"




# ------------------------------------------------------------
# 仮想実行: 「◎になったら翌日の始値で100株買い、+5000円の指値で売る」
# を過去1年の日足でなぞる（検証レポート用）
# ------------------------------------------------------------
def simulate_grandma(days):
    """取引のリストを返す。sell_dateがNoneのものは未決済（含み損益）"""
    n = CONFIG["RECENT_DAYS"]
    if len(days) < n + 5:
        return []
    opens = [d["open"] for d in days]
    highs = [d["high"] for d in days]
    closes = [d["close"] for d in days]
    trades, pos = [], None
    for i in range(n, len(days) - 1):
        if pos is None:
            high20 = max(highs[i - n + 1:i + 1])
            c = closes[i]
            drop_pct = (high20 - c) / high20 * 100 if high20 > 0 else 0
            if drop_pct >= CONFIG["CHEAP_PCT"] and c >= CONFIG["MIN_PRICE"]:
                pos = {"buy_i": i + 1, "buy": opens[i + 1]}
        elif i >= pos["buy_i"] and highs[i] >= pos["buy"] + 50.0:
            # 100株なら +50円/株 = +5,000円 で指値成立
            trades.append({
                "buy_date": days[pos["buy_i"]]["date"],
                "sell_date": days[i]["date"],
                "held": max(1, i - pos["buy_i"] + 1),
                "pnl": 5000.0,
            })
            pos = None
    if pos is not None and pos["buy_i"] < len(days):
        trades.append({
            "buy_date": days[pos["buy_i"]]["date"],
            "sell_date": None,
            "held": len(days) - 1 - pos["buy_i"],
            "pnl": (closes[-1] - pos["buy"]) * 100,
        })
    return trades


# ------------------------------------------------------------
# 4. スクリーニング本体
# ------------------------------------------------------------
_thread_local = None


def _get_session():
    """スレッドごとに専用の通信セッションを持たせる"""
    global _thread_local
    import threading
    import requests
    if _thread_local is None:
        _thread_local = threading.local()
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


def run_screening():
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print("上場銘柄一覧を取得中...")
    universe = fetch_universe()

    # テストモード: 環境変数 TEST_LIMIT に数字が入っていたら先頭N銘柄だけ
    limit = int(os.environ.get("TEST_LIMIT", "0") or 0)
    if limit > 0:
        universe = universe[:limit]
        print(f"  ★テストモード: 先頭{limit}銘柄のみで実行します")
    print(f"  対象: {len(universe)}銘柄")

    def task(stock):
        days = fetch_daily(_get_session(), stock["code"], stock["suffix"])
        time.sleep(CONFIG["THROTTLE_SEC"])
        return stock, days

    candidates, dead_count, skip_count, fail_count = [], 0, 0, 0
    all_results, sim_records = [], []
    done = 0
    with ThreadPoolExecutor(max_workers=CONFIG["WORKERS"]) as pool:
        futures = [pool.submit(task, s) for s in universe]
        for fut in as_completed(futures):
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(universe)} 取得済み...")
            stock, days = fut.result()
            base = {"code": stock["code"], "name": stock["name"],
                    "market": stock["market"], "sector": stock["sector"]}
            if not days:
                fail_count += 1
                all_results.append({**base, "status": "fail",
                                    "reason": "データ取得失敗"})
                continue
            m = compute_metrics(days)
            status, reason = classify(m)
            base["close"] = round(m["close"], 1)
            base["drop_pct"] = round(m["drop_pct"], 2)
            if status == "dead":
                dead_count += 1
                all_results.append({**base, "status": "dead", "reason": reason})
                continue
            if status == "skip":
                skip_count += 1
                all_results.append({**base, "status": "skip", "reason": reason})
                continue
            candidates.append({**stock, **m, "days": days[-10:]})
            all_results.append({**base, "status": "ok", "reason": ""})
            trades = simulate_grandma(days)
            if trades:
                sim_records.append({"code": stock["code"],
                                    "name": stock["name"], "trades": trades})

    candidates.sort(key=lambda s: s["drop_pct"], reverse=True)
    picked = candidates[:CONFIG["TOP_N"]]
    picked_codes = {s["code"] for s in picked}
    for r in all_results:
        if r["status"] == "ok":
            r["status"] = "picked" if r["code"] in picked_codes else "bench"

    stats = {
        "universe": len(universe),
        "dead_excluded": dead_count,
        "skipped": skip_count,
        "failed": fail_count,
    }
    return picked, stats, all_results, sim_records


# ------------------------------------------------------------
# デモモード（ネット接続なしでページ生成を確認する用のダミーデータ）
# ------------------------------------------------------------
DEMO_NAMES = [
    ("7203", "トヨタ自動車", "輸送用機器", "プライム"),
    ("6902", "デンソー", "輸送用機器", "プライム"),
    ("7270", "SUBARU", "輸送用機器", "プライム"),
    ("6594", "ニデック", "電気機器", "プライム"),
    ("6971", "京セラ", "電気機器", "プライム"),
    ("6750", "エレコム", "電気機器", "プライム"),
    ("7735", "SCREEN", "電気機器", "プライム"),
    ("4063", "信越化学工業", "化学", "プライム"),
    ("4519", "中外製薬", "医薬品", "プライム"),
    ("2802", "味の素", "食料品", "プライム"),
    ("3391", "ツルハHD", "小売業", "プライム"),
    ("3038", "神戸物産", "卸売業", "プライム"),
    ("9101", "日本郵船", "海運業", "プライム"),
    ("1928", "積水ハウス", "建設業", "プライム"),
    ("8306", "三菱UFJ FG", "銀行業", "プライム"),
    ("9432", "NTT", "情報・通信業", "プライム"),
    ("9613", "NTTデータG", "情報・通信業", "プライム"),
    ("2413", "エムスリー", "サービス業", "プライム"),
    ("7095", "ジャパンM&A", "サービス業", "グロース"),
    ("5032", "ANYCOLOR", "情報・通信業", "グロース"),
    ("2929", "ファーマフーズ", "食料品", "スタンダード"),
    ("7839", "SHOEI", "その他製品", "プライム"),
    ("5108", "ブリヂストン", "ゴム製品", "プライム"),
    ("3861", "王子HD", "パルプ・紙", "プライム"),
]


def make_demo_data():
    rng = random.Random(42)
    picked = []
    for code, name, sector, market in DEMO_NAMES:
        base = rng.choice([800, 1500, 2400, 3100, 5200, 9800])
        drop_pct = rng.uniform(2.5, 9.5)
        high20 = base * rng.uniform(1.0, 1.06)
        close = high20 * (1 - drop_pct / 100)
        picked.append({
            "code": code, "name": name, "sector": sector, "market": market,
            "suffix": ".T",
            "date": datetime.now(JST).date().isoformat(),
            "open": close * rng.uniform(0.99, 1.02),
            "high": close * rng.uniform(1.0, 1.03),
            "low": close * rng.uniform(0.97, 1.0),
            "close": close,
            "usual": close * rng.uniform(1.0, 1.05),
            "high20": high20,
            "drop_yen": high20 - close,
            "drop_pct": drop_pct,
            "high1y": high20 * rng.uniform(1.0, 1.2),
            "drawdown_1y": rng.uniform(0.05, 0.35),
            "below_ma_ratio": rng.uniform(0, 0.5),
            "turnover": 1e9,
            "records": 245,
            "days": [
                {"date": f"2026-08-{3 + i:02d}",
                 "open": close * rng.uniform(0.98, 1.03),
                 "high": close * rng.uniform(1.03, 1.06),
                 "low": close * rng.uniform(0.95, 0.98),
                 "close": close * rng.uniform(0.98, 1.03),
                 "volume": 1_000_000}
                for i in range(10)
            ],
        })
    picked.sort(key=lambda s: s["drop_pct"], reverse=True)
    stats = {"universe": 3912, "dead_excluded": 214, "skipped": 1480, "failed": 3}

    all_results = []
    for i, s in enumerate(picked):
        all_results.append({"code": s["code"], "name": s["name"],
                            "market": s["market"], "sector": s["sector"],
                            "close": round(s["close"], 1),
                            "drop_pct": round(s["drop_pct"], 2),
                            "status": "picked", "reason": ""})
    all_results += [
        {"code": "9999", "name": "デモ圏外株", "market": "プライム", "sector": "サービス業",
         "close": 1200.0, "drop_pct": 1.2, "status": "bench", "reason": ""},
        {"code": "6800", "name": "デモ右肩下がり", "market": "スタンダード", "sector": "電気機器",
         "close": 300.0, "drop_pct": 8.0, "status": "dead", "reason": "1年高値から55%下落"},
        {"code": "7777", "name": "デモ流動性不足", "market": "グロース", "sector": "サービス業",
         "close": 800.0, "drop_pct": 4.0, "status": "skip", "reason": "流動性不足"},
        {"code": "8888", "name": "デモ取得失敗", "market": "プライム", "sector": "機械",
         "status": "fail", "reason": "データ取得失敗"},
    ]
    sim_records = []
    for s in picked[:10]:
        sim_records.append({"code": s["code"], "name": s["name"], "trades": [
            {"buy_date": "2026-05-11", "sell_date": "2026-05-18", "held": 6, "pnl": 5000.0},
            {"buy_date": "2026-06-02", "sell_date": "2026-07-13", "held": 28, "pnl": 5000.0},
            {"buy_date": "2026-07-30", "sell_date": None, "held": 9,
             "pnl": rng.uniform(-15000, 4000)},
        ]})
    return picked, stats, all_results, sim_records


# ------------------------------------------------------------
# 5. 出力（data.json と 案A帳簿型 index.html）
# ------------------------------------------------------------
def load_previous_codes():
    """前回のdata.jsonから銘柄コード集合を読む（NEW表示用）"""
    path = DOCS / "data.json"
    if not path.exists():
        return None
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
        return {s["code"] for s in prev.get("stocks", [])}
    except Exception:  # noqa: BLE001
        return None


def build_output(picked, stats):
    prev_codes = load_previous_codes()
    now = datetime.now(JST)

    stocks_out = []
    for rank, s in enumerate(picked, 1):
        stocks_out.append({
            "rank": rank,
            "code": s["code"],
            "name": s["name"],
            "market": s["market"],
            "sector": s["sector"],
            "group": SECTOR_GROUPS.get(s["sector"], DEFAULT_GROUP),
            "date": s["date"],
            "open": round(s["open"], 1),
            "high": round(s["high"], 1),
            "low": round(s["low"], 1),
            "close": round(s["close"], 1),
            "usual": round(s["usual"], 1),
            "high20": round(s["high20"], 1),
            "drop_yen": round(s["drop_yen"], 1),
            "drop_pct": round(s["drop_pct"], 2),
            "high1y": round(s.get("high1y", 0), 1),
            "low1y": round(s.get("low1y", 0), 1),
            "suffix": s.get("suffix", ".T"),
            "level": level_of(s["drop_pct"]),
            "is_new": (prev_codes is not None and s["code"] not in prev_codes),
            "days": [
                {"date": d["date"], "open": round(d["open"], 1),
                 "high": round(d["high"], 1), "low": round(d["low"], 1),
                 "close": round(d["close"], 1)}
                for d in reversed(s.get("days", []))
            ],
        })

    data = {
        "generated_at": now.isoformat(),
        "date_label": now.strftime("%-m/%-d") if sys.platform != "win32" else now.strftime("%m/%d"),
        "stats": stats,
        "config": {k: CONFIG[k] for k in
                   ("TOP_N", "RECENT_DAYS", "CHEAP_PCT", "MILD_PCT",
                    "DEAD_DRAWDOWN", "DEAD_BELOW_MA_RATIO")},
        "stocks": stocks_out,
    }
    return data


def yen(v):
    return f"{v:,.0f}"


def render_html(data):
    stocks = data["stocks"]
    stats = data["stats"]

    # カテゴリごとにまとめ、銘柄数の多い順にセクションを並べる
    groups = {}
    for s in stocks:
        groups.setdefault(s["group"], []).append(s)
    ordered = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)

    weekdays = "月火水木金土日"
    dt = datetime.fromisoformat(data["generated_at"])
    date_str = f"{dt.month}/{dt.day}（{weekdays[dt.weekday()]}）"
    # 平日9:00〜15:35の実行なら「取引時間中の途中経過」とみなす
    mins = dt.hour * 60 + dt.minute
    is_intraday = dt.weekday() < 5 and (9 * 60) <= mins <= (15 * 60 + 35)
    price_label = "現在値" if is_intraday else "終値"

    chip_class = {"プライム": "prime", "スタンダード": "std", "グロース": "growth"}

    def latest_block(s):
        days = s.get("days", [])
        if not days:
            return ""
        d = days[0]  # 新しい順の先頭 = 最新日
        dt2 = datetime.fromisoformat(d["date"])
        label = f"{dt2.month}/{dt2.day}({weekdays[dt2.weekday()]})"
        suffix2 = "（取引時間中の途中経過）" if is_intraday else "（確定）"
        return (
            f'<div class="nhead">最新日 {label} の値段{suffix2}</div>'
            f'<div class="fact"><span>始値</span><span class="num">{yen(d["open"])}円</span></div>'
            f'<div class="fact"><span>高値</span><span class="num">{yen(d["high"])}円</span></div>'
            f'<div class="fact"><span>安値</span><span class="num">{yen(d["low"])}円</span></div>'
            f'<div class="fact"><span>{price_label}</span><span class="num">{yen(d["close"])}円</span></div>'
        )

    def day_rows(s):
        out = []
        for d in s.get("days", []):
            dt2 = datetime.fromisoformat(d["date"])
            label = f"{dt2.month}/{dt2.day}({weekdays[dt2.weekday()]})"
            out.append(
                f'<div class="nrow num"><span class="nd">{label}</span>'
                f'<span>始:{yen(d["open"])} 高:{yen(d["high"])} '
                f'安:{yen(d["low"])} 終:{yen(d["close"])}</span></div>'
            )
        return "\n".join(out)

    rows_html = []
    for group_name, members in ordered:
        rows_html.append(
            f'<div class="sec">{html.escape(group_name)}'
            f'<span class="cnt">{len(members)}銘柄</span></div>'
        )
        for i, s in enumerate(members, 1):
            chip = chip_class.get(s["market"], "local")
            badge = ('<span class="badge cheap">◎</span>' if s["level"] == "cheap"
                     else '<span class="badge mild">○</span>' if s["level"] == "mild"
                     else "")
            new_mark = '<span class="new">NEW</span>' if s["is_new"] else ""
            yahoo_url = f'https://finance.yahoo.co.jp/quote/{s["code"]}{s.get("suffix", ".T")}'
            range1y = ""
            if s.get("low1y") and s.get("high1y"):
                range1y = (f'<div class="fact"><span>1年の値段の範囲</span>'
                           f'<span class="num">{yen(s["low1y"])} 〜 {yen(s["high1y"])}円</span></div>')
            rows_html.append(f"""
      <details class="drow">
      <summary class="row">
        <div class="rk num">{i}</div>
        <div class="nm">
          <div class="n1">{html.escape(s["name"])} <span class="chip {chip}">{html.escape(s["market"])}</span>{new_mark}</div>
          <div class="n2 num">{s["code"]} ・ 普段 {yen(s["usual"])}円 ・ 総合{s["rank"]}位</div>
        </div>
        <div class="px">
          <div class="p1 num"><small>{price_label}</small> {yen(s["close"])}<small>円</small></div>
          <div class="p2 num drop">高値から −{s["drop_pct"]:.1f}%</div>
        </div>
        <div>{badge}</div>
        <div class="chev">›</div>
      </summary>
      <div class="notebox">
        {latest_block(s)}
        <div class="fact"><span>普段の値段（20日平均）</span><span class="num">{yen(s["usual"])}円</span></div>
        <div class="fact"><span>直近の高値（20日）</span><span class="num">{yen(s["high20"])}円</span></div>
        <div class="fact"><span>高値からの下げ</span><span class="num drop">−{yen(s["drop_yen"])}円（−{s["drop_pct"]:.1f}%）</span></div>
        {range1y}
        <div class="nhead">ノート（新しい順）</div>
        {day_rows(s)}
        <a class="ylink" href="{yahoo_url}" target="_blank" rel="noopener">Yahoo!ファイナンスでこの銘柄の詳細を見る →</a>
      </div>
      </details>"""
            )

    body_rows = "\n".join(rows_html)
    excluded = stats.get("dead_excluded", 0)
    universe = stats.get("universe", 0)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="robots" content="noindex, nofollow">
<link rel="apple-touch-icon" href="icon.png">
<link rel="icon" type="image/png" href="icon.png">
<title>Kabuobaa - 今夜の{len(stocks)}銘柄</title>
<style>
  :root{{
    --ink:#1c1c1e; --ink2:#6e6e73; --ink3:#aeaeb2;
    --paper:#faf6ec; --paper-line:#e7e0cf;
    --bg:#f2f2f7; --line:#e5e5ea;
    --cheap:#c62f2f; --cheap-bg:#fdeeee;
    --mild:#b06a00; --mild-bg:#fdf3e3;
  }}
  *{{box-sizing:border-box; margin:0; padding:0;}}
  body{{
    font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
    background:var(--bg); color:var(--ink);
    max-width:560px; margin:0 auto; padding:14px 12px 40px;
  }}
  .num{{font-family:ui-monospace,"SF Mono",Menlo,monospace; font-variant-numeric:tabular-nums;}}
  header{{padding:6px 4px 12px;}}
  header .t{{font-size:22px; font-weight:800;}}
  header .s{{font-size:12px; color:var(--ink2); margin-top:3px;}}
  .ledger{{background:var(--paper); border-radius:14px; padding:4px 0;
    box-shadow:0 1px 3px rgba(0,0,0,.06);}}
  .sec{{font-size:12px; font-weight:800; color:#7a6a45; letter-spacing:.06em;
    padding:14px 14px 6px; display:flex; justify-content:space-between; align-items:baseline;}}
  .sec .cnt{{font-weight:600; color:#a99a76; font-size:10.5px;}}
  .row{{display:flex; align-items:center; gap:9px; padding:9px 14px;
    border-top:1px solid var(--paper-line); text-decoration:none; color:inherit;}}
  .rk{{width:22px; font-size:12px; color:#a99a76; font-weight:700; text-align:right; flex:none;}}
  .nm{{flex:1; min-width:0;}}
  .n1{{font-size:13.5px; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}}
  .n2{{font-size:10.5px; color:var(--ink2); margin-top:2px;}}
  .px{{text-align:right; flex:none;}}
  .p1{{font-size:14.5px; font-weight:700;}}
  .p1 small{{font-size:10px; font-weight:600; color:var(--ink2);}}
  .p2{{font-size:10.5px; margin-top:2px;}}
  .drop{{color:var(--cheap); font-weight:700;}}
  .chip{{display:inline-block; font-size:9.5px; font-weight:600; border-radius:5px;
    padding:1.5px 5px; vertical-align:1px; margin-left:2px;}}
  .chip.prime{{background:#e8eef8; color:#2e4d7b;}}
  .chip.std{{background:#e9f3ea; color:#3a5a40;}}
  .chip.growth{{background:#f4ecf9; color:#6b4487;}}
  .chip.local{{background:#f7efe4; color:#8a5a17;}}
  .new{{display:inline-block; font-size:9px; font-weight:800; color:#b06a00;
    background:var(--mild-bg); border-radius:4px; padding:1px 4px; margin-left:4px; vertical-align:1px;}}
  .badge{{font-size:12px; font-weight:800; border-radius:6px; padding:3px 7px; white-space:nowrap;}}
  .badge.cheap{{color:var(--cheap); background:var(--cheap-bg);}}
  .badge.mild{{color:var(--mild); background:var(--mild-bg);}}
  footer{{padding:16px 8px; font-size:10.5px; color:var(--ink3); text-align:center; line-height:1.7;}}
  details.drow summary{{list-style:none; cursor:pointer;}}
  details.drow summary::-webkit-details-marker{{display:none;}}
  .chev{{color:#c9bd9d; font-size:16px; font-weight:700; transition:transform .15s;}}
  details[open] .chev{{transform:rotate(90deg);}}
  details[open] summary.row{{background:#f4eedd;}}
  .notebox{{background:#fffdf6; border-top:1px dashed var(--paper-line); padding:10px 14px 14px;}}
  .fact{{display:flex; justify-content:space-between; font-size:12px; padding:5px 0;
    border-bottom:1px solid #f0ead9;}}
  .fact span:first-child{{color:var(--ink2);}}
  .fact .num{{font-weight:700;}}
  .nhead{{font-size:10.5px; font-weight:800; color:#7a6a45; letter-spacing:.06em; margin:12px 0 4px;}}
  .nrow{{display:flex; gap:10px; font-size:11.5px; padding:4px 0;}}
  .nrow .nd{{width:58px; font-weight:700; flex:none;}}
  .ylink{{display:block; margin-top:12px; font-size:12px; font-weight:700; color:#2e4d7b;
    text-decoration:none; text-align:center; background:#eef2f8; border-radius:9px; padding:9px;}}
  .pnav{{display:flex; gap:8px; padding:0 0 12px;}}
  .pnav a{{flex:1; font-size:12px; font-weight:700; color:#4a3f28; text-decoration:none;
    background:#f4eedd; border-radius:10px; padding:9px 10px; text-align:center;}}
</style>
</head>
<body>
<header>
  <div class="t">今夜の{len(stocks)}銘柄</div>
  <div class="s">{date_str} {dt.hour:02d}:{dt.minute:02d} 記帳{"（取引時間中・当日分は途中経過）" if is_intraday else ""} ・ 行をタップでノート ・ 判断はご自身で</div>
</header>
<div class="pnav">
  <a href="universe.html">全銘柄の判定一覧 ›</a>
  <a href="backtest.html">手法の検証レポート ›</a>
</div>
<div class="ledger">
{body_rows}
</div>
<footer>
  対象 {universe:,}銘柄 ／ 右肩下がりのため除外 {excluded:,}銘柄<br>
  ◎=直近{data["config"]["RECENT_DAYS"]}日高値から{data["config"]["CHEAP_PCT"]:.0f}%以上安い ・ ○={data["config"]["MILD_PCT"]:.0f}%以上安い<br>
  データ: Yahoo Finance ・ このページは判断材料の表示のみ
</footer>
</body>
</html>
"""


# ------------------------------------------------------------
# サブページ共通の枠（cream帳簿デザイン）
# ------------------------------------------------------------
SUBPAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<link rel="apple-touch-icon" href="icon.png">
<link rel="icon" type="image/png" href="icon.png">
<title>__TITLE__</title>
<style>
  :root{--ink:#1c1c1e; --ink2:#6e6e73; --ink3:#aeaeb2; --paper:#faf6ec;
    --paper-line:#e7e0cf; --bg:#f2f2f7; --cheap:#c62f2f; --cheap-bg:#fdeeee;
    --mild:#b06a00; --mild-bg:#fdf3e3;}
  *{box-sizing:border-box; margin:0; padding:0;}
  body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
    background:var(--bg); color:var(--ink); max-width:640px; margin:0 auto;
    padding:14px 12px 40px;}
  .num{font-family:ui-monospace,"SF Mono",Menlo,monospace; font-variant-numeric:tabular-nums;}
  header{padding:6px 4px 12px;}
  header .t{font-size:20px; font-weight:800;}
  header .s{font-size:12px; color:var(--ink2); margin-top:3px; line-height:1.6;}
  .back{display:inline-block; font-size:12px; font-weight:700; color:#2e4d7b;
    text-decoration:none; margin-bottom:8px;}
  .card{background:var(--paper); border-radius:14px; padding:12px 14px;
    margin-bottom:12px; box-shadow:0 1px 3px rgba(0,0,0,.06);}
  .card h2{font-size:13px; font-weight:800; color:#7a6a45; letter-spacing:.05em;
    margin-bottom:8px;}
  .fact{display:flex; justify-content:space-between; font-size:12.5px; padding:6px 0;
    border-bottom:1px solid #f0ead9;}
  .fact span:first-child{color:var(--ink2);}
  .fact .v{font-weight:700;}
  .plus{color:#2e7d32;} .minus{color:var(--cheap);}
  .note{font-size:11px; color:var(--ink3); line-height:1.7; padding:8px 4px;}
  __EXTRA_CSS__
</style>
</head>
<body>
<a class="back" href="index.html">‹ 帳簿にもどる</a>
<header><div class="t">__TITLE__</div><div class="s">__SUBTITLE__</div></header>
__BODY__
<div class="note">__FOOTNOTE__</div>
__SCRIPT__
</body>
</html>
"""


def render_backtest(sim_records, dt):
    """「◎で買って+5000円で売る」仮想実行の検証レポートページ"""
    closed, open_pos = [], []
    for rec in sim_records:
        for t in rec["trades"]:
            (closed if t["sell_date"] else open_pos).append({**t, "code": rec["code"], "name": rec["name"]})

    n_closed = len(closed)
    total_realized = sum(t["pnl"] for t in closed)
    avg_held = (sum(t["held"] for t in closed) / n_closed) if n_closed else 0
    open_losers = [t for t in open_pos if t["pnl"] < 0]
    open_total = sum(t["pnl"] for t in open_pos)
    worst = sorted(open_pos, key=lambda t: t["pnl"])[:15]
    n_all = n_closed + len(open_pos)
    win_rate = (n_closed / n_all * 100) if n_all else 0

    # 月別の成立回数
    monthly = {}
    for t in closed:
        monthly[t["sell_date"][:7]] = monthly.get(t["sell_date"][:7], 0) + 1

    def yen2(v):
        sign = "+" if v >= 0 else "−"
        return f"{sign}{abs(v):,.0f}円"

    body = ['<div class="card"><h2>この1年、手法をそのまま機械的に続けていたら</h2>']
    body.append(f'<div class="fact"><span>買いに入った回数</span><span class="v num">{n_all:,}回</span></div>')
    body.append(f'<div class="fact"><span>+5,000円で売れた回数</span><span class="v num">{n_closed:,}回（{win_rate:.0f}%）</span></div>')
    body.append(f'<div class="fact"><span>確定した利益の合計</span><span class="v num plus">{yen2(total_realized)}</span></div>')
    body.append(f'<div class="fact"><span>+5,000円までの平均日数</span><span class="v num">約{avg_held:.0f}営業日</span></div>')
    body.append(f'<div class="fact"><span>まだ売れていない持ち越し</span><span class="v num">{len(open_pos):,}件（うち含み損 {len(open_losers):,}件）</span></div>')
    cls = "plus" if open_total >= 0 else "minus"
    body.append(f'<div class="fact"><span>持ち越し分の含み損益 合計</span><span class="v num {cls}">{yen2(open_total)}</span></div>')
    body.append("</div>")

    if monthly:
        body.append('<div class="card"><h2>月別・+5,000円が取れた回数</h2>')
        for m in sorted(monthly):
            body.append(f'<div class="fact"><span class="num">{m.replace("-", "/")}</span><span class="v num">{monthly[m]:,}回</span></div>')
        body.append("</div>")

    if worst:
        body.append('<div class="card"><h2>塩漬け注意リスト（含み損の大きい持ち越し）</h2>')
        for t in worst:
            body.append(
                f'<div class="fact"><span>{html.escape(t["name"])} '
                f'<span class="num">{t["code"]}</span>（{t["buy_date"][5:].replace("-", "/")}買い・{t["held"]}日経過）</span>'
                f'<span class="v num minus">{yen2(t["pnl"])}</span></div>')
        body.append("</div>")

    weekdays = "月火水木金土日"
    subtitle = (f"{dt.month}/{dt.day}（{weekdays[dt.weekday()]}）時点 ・ 過去1年の日足で仮想実行 ・ "
                "ルール: ◎（20日高値から5%安）になったら翌日の始値で100株買い → +5,000円の指値で売る")
    footnote = ("この検証は「いまの対象銘柄」の過去1年をなぞった簡易計算です。手数料・税金は含みません。"
                "買値は翌営業日の始値、売りは+50円/株に到達した日に成立と仮定。同時に何銘柄でも買える前提のため、"
                "実際の資金では全部は買えません。傾向を掴む道具としてお使いください。")
    return (SUBPAGE_TEMPLATE
            .replace("__TITLE__", "手法の検証レポート")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", "\n".join(body))
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", "")
            .replace("__SCRIPT__", ""))


STATUS_DEF = {
    "picked": ("選定", "#fdeeee", "#c62f2f", "今夜の100銘柄に選定"),
    "bench":  ("圏外", "#eef0f4", "#4b4f57", "対象内だが上位100に届かず"),
    "dead":   ("除外", "#efe6f5", "#6b4487", "終わった株ルールで除外"),
    "skip":   ("対象外", "#fdf3e3", "#b06a00", "土俵に上げない条件に該当"),
    "fail":   ("失敗", "#e8e8e8", "#666", "データ取得失敗"),
}


def render_universe(all_results, stats, dt):
    """全銘柄の判定一覧ページ（なぜ対象外かが後から分かる台帳）"""
    counts = {}
    for r in all_results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    chips = ['<div class="chips"><button class="chip on" data-f="all">すべて '
             f'{len(all_results):,}</button>']
    for key, (label, bg, fg, _desc) in STATUS_DEF.items():
        if counts.get(key):
            chips.append(f'<button class="chip" data-f="{key}" style="background:{bg}; color:{fg}">'
                         f'{label} {counts[key]:,}</button>')
    chips.append("</div>")
    chips.append('<input id="q" class="search" type="search" placeholder="銘柄名・コードで検索">')

    rows = []
    for r in sorted(all_results, key=lambda x: x["code"]):
        label, bg, fg, _d = STATUS_DEF[r["status"]]
        close = f'{r["close"]:,.0f}円' if r.get("close") is not None else "−"
        drop = f'−{r["drop_pct"]:.1f}%' if r.get("drop_pct") is not None else ""
        reason = html.escape(r.get("reason") or "")
        reason_html = f'<span class="why">{reason}</span>' if reason else ""
        rows.append(
            f'<div class="urow" data-s="{r["status"]}" data-t="{html.escape(r["name"].lower())} {r["code"]}">'
            f'<span class="st" style="background:{bg}; color:{fg}">{label}</span>'
            f'<span class="un"><b>{html.escape(r["name"])}</b> '
            f'<span class="num uc">{r["code"]}</span> ・ {html.escape(r.get("sector", ""))}{reason_html}</span>'
            f'<span class="up num">{close}<small>{drop}</small></span></div>')

    legend = "".join(
        f'<div class="fact"><span><span class="st" style="background:{bg}; color:{fg}">{label}</span></span>'
        f'<span style="font-size:11.5px; color:var(--ink2)">{desc}</span></div>'
        for label, bg, fg, desc in STATUS_DEF.values())

    extra_css = """
  .chips{display:flex; gap:6px; flex-wrap:wrap; padding:2px 0 8px;}
  .chip{font-size:11.5px; font-weight:700; border:none; border-radius:14px;
    padding:5px 11px; background:#fff; color:var(--ink2); cursor:pointer;}
  .chip.on{outline:2px solid var(--ink);}
  .search{width:100%; font-size:14px; padding:9px 12px; border:1.5px solid #d9d2bf;
    border-radius:10px; background:#fff; margin-bottom:10px;}
  .list{background:var(--paper); border-radius:14px; padding:2px 0;}
  .urow{display:flex; align-items:center; gap:8px; padding:6.5px 12px;
    border-top:1px solid var(--paper-line); font-size:12px;}
  .urow:first-child{border-top:none;}
  .st{flex:none; font-size:9.5px; font-weight:800; border-radius:5px; padding:2px 6px;}
  .un{flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
  .uc{color:var(--ink2);}
  .why{color:#6b4487; font-size:10.5px; margin-left:6px;}
  .up{flex:none; text-align:right; font-weight:700; font-size:12px;}
  .up small{display:block; font-weight:600; color:var(--cheap); font-size:10px;}
  .hidden{display:none;}
"""
    script = """<script>
const rows = Array.from(document.querySelectorAll('.urow'));
let filter = 'all';
function apply(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  for (const r of rows){
    const okF = (filter === 'all' || r.dataset.s === filter);
    const okQ = (!q || r.dataset.t.includes(q));
    r.classList.toggle('hidden', !(okF && okQ));
  }
}
document.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => {
  document.querySelectorAll('.chip').forEach(x => x.classList.remove('on'));
  c.classList.add('on'); filter = c.dataset.f; apply();
}));
document.getElementById('q').addEventListener('input', apply);
</script>"""

    weekdays = "月火水木金土日"
    subtitle = (f"{dt.month}/{dt.day}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d} 判定 ・ "
                f"全{len(all_results):,}銘柄の扱いと理由の台帳")
    body = ('<div class="card"><h2>判定の凡例</h2>' + legend + "</div>"
            + "".join(chips)
            + '<div class="list">' + "\n".join(rows) + "</div>")
    footnote = ("「対象外」は上場間もない・株価100円未満・売買代金が少ない、のいずれか。"
                "「除外」は1年高値から40%以上下落、または長期の下落トレンド継続（ピクセラ型）。"
                "判定は毎回の実行で更新されます。")
    return (SUBPAGE_TEMPLATE
            .replace("__TITLE__", "全銘柄の判定一覧")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", body)
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", script))


# ------------------------------------------------------------
# 施錠（パスワード付き公開）
# GitHubのSecretsに PAGE_PASSWORD を登録すると、ページ全体を
# AES-256-GCMで暗号化して公開する。正しいパスワードを入れた
# ブラウザの中でだけ復号されるので、ソースを見ても解読できない。
# ------------------------------------------------------------
PBKDF2_ITERS = 310000

LOCK_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="apple-touch-icon" href="icon.png">
<link rel="icon" type="image/png" href="icon.png">
<title>Kabuobaa</title>
<style>
  *{box-sizing:border-box; margin:0; padding:0;}
  body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;
    background:#faf6ec; min-height:100vh; display:flex; align-items:center;
    justify-content:center; padding:24px;}
  .card{width:100%; max-width:320px; text-align:center;}
  .mark{font-size:44px; font-weight:800; color:#1c1c1e;}
  .mark span{color:#c62f2f;}
  .msg{font-size:13px; color:#6e6e73; margin:10px 0 22px;}
  input{width:100%; font-size:16px; padding:12px 14px; border:1.5px solid #d9d2bf;
    border-radius:10px; background:#fff; text-align:center;}
  input:focus{outline:none; border-color:#8a7a55;}
  button{width:100%; margin-top:10px; font-size:15px; font-weight:700; color:#fff;
    background:#1c1c1e; border:none; border-radius:10px; padding:12px;}
  label{display:flex; align-items:center; justify-content:center; gap:6px;
    font-size:12px; color:#6e6e73; margin-top:14px;}
  .err{color:#c62f2f; font-size:12px; font-weight:700; margin-top:12px; min-height:16px;}
</style>
</head>
<body>
<div class="card">
  <div class="mark">株<span>◎</span></div>
  <div class="msg">合言葉を入れてノートを開く</div>
  <input id="pw" type="password" placeholder="パスワード" autocomplete="current-password">
  <button onclick="go()">開く</button>
  <label><input id="rem" type="checkbox" checked style="width:auto"> この端末では次回から自動で開く</label>
  <div class="err" id="err"></div>
</div>
<script>
const SALT="__SALT__", IV="__IV__", CT="__CT__", ITERS=__ITERS__;
const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
async function tryOpen(pw){
  const enc = new TextEncoder();
  const mat = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    {name:"PBKDF2", salt:b64(SALT), iterations:ITERS, hash:"SHA-256"},
    mat, {name:"AES-GCM", length:256}, false, ["decrypt"]);
  const plain = await crypto.subtle.decrypt({name:"AES-GCM", iv:b64(IV)}, key, b64(CT));
  document.open(); document.write(new TextDecoder().decode(plain)); document.close();
}
async function go(){
  const pw = document.getElementById("pw").value;
  const err = document.getElementById("err");
  err.textContent = "";
  try{
    if(document.getElementById("rem").checked) localStorage.setItem("kabuobaa_pw", pw);
    await tryOpen(pw);
  }catch(e){
    localStorage.removeItem("kabuobaa_pw");
    err.textContent = "合言葉が違うようです";
  }
}
document.getElementById("pw").addEventListener("keydown", e => { if(e.key === "Enter") go(); });
(async () => {
  const saved = localStorage.getItem("kabuobaa_pw");
  if(saved){ try{ await tryOpen(saved); }catch(e){ localStorage.removeItem("kabuobaa_pw"); } }
})();
</script>
</body>
</html>
"""


def render_locked(inner_html, password):
    """ページ全体をAES-256-GCMで暗号化し、パスワード入力画面に包んで返す"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = os.urandom(16)
    iv = os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=PBKDF2_ITERS)
    key = kdf.derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, inner_html.encode("utf-8"), None)
    return (LOCK_TEMPLATE
            .replace("__SALT__", base64.b64encode(salt).decode())
            .replace("__IV__", base64.b64encode(iv).decode())
            .replace("__CT__", base64.b64encode(ct).decode())
            .replace("__ITERS__", str(PBKDF2_ITERS)))




# ホーム画面用アイコン（PNGをbase64で埋め込み。実行時にdocs/icon.pngとして書き出す）
ICON_B64 = (
    "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"
)


# ------------------------------------------------------------
# main
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true",
                        help="ダミーデータでページ生成のみ確認")
    args = parser.parse_args()

    if args.demo:
        print("デモモード: ダミーデータでページを生成します")
        picked, stats, all_results, sim_records = make_demo_data()
    else:
        picked, stats, all_results, sim_records = run_screening()

    data = build_output(picked, stats)

    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    (DOCS / "icon.png").write_bytes(base64.b64decode(ICON_B64))
    (DOCS / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    html_out = render_html(data)
    password = os.environ.get("PAGE_PASSWORD", "").strip()
    if password:
        html_out = render_locked(html_out, password)
        print("施錠モード: パスワード付きで公開します")
    else:
        print("注意: PAGE_PASSWORD が未設定のため、施錠なしで公開します")
    (DOCS / "index.html").write_text(html_out, encoding="utf-8")

    # サブページ（公開データのみなので施錠の対象外）
    dt_now = datetime.fromisoformat(data["generated_at"])
    (DOCS / "universe.html").write_text(
        render_universe(all_results, stats, dt_now), encoding="utf-8")
    (DOCS / "backtest.html").write_text(
        render_backtest(sim_records, dt_now), encoding="utf-8")

    print(f"完了: {len(data['stocks'])}銘柄を選定 "
          f"(除外 {stats.get('dead_excluded', 0)}銘柄) → docs/index.html"
          f" + universe.html + backtest.html")


if __name__ == "__main__":
    main()
