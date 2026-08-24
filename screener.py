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
    # 売りルール（検証・スコアで使う基準値。持ち株管理画面では個人設定が優先）
    "TP_PCT": 5.0,           # 利確: 買値から+この%で売る

    # 選定
    "TOP_N": 10,             # 画面に出す厳選銘柄数
    "SHORTLIST_N": 60,       # 候補として用意する数（持ち金で外れる分の予備を含む）
    "SAFE_MAX_DEMERIT": 12,  # 三層選定の「安全」判定: 減点合計がこれ以下（かつ致命なし）
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

    # 「下げの質」による除外（落ちるナイフ・荒れ銘柄を拾わないため）
    "KNIFE_DROP_1D": 8.0,        # 直近10日に1日でこの%以上の急落 → 除外（材料落ちの疑い）
    "MAX_VOL20": 4.5,            # 直近20日の日次変動の標準偏差がこの%以上 → 除外（値動きが荒すぎ）
    "MIN_POS_1Y": 0.12,          # 1年の値幅の下から12%未満の位置 → 除外（安値圏を更新中）

    # 通信
    "WORKERS": 6,            # 同時に取得する並列数（上げすぎるとブロックされる）
    "THROTTLE_SEC": 0.1,     # 各作業員が1銘柄ごとに入れる待ち時間（礼儀）
    "RETRIES": 3,
}

# 東証以外（札幌・名古屋・福岡の単独上場銘柄）を対象に加えたい場合はここに追記。
# Yahoo Financeのサフィックス: 札証=.S / 名証=.N / 福証=.F
# 例: {"code": "3544", "name": "サツドラHD", "sector": "小売業", "market": "札証", "suffix": ".S"},
EXTRA_TICKERS = [
    # ---- 札幌証券取引所 単独上場（本則） ----
    {"code": "1449", "name": "FUJIジャパン", "sector": "建設業", "market": "札証", "suffix": ".S"},
    {"code": "1832", "name": "北海電工", "sector": "建設業", "market": "札証", "suffix": ".S"},
    {"code": "2218", "name": "日糧製パン", "sector": "食料品", "market": "札証", "suffix": ".S"},
    {"code": "9027", "name": "ロジネットジャパン", "sector": "陸運業", "market": "札証", "suffix": ".S"},
    {"code": "9085", "name": "北海道中央バス", "sector": "陸運業", "market": "札証", "suffix": ".S"},
    {"code": "5579", "name": "GSI", "sector": "卸売業", "market": "札証", "suffix": ".S"},
    {"code": "3055", "name": "ほくやく・竹山HD", "sector": "卸売業", "market": "札証", "suffix": ".S"},
    {"code": "8594", "name": "中道リース", "sector": "その他金融業", "market": "札証", "suffix": ".S"},
    {"code": "2172", "name": "インサイト", "sector": "サービス業", "market": "札証", "suffix": ".S"},
    # ---- 札幌証券取引所 単独上場（アンビシャス） ----
    {"code": "3849", "name": "日本テクノ・ラボ", "sector": "情報・通信業", "market": "札証", "suffix": ".S"},
    {"code": "3977", "name": "フュージョン", "sector": "サービス業", "market": "札証", "suffix": ".S"},
    {"code": "5039", "name": "キットアライブ", "sector": "情報・通信業", "market": "札証", "suffix": ".S"},
    {"code": "2928", "name": "RIZAPグループ", "sector": "サービス業", "market": "札証", "suffix": ".S"},
    {"code": "2976", "name": "日本グランデ", "sector": "不動産業", "market": "札証", "suffix": ".S"},
    {"code": "2137", "name": "光ハイツ・ヴェラス", "sector": "サービス業", "market": "札証", "suffix": ".S"},
    # ---- 福岡証券取引所 単独上場（本則） ----
    {"code": "1771", "name": "日本乾溜工業", "sector": "建設業", "market": "福証", "suffix": ".F"},
    {"code": "1999", "name": "サイタHD", "sector": "建設業", "market": "福証", "suffix": ".F"},
    {"code": "2058", "name": "ヒガシマル", "sector": "食料品", "market": "福証", "suffix": ".F"},
    {"code": "2919", "name": "マルタイ", "sector": "食料品", "market": "福証", "suffix": ".F"},
    {"code": "4995", "name": "サンケイ化学", "sector": "化学", "market": "福証", "suffix": ".F"},
    {"code": "7894", "name": "丸東産業", "sector": "化学", "market": "福証", "suffix": ".F"},
    {"code": "5953", "name": "昭和鉄工", "sector": "金属製品", "market": "福証", "suffix": ".F"},
    {"code": "9035", "name": "第一交通産業", "sector": "陸運業", "market": "福証", "suffix": ".F"},
    {"code": "9407", "name": "RKB毎日HD", "sector": "情報・通信業", "market": "福証", "suffix": ".F"},
    {"code": "272A", "name": "グリーンクロスHD", "sector": "卸売業", "market": "福証", "suffix": ".F"},
    {"code": "7441", "name": "Misumi", "sector": "卸売業", "market": "福証", "suffix": ".F"},
    {"code": "9942", "name": "ジョイフル", "sector": "小売業", "market": "福証", "suffix": ".F"},
    {"code": "8398", "name": "筑邦銀行", "sector": "銀行業", "market": "福証", "suffix": ".F"},
    {"code": "8554", "name": "南日本銀行", "sector": "銀行業", "market": "福証", "suffix": ".F"},
    {"code": "8559", "name": "豊和銀行", "sector": "銀行業", "market": "福証", "suffix": ".F"},
    {"code": "8560", "name": "宮崎太陽銀行", "sector": "銀行業", "market": "福証", "suffix": ".F"},
    {"code": "2974", "name": "大英産業", "sector": "不動産業", "market": "福証", "suffix": ".F"},
    {"code": "6076", "name": "アメイズ", "sector": "サービス業", "market": "福証", "suffix": ".F"},
    # ---- 福岡証券取引所 単独上場（Q-Board） ----
    {"code": "231A", "name": "クロスイー", "sector": "不動産業", "market": "福証", "suffix": ".F"},
    {"code": "4250", "name": "オールフロンティア", "sector": "化学", "market": "福証", "suffix": ".F"},
    {"code": "3824", "name": "メディアファイブ", "sector": "情報・通信業", "market": "福証", "suffix": ".F"},
    {"code": "4018", "name": "ジオロケーション・テクノロジー", "sector": "情報・通信業", "market": "福証", "suffix": ".F"},
    {"code": "3047", "name": "TRUCK-ONE", "sector": "卸売業", "market": "福証", "suffix": ".F"},
    {"code": "4827", "name": "ビジネス・ワンHD", "sector": "不動産業", "market": "福証", "suffix": ".F"},
    {"code": "242A", "name": "リプライオリティ", "sector": "サービス業", "market": "福証", "suffix": ".F"},
    {"code": "9388", "name": "パパネッツ", "sector": "サービス業", "market": "福証", "suffix": ".F"},
]

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
def _fetch_chart(session, code, suffix=".T", range_="10y", interval="1d", retries=None):
    """Yahoo chart APIから [{date, open, high, low, close, volume}] (古い順) を返す。"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}"
    params = {"range": range_, "interval": interval}
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    last_err = None
    for attempt in range(retries if retries is not None else CONFIG["RETRIES"]):
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
            days_out = [dedup[k] for k in sorted(dedup)]
            # chart APIのmetaに含まれるファンダ系の値を拾う（銘柄により欠ける項目あり）
            fund = {}
            for k_src, k_dst in (("trailingPE", "per"), ("priceToBook", "pbr"),
                                 ("marketCap", "mcap"), ("sharesOutstanding", "shares"),
                                 ("trailingAnnualDividendYield", "dy"),
                                 ("epsTrailingTwelveMonths", "eps"),
                                 ("bookValue", "bvps")):
                v = meta.get(k_src)
                if isinstance(v, (int, float)):
                    fund[k_dst] = v
            if fund:
                _FUND_CACHE[code] = fund
            return days_out
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1 + attempt)
    print(f"  ! {code}: 取得失敗 ({last_err})", file=sys.stderr)
    return None


def _median_gap_days(days, tail=60):
    """直近tail本の日付間隔の中央値（日）。日足なら1〜3、週足なら7、月足なら28-31。"""
    seg = days[-tail:] if len(days) > tail else days
    if len(seg) < 8:
        return 1
    gaps = []
    prev = None
    for d in seg:
        cur = datetime.strptime(d["date"], "%Y-%m-%d").date()
        if prev is not None:
            gaps.append((cur - prev).days)
        prev = cur
    gaps.sort()
    return gaps[len(gaps) // 2]


def fetch_daily(session, code, suffix=".T", range_="10y"):
    """日足を [{date, open, high, low, close, volume}] (古い順) で返す。

    重要: Yahooは range=max & interval=1d を指定しても月足を返すことがある
    （2026-08に本番で発生したデグレの原因）。そのため:
      1. 主データは range=10y で取得（日足が保証される）
      2. 念のため粒度を検証し、日足でなければ10yで取り直す
      3. 全期間チャート用の月足は fetch_all_history() で別途明示的に取得する
    """
    days = _fetch_chart(session, code, suffix, range_, "1d")
    if not days:
        return days
    # 粒度ガード: 「日足のはず」が週足/月足で返ってきたら取り直す
    if _median_gap_days(days) > 4:
        print(f"  ! {code}: 日足要求に対し粗い足が返却 (range={range_}) → 10yで再取得",
              file=sys.stderr)
        redo = _fetch_chart(session, code, suffix, "10y", "1d")
        if redo and _median_gap_days(redo) <= 4:
            days = redo
        elif redo is None or _median_gap_days(days) > 4:
            # 日足が得られない場合はこの銘柄を不成立扱い（月足で判定すると誤判定するため）
            return None
    return days


def fetch_all_history(session, code, suffix=".T"):
    """全期間チャート用に月足の終値系列 [[date, close], ...] を返す（失敗時None）。
    月足はあくまで長期の形を見るための素材で、判定・シミュレーションには使わない。"""
    mo = _fetch_chart(session, code, suffix, "max", "1mo", retries=1)
    if not mo or len(mo) < 12:
        return None
    return [[d["date"], round(d["close"], 1)] for d in mo]


_ALLHIST_CACHE = {}  # code -> [[date, close], ...] 月足の全期間終値（全期間チャート用）

_FUND_CACHE = {}  # code -> metaから拾ったファンダ素材（_fetch_chart内で埋まる）


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

    # 下げの質: 日次リターン・ギャップ・変動の荒さ・1年レンジ内の位置・下げ止まり
    import statistics
    rets10, rets20 = [], []
    for i in range(max(1, len(days) - 20), len(days)):
        pc = days[i - 1]["close"]
        if pc > 0:
            r = (days[i]["close"] - pc) / pc * 100
            rets20.append(r)
            if i >= len(days) - 10:
                rets10.append(r)
    worst_1d = min(rets10) if rets10 else 0.0
    vol20 = statistics.pstdev(rets20) if len(rets20) >= 5 else 0.0
    pos1y = ((latest["close"] - low1y) / (high1y - low1y)
             if high1y > low1y else 0.5)
    # 下げの集中度: 20日の下げ幅のうち最悪の1日が占める割合（1に近いほど崩落型）
    worst20 = abs(min(rets20)) if rets20 else 0.0
    concentration = min(worst20 / drop_pct, 1.0) if drop_pct > 0.5 else 0.0
    # 下げ止まりの兆し: 直近日が前日終値以上、または安値を切り上げ
    stabilizing = (len(days) >= 3 and
                   (days[-1]["close"] >= days[-2]["close"] or
                    days[-1]["low"] >= days[-2]["low"]))
    ma200_above = None
    if len(closes) >= 200:
        ma200_above = latest["close"] > sum(closes[-200:]) / 200

    # コメント用: 出来高倍率・◎水準の連続日数・前日比
    vols = [d.get("volume") or 0 for d in days[-21:-1]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    vol_ratio = (latest.get("volume") or 0) / avg_vol if avg_vol > 0 else None
    cheap_streak = 0
    for d in reversed(days[-n:]):
        if high20 > 0 and (high20 - d["close"]) / high20 * 100 >= CONFIG["CHEAP_PCT"]:
            cheap_streak += 1
        else:
            break
    prev_change = rets20[-1] if rets20 else None

    # MACD(12,26,9): 直近の買い転換を検出
    macd_state = None
    if len(closes) >= 40:
        def _ema(vals, span):
            k = 2 / (span + 1)
            e = vals[0]
            out = [e]
            for v in vals[1:]:
                e = v * k + e * (1 - k)
                out.append(e)
            return out
        e12 = _ema(closes, 12)
        e26 = _ema(closes, 26)
        macd_line = [a - b for a, b in zip(e12, e26)]
        sig = _ema(macd_line, 9)
        hist = [m - s2 for m, s2 in zip(macd_line, sig)]
        if hist[-1] > 0:
            crossed = any(hist[-1 - i] <= 0 for i in range(1, 6))
            macd_state = "golden_recent" if crossed else "above"
        else:
            macd_state = "below"

    # ボリンジャーバンド(20日): 現在値が何σの位置か
    boll_sigma = None
    if len(closes) >= 20:
        seg = closes[-20:]
        mu = sum(seg) / 20
        sd = (sum((x - mu) ** 2 for x in seg) / 20) ** 0.5
        if sd > 0:
            boll_sigma = (closes[-1] - mu) / sd

    # 25日移動平均乖離率（逆張りの定番）
    dev25 = None
    if len(closes) >= 25:
        ma25 = sum(closes[-25:]) / 25
        if ma25 > 0:
            dev25 = (closes[-1] / ma25 - 1) * 100

    # 夜間ギャップ: 前日終値→翌朝始値の乖離の平均（夜の指値が守りやすいか）
    gaps = []
    for i in range(max(1, len(days) - 20), len(days)):
        pc = days[i - 1]["close"]
        if pc > 0:
            gaps.append(abs(days[i]["open"] - pc) / pc * 100)
    gap_avg = sum(gaps) / len(gaps) if gaps else None

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
        "worst_1d": worst_1d, "vol20": vol20, "pos1y": pos1y,
        "concentration": concentration, "stabilizing": stabilizing,
        "ma200_above": ma200_above,
        "vol_ratio": vol_ratio, "cheap_streak": cheap_streak,
        "prev_change": prev_change, "gap_avg": gap_avg,
        "macd_state": macd_state, "boll_sigma": boll_sigma, "dev25": dev25,
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
    if metrics["worst_1d"] <= -c["KNIFE_DROP_1D"]:
        return "dead", f"直近10日に1日で{abs(metrics['worst_1d']):.0f}%の急落あり（材料落ちの疑い）"
    if metrics["vol20"] >= c["MAX_VOL20"]:
        return "dead", f"日々の値動きが±{metrics['vol20']:.1f}%と荒すぎる"
    if metrics["pos1y"] <= c["MIN_POS_1Y"]:
        return "dead", "1年の安値圏を更新中（下げ止まり前）"
    if not metrics["stabilizing"]:
        return "dead", "下げ止まり未確認（前日から安値を切り下げ中）"
    return "ok", ""


def level_of(drop_pct):
    if drop_pct >= CONFIG["CHEAP_PCT"]:
        return "cheap"   # ◎ かなり安い
    if drop_pct >= CONFIG["MILD_PCT"]:
        return "mild"    # ○ 安め
    return "normal"




def sim_summary(trades):
    """銘柄単体の仮想実行結果を要約（根拠スコア用）"""
    closed = [t for t in trades if t["sell_date"]]
    opened = [t for t in trades if not t["sell_date"]]
    return {
        "wins": len(closed),
        "avg_held": (sum(t["held"] for t in closed) / len(closed)) if closed else None,
        "open_loss": any(t["pnl"] < 0 for t in opened),
    }


def score_stock(s):
    """厳選の根拠スコア。(点数, 根拠の文リスト) を返す。
    内部で「質（quality）」と「タイミング（timing）」に振り分け、s["q_score"], s["t_score"] にも保存する"""
    reasons = []
    score = 0.0
    _q = [0.0]
    _t = [0.0]
    _tag = {"cur": "q"}

    def _add(pt):
        (_t if _tag["cur"] == "t" else _q)[0] += pt

    _tag["cur"] = "t"
    # 1. いま安いか（主基準だが、深すぎる下げは加点を頭打ちに）
    capped = min(s["drop_pct"], 8.0)
    pt = capped * 8
    score += pt
    _add((pt))
    reasons.append(f"直近20日高値から −{s['drop_pct']:.1f}%（安さの主基準 +{pt:.0f}点・8%で頭打ち）")
    if s.get("usual"):
        vs = (s["usual"] - s["close"]) / s["usual"] * 100
        if vs > 0:
            pt = min(vs * 4, 20)
            score += pt
            _add((pt))
            reasons.append(f"普段の値段（20日平均）より {vs:.1f}%安い（+{pt:.0f}点）")

    _tag["cur"] = "t"
    # 2. 下げの「質」: じわ下げ か 崩落か
    conc = s.get("concentration", 0)
    if conc < 0.45:
        score += 15
        _add((15))
        reasons.append("下げが複数日に分散した「じわ下げ」で、1日の急落で作られた安さではない（+15点）")
    elif conc < 0.7:
        score += 5
        _add((5))
        reasons.append("下げはやや急だが崩落型ではない（+5点）")
    else:
        reasons.append("下げの大半が特定の1日に集中（急落型・加点なし）")
    vol = s.get("vol20", 0)
    if 0 < vol <= 2.5:
        score += 10
        _add((10))
        reasons.append(f"日々の値動きが±{vol:.1f}%と穏やかで読みやすい（+10点）")

    _tag["cur"] = "q"
    # 3. トレンドの地合い: 上昇トレンド中の押し目が理想形
    if s.get("ma200_above"):
        score += 20
        _add((20))
        reasons.append("200日平均線より上での下げ＝上昇トレンド中の一時的な押し目（+20点）")
    dd = s["drawdown_1y"] * 100
    if dd <= 15:
        score += 12
        _add((12))
        reasons.append(f"1年高値からの下落 −{dd:.0f}% で長期トレンド健全（+12点）")
    elif dd <= 25:
        score += 6
        _add((6))
        reasons.append(f"1年高値からの下落 −{dd:.0f}%（+6点）")
    pos = s.get("pos1y")
    if pos is not None and 0.25 <= pos <= 0.65:
        score += 10
        _add((10))
        reasons.append(f"1年の値幅の中腹（下から{pos*100:.0f}%）での下げ。底抜けでも高値掴みでもない位置（+10点）")

    _tag["cur"] = "q"
    # 4. この銘柄で手法が効いてきた実績（安く買って+TP_PCT%で売る、の再現性）
    t = s.get("sim") or {}
    wins = t.get("wins", 0)
    tp = CONFIG["TP_PCT"]
    if wins > 0:
        base_pt = min(wins, 10) * 5
        score += base_pt
        _add((base_pt))
        reasons.append(f"過去1年、同じ買い方で {wins}回 利確ライン（+{tp:.0f}%）に到達（+{base_pt:.0f}点）")
        ah = t.get("avg_held")
        if ah is not None:
            if ah <= 7:
                score += 15
                _add((15))
                reasons.append(f"利確まで平均 {ah:.0f}営業日。短期回転スタイルに最適（+15点）")
            elif ah <= 15:
                score += 8
                _add((8))
                reasons.append(f"利確まで平均 {ah:.0f}営業日（+8点）")
            elif ah > 25:
                score -= 5
                _add(-(5))
                reasons.append(f"利確まで平均 {ah:.0f}営業日と資金拘束が長め。"
                               f"短期回転スタイルでは機会損失（−5点）")
    else:
        reasons.append("過去1年はこの買い方の成立実績なし（加点なし）")
    if t.get("open_loss"):
        score -= 15
        _add(-(15))
        reasons.append("ただし直近の仮想買いが塩漬け中（−15点）")

    _tag["cur"] = "q"
    # 5. 長期テクニカル（10年データからの定石）
    lg = s.get("long") or {}
    z = lg.get("zone")
    if z:
        if z["touches"] <= 2 and z["dist_pct"] >= -1:
            pt = 12
            score += pt
            _add((pt))
            reasons.append(f"長期支持帯 {z['zone_low']:,.0f}〜{z['zone_top']:,.0f}円の直上。"
                           f"過去{z['touches']}回反発し、今回が{z['touches'] + 1}回目の試し"
                           f"（〜3回目までは支持されやすいという定石の圏内 +{pt}点）")
        else:
            score -= 5
            _add(-(5))
            reasons.append(f"長期支持帯 {z['zone_top']:,.0f}円付近は過去{z['touches']}回試されており、"
                           f"今回で{z['touches'] + 1}回目。支持線は試されるほど割れやすい（−5点・警戒）")
    if lg.get("gc") is True:
        score += 10
        _add((10))
        reasons.append("50日線が200日線の上（ゴールデンクロス継続中の長期上昇形 +10点）")
    elif lg.get("gc") is False:
        reasons.append("50日線が200日線の下（長期は調整形・加点なし）")
    _tag["cur"] = "t"
    rsi = lg.get("rsi")
    if rsi is not None:
        if rsi <= 30:
            score += 10
            _add((10))
            reasons.append(f"RSI(14)={rsi:.0f} の売られすぎ水準（+10点）")
        elif rsi <= 40:
            score += 5
            _add((5))
            reasons.append(f"RSI(14)={rsi:.0f} でやや売られすぎ（+5点）")
    _tag["cur"] = "q"
    if lg.get("w_bottom"):
        score += 8
        _add((8))
        reasons.append(f"W底（ダブルボトム）を形成しネックライン{lg['w_bottom']['neck']:,.0f}円を上抜け（+8点）")
    _tag["cur"] = "t"
    if lg.get("climax"):
        score += 6
        _add((6))
        reasons.append("直近に出来高急増+長い下ヒゲ（セリングクライマックス=投げ売り一巡の兆候 +6点）")

    _tag["cur"] = "t"
    # 5.3 補助テクニカル指標（MACD・ボリンジャー・移動平均乖離）
    ms = s.get("macd_state")
    if ms == "golden_recent":
        score += 8
        _add((8))
        reasons.append("MACDが直近5日以内に買い転換（下げの勢いが尽きた定番シグナル +8点）")
    elif ms == "above":
        score += 4
        _add((4))
        reasons.append("MACDがシグナル線の上で上向き（+4点）")
    bs = s.get("boll_sigma")
    if bs is not None:
        if bs <= -2:
            score += 8
            _add((8))
            reasons.append(f"ボリンジャーバンド−2σ以下（統計的な売られすぎ圏 +8点）")
        elif bs <= -1.5:
            score += 4
            _add((4))
            reasons.append(f"ボリンジャーバンド−{abs(bs):.1f}σと下限付近（+4点）")
    dv = s.get("dev25")
    if dv is not None:
        if -20 < dv <= -8:
            score += 6
            _add((6))
            reasons.append(f"25日移動平均線から{dv:.1f}%下方乖離（逆張りの定番圏 +6点）")
        elif dv <= -20:
            score -= 5
            _add(-(5))
            reasons.append(f"25日線から{dv:.1f}%と乖離しすぎ（異常事態の可能性 −5点）")

    # 5.35 準主要テクニカル（重みは主要より小さく）
    _tag["cur"] = "t"
    st_ = lg.get("stoch")
    if st_ is not None and st_ <= 20:
        score += 4
        _add((4))
        reasons.append(f"スローストキャス {st_:.0f} の売られすぎ圏（+4点）")
    adx = lg.get("adx")
    if adx is not None and lg.get("di_plus_over") is not None:
        if adx >= 25 and lg["di_plus_over"]:
            score += 4
            _add((4))
            reasons.append(f"ADX {adx:.0f}・+DI優勢＝上昇トレンドに勢いがある中の押し目（+4点）")
        elif adx >= 25 and not lg["di_plus_over"]:
            score -= 4
            _add(-(4))
            reasons.append(f"ADX {adx:.0f}・−DI優勢＝下降トレンドに勢い（−4点）")
    ich = lg.get("ichimoku")
    if ich == "above":
        score += 4
        _add((4))
        reasons.append("一目均衡表の雲の上（中期の地合いは良好 +4点）")
    elif ich == "below":
        score -= 3
        _add(-(3))
        reasons.append("一目均衡表の雲の下（中期は弱い −3点）")
    obv_t = lg.get("obv_trend")
    if obv_t is not None and obv_t > 5:
        score += 3
        _add((3))
        reasons.append("OBVが上向き＝下げの中でも買い集めの気配（+3点）")
    mfi = lg.get("mfi")
    if mfi is not None and mfi <= 20:
        score += 3
        _add((3))
        reasons.append(f"MFI {mfi:.0f}（出来高込みの売られすぎ +3点）")
    atrp = lg.get("atr_pct")
    if atrp is not None:
        if atrp <= 2.0:
            score += 3
            _add((3))
            reasons.append(f"ATR {atrp:.1f}%と日々の値幅が小さく、損切り幅を狭く置ける（+3点）")
        elif atrp >= 5.0:
            score -= 3
            _add(-(3))
            reasons.append(f"ATR {atrp:.1f}%と値幅が大きく、損切りが機能しにくい（−3点）")

    _tag["cur"] = "q"
    # 5.4 ファンダメンタルの健全性（システムが固定基準で自動判定）
    fu = s.get("fund")
    if fu:
        per = fu.get("per")
        pbr = fu.get("pbr")
        roe = fu.get("roe")
        dy = fu.get("div_yield")
        mcap = fu.get("mcap_oku")
        if per is not None and per < 0:
            score -= 20
            _add(-(20))
            reasons.append("赤字（PERマイナス）。業績不振の銘柄は下げても戻りが鈍い（−20点）")
        elif per is not None and per <= 20:
            score += 8
            _add((8))
            reasons.append(f"PER {per:.1f}倍と利益に対して妥当〜割安の水準（+8点）")
        elif per is not None and per > 60:
            score -= 10
            _add(-(10))
            reasons.append(f"PER {per:.1f}倍と利益に対して異常な割高。期待剥落時の下げが深い（−10点）")
        if pbr is not None:
            if 0.5 <= pbr <= 1.5:
                score += 6
                _add((6))
                reasons.append(f"PBR {pbr:.2f}倍と資産に対して割安圏。下値が固くなりやすい（+6点）")
            elif pbr > 8:
                score -= 8
                _add(-(8))
                reasons.append(f"PBR {pbr:.2f}倍と資産比で過熱気味（−8点）")
        if roe is not None and per is not None and per > 0:
            if roe >= 10:
                score += 8
                _add((8))
                reasons.append(f"ROE {roe:.1f}%と資本効率が高く、稼ぐ力のある会社（+8点）")
            elif roe < 3:
                score -= 5
                _add(-(5))
                reasons.append(f"ROE {roe:.1f}%と収益力が弱い（−5点）")
        if dy is not None and dy >= 3:
            score += 5
            _add((5))
            reasons.append(f"配当利回り{dy:.1f}%。配当が下値を支えやすい（+5点）")
        if mcap:
            if mcap < 50:
                score -= 5
                _add(-(5))
                reasons.append(f"時価総額{mcap:,}億円と小型で、値が飛びやすい（−5点）")
            elif mcap >= 1000:
                score += 3
                _add((3))
                reasons.append(f"時価総額{mcap:,}億円の中大型で値動きが安定しやすい（+3点）")
        er = fu.get("equity_ratio")
        if er is not None:
            if er >= 50:
                score += 4
                _add((4))
                reasons.append(f"自己資本比率{er:.0f}%と財務が厚く倒産リスクが低い（+4点）")
            elif er < 20:
                score -= 5
                _add(-(5))
                reasons.append(f"自己資本比率{er:.0f}%と借入依存が高い（−5点）")
        om = fu.get("op_margin")
        if om is not None:
            if om >= 10:
                score += 3
                _add((3))
                reasons.append(f"営業利益率{om:.1f}%と本業の稼ぐ力が高い（+3点）")
            elif om < 0:
                score -= 5
                _add(-(5))
                reasons.append(f"営業赤字（営業利益率{om:.1f}%）（−5点）")
        roa = fu.get("roa")
        if roa is not None and roa >= 5:
            score += 3
            _add((3))
            reasons.append(f"ROA {roa:.1f}%と資産効率が良い（+3点）")
        po = fu.get("payout")
        if po is not None and po > 100:
            score -= 3
            _add(-(3))
            reasons.append(f"配当性向{po:.0f}%と利益以上に配当しており減配リスク（−3点）")
        peg = fu.get("peg")
        if peg is not None and 0 < peg <= 1.0:
            score += 4
            _add((4))
            reasons.append(f"PEG {peg:.2f}倍＝成長率に対して株価が割安（+4点）")
    else:
        reasons.append("ファンダ指標が本日取得できず中立扱い（加減点なし）")

    _tag["cur"] = "t"
    # 5.5 夜1回の判断スタイルとの相性（翌朝の窓開けの小ささ）
    ga = s.get("gap_avg")
    if ga is not None:
        if ga <= 1.0:
            score += 8
            _add((8))
            reasons.append(f"夜間ギャップ（翌朝の窓開け）平均±{ga:.1f}%と小さく、"
                           f"夜に決めた指値が翌朝も有効に働きやすい（+8点）")
        elif ga >= 2.5:
            score -= 5
            _add(-(5))
            reasons.append(f"夜間ギャップ平均±{ga:.1f}%と大きく、夜の判断が翌朝ズレやすい（−5点）")

    _tag["cur"] = "q"
    # 6. 売り買いのしやすさ
    tv = s.get("turnover", 0)
    if tv >= 1_000_000_000:
        score += 12
        _add((12))
        reasons.append(f"1日平均 {tv/100_000_000:.0f}億円の売買があり注文が通りやすい（+12点）")
    elif tv >= 100_000_000:
        score += 6
        _add((6))
        reasons.append(f"1日平均 {tv/100_000_000:.1f}億円の売買（+6点）")

    ec = s.get("exec_change") or []
    if ec:
        reasons.insert(0, f"⚠ 直近に代表取締役の異動を開示（{ec[0]['date'][5:].replace('-', '/')}）。"
                          f"経営トップ交代は株価が大きく動く最重要イベント。開示原文を必ず確認（採点には含めず注意喚起のみ）")
    s["q_score"] = round(_q[0], 1)
    s["t_score"] = round(_t[0], 1)
    return score, reasons


# ------------------------------------------------------------
# 長期テクニカル分析（10年データから計算する定石の技法）
#  - 長期支持帯とタッチ回数（支持線は試されるほど割れやすい=4回目警戒）
#  - ゴールデンクロス状態（50日/200日移動平均）
#  - RSI(14) の売られすぎ
#  - W底（ダブルボトム）形成
#  - セリングクライマックス兆候（出来高急増+長い下ヒゲ）
# ------------------------------------------------------------
def compute_long_metrics(days_full, code=None):
    out = {}
    if not days_full or len(days_full) < 120:
        return out
    closes = [d["close"] for d in days_full]
    lows = [d["low"] for d in days_full]
    highs = [d["high"] for d in days_full]
    opens = [d["open"] for d in days_full]
    vols = [d.get("volume") or 0 for d in days_full]
    dates = [d["date"] for d in days_full]
    cur = closes[-1]

    # RSI(14)
    if len(closes) >= 15:
        gains = losses = 0.0
        for i in range(len(closes) - 14, len(closes)):
            ch = closes[i] - closes[i - 1]
            if ch >= 0:
                gains += ch
            else:
                losses -= ch
        rs = gains / losses if losses > 0 else 99.0
        out["rsi"] = round(100 - 100 / (1 + rs), 1)

    # 50日/200日移動平均のクロス状態
    if len(closes) >= 200:
        sma50 = sum(closes[-50:]) / 50
        sma200 = sum(closes[-200:]) / 200
        out["gc"] = sma50 > sma200

    # 長期支持帯（直近3年の谷をクラスタリング）
    n3 = min(len(days_full), 245 * 3)
    lows3 = lows[-n3:]
    dates3 = dates[-n3:]
    w = 10
    minima = [i for i in range(w, n3 - w) if lows3[i] == min(lows3[i - w:i + w + 1])]
    clusters = []
    for i in sorted(minima, key=lambda j: lows3[j]):
        placed = False
        for cl in clusters:
            center = sum(lows3[j] for j in cl) / len(cl)
            if center > 0 and abs(lows3[i] - center) / center * 100 <= 4.0:
                cl.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
    best = None
    for cl in clusters:
        cl.sort()
        distinct = [cl[0]]
        for i in cl[1:]:
            if i - distinct[-1] > w:
                distinct.append(i)
        if len(distinct) < 2:
            continue
        z_low = min(lows3[i] for i in distinct)
        z_top = max(lows3[i] for i in distinct)
        if z_top <= 0 or cur < z_low * 0.97 or cur > z_top * 1.15:
            continue  # 現在値と関係の薄い帯・すでに割れた帯は対象外
        if best is None or len(distinct) > best["touches"]:
            best = {"zone_low": round(z_low, 1), "zone_top": round(z_top, 1),
                    "touches": len(distinct),
                    "touch_dates": [dates3[i] for i in distinct],
                    "dist_pct": round((cur - z_top) / z_top * 100, 1)}
    if best:
        out["zone"] = best

    # W底（直近120日: 2つの谷が±4%以内・間の戻り高値=ネックラインを上抜け）
    n4 = min(len(days_full), 120)
    lowsW = lows[-n4:]
    mm = [i for i in range(5, n4 - 5) if lowsW[i] == min(lowsW[i - 5:i + 6])]
    if len(mm) >= 2:
        a, b = mm[-2], mm[-1]
        if b - a >= 15 and lowsW[a] > 0 and abs(lowsW[b] - lowsW[a]) / lowsW[a] * 100 <= 4.0:
            neckline = max(highs[-n4:][a:b + 1])
            if cur > neckline:
                out["w_bottom"] = {"neck": round(neckline, 1)}

    # セリングクライマックス兆候（直近5日）
    if len(vols) >= 25:
        avg_vol = sum(vols[-25:-5]) / 20
        for i in range(len(days_full) - 5, len(days_full)):
            body = abs(closes[i] - opens[i])
            tail = min(closes[i], opens[i]) - lows[i]
            if avg_vol > 0 and vols[i] >= avg_vol * 2 and tail >= body * 2 and tail > 0:
                out["climax"] = {"date": dates[i]}
                break

    # ---- 準主要テクニカル（10年データから計算・追加通信なし） ----
    n_ = len(closes)
    if n_ >= 30:
        # ATR(14) と ATR%（値幅の大きさ。損切り幅の目安）
        trs = []
        for i in range(1, n_):
            trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        atr = sum(trs[-14:]) / 14
        out["atr_pct"] = round(atr / cur * 100, 2) if cur > 0 else None
        # スローストキャスティクス %D(3)
        ks = []
        for i in range(n_ - 16, n_):
            hh = max(highs[i - 13:i + 1]); ll = min(lows[i - 13:i + 1])
            ks.append((closes[i] - ll) / (hh - ll) * 100 if hh > ll else 50.0)
        d_fast = [sum(ks[i - 2:i + 1]) / 3 for i in range(2, len(ks))]
        out["stoch"] = round(sum(d_fast[-3:]) / 3, 1) if len(d_fast) >= 3 else None
        # DMI / ADX(14)
        pdm = []; ndm = []
        for i in range(1, n_):
            up = highs[i] - highs[i - 1]; dn = lows[i - 1] - lows[i]
            pdm.append(up if (up > dn and up > 0) else 0.0)
            ndm.append(dn if (dn > up and dn > 0) else 0.0)
        def _wilder(vals, p=14):
            s = sum(vals[:p]); res = [s]
            for v in vals[p:]:
                s = s - s / p + v; res.append(s)
            return res
        tr_w = _wilder(trs); p_w = _wilder(pdm); n_w = _wilder(ndm)
        pdi = [100 * p / t if t > 0 else 0 for p, t in zip(p_w, tr_w)]
        ndi = [100 * n / t if t > 0 else 0 for n, t in zip(n_w, tr_w)]
        dx = [100 * abs(a - b) / (a + b) if (a + b) > 0 else 0 for a, b in zip(pdi, ndi)]
        if len(dx) >= 14:
            adx = sum(dx[-14:]) / 14
            out["adx"] = round(adx, 1)
            out["di_plus_over"] = pdi[-1] > ndi[-1]
        # 一目均衡表: 雲との位置関係（転換9・基準26・先行スパン52）
        if n_ >= 78:
            def _mid(a, b, i):
                return (max(highs[i - a + 1:i + 1]) + min(lows[i - a + 1:i + 1])) / 2
            i = n_ - 1 - 26  # 26日前に描かれた雲が今日の位置
            span_a = (_mid(9, 9, i) + _mid(26, 26, i)) / 2
            span_b = _mid(52, 52, i)
            top, bot = max(span_a, span_b), min(span_a, span_b)
            out["ichimoku"] = "above" if cur > top else ("below" if cur < bot else "in")
        # OBV の傾き（20日）と MFI(14)
        obv = 0.0; obvs = []
        for i in range(1, n_):
            obv += vols[i] if closes[i] > closes[i - 1] else (-vols[i] if closes[i] < closes[i - 1] else 0)
            obvs.append(obv)
        if len(obvs) >= 21:
            base = abs(obvs[-21]) or 1
            out["obv_trend"] = round((obvs[-1] - obvs[-21]) / base * 100, 1)
        tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n_)]
        pos_mf = neg_mf = 0.0
        for i in range(n_ - 14, n_):
            mf = tp[i] * vols[i]
            if tp[i] > tp[i - 1]: pos_mf += mf
            elif tp[i] < tp[i - 1]: neg_mf += mf
        out["mfi"] = round(100 - 100 / (1 + pos_mf / neg_mf), 1) if neg_mf > 0 else 100.0
        # ヒストリカル・ボラティリティ（20日・年率）
        import math as _m
        lr = [_m.log(closes[i] / closes[i - 1]) for i in range(n_ - 20, n_) if closes[i - 1] > 0]
        if len(lr) >= 10:
            mu = sum(lr) / len(lr)
            sd = (sum((x - mu) ** 2 for x in lr) / len(lr)) ** 0.5
            out["hv20"] = round(sd * (250 ** 0.5) * 100, 1)

    # スパークライン: 1年 / 3年 / 10年 / 全期間（各約50〜160点に間引き）
    def _spark_slice(n_days, target=150):
        seg_c = closes[-n_days:] if n_days else closes
        seg_d = dates[-n_days:] if n_days else dates
        step = max(1, len(seg_c) // target)
        sp = [[seg_d[i], round(seg_c[i], 1)] for i in range(0, len(seg_c), step)]
        if sp and sp[-1][0] != seg_d[-1]:
            sp.append([seg_d[-1], round(seg_c[-1], 1)])
        return sp
    out["spark1"] = _spark_slice(min(len(closes), 245), 60)
    closes3 = closes[-n3:]
    out["spark"] = [[dates3[i], round(closes3[i], 1)] for i in range(0, n3, 5)]
    out["spark10"] = _spark_slice(min(len(closes), 2450), 150)
    # 全期間: 別途取得した月足（_ALLHIST_CACHE）を使う。
    # 日足の10年分より約1年以上長い履歴があるときだけ「全期間」を出す
    # （上場10年未満なら10年チャートが実質全期間なので重複表示しない）。
    allhist = _ALLHIST_CACHE.get(code) if code else None
    if allhist and len(allhist) >= 12:
        try:
            d_first = datetime.strptime(allhist[0][0], "%Y-%m-%d").date()
            d_last = datetime.strptime(allhist[-1][0], "%Y-%m-%d").date()
            daily_first = datetime.strptime(dates[0], "%Y-%m-%d").date()
            span_years = (d_last - d_first).days / 365.25
            if (daily_first - d_first).days > 365:
                step = max(1, len(allhist) // 160)
                sp = [allhist[i] for i in range(0, len(allhist), step)]
                if sp and sp[-1][0] != allhist[-1][0]:
                    sp.append(allhist[-1])
                # 最新の日足終値で末尾を上書き（月足の遅延対策）
                sp[-1] = [dates[-1], round(closes[-1], 1)]
                out["sparkall"] = sp
                out["years_all"] = max(1, round(span_years))
        except Exception:
            pass
    return out


DEMERIT_RULES_DOC = [
    ("致命", "赤字（PERマイナス）", 30),
    ("致命", "直近10日に1日8%超の急落（材料落ち）", 25),
    ("致命", "1年安値圏を更新中（底が見えない）", 25),
    ("致命", "長期の下落トレンド継続（200日線割れが続く）", 20),
    ("重い", "PER 60倍超（期待先行の割高）", 15),
    ("重い", "PBR 8倍超（資産比で過熱）", 12),
    ("重い", "ROE 3%未満（稼ぐ力が弱い）", 12),
    ("重い", "日々の値動きが±4.5%超（荒すぎる）", 12),
    ("重い", "50日線が200日線の下（長期は調整形）", 10),
    ("重い", "25日線から−20%超の異常乖離", 10),
    ("重い", "時価総額50億円未満（値が飛びやすい）", 10),
    ("重い", "売買代金が1日1億円未満（約定しにくい）", 10),
    ("重い", "長期支持帯を4回目以上試している（割れやすい）", 8),
    ("軽い", "下げ止まり未確認（前日から安値切り下げ）", 8),
    ("軽い", "下げが1日に集中（崩落型）", 6),
    ("軽い", "夜間ギャップ平均±2.5%超（夜の判断がズレやすい）", 6),
    ("軽い", "RSI 70超（買われすぎ）", 6),
    ("軽い", "ボリンジャー+2σ超（統計的な買われすぎ）", 6),
    ("軽い", "利確まで平均25日超（資金拘束が長い）", 5),
    ("軽い", "直近の仮想買いが塩漬け中", 5),
]


def demerit_stock(s):
    """(減点合計, [(重さ, 理由, 点)]) を返す。減点0が「無傷」"""
    hits = []
    fu = s.get("fund") or {}
    lg = s.get("long") or {}
    per, pbr, roe = fu.get("per"), fu.get("pbr"), fu.get("roe")

    def hit(label, pts):
        hits.append(label + (pts,))

    if fu:
        # PERが「取得できたが負」なら赤字。項目自体が無い場合は判定しない（欠損≠赤字）
        if per is not None and per < 0:
            hit(("致命", "赤字（PERマイナス）"), 30)
        elif per is not None and per > 60:
            hit(("重い", f"PER {per:.0f}倍と期待先行の割高"), 15)
        if pbr is not None and pbr > 8:
            hit(("重い", f"PBR {pbr:.1f}倍と資産比で過熱"), 12)
        if roe is not None and per and per > 0 and roe < 3:
            hit(("重い", f"ROE {roe:.1f}%と稼ぐ力が弱い"), 12)
        mc = fu.get("mcap_oku")
        if mc and mc < 50:
            hit(("重い", f"時価総額{mc}億円と小型で値が飛びやすい"), 10)

    if s.get("worst_1d", 0) <= -8.0:
        hit(("致命", f"直近10日に1日{abs(s['worst_1d']):.0f}%の急落（材料落ち）"), 25)
    if s.get("pos1y") is not None and s["pos1y"] <= 0.12:
        hit(("致命", "1年安値圏を更新中（底が見えない）"), 25)
    if s.get("below_ma_ratio", 0) >= 0.9:
        hit(("致命", "長期の下落トレンド継続（200日線割れが続く）"), 20)
    if s.get("vol20", 0) >= 4.5:
        hit(("重い", f"日々の値動きが±{s['vol20']:.1f}%と荒すぎる"), 12)
    if lg.get("gc") is False:
        hit(("重い", "50日線が200日線の下（長期は調整形）"), 10)
    dv = s.get("dev25")
    if dv is not None and dv <= -20:
        hit(("重い", f"25日線から{dv:.0f}%の異常乖離"), 10)
    if s.get("turnover", 0) < 100_000_000:
        hit(("重い", "売買代金が1日1億円未満（約定しにくい）"), 10)
    z = lg.get("zone")
    if z and z.get("touches", 0) >= 3 and z.get("dist_pct", 99) <= 3:
        hit(("重い", f"長期支持帯を{z['touches'] + 1}回目に試す位置（割れやすい）"), 8)
    if s.get("stabilizing") is False:
        hit(("軽い", "下げ止まり未確認（前日から安値切り下げ）"), 8)
    if s.get("concentration", 0) >= 0.7:
        hit(("軽い", "下げが1日に集中（崩落型）"), 6)
    ga = s.get("gap_avg")
    if ga is not None and ga >= 2.5:
        hit(("軽い", f"夜間ギャップ平均±{ga:.1f}%（夜の判断がズレやすい）"), 6)
    rsi = lg.get("rsi")
    if rsi is not None and rsi >= 70:
        hit(("軽い", f"RSI {rsi:.0f} の買われすぎ"), 6)
    bs = s.get("boll_sigma")
    if bs is not None and bs >= 2:
        hit(("軽い", "ボリンジャー+2σ超（統計的な買われすぎ）"), 6)
    sim = s.get("sim") or {}
    ah = sim.get("avg_held")
    if ah is not None and ah > 25:
        hit(("軽い", f"利確まで平均{ah:.0f}日と資金拘束が長い"), 5)
    if sim.get("open_loss"):
        hit(("軽い", "直近の仮想買いが塩漬け中"), 5)

    total = sum(h[2] for h in hits)
    return total, hits


# ------------------------------------------------------------
# TOB素地スコア: M&Aアドバイザー・中小企業診断士が「買収候補」を
# 見るときの定石を、公開データで計算できる範囲だけ固定基準化したもの。
# ※確率の予測ではない。親子上場・創業家持分・アクティビスト保有など
#   決定的な要因は無料データでは取れないため「素地」の順位付けに留まる。
# ------------------------------------------------------------
def tob_score(s):
    """(score, hits) を返す。判定素材が無ければ (None, [])。
    hits = [(観点ラベル, 加点)] で内訳をそのまま画面に出せる形にする"""
    fu = s.get("fund") or {}
    pbr = fu.get("pbr")
    mcap = fu.get("mcap_oku")
    if pbr is None or not mcap:
        return None, []
    hits = []

    def add(label, pts):
        hits.append((label, pts))

    # 1. 解散価値との比較（買収側から見た「値札の安さ」の核心）
    if pbr < 0.6:
        add(f"PBR {pbr:.2f}倍 ＝ 解散価値の6割未満（最重要の割安シグナル）", 22)
    elif pbr < 0.8:
        add(f"PBR {pbr:.2f}倍 ＝ 解散価値を大きく下回る", 16)
    elif pbr < 1.0:
        add(f"PBR {pbr:.2f}倍 ＝ 1倍割れ（東証の改善要請対象圏）", 10)
    elif pbr < 1.3:
        add(f"PBR {pbr:.2f}倍 ＝ 1倍をわずかに超える程度", 4)
    elif pbr >= 3.0:
        add(f"PBR {pbr:.1f}倍 ＝ 資産面の買収妙味は薄い", -8)

    # 2. 買収資金の現実性（TOBの主戦場は数百億円クラス）
    if 100 <= mcap < 1000:
        add(f"時価総額 {mcap:,}億円 ＝ TOB・MBOの主戦場サイズ", 15)
    elif 50 <= mcap < 100 or 1000 <= mcap < 2000:
        add(f"時価総額 {mcap:,}億円 ＝ 買収資金が十分現実的", 8)
    elif 20 <= mcap < 50:
        add(f"時価総額 {mcap:,}億円 ＝ 小粒だが買収可能圏", 4)
    elif mcap >= 5000:
        add(f"時価総額 {mcap:,}億円 ＝ 大型で買収資金のハードル高", -12)

    # 3. ため込み体質（現金・資産を抱えて活かせていない＝ファンドの好物）
    er = fu.get("equity_ratio")
    roe = fu.get("roe")
    if er is not None:
        if er >= 70:
            add(f"自己資本比率 {er:.0f}% ＝ ほぼ無借金のため込み体質", 12)
        elif er >= 60:
            add(f"自己資本比率 {er:.0f}% ＝ 財務が厚く買収後の余力大", 8)
        elif er >= 50:
            add(f"自己資本比率 {er:.0f}% ＝ 財務健全", 4)
        if er >= 60 and roe is not None and roe < 8:
            add(f"厚い資本にROE {roe:.1f}% ＝ 資本を活かせていない（アクティビスト誘因）", 6)

    # 4. 稼ぐ力（壊れた会社は買われない。「安いが健全」が核心）
    om = fu.get("op_margin")
    if om is not None:
        if om >= 8:
            add(f"営業利益率 {om:.1f}% ＝ 本業の稼ぐ力あり", 8)
        elif om >= 3:
            add(f"営業利益率 {om:.1f}% ＝ 本業は黒字", 5)
        elif om < 0:
            add("営業赤字 ＝ 事業目的の買収対象になりにくい", -12)

    # 5. 還元の渋さ（黒字なのに配当性向が低い＝ため込みの傍証）
    po = fu.get("payout")
    if po is not None and om is not None and om > 0 and 0 <= po < 30:
        add(f"配当性向 {po:.0f}% ＝ 黒字なのに還元が渋い", 5)

    # 6. 株価の放置（市場に評価されていないほどプレミアムを乗せやすい）
    p1 = s.get("pos1y")
    if p1 is not None:
        if p1 < 0.25:
            add("株価は1年レンジの下位25%に放置", 6)
        elif p1 < 0.40:
            add("株価は1年レンジの下位40%", 3)

    # 7. 出来高の薄さ（アナリストも機関も見ていない銘柄は歪みが残る）
    tv = s.get("turnover")
    if tv is not None and tv < 300_000_000:
        add("売買代金が薄く市場の目が届きにくい", 4)

    # 8. 市場区分（非公開化のしやすさ・東証圧力）
    mkt = s.get("market", "")
    if mkt in ("スタンダード", "グロース", "札幌", "福岡"):
        add(f"{mkt}市場 ＝ 上場維持メリットが薄く非公開化しやすい", 4)
    elif mkt == "プライム" and pbr < 1.0:
        add("プライムでPBR1倍割れ ＝ 東証の改善要請が直撃", 4)

    # 9. 利益面の割安（おまけ）
    per = fu.get("per")
    if per is not None and 0 < per < 10:
        add(f"PER {per:.1f}倍 ＝ 利益面でも割安", 4)

    score = max(0, sum(h[1] for h in hits))
    return score, hits


# 画面に出す「取れていない決定的要因」の注記（正直な限界の明示）
TOB_MISSING_NOTE = ("このスコアに入っていない決定的な要因: 親子上場・支配株主の存在、"
                    "創業家の持株比率と経営者の年齢（MBOの最大要因）、アクティビストの大量保有、"
                    "政策保有株の解消動向、不動産などの含み益。これらは無料の公開データでは"
                    "機械判定できないため、上位に入った銘柄はこの観点を人間が確認してください。")


# ------------------------------------------------------------
# 関連銘柄マップ: 全銘柄を「財務 × テクニカル × 値動きの連動」で
# 高次元ベクトル化し、類似度グラフと3D埋め込み座標を計算する。
# 専門家が頭の中でやる「似ている銘柄の連想」を機械化した空間。
# 依存は numpy のみ（pandasに同梱・requirements変更不要）。
# ------------------------------------------------------------
SIM_WHY = [  # 類似の根拠フラグ（ビット順・フロントと共有）
    (1,   "同業種"),
    (2,   "企業規模が近い"),
    (4,   "財務体質が近い"),
    (8,   "割安度が近い"),
    (16,  "値動きが連動"),
    (32,  "高配当同士"),
    (64,  "値動きの荒さが近い"),
    (128, "同じ市場区分"),
]


def build_stock_map(detail_map, series=None, k_neighbors=8):
    """detail_map から docs/map.json を生成し、各銘柄に similar(top5) を付与する。
    返り値: マップに載せた銘柄数（素材不足なら 0）"""
    import numpy as np

    entries = [e for e in detail_map.values()
               if e.get("status") != "fail" and e.get("close") is not None]
    n = len(entries)
    if n < 10:
        return 0
    codes = [e["code"] for e in entries]

    # ---------- 数値特徴量（欠損は中央値で補完 → 1-99%でクリップ → 標準化） ----------
    def fu(e):
        return e.get("fund") or {}

    def col(fn):
        out = np.full(n, np.nan)
        for i, e in enumerate(entries):
            try:
                v = fn(e)
                if v is not None and math.isfinite(float(v)):
                    out[i] = float(v)
            except Exception:  # noqa: BLE001
                pass
        return out

    cols = [
        col(lambda e: math.log10(fu(e)["mcap_oku"]) if fu(e).get("mcap_oku") else None),
        col(lambda e: math.log(max(0.05, fu(e)["pbr"])) if fu(e).get("pbr") is not None else None),
        col(lambda e: math.log(min(150.0, fu(e)["per"])) if (fu(e).get("per") or 0) > 0 else None),
        col(lambda e: fu(e).get("roe")),
        col(lambda e: fu(e).get("equity_ratio")),
        col(lambda e: fu(e).get("op_margin")),
        col(lambda e: fu(e).get("div_yield")),
        col(lambda e: fu(e).get("payout")),
        col(lambda e: e.get("vol20")),
        col(lambda e: e.get("pos1y")),
        col(lambda e: e.get("drawdown_1y")),
        col(lambda e: math.log10(e["turnover"]) if e.get("turnover") else None),
    ]
    X = np.stack(cols, axis=1)
    for j in range(X.shape[1]):
        c = X[:, j]
        med = np.nanmedian(c)
        if not np.isfinite(med):
            med = 0.0
        c = np.where(np.isfinite(c), c, med)
        lo, hi = np.percentile(c, 1), np.percentile(c, 99)
        c = np.clip(c, lo, hi)
        sd = c.std()
        X[:, j] = (c - c.mean()) / (sd if sd > 1e-9 else 1.0)

    # 業種のone-hot（専門家の連想の第一軸「同業他社」を強めに効かせる）
    secs = [e.get("sector") or "" for e in entries]
    uniq_sec = sorted(set(secs))
    S = np.zeros((n, len(uniq_sec)))
    for i, s in enumerate(secs):
        S[i, uniq_sec.index(s)] = 1.0
    F = np.concatenate([X, S * 1.6], axis=1)  # 同業種は強めに・ただし異業種でも体質が瓜二つなら近傍に入れる

    # ---------- 類似度①: 特徴ベクトルのコサイン ----------
    Fn = F / (np.linalg.norm(F, axis=1, keepdims=True) + 1e-9)
    cosS = (Fn @ Fn.T).astype(np.float32)

    # ---------- 類似度②: 値動きの相関（直近130営業日の日次リターン） ----------
    corr = None
    if series:
        date_map = {}
        for i, c in enumerate(codes):
            s = series.get(c)
            if s and len(s[0]) >= 60:
                date_map[i] = dict(zip(s[0], s[1]))
        if len(date_map) >= 10:
            all_dates = sorted({d for m in date_map.values() for d in m})[-130:]
            P = np.full((len(all_dates), n), np.nan)
            for i, m in date_map.items():
                P[:, i] = [m.get(d, np.nan) for d in all_dates]
            R = np.diff(P, axis=0) / np.where(np.isfinite(P[:-1]), P[:-1], np.nan)
            mask = np.isfinite(R)
            R0 = np.where(mask, R, 0.0)
            cnt = mask.sum(0)
            mu = R0.sum(0) / np.maximum(1, cnt)
            Rc = np.where(mask, R - mu, 0.0)
            sd = np.sqrt((Rc ** 2).sum(0) / np.maximum(1, cnt)) + 1e-12
            Rn = (Rc / sd).astype(np.float32)
            pair_cnt = (mask.astype(np.float32).T @ mask.astype(np.float32))
            corr = (Rn.T @ Rn) / np.maximum(20.0, pair_cnt)
            corr = np.clip(corr, -1.0, 1.0)

    cosC = np.clip(cosS, -1, 1)
    corr_ok = corr is not None
    if corr_ok:
        # 相関が計算できたペアだけ 0.55:0.45 で混合。できないペアはコサインのみ（%の物差しを揃える）
        has = np.zeros(n, dtype=bool)
        for i, c in enumerate(codes):
            s2 = series.get(c) if series else None
            has[i] = bool(s2 and len(s2[0]) >= 60)
        pair_has = has[:, None] & has[None, :]
        combined = np.where(pair_has, 0.55 * cosC + 0.45 * corr, cosC)
    else:
        combined = cosC.copy()
        corr = np.zeros_like(cosS)
    np.fill_diagonal(combined, -9.0)

    # ---------- 近傍 top-k と「なぜ似ているか」フラグ ----------
    kk = min(k_neighbors, n - 1)
    nb_idx = np.argpartition(-combined, kk, axis=1)[:, :kk]
    row = np.arange(n)[:, None]
    order = np.argsort(-combined[row, nb_idx], axis=1)
    nb_idx = nb_idx[row, order]

    def _sf(v):
        try:
            v = float(v)
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            return None

    def why_flags(i, j):
        f = 0
        if secs[i] and secs[i] == secs[j]:
            f |= 1
        mi, mj = _sf(fu(entries[i]).get("mcap_oku")), _sf(fu(entries[j]).get("mcap_oku"))
        if mi and mj and mi > 0 and mj > 0 and abs(math.log10(mi) - math.log10(mj)) < 0.3:
            f |= 2
        ei, ej = _sf(fu(entries[i]).get("equity_ratio")), _sf(fu(entries[j]).get("equity_ratio"))
        ri, rj = _sf(fu(entries[i]).get("roe")), _sf(fu(entries[j]).get("roe"))
        if None not in (ei, ej, ri, rj) and abs(ei - ej) < 12 and abs(ri - rj) < 6:
            f |= 4
        pi, pj = _sf(fu(entries[i]).get("pbr")), _sf(fu(entries[j]).get("pbr"))
        if pi and pj and pi > 0 and pj > 0 and abs(math.log(pi) - math.log(pj)) < 0.22:
            f |= 8
        if corr[i, j] >= 0.55:
            f |= 16
        di, dj = _sf(fu(entries[i]).get("div_yield")), _sf(fu(entries[j]).get("div_yield"))
        if di is not None and dj is not None and di >= 3 and dj >= 3:
            f |= 32
        vi, vj = _sf(entries[i].get("vol20")), _sf(entries[j].get("vol20"))
        if vi is not None and vj is not None and abs(vi - vj) < 0.5:
            f |= 64
        if entries[i].get("market") and entries[i].get("market") == entries[j].get("market"):
            f |= 128
        return f

    # ---------- 3D埋め込み: PCA初期化 → 近傍引力・ランダム斥力の力学法（決定的） ----------
    Fc = F - F.mean(0)
    try:
        U, sv, _ = np.linalg.svd(Fc, full_matrices=False)
        pos = (U[:, :3] * sv[:3]).astype(np.float64)
    except Exception:  # noqa: BLE001
        pos = np.random.default_rng(0).normal(size=(n, 3))
    pos = pos / (pos.std() + 1e-9)
    rng = np.random.default_rng(42)
    iters = 220 if n > 500 else 120
    for it in range(iters):
        target = pos[nb_idx].mean(axis=1)
        ridx = rng.integers(0, n, (n, 6))
        diff = pos[:, None, :] - pos[ridx]
        dist2 = (diff ** 2).sum(-1, keepdims=True) + 1e-3
        rep = (diff / dist2).mean(axis=1)
        alpha = 0.14 * (1.0 - it / iters)
        pos += alpha * (0.75 * (target - pos) + 0.45 * rep)
    pos = pos - pos.mean(0)
    pos = pos / (np.abs(pos).max() + 1e-9) * 2.6

    # ---------- 軸の意味付け: 最終座標と解釈可能な特徴の相関から各軸に名前を付ける ----------
    # X列: 0=規模 1=PBR 3=ROE 4=自己資本比率 6=配当利回り 8=値動きの荒さ 9=1年レンジ位置
    axis_cands = [("企業規模", 0, "大きい", "小さい"),
                  ("割安・割高", 1, "割高", "割安"),
                  ("収益力(ROE)", 3, "高い", "低い"),
                  ("財務の厚さ", 4, "厚い", "薄い"),
                  ("配当利回り", 6, "高い", "低い"),
                  ("値動きの荒さ", 8, "荒い", "穏やか"),
                  ("1年レンジ位置", 9, "高値圏", "安値圏")]
    axes_meta = []
    used_ax = set()
    for a in range(3):
        best = None
        for name, j, plab, mlab in axis_cands:
            if name in used_ax:
                continue
            v = X[:, j]
            if v.std() < 1e-9:
                continue
            r = float(np.corrcoef(pos[:, a], v)[0, 1])
            if not math.isfinite(r):
                continue
            if best is None or abs(r) > abs(best[0]):
                best = (r, name, plab, mlab)
        if best is not None and abs(best[0]) >= 0.25:
            r, name, plab, mlab = best
            used_ax.add(name)
            if r < 0:
                plab, mlab = mlab, plab
            axes_meta.append({"label": name, "plus": plab, "minus": mlab, "r": round(abs(r), 2)})
        else:
            axes_meta.append({"label": f"合成軸{a + 1}", "plus": "", "minus": "", "r": 0})

    # ---------- 出力 ----------
    groups = sorted({SECTOR_GROUPS.get(s, DEFAULT_GROUP) for s in secs})
    g_idx = {g: i for i, g in enumerate(groups)}
    markets = sorted({e.get("market") or "" for e in entries})
    m_idx = {m: i for i, m in enumerate(markets)}
    stocks_out = []
    for i, e in enumerate(entries):
        f = fu(e)
        nbs = []
        for j in nb_idx[i]:
            j = int(j)
            sim01 = float(np.clip((combined[i, j] + 0.2) / 1.2, 0, 1))
            nbs.extend([codes[j], round(sim01 * 100), why_flags(i, j)])
        stocks_out.append([
            e["code"], e["name"],
            g_idx[SECTOR_GROUPS.get(secs[i], DEFAULT_GROUP)],
            m_idx.get(e.get("market") or "", 0),
            round(float(pos[i, 0]), 3), round(float(pos[i, 1]), 3), round(float(pos[i, 2]), 3),
            (round(math.log10(f["mcap_oku"]), 2) if f.get("mcap_oku") else None),
            (round(f["pbr"], 2) if f.get("pbr") is not None else None),
            (int(round(e["score"])) if e.get("score") is not None else None),
            e.get("tob"),
            1 if e.get("tri") else 0,
            round(e["close"], 1),
            (round(e["drop_pct"], 1) if e.get("drop_pct") is not None else None),
            e.get("status", ""),
            nbs,
        ])
        # 詳細ページ用の「似ている銘柄」top5（根拠ラベル付き・優先順はマップ側と同一）
        why_lut = dict(SIM_WHY)
        prio = [16, 1, 4, 8, 2, 32, 64, 128]
        sims = []
        for j in nb_idx[i][:5]:
            j = int(j)
            flags = why_flags(i, j)
            labels = [why_lut[b] for b in prio if flags & b][:3]
            sims.append({"code": codes[j], "name": entries[j]["name"],
                         "sim": round(float(np.clip((combined[i, j] + 0.2) / 1.2, 0, 1)) * 100),
                         "why": labels,
                         "ex": entries[j].get("status") in ("dead", "skip")})
        e["similar"] = sims

    payload = {
        "generated_at": datetime.now(JST).isoformat(),
        "groups": groups,
        "markets": markets,
        "why": {str(bit): lab for bit, lab in SIM_WHY},
        "axes": axes_meta,
        "dims": int(F.shape[1]),
        "stocks": stocks_out,
    }
    DOCS.mkdir(exist_ok=True)
    (DOCS / "map.json").write_text(json.dumps(payload, ensure_ascii=False,
                                              separators=(",", ":")), encoding="utf-8")
    print(f"  関連銘柄マップ: {n:,}銘柄を埋め込み（値動き相関 {'あり' if corr_ok else 'なし'}）")
    return n


MAP_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="robots" content="noindex, nofollow">
<link rel="apple-touch-icon" href="icon.png">
<link rel="icon" type="image/png" href="icon.png">
<style>html,body{touch-action:pan-x pan-y;}</style>
<script>
/* スマホでページ自体が拡大されて操作しづらくなるのを防止（ダブルタップ拡大・ピンチのページ拡大）。
   マップ内のピンチ操作は独自実装(touch-action:none)なので影響なし */
document.addEventListener('gesturestart',function(e){e.preventDefault();});
document.addEventListener('gesturechange',function(e){e.preventDefault();});
</script>
<title>関連銘柄マップ ｜ 株ノート</title>
<style>
:root{
  --bg:#080b10; --panel:#0e1420; --panel2:#0b0f16;
  --line:#1e293b; --line2:#2b3a52;
  --tx:#dce5f2; --tx2:#8fa0b8; --dim:#6b7a91;
  --cy:#4dd7ff; --gr:#3ddc97; --am:#ffc14d; --rd:#ff5a76; --pu:#b78cff; --bl:#5b8cff;
  --field:#0a0f18; --card2:#0f1624; --chipbg:#101828; --legbg:rgba(9,13,19,.78); --onink:#06131a;
}
html[data-theme="gray"]{
  --bg:#51565f; --panel:#464b54; --panel2:#4b505a;
  --line:#5c626e; --line2:#6d7382;
  --tx:#f0f3f8; --tx2:#ccd2dd; --dim:#a3aab6;
  --field:#3f444d; --card2:#3f444d; --chipbg:#3a3f48; --legbg:rgba(64,68,76,.82); --onink:#20242b;
}
html[data-theme="light"]{
  --bg:#f2f2f7; --panel:#ffffff; --panel2:#f7f7fa;
  --line:#e3e2dc; --line2:#cfcec6;
  --tx:#1c1c1e; --tx2:#4c586a; --dim:#8e8e93;
  --cy:#0e7ea8;
  --field:#ffffff; --card2:#f4f5f8; --chipbg:#eef0f4; --legbg:rgba(255,255,255,.88); --onink:#ffffff;
}
*{box-sizing:border-box; margin:0; padding:0; -webkit-tap-highlight-color:transparent;}
html,body{height:100%; overflow:hidden; background:var(--bg);}
body{color:var(--tx); font-family:"Hiragino Sans","Yu Gothic",system-ui,sans-serif;
  font-size:13px; line-height:1.65; -webkit-font-smoothing:antialiased;}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;}
#app{display:flex; flex-direction:column; height:100%; height:100dvh;}

.hdr{flex:none; display:flex; align-items:center; gap:10px; padding:0 12px; height:46px;
  background:linear-gradient(180deg,#0d1219,#080b10); border-bottom:1px solid var(--line);}
html[data-theme="gray"] .hdr{background:linear-gradient(180deg,#4b505a,#464b54);}
html[data-theme="light"] .hdr{background:linear-gradient(180deg,#ffffff,#f4f4f7);}
.hdr .back{color:var(--tx2); text-decoration:none; font-size:12px; font-weight:700; flex:none;}
.hdr .logo{font-family:ui-monospace,Menlo,monospace; font-size:12px; font-weight:700;
  letter-spacing:.2em; color:var(--cy); text-shadow:0 0 16px rgba(77,215,255,.4); flex:none;}
.hdr .cnt{font-family:ui-monospace,Menlo,monospace; font-size:10px; color:var(--dim); letter-spacing:.08em;
  margin-left:auto; flex:none;}
.dnav{flex:none; display:flex; gap:6px; padding:7px 10px 0; background:var(--panel2);
  overflow-x:auto; scrollbar-width:none; -webkit-overflow-scrolling:touch;}
.dnav::-webkit-scrollbar{display:none;}
.dnav a{flex:none; font-size:11.5px; font-weight:700; color:var(--tx2); text-decoration:none;
  background:var(--field); border:1px solid var(--line); border-radius:14px; padding:5px 12px;}
.dnav a.act{background:var(--cy); color:var(--onink); border-color:var(--cy);}

.toolrow{flex:none; display:flex; gap:6px; align-items:center; padding:7px 10px;
  background:var(--panel2); border-bottom:1px solid var(--line); overflow-x:auto;
  -webkit-overflow-scrolling:touch; scrollbar-width:none;}
.toolrow::-webkit-scrollbar{display:none;}
.toolwrap{position:relative; flex:none;}
.toolwrap::after{content:''; position:absolute; right:0; top:0; bottom:0; width:36px;
  background:linear-gradient(90deg, rgba(11,15,22,0), var(--panel2)); pointer-events:none;}
.srchwrap{position:relative; flex:none;}
.srch{width:168px; background:var(--field); border:1px solid var(--line2); border-radius:6px;
  color:var(--tx); font-size:13px; padding:6px 9px; outline:none;}
.srch:focus{border-color:var(--cy);}
.sugg{position:absolute; left:0; top:34px; width:238px; background:var(--panel); border:1px solid var(--line2);
  border-radius:6px; z-index:40; max-height:250px; overflow-y:auto; display:none;
  box-shadow:0 10px 34px rgba(0,0,0,.5);}
.sugg.show{display:block;}
.sugg .it{padding:7px 10px; font-size:12px; cursor:pointer; border-top:1px solid var(--line);}
.sugg .it:first-child{border-top:none;}
.sugg .it:hover{background:var(--card2);}
.sugg .it small{color:var(--dim); font-family:ui-monospace,Menlo,monospace; margin-left:5px;}
.sugg .cnt-it{padding:6px 10px; font-size:10px; color:var(--dim); font-weight:700;
  border-bottom:1px solid #131a28; letter-spacing:.05em;}
.modes{display:flex; border:1px solid var(--line2); border-radius:6px; overflow:hidden; flex:none;}
.modes button{border:0; padding:6px 10px; font-size:10.5px; font-weight:700; letter-spacing:.06em;
  background:transparent; color:var(--dim); cursor:pointer; white-space:nowrap;}
.modes button.on{background:var(--cy); color:var(--onink);}
.tbtn{flex:none; border:1px solid var(--line2); border-radius:6px; background:transparent;
  color:var(--tx2); font-size:10.5px; font-weight:700; padding:6px 9px; cursor:pointer; white-space:nowrap;}
.tbtn.on{border-color:var(--cy); color:var(--cy);}

.main{flex:1; display:flex; min-height:0; position:relative;}
#stage{flex:1; position:relative; min-width:0; overflow:hidden;
  background:radial-gradient(120% 90% at 50% 40%, #0d141f 0%, #080b10 70%);}
html[data-theme="gray"] #stage{background:radial-gradient(120% 90% at 50% 40%, #5a5f6a 0%, #4b5058 70%);}
html[data-theme="light"] #stage{background:radial-gradient(120% 90% at 50% 40%, #ffffff 0%, #eceef2 70%);}
canvas{display:block; width:100%; height:100%; cursor:grab; touch-action:none;}
canvas.drag{cursor:grabbing;}

.legend{position:absolute; left:10px; bottom:10px; pointer-events:auto; cursor:default;
  background:var(--legbg); backdrop-filter:blur(6px); border:1px solid var(--line);
  border-radius:6px; padding:7px 10px; max-width:230px; z-index:5;}
.legend .t{font-size:9.5px; font-weight:700; color:var(--dim); letter-spacing:.14em; margin-bottom:3px;}
.legend .row{display:flex; align-items:center; gap:6px; font-size:10.5px; color:var(--tx2); padding:1px 0;}
.legend .sw{width:9px; height:9px; border-radius:50%; flex:none;}
.legend .gtog{cursor:pointer;}
.legend .gtog.off{opacity:.32; text-decoration:line-through;}
.legend .gclear{cursor:pointer; color:var(--cy); font-weight:700;}
.legend .lgh{display:flex; justify-content:space-between; gap:10px; align-items:baseline;}
.legend .lgx{cursor:pointer; color:var(--cy); font-weight:700; letter-spacing:0;}
.legend .lgopen{cursor:pointer; color:var(--cy); font-weight:700;}
.legend .lgall{gap:8px; padding-top:4px;}
.legend .lgbtn{cursor:pointer; font-size:10px; font-weight:800; color:var(--cy);
  border:1px solid var(--line2); border-radius:5px; padding:2px 9px;}
.legend .mean{margin-top:5px; padding-top:5px; border-top:1px solid var(--line);
  font-size:9.5px; color:var(--dim); line-height:1.6;}
.legend .mean b{color:var(--tx2);}
.hint{position:absolute; right:10px; top:10px; pointer-events:none; font-size:10px; color:var(--dim);
  background:var(--legbg); border:1px solid var(--line); border-radius:6px; padding:5px 9px; z-index:5;}

.panel{flex:none; width:0; overflow:hidden; background:var(--panel); border-left:1px solid var(--line);
  transition:width .18s ease; display:flex; flex-direction:column;}
.panel.show{width:320px;}
/* ✕で閉じたあともパネルの幅は保持して「空間」を残す:
   閉じるたびにキャンバスの寸法が変わって点が引き伸ばされるバグを根本から断つ */
.panel.ghost{width:320px; background:transparent; border-left-color:transparent;}
.panel.ghost .pbody{visibility:hidden;}
.panel.ghost .pclose{display:none;}
.pbody{flex:1; overflow-y:auto; padding:14px; min-width:290px;}
.pclose{position:absolute; right:10px; top:10px; border:1px solid var(--line2); background:transparent;
  color:var(--dim); border-radius:5px; font-size:12px; padding:2px 8px; cursor:pointer;}
.pn{font-size:17px; font-weight:800; color:var(--tx); padding-right:40px; line-height:1.4;}
.pc{font-family:ui-monospace,Menlo,monospace; font-size:11px; color:var(--dim); margin-bottom:8px;}
.pfacts{display:flex; flex-wrap:wrap; gap:5px; margin-bottom:12px;}
.pf{font-size:10.5px; color:var(--tx2); background:var(--chipbg); border:1px solid var(--line);
  border-radius:5px; padding:3px 8px;}
.ph{font-size:10px; font-weight:700; color:var(--dim); letter-spacing:.16em; margin:12px 0 6px;}
.nb{display:block; background:var(--card2); border:1px solid var(--line); border-radius:8px;
  padding:8px 10px; margin-bottom:6px; cursor:pointer;}
.nb:hover{border-color:var(--line2);}
.nb .nbn{font-size:13px; font-weight:700; color:var(--tx);}
.nb .nbn small{font-family:ui-monospace,Menlo,monospace; color:var(--dim); font-weight:400; margin-left:5px;}
.nb .sim{float:right; font-family:ui-monospace,Menlo,monospace; font-size:12px; font-weight:700; color:var(--cy);}
.nb .why{margin-top:3px; display:flex; flex-wrap:wrap; gap:4px;}
.nb .wt{font-size:9px; font-weight:700; color:#9fd8b4; background:rgba(61,220,151,.1);
  border:1px solid rgba(61,220,151,.28); border-radius:4px; padding:1px 6px;}
.nb .wt.mv{color:#ffd9a3; background:rgba(255,193,77,.1); border-color:rgba(255,193,77,.3);}
.nb .nbf{font-size:10px; color:var(--dim); font-family:ui-monospace,Menlo,monospace; margin-top:2px;}
.trailrow{display:flex; gap:5px; flex-wrap:wrap; align-items:center; margin-bottom:8px;}
.trlab{font-size:9.5px; color:var(--dim); font-weight:700; letter-spacing:.1em;}
.tchip{font-size:10px; font-weight:700; color:var(--cy); background:rgba(77,215,255,.08);
  border:1px solid rgba(77,215,255,.3); border-radius:10px; padding:2px 8px; cursor:pointer;}
.pnav{display:flex; gap:6px; margin-bottom:10px; padding-right:44px;}
.pnv{flex:1; border:1px solid var(--line2); background:transparent; color:var(--cy);
  border-radius:6px; font-size:11px; font-weight:700; padding:6px 0; cursor:pointer;}
.pnv:active{background:rgba(77,215,255,.12);}
.plinks{display:flex; gap:6px; margin-top:12px;}
.plink{flex:1; display:block; text-align:center; text-decoration:none; font-size:11.5px; font-weight:700;
  color:var(--cy); border:1px solid var(--line2); border-radius:7px; padding:8px 4px;}
.plink.y{color:var(--am);}

/* モバイル: パネルは下からのシート */
@media (max-width: 760px){
  .main{flex-direction:column;}
  body.focused .legend{display:none;}
  .panel{width:100%; border-left:none; border-top:1px solid var(--line);
    transition:height .18s ease; height:0;}
  .panel.show{width:100%; height:46%;}
  .panel.ghost{width:100%; height:46%; border-top-color:transparent;}
  .pbody{min-width:0;}
  .legend{max-width:180px;}
  .srch{width:132px;}
}
.intro{position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
  background:rgba(8,11,16,.86); z-index:30; padding:20px;}
.introcard{max-width:430px; background:var(--panel); border:1px solid var(--line2); border-radius:12px;
  padding:20px 22px; box-shadow:0 20px 60px rgba(0,0,0,.5);}
.introcard h1{font-size:16px; color:var(--cy); margin-bottom:10px; letter-spacing:.04em;}
html[data-theme="light"] .intro{background:rgba(240,240,245,.82);}
.introcard p{font-size:12px; color:var(--tx2); line-height:1.9; margin-bottom:8px;}
.introcard b{color:var(--tx);}
.gostart{width:100%; margin-top:8px; border:none; border-radius:8px; background:var(--cy); color:var(--onink);
  font-size:14px; font-weight:800; padding:11px; cursor:pointer;}
.nointro{display:block; margin-top:8px; text-align:center; font-size:10.5px; color:var(--dim);
  background:none; border:none; cursor:pointer; width:100%;}
.setrow{display:flex; justify-content:space-between; align-items:center; gap:10px;
  font-size:12px; color:var(--tx2); padding:8px 0; border-bottom:1px solid var(--line);}
.seg{display:flex; border:1px solid var(--line2); border-radius:6px; overflow:hidden;}
.seg button{border:0; padding:5px 11px; font-size:11px; font-weight:700; background:transparent;
  color:var(--dim); cursor:pointer;}
.seg button.on{background:var(--cy); color:var(--onink);}
.setnote{font-size:10.5px; color:var(--dim); line-height:1.7; padding:8px 0 2px;}
#loading{position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
  color:var(--dim); font-family:ui-monospace,Menlo,monospace; font-size:12px; letter-spacing:.2em; z-index:20;}
</style>
</head>
<body>
<div id="app">
  <div class="hdr">
    <div class="logo">銘柄マップ</div>
    <div class="cnt mono" id="cnt">…</div>
  </div>
  <nav class="dnav">
    <a href="guide.html">はじめに</a><a href="indicators.html">指標の読み方</a><a href="universe.html">全銘柄台帳</a><a href="index.html">今夜の厳選</a><a class="act">銘柄マップ</a><a href="caps.html">時価総額マップ</a>
  </nav>
  <div class="toolwrap"><div class="toolrow">
    <div class="srchwrap">
      <input id="q" class="srch" type="search" placeholder="銘柄名・コード検索" autocomplete="off">
      <div id="sugg" class="sugg"></div>
    </div>
    <div class="modes" id="modes">
      <button data-m="sec" class="on">業種</button>
      <button data-m="pick">今夜の厳選</button>
      <button data-m="mine">マイ銘柄</button>
      <button data-m="mkt">市場区分</button>
    </div>
    <button class="tbtn on" id="spin">自動回転</button>
    <button class="tbtn" id="reset">視点リセット</button>
    <button class="tbtn on" id="showex">除外も表示</button>
    <button class="tbtn" id="camset">⚙ カメラ設定</button>
    <button class="tbtn" id="howbtn">❓ 仕組みと使い道</button>
  </div></div>
  <div class="main">
    <div id="stage">
      <canvas id="cv"></canvas>
      <div id="loading">LOADING MAP…</div>
      <div class="hint" id="hint">1本指=回転 ・ 2本指=移動＆拡大 ・ ダブルタップ=ズーム ・ タップ=銘柄 <span id="hintx" style="pointer-events:auto; cursor:pointer; color:#4dd7ff; font-weight:700; margin-left:4px;">✕</span></div>
      <div class="legend" id="legend"></div>
    </div>
    <div class="panel" id="panel">
      <div class="pbody" style="position:relative">
        <button class="pclose" id="pclose">✕</button>
        <div id="pcontent"></div>
      </div>
    </div>
  </div>
</div>
<div class="intro" id="howsheet" style="display:none">
  <div class="introcard">
    <h1>❓ この空間の仕組みと使い道</h1>
    <p><b>仕組み</b> ── AIが言葉を数千次元のパラメータ（埋め込み）で扱い「意味の近い言葉を近くに置く」のと同じ発想です。
    このマップは全銘柄をPER・PBR・ROE・配当利回り・自己資本比率・時価総額・値動きの荒さ・業種など
    <b><span id="dimN">約45</span>次元のパラメータベクトル</b>にし、さらに直近130営業日の値動きの連動（相関）を混ぜて銘柄同士の距離を計算。
    それを毎晩、似た銘柄が近くに来るように3次元へ圧縮しています。</p>
    <p><b>使い道の具体例</b></p>
    <p>① <b>乗り換え候補さがし</b> ── 気になる銘柄を検索してフォーカス。糸の先に「同じ体質でPBRがもっと安い」銘柄がいれば、比較検討の候補になります。</p>
    <p>② <b>厳選銘柄の"仲間"を先回り</b> ── 色モードを「今夜の厳選」にして緑の点の周りを見る。近くの暗い点は<b>同じ体質でまだ買い場が来ていない</b>銘柄＝ウォッチリストの種です。</p>
    <p>③ <b>ほんとうの分散投資</b> ── 持ち株同士がこの空間で近すぎたら、実は同じリスクを重ねて持っているだけかも。<b>離れた場所から選ぶと分散になります</b>。</p>
    <p>④ <b>自分だけの地図</b> ── 色モードを「マイ銘柄」にして凡例の「その他」を非表示にすると、★と持ち株だけの空間に。持ち株同士が固まっていたら分散を、★の近くの無印は次の候補を意味します。</p>
    <p style="color:#8fa0b8">注意: 距離は「体質と値動きの類似」です。取引関係・サプライチェーン・ニュースの繋がりはまだ含まれていません。</p>
    <button class="gostart" id="howclose">閉じる</button>
  </div>
</div>
<div class="intro" id="camsheet" style="display:none">
  <div class="introcard">
    <h1>⚙ カメラ設定（この端末に保存）</h1>
    <div class="setrow"><span>横回転の向き</span><span class="seg" data-k="invX">
      <button data-v="false">標準</button><button data-v="true">反転</button></span></div>
    <div class="setrow"><span>縦回転の向き</span><span class="seg" data-k="invY">
      <button data-v="false">標準</button><button data-v="true">反転</button></span></div>
    <div class="setrow"><span>回転の感度</span><span class="seg" data-k="sens">
      <button data-v="0.6">低</button><button data-v="1">標準</button><button data-v="1.6">高</button></span></div>
    <div class="setrow"><span>自動回転の速さ</span><span class="seg" data-k="spin">
      <button data-v="0.5">遅い</button><button data-v="1">標準</button><button data-v="2">速い</button></span></div>
    <div class="setrow"><span>奥行きの霧</span><span class="seg" data-k="fog">
      <button data-v="0">なし</button><button data-v="0.5">弱い</button><button data-v="1">標準</button></span></div>
    <div class="setrow"><span>配色テーマ</span><span class="seg" data-k="theme">
      <button data-v="dark">ダーク</button><button data-v="gray">グレー</button><button data-v="light">ホワイト</button></span></div>
    <div class="setrow"><span>座標軸の表示</span><span class="seg" data-k="axes">
      <button data-v="true">表示</button><button data-v="false">非表示</button></span></div>
    <div class="setrow"><span>銘柄名の常時表示</span><span class="seg" data-k="labels">
      <button data-v="true">表示</button><button data-v="false">非表示</button></span></div>
    <div class="setrow"><span>つながり線（星座）</span><span class="seg" data-k="links">
      <button data-v="true">表示</button><button data-v="false">非表示</button></span></div>
    <div class="setnote">霧は「奥にある点ほど薄く」の3D表現です。軸の名前は毎晩、実データとの相関から自動で付き直します。</div>
    <button class="gostart" id="camclose">閉じる</button>
  </div>
</div>
<div class="intro" id="intro" style="display:none">
  <div class="introcard">
    <h1>◈ 関連銘柄マップ ── 銘柄の意味空間</h1>
    <p>全銘柄を<b>財務体質 × テクニカル × 値動きの連動</b>という高次元のパラメータでベクトル化し、
    似ている銘柄が近くに集まるように3D空間へ配置しました。</p>
    <p>専門家が「この銘柄に似た会社といえば…」と頭の中で連想する動きを、機械の埋め込み空間で再現したものです。</p>
    <p><b>タップした銘柄</b>から光の糸が伸びる先が「発想が繋がる銘柄」。なぜ似ているか（同業種・値動きが連動・財務体質が近い…）も表示されます。</p>
    <p>詳しい仕組みと使い道の具体例は、上の「❓ 仕組みと使い道」からいつでも読めます。</p>
    <button class="gostart" id="gostart">空間に入る</button>
    <button class="nointro" id="nointro">次回からこの説明を表示しない</button>
  </div>
</div>
<script>
(function(){
'use strict';
/* ═══ palette ═══ */
var PAL=['#4dd7ff','#ff5a76','#3ddc97','#ffc14d','#b78cff','#ff9c6b','#5b8cff','#ffe066','#a8e05f','#f28ab5','#e8c49a','#66d9c2'];
function rgbaOf(hex,a){
  var c=[parseInt(hex.slice(1,3),16),parseInt(hex.slice(3,5),16),parseInt(hex.slice(5,7),16)];
  return 'rgba('+c[0]+','+c[1]+','+c[2]+','+a+')';
}
/* ═══ state ═══ */
var DATA=null, ST=[], byCode={}, GROUPS=[], WHY={}, AXES=[];
var MODE='sec', SHOW_EX=true, SPIN=true;
var rotY=0.6, rotX=0.32, zoom=1, ox=0, oy=0, autoT=0;
var focusI=-1, fEdges=[], f2Edges=[], fset2={}, hoverI=-1, TRAIL=[], camGoal=null;
var spinHoldUntil=0;
var EDGES=[], prio=[];
/* カメラ設定（この端末に保存） */
var CAM_KEY='kabuobaa_map_cam';
var CAM={invX:false, invY:false, sens:1, spin:1, fog:1, axes:true, spinOn:true,
         labels:true, links:true, legendOpen:true, theme:'dark'};
try{ var cs=JSON.parse(localStorage.getItem(CAM_KEY)||'{}');
  for(var ck in CAM){ if(cs[ck]!==undefined) CAM[ck]=cs[ck]; } }catch(e){}
SPIN=CAM.spinOn!==false;
if(CAM.theme&&CAM.theme!=='dark'){ document.documentElement.dataset.theme=CAM.theme; }
function saveCam(){ CAM.spinOn=SPIN; try{ localStorage.setItem(CAM_KEY,JSON.stringify(CAM)); }catch(e){} }
/* 配色テーマ（ダーク/グレー/ホワイト）: キャンバス側の色もここで切替 */
var LIGHTMAP={'#4dd7ff':'#0e7ea8','#ff5a76':'#c62f4f','#3ddc97':'#178a5b','#ffc14d':'#b07c10',
  '#b78cff':'#7d55c7','#ff9c6b':'#c9661f','#5b8cff':'#2f57c9','#ffe066':'#a08a10',
  '#a8e05f':'#5f9021','#f28ab5':'#c05585','#e8c49a':'#8a6f4d','#66d9c2':'#1d8a78','#33405c':'#c6ccd6'};
function colAdj(hex){ return CAM.theme==='light' ? (LIGHTMAP[hex]||hex) : hex; }
function themeC(){
  if(CAM.theme==='light') return {star:'#c3cddf', label:'#3c485c', labelDim:'#98a2b3',
    labelBg:'rgba(255,255,255,.75)', edgeRGB:'70,105,160', focusLabel:'#12202f', nbLabel:'#31445c'};
  if(CAM.theme==='gray') return {star:'#d6dce8', label:'#e6ebf4', labelDim:'#b3bac6',
    labelBg:'rgba(52,56,63,.72)', edgeRGB:'205,218,240', focusLabel:'#ffffff', nbLabel:'#e8eef8'};
  return {star:'#9db8e8', label:'#a9bcd6', labelDim:'#7b8ba3',
    labelBg:'rgba(6,10,16,.72)', edgeRGB:'122,168,228', focusLabel:'#eaf6ff', nbLabel:'#cfe0f5'};
}
function applyTheme(){
  document.documentElement.dataset.theme=CAM.theme;
  if(ST.length) refreshColors();
}
var W=0,H=0,DPR=1;
var cv=document.getElementById('cv'), ctx=cv.getContext('2d');

function resize(){
  var st=document.getElementById('stage');
  var vv=window.visualViewport;
  var pageZoom=vv?Math.max(1,vv.scale):1;   // ページごとピンチ拡大された時も高精細で描き直す
  DPR=Math.min(3.5,(window.devicePixelRatio||1)*pageZoom);
  var oldSc=(W>0&&H>0)?Math.min(W,H):0;
  W=st.clientWidth; H=st.clientHeight;
  /* パネル開閉などでキャンバス寸法が変わっても、画面中央に見ていた場所を保つ */
  if(oldSc>0){
    var k=Math.min(W,H)/oldSc;
    ox*=k; oy*=k;
  }
  cv.width=Math.round(W*DPR); cv.height=Math.round(H*DPR);
  ctx.setTransform(DPR,0,0,DPR,0,0);
}
window.addEventListener('resize',resize);
if(window.visualViewport){
  var vvT=null;
  window.visualViewport.addEventListener('resize',function(){
    clearTimeout(vvT); vvT=setTimeout(resize,120);
  });
}

/* ═══ projection ═══ */
function projAll(){
  var cy=Math.cos(rotY), sy=Math.sin(rotY), cx=Math.cos(rotX), sx=Math.sin(rotX);
  var sc=Math.min(W,H)*0.19*zoom;
  for(var i=0;i<ST.length;i++){
    var s=ST[i];
    var x=s.x*cy+s.z*sy, z=-s.x*sy+s.z*cy, y=s.y;
    var y2=y*cx-z*sx, z2=y*sx+z*cx;
    var f=4.6/(4.6-z2*0.85);
    s.px=W/2+x*f*sc+ox; s.py=H/2-y2*f*sc+oy; s.pz=z2; s.pf=f;
  }
}
function fogA(s){
  if(!CAM.fog) return 1;                       /* 霧なし */
  /* 奥(pzがマイナス)ほど薄く＝3Dの遠近感。手前は常にくっきり */
  return Math.max(0.16, Math.min(1, 1 + 0.3*s.pz*CAM.fog));
}
/* ═══ 球体スプライト: 色ごとに1回だけ描いてキャッシュ（3D風の点） ═══ */
var SPRITES={};
function _hex3(col){
  var m=/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.exec(col||'');
  if(!m) return null;
  var h=m[1];
  if(h.length===3) h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
  var v=parseInt(h,16);
  return [(v>>16)&255,(v>>8)&255,v&255];
}
function _mix(c,t,to){ return 'rgb('+c.map(function(x,i){return Math.round(x+(to[i]-x)*t);}).join(',')+')'; }
function sphereSprite(col){
  var sp=SPRITES[col];
  if(sp) return sp;
  var c=document.createElement('canvas'); c.width=c.height=32;
  var g=c.getContext('2d');
  var rgb=_hex3(col);
  if(rgb){
    /* 左上からの光: ハイライト → 本来色 → 陰 で球体に見せる */
    var grad=g.createRadialGradient(11.5,10.5,1.5,16,16,15.5);
    grad.addColorStop(0,_mix(rgb,0.85,[255,255,255]));
    grad.addColorStop(0.3,_mix(rgb,0.4,[255,255,255]));
    grad.addColorStop(0.68,'rgb('+rgb.join(',')+')');
    grad.addColorStop(1,_mix(rgb,0.55,[0,0,0]));
    g.fillStyle=grad;
  } else {
    g.fillStyle=col;
  }
  g.beginPath(); g.arc(16,16,15.5,0,Math.PI*2); g.fill();
  SPRITES[col]=c;
  return c;
}
function projPt(x,y,z){
  var cy=Math.cos(rotY), sy=Math.sin(rotY), cx=Math.cos(rotX), sx=Math.sin(rotX);
  var sc=Math.min(W,H)*0.19*zoom;
  var x2=x*cy+z*sy, z1=-x*sy+z*cy, y2=y*cx-z1*sx, z2=y*sx+z1*cx;
  var f=4.6/(4.6-z2*0.85);
  return {x:W/2+x2*f*sc+ox, y:H/2-y2*f*sc+oy, pz:z2, f:f};
}

/* ═══ colors & category filters ═══
   各モードは「カテゴリ分け」を持ち、凡例のタップで表示/非表示できる */
var MARKETS=[], FAVS=new Set(), HOLDS=new Set();
var MKTCOL={'プライム':'#4dd7ff','スタンダード':'#3ddc97','グロース':'#b78cff','札幌':'#ffc14d','福岡':'#ff9c6b'};
var HID={sec:{}, pick:{}, mine:{}, mkt:{}};
function catsOf(){
  if(MODE==='sec') return GROUPS.map(function(g,i){return {label:g, color:PAL[i%PAL.length]};});
  if(MODE==='pick') return [
    {label:'三層合格（今夜の厳選圏）', color:'#3ddc97'},
    {label:'高スコア候補', color:'#4dd7ff'},
    {label:'その他', color:'#33405c'}];
  if(MODE==='mine') return [
    {label:'★お気に入り', color:'#ffc14d'},
    {label:'持ち株', color:'#5b8cff'},
    {label:'★かつ持ち株', color:'#ff5a76'},
    {label:'その他', color:'#33405c'}];
  return MARKETS.map(function(m,i){return {label:m||'不明', color:MKTCOL[m]||PAL[i%PAL.length]};});
}
function catOf(s){
  if(MODE==='sec') return s.g;
  if(MODE==='pick') return s.tri?0:((s.score!=null&&s.score>=120)?1:2);
  if(MODE==='mine'){
    var f=FAVS.has(s.code), hh=HOLDS.has(s.code);
    return (f&&hh)?2:(f?0:(hh?1:3));
  }
  return s.m||0;
}
function isHidden(s){ return HID[MODE][s.cat]===true; }
function refreshColors(){
  var cats=catsOf();
  for(var i=0;i<ST.length;i++){
    var s=ST[i];
    s.cat=catOf(s);
    var base=(cats[s.cat]||cats[0]||{color:'#8fa0b8'}).color;
    s.dimc=(base==='#33405c');
    s.col=colAdj(base);
  }
  legend();
}
function loadMine(){
  FAVS=new Set(); HOLDS=new Set();
  try{ JSON.parse(localStorage.getItem('kabuobaa_favs')||'[]').forEach(function(c){FAVS.add(String(c));}); }catch(e){}
  try{ JSON.parse(localStorage.getItem('kabuobaa_holdmarks')||'[]').forEach(function(c){HOLDS.add(String(c));}); }catch(e){}
  try{ JSON.parse(localStorage.getItem('kabuobaa_holdings')||'[]').forEach(function(hd){ if(hd&&hd.code) HOLDS.add(String(hd.code)); }); }catch(e){}
}

var MODE_TITLE={sec:'業種カテゴリ', pick:'今夜の厳選', mine:'マイ銘柄（★・持ち株）', mkt:'市場区分'};
function legend(){
  var el=document.getElementById('legend'), h='';
  var hid=HID[MODE];
  var nHid=Object.keys(hid).filter(function(k){return hid[k];}).length;
  if(!CAM.legendOpen){
    el.innerHTML='<div class="row lgopen">凡例と見方 ▸'+(nHid?'（絞り込み中）':'')+'</div>';
    el.querySelector('.lgopen').addEventListener('click',function(){
      CAM.legendOpen=true; saveCam(); legend();
    });
    return;
  }
  h+='<div class="t lgh">凡例と見方 <span class="lgx">▾ たたむ</span></div>';
  h+='<div class="t">'+MODE_TITLE[MODE]+'（タップで表示/非表示）</div>';
  var cats=catsOf();
  for(var i=0;i<cats.length;i++)
    h+='<div class="row gtog'+(hid[i]?' off':'')+'" data-g="'+i+'"><span class="sw" style="background:'+colAdj(cats[i].color)+'"></span>'+esc(cats[i].label)+'</div>';
  h+='<div class="row lgall"><span class="lgbtn" data-a="show">全て表示</span><span class="lgbtn" data-a="hide">全て非表示</span></div>';
  if(MODE==='mine') h+='<div class="mean">★と持ち株は「今夜の厳選」「全銘柄台帳」で付けた印（この端末に保存）。「その他」を非表示にすると自分の銘柄だけの地図になります。</div>';
  h+='<div class="mean">この空間の見方: <b>近く＝体質と値動きが似ている</b> ・ 中心ほど平均的な銘柄、外側ほど個性が強い ・ 奥にある点は霧で薄く見えます（3D）</div>';
  el.innerHTML=h;
  el.querySelector('.lgx').addEventListener('click',function(){
    CAM.legendOpen=false; saveCam(); legend();
  });
  el.querySelectorAll('.gtog').forEach(function(r){
    r.addEventListener('click',function(){
      var g=+r.dataset.g; hid[g]=!hid[g]; legend();
    });
  });
  el.querySelectorAll('.lgbtn').forEach(function(b){
    b.addEventListener('click',function(){
      var cats2=catsOf();
      if(b.dataset.a==='show'){ HID[MODE]={}; }
      else { for(var k3=0;k3<cats2.length;k3++) hid[k3]=true; }
      legend();
    });
  });
}

/* ═══ stars ═══ */
var STARS=[];
for(var i=0;i<150;i++){
  var th=Math.random()*Math.PI*2, ph=Math.acos(Math.random()*2-1);
  STARS.push({x:Math.sin(ph)*Math.cos(th)*30, y:Math.cos(ph)*22, z:Math.sin(ph)*Math.sin(th)*30,
              tw:Math.random()*6.28, sz:Math.random()*1.1+0.4});
}

/* ═══ draw loop ═══ */
var order=[], lastSort=0;
function draw(ts){
  ctx.clearRect(0,0,W,H);
  var cy=Math.cos(rotY), sy=Math.sin(rotY);
  for(var i=0;i<STARS.length;i++){
    var st=STARS[i];
    var x=st.x*cy+st.z*sy, z=-st.x*sy+st.z*cy;
    if(z>0) continue;
    var px=W/2+x*10, py=H/2-st.y*10;
    if(px<-8||px>W+8||py<-8||py>H+8) continue;
    ctx.globalAlpha=0.08+0.08*Math.sin(ts*0.0012+st.tw);
    ctx.fillStyle=themeC().star;
    ctx.beginPath(); ctx.arc(px,py,st.sz,0,Math.PI*2); ctx.fill();
  }
  ctx.globalAlpha=1;
  if(!ST.length) return;
  projAll();
  if(CAM.axes) drawAxes();
  if(ts-lastSort>120){ order.sort(function(a,b){return ST[b].pz-ST[a].pz;}); lastSort=ts; }

  var focused=focusI>=0, fset=null;
  if(focused){
    fset={}; fset[focusI]=1;
    for(var e=0;e<fEdges.length;e++) fset[fEdges[e].j]=1;
  }
  /* 常時のつながり線（星座）: 強い類似ペアだけを淡く。フォーカス中は消して主役の糸に譲る。
     描画はアルファ3段階のバケツにまとめて3ストロークで済ませる（4000銘柄でも60fps） */
  if(CAM.links && !focused && EDGES.length){
    var EB0=[],EB1=[],EB2=[];
    for(var ei=0;ei<EDGES.length;ei++){
      var EA=ST[EDGES[ei][0]], EBv=ST[EDGES[ei][1]];
      if(!SHOW_EX&&(EA.ex||EBv.ex)) continue;
      if(isHidden(EA)||isHidden(EBv)) continue;
      if((EA.px<0&&EBv.px<0)||(EA.px>W&&EBv.px>W)||(EA.py<0&&EBv.py<0)||(EA.py>H&&EBv.py>H)) continue;
      var eal=(0.045+0.07*(EDGES[ei][2]-50)/50)*Math.min(fogA(EA),fogA(EBv));
      if(CAM.theme!=='dark') eal*=1.6;
      if(eal<0.02) continue;
      (eal<0.05?EB0:(eal<0.085?EB1:EB2)).push(EA.px,EA.py,EBv.px,EBv.py);
    }
    ctx.lineWidth=1.3;   /* つながり線は少し太く（ユーザ要望） */
    var bAls=[0.045,0.08,0.125], bArr=[EB0,EB1,EB2];
    var thEdge=themeC().edgeRGB;
    for(var bi=0;bi<3;bi++){
      var arr2=bArr[bi];
      if(!arr2.length) continue;
      ctx.strokeStyle='rgba('+thEdge+','+(bAls[bi]*(CAM.theme!=='dark'?1.7:1)).toFixed(3)+')';
      ctx.beginPath();
      for(var pi3=0;pi3<arr2.length;pi3+=4){
        ctx.moveTo(arr2[pi3],arr2[pi3+1]); ctx.lineTo(arr2[pi3+2],arr2[pi3+3]);
      }
      ctx.stroke();
    }
  }
  /* edges first */
  if(focused){
    var F=ST[focusI];
    /* 2ホップ（連想の連鎖）の薄い糸 */
    ctx.lineWidth=1.2;
    ctx.strokeStyle='rgba('+themeC().edgeRGB+',0.16)';
    ctx.beginPath();
    for(var e9=0;e9<f2Edges.length;e9++){
      var A9=ST[f2Edges[e9].a], B9=ST[f2Edges[e9].b];
      if((!SHOW_EX&&(A9.ex||B9.ex))||isHidden(A9)||isHidden(B9)) continue;
      ctx.moveTo(A9.px,A9.py); ctx.lineTo(B9.px,B9.py);
    }
    ctx.stroke();
    for(var e2=0;e2<fEdges.length;e2++){
      var ed=fEdges[e2], T=ST[ed.j];
      if(!SHOW_EX && T.ex) continue;
      if(isHidden(T)) continue;
      var g=ctx.createLinearGradient(F.px,F.py,T.px,T.py);
      g.addColorStop(0,rgbaOf(colAdj('#4dd7ff'),0.55));
      g.addColorStop(1,rgbaOf(T.col,0.75));
      ctx.strokeStyle=g; ctx.lineWidth=1.5+ed.sim/55;
      ctx.beginPath(); ctx.moveTo(F.px,F.py); ctx.lineTo(T.px,T.py); ctx.stroke();
      var t=(ts*0.0006+e2*0.17)%1;
      var mx=F.px+(T.px-F.px)*t, my=F.py+(T.py-F.py)*t;
      ctx.fillStyle=CAM.theme==='light'?'rgba(30,80,140,0.85)':'rgba(200,235,255,0.85)';
      ctx.beginPath(); ctx.arc(mx,my,1.6,0,Math.PI*2); ctx.fill();
    }
  }
  /* points: 全銘柄おなじ大きさの小さな球体（3D風スプライト）。
     - 拡大しても大きくならない（ぼやけ防止）
     - 縮小時だけ少し小さくなる（全景で点が潰れて重ならないように）
     - 球はスプライトに事前描画してdrawImageするので4000銘柄でも60fps */
  var R_DOT=1.9;
  var rz=R_DOT*Math.max(0.5,Math.min(1,Math.pow(zoom,0.45)));
  for(var oi=0;oi<order.length;oi++){
    var idx=order[oi], s=ST[idx];
    if(!SHOW_EX && s.ex) continue;
    if(isHidden(s)) continue;
    if(s.px<-20||s.px>W+20||s.py<-20||s.py>H+20) continue;
    var r=rz;
    var a=fogA(s)*(s.ex?0.45:1);
    if(focused) a*= fset[idx]? 1 : (fset2[idx]? 0.34 : 0.07);
    if(a<0.02) continue;
    if(focused&&fset&&fset[idx]){ r=rz*1.5; }  /* フォーカス時の関連銘柄のみ僅かに強調 */
    ctx.globalAlpha=a;
    ctx.drawImage(sphereSprite(s.col), s.px-r, s.py-r, r*2, r*2);
  }
  ctx.globalAlpha=1;
  /* 常時銘柄名: 大型・厳選銘柄から優先し、重なる場所には出さない */
  if(CAM.labels && !focused && prio.length){
    var cells={}, placed=0, maxL=((W<520)?30:60)+(zoom>3?30:0);
    ctx.font='600 9.5px "Hiragino Sans",sans-serif';
    /* 1周目=いまの色分けで強調されている銘柄、2周目=灰色カテゴリ（後回し） */
    for(var sweep=0;sweep<2&&placed<maxL;sweep++){
      for(var pi2=0;pi2<prio.length && placed<maxL;pi2++){
        var ls=ST[prio[pi2]];
        var dimc=!!ls.dimc;
        if(sweep===0?dimc:!dimc) continue;
        if((!SHOW_EX&&ls.ex)||isHidden(ls)) continue;
        if(ls.px<8||ls.px>W-8||ls.py<14||ls.py>H-6) continue;
        if(ls.pz<-1.7) continue;
        var gx=Math.floor(ls.px/94), gy=Math.floor(ls.py/26);
        if(cells[gx+'_'+gy]||cells[(gx+1)+'_'+gy]||cells[(gx-1)+'_'+gy]) continue;
        cells[gx+'_'+gy]=1; placed++;
        ctx.globalAlpha=Math.min(0.8, fogA(ls)*0.85+0.05)*(dimc?0.65:1);
        ctx.fillStyle=dimc?themeC().labelDim:themeC().label;
        ctx.fillText(ls.sn, ls.px+6, ls.py+3.5);
      }
    }
    ctx.globalAlpha=1;
  }
  /* hover highlight（マウス） */
  if(hoverI>=0 && hoverI!==focusI && ST[hoverI]){
    var hs=ST[hoverI];
    if(!(focused && !fset[hoverI] && !fset2[hoverI])){
      ctx.strokeStyle=CAM.theme==='light'?'rgba(20,40,70,.7)':'rgba(255,255,255,.75)'; ctx.lineWidth=1.2;
      ctx.beginPath(); ctx.arc(hs.px,hs.py,5.2,0,Math.PI*2); ctx.stroke();
      labelFor(hs,hs.name,11.5,themeC().focusLabel);
    }
  }
  /* focus label */
  if(focused){
    var FS=ST[focusI];
    var lr=3.4;
    ctx.strokeStyle=rgbaOf(colAdj('#4dd7ff'),0.9); ctx.lineWidth=1.4;
    ctx.beginPath(); ctx.arc(FS.px,FS.py,lr+3.5+Math.sin(ts*0.004)*1.2,0,Math.PI*2); ctx.stroke();
    labelFor(FS,FS.name,15,themeC().focusLabel);
    for(var e3=0;e3<fEdges.length;e3++){
      var TT=ST[fEdges[e3].j];
      if(!SHOW_EX && TT.ex) continue;
      if(isHidden(TT)) continue;
      labelFor(TT,TT.name,11.5,themeC().nbLabel);
    }
  }
}
var AXCOL=['#5b8cff','#3ddc97','#ffc14d'];
function drawAxes(){
  var basis=[[1,0,0],[0,1,0],[0,0,1]];
  for(var a=0;a<3;a++){
    var b=basis[a], L=2.75;
    var P1=projPt(-b[0]*L,-b[1]*L,-b[2]*L), P2=projPt(b[0]*L,b[1]*L,b[2]*L);
    ctx.strokeStyle=rgbaOf(AXCOL[a],0.34); ctx.lineWidth=1;
    ctx.setLineDash([5,5]);
    ctx.beginPath(); ctx.moveTo(P1.x,P1.y); ctx.lineTo(P2.x,P2.y); ctx.stroke();
    ctx.setLineDash([]);
    var ax=AXES[a]||{};
    var lab=ax.label||('軸'+(a+1));
    var plus=ax.plus?('→'+ax.plus):'';
    var minus=ax.minus?('→'+ax.minus):'';
    ctx.font='700 10px ui-monospace,Menlo,monospace';
    ctx.fillStyle=rgbaOf(AXCOL[a],0.85);
    ctx.fillText(lab+plus, P2.x+5, P2.y+3);
    if(minus){ ctx.fillStyle=rgbaOf(AXCOL[a],0.5); ctx.fillText(minus, P1.x+5, P1.y+3); }
  }
}
function labelFor(s,txt,fs,colr){
  ctx.font='700 '+fs+'px "Hiragino Sans",sans-serif';
  var w=ctx.measureText(txt).width;
  ctx.fillStyle=themeC().labelBg;
  ctx.fillRect(s.px+8, s.py-fs, w+10, fs+7);
  ctx.fillStyle=colr;
  ctx.fillText(txt, s.px+13, s.py+3);
}
function loop(ts){
  if(camGoal){
    var TW=Math.PI*2;
    var dyw=(((camGoal.y-rotY)%TW)+TW)%TW; if(dyw>Math.PI) dyw-=TW;
    rotY+=dyw*0.11; rotX+=(camGoal.x-rotX)*0.11; zoom+=(camGoal.z-zoom)*0.11;
    ox+=(0-ox)*0.11; oy+=(0-oy)*0.11;
    if(Math.abs(dyw)<0.01&&Math.abs(camGoal.x-rotX)<0.01&&Math.abs(camGoal.z-zoom)<0.02) camGoal=null;
  } else if(PTRS.size===0){
    /* 指を離した後の慣性（プロ仕様のヌルッと感） */
    if(Math.abs(velY)>0.00004||Math.abs(velX)>0.00004){
      rotY+=velY; rotX+=velX;   /* 上下も制限なし: 一回転できる */
      velY*=0.93; velX*=0.93;
    } else { velY=velX=0; }
    if(Math.abs(velPx)>0.12||Math.abs(velPy)>0.12){
      ox+=velPx; oy+=velPy; velPx*=0.9; velPy*=0.9;
    } else { velPx=velPy=0; }
    if(SPIN && zoom<2.2 && Date.now()-lastPointer>1600 && Date.now()>spinHoldUntil && !velY && !velPx){ rotY+=0.0016*CAM.spin; }
  }
  draw(ts||0);
  requestAnimationFrame(loop);
}

/* ═══ interaction: プロ仕様カメラ ═══
   1本指/左ドラッグ=軌道回転(慣性つき) ・ 2本指=視点の平行移動+ピンチ位置中心ズーム
   ホイール=カーソル中心ズーム ・ Shift/中/右ドラッグ=平行移動 ・ ダブルタップ=その場ズーム */
var lastPointer=0;
var PTRS=new Map(), gMode=null;
var lx=0, ly=0, moved=0;
var pc0=null, pd0=0;
var velY=0, velX=0, velPx=0, velPy=0;
var lastTapT=0, lastTapX=0, lastTapY=0;
function canvasXY(e){
  var r=cv.getBoundingClientRect();
  return [e.clientX-r.left, e.clientY-r.top];
}
function zoomAt(cx2,cy2,k){
  var nz=Math.max(0.3,Math.min(48,zoom*k)); k=nz/zoom;   /* かなり奥まで拡大できるように */
  ox=k*ox+(1-k)*(cx2-W/2);
  oy=k*oy+(1-k)*(cy2-H/2);
  zoom=nz;
}
function centroidDist(){
  var xs=0,ys=0,n=0,arr=[];
  PTRS.forEach(function(p){xs+=p.x; ys+=p.y; n++; arr.push(p);});
  var d=(n>=2)?Math.hypot(arr[0].x-arr[1].x,arr[0].y-arr[1].y):0;
  return {c:[xs/n,ys/n], d:d};
}
cv.addEventListener('pointerdown',function(e){
  var xy=canvasXY(e);
  PTRS.set(e.pointerId,{x:xy[0],y:xy[1]});
  try{ cv.setPointerCapture(e.pointerId); }catch(err){}
  velY=velX=velPx=velPy=0;
  if(PTRS.size===1){
    gMode=(e.shiftKey||e.button===1||e.button===2)?'panBtn':'orbit';
    lx=xy[0]; ly=xy[1]; moved=0;
  } else {
    gMode='pan2'; var cd=centroidDist(); pc0=cd.c; pd0=cd.d; camGoal=null;
  }
  cv.classList.add('drag'); lastPointer=Date.now();
});
function nearestAt(x,y,maxd2){
  var best=-1, bd=maxd2;
  for(var i=0;i<ST.length;i++){
    var s=ST[i];
    if(!SHOW_EX&&s.ex) continue;
    if(isHidden(s)) continue;
    var d=(s.px-x)*(s.px-x)+(s.py-y)*(s.py-y);
    if(d<bd){ bd=d; best=i; }
  }
  return best;
}
cv.addEventListener('pointerleave',function(){ hoverI=-1; });
cv.addEventListener('pointermove',function(e){
  if(PTRS.size===0 && e.pointerType==='mouse'){
    var hxy=canvasXY(e);
    hoverI=nearestAt(hxy[0],hxy[1],460);   /* マウス追従ハイライト（PCのプロ感） */
    return;
  }
  if(!PTRS.has(e.pointerId)) return;
  var xy=canvasXY(e);
  PTRS.set(e.pointerId,{x:xy[0],y:xy[1]});
  if(gMode==='orbit'&&PTRS.size===1){
    var dx=xy[0]-lx, dy=xy[1]-ly;
    moved+=Math.abs(dx)+Math.abs(dy);
    if(moved>6) camGoal=null;
    var dirX=CAM.invX?1:-1, dirY=CAM.invY?1:-1;
    var fine=1/Math.max(1,Math.pow(zoom,0.35));   /* 拡大中は回転を細かく */
    var ry=dx*0.0058*CAM.sens*dirX*fine, rx=dy*0.0046*CAM.sens*dirY*fine;
    rotY+=ry; rotX+=rx;   /* 上下も制限なし: 一回転できる */
    velY=ry*0.85; velX=rx*0.85;
    lx=xy[0]; ly=xy[1];
  } else if(gMode==='panBtn'&&PTRS.size===1){
    var dx2=xy[0]-lx, dy2=xy[1]-ly;
    moved+=Math.abs(dx2)+Math.abs(dy2); camGoal=null;
    ox+=dx2; oy+=dy2; velPx=dx2*0.85; velPy=dy2*0.85;
    lx=xy[0]; ly=xy[1];
  } else if(PTRS.size>=2){
    gMode='pan2';
    var cd=centroidDist();
    if(pc0){
      ox+=cd.c[0]-pc0[0]; oy+=cd.c[1]-pc0[1];
      velPx=(cd.c[0]-pc0[0])*0.85; velPy=(cd.c[1]-pc0[1])*0.85;
      if(pd0>0&&cd.d>0) zoomAt(cd.c[0],cd.c[1],cd.d/pd0);
    }
    pc0=cd.c; pd0=cd.d; camGoal=null;
  }
  lastPointer=Date.now();
});
function endPointer(e, cancelled){
  var xy=canvasXY(e);
  PTRS.delete(e.pointerId);
  if(PTRS.size===1){
    /* 2本→1本: 置き直すまで回転させない（ピンチ後の誤回転防止） */
    gMode='hold'; velPx=velPy=0;
  }
  if(PTRS.size===0){
    if(!cancelled && gMode==='orbit' && moved<7){
      var now=Date.now();
      if(now-lastTapT<330 && Math.hypot(xy[0]-lastTapX,xy[1]-lastTapY)<44){
        zoomAt(xy[0],xy[1],1.8); lastTapT=0;   /* ダブルタップ=その場ズーム */
      } else {
        tapAt(e.clientX,e.clientY);
        lastTapT=now; lastTapX=xy[0]; lastTapY=xy[1];
      }
      velY=velX=velPx=velPy=0;
    }
    gMode=null; pc0=null; pd0=0;
    cv.classList.remove('drag');
  }
  lastPointer=Date.now();
}
cv.addEventListener('pointerup',function(e){ endPointer(e,false); });
cv.addEventListener('pointercancel',function(e){ endPointer(e,true); });
cv.addEventListener('contextmenu',function(e){ e.preventDefault(); });
cv.addEventListener('wheel',function(e){
  e.preventDefault(); camGoal=null;
  var xy=canvasXY(e);
  zoomAt(xy[0],xy[1],Math.pow(1.0015,-e.deltaY));
  lastPointer=Date.now();
},{passive:false});

function tapAt(cx2,cyy){
  var rect=cv.getBoundingClientRect();
  var best=nearestAt(cx2-rect.left, cyy-rect.top, 900);
  if(best>=0) focusOn(best); else clearFocus();
}

/* ═══ focus & panel ═══ */
function whyTags(flags){
  var out=[], bits=[16,1,4,8,2,32,64,128];
  for(var b=0;b<bits.length;b++){
    if(flags & bits[b]){
      var lab=WHY[String(bits[b])];
      if(lab) out.push(lab);
    }
    if(out.length>=3) break;
  }
  return out;
}
function focusOn(i, fly){
  if(fly===undefined) fly=true;
  focusI=i; fEdges=[]; f2Edges=[]; fset2={}; hoverI=-1;
  spinHoldUntil=Date.now()+8000;
  /* 発想の旅（たどった履歴） */
  var ti=TRAIL.indexOf(i);
  if(ti>=0) TRAIL.splice(ti,1);
  TRAIL.push(i);
  if(TRAIL.length>8) TRAIL.shift();
  var s=ST[i];
  /* 最後に見た銘柄を記憶 → 次にタブへ戻ってきたとき同じ銘柄から再開できる */
  try{ localStorage.setItem('kabuobaa_map_last', s.code); }catch(e){}
  /* どの経路でも: カメラはその銘柄が中央に来るよう寄って拡大。
     ✕で外してもこのカメラ位置に留まる（clearFocusはカメラに触れない） */
  if(fly){
    /* 一回転後でもまっすぐ最短で飛べるよう、縦回転を±πに正規化してから飛行 */
    rotX=((rotX%(2*Math.PI))+3*Math.PI)%(2*Math.PI)-Math.PI;
    var hxz=Math.hypot(s.x,s.z)||1e-6;
    camGoal={y:Math.atan2(-s.x,s.z),
             x:Math.max(-1.3,Math.min(1.3,Math.atan2(s.y,hxz))),
             z:Math.max(zoom,2.0)};   /* 現在の拡大率より引くことはしない */
  }
  for(var k=0;k<s.nb.length;k+=3){
    var j=byCode[s.nb[k]];
    if(j!=null) fEdges.push({j:j, sim:s.nb[k+1], flags:s.nb[k+2]});
  }
  /* 2ホップ先（連想の連鎖）: 隣の銘柄からさらに繋がる先を薄く光らせる */
  var inRing={}; inRing[i]=1;
  fEdges.forEach(function(ed){ inRing[ed.j]=1; });
  for(var e0=0;e0<fEdges.length&&f2Edges.length<36;e0++){
    var jn=ST[fEdges[e0].j].nb;
    for(var k0=0, c0=0;k0<jn.length&&c0<2;k0+=3){
      var kk=byCode[jn[k0]];
      if(kk==null||inRing[kk]) continue;
      f2Edges.push({a:fEdges[e0].j, b:kk}); fset2[kk]=1; c0++;
    }
  }
  var facts=[];
  if(s.close!=null) facts.push('終値 '+s.close.toLocaleString()+'円');
  if(s.pbr!=null) facts.push('PBR '+s.pbr+'倍');
  if(s.msize!=null){
    var oku=Math.pow(10,s.msize);
    facts.push('時価総額 '+(oku>=10000?(oku/10000).toFixed(1)+'兆円':Math.round(oku).toLocaleString()+'億円'));
  }
  if(s.score!=null) facts.push('総合 '+s.score+'点');
  if(s.tob!=null) facts.push('TOB素地 '+s.tob+'点');
  if(s.drop!=null) facts.push('高値から −'+s.drop+'%');
  var mchip = s.tri? '<span class="pf" style="color:#3ddc97;border-color:rgba(61,220,151,.4)">三層合格（今夜の厳選圏）</span>':'';
  var exchip = s.ex? '<span class="pf" style="color:#ffc14d;border-color:rgba(255,193,77,.4)">今夜の判定: 除外・対象外</span>':'';
  var trailH='';
  if(TRAIL.length>1){
    trailH='<div class="trailrow"><span class="trlab">発想の旅:</span>'+TRAIL.slice(0,-1).map(function(t2){
      return '<span class="tchip" data-i="'+t2+'">'+esc(ST[t2].sn||ST[t2].name)+'</span>';
    }).join('')+'</div>';
  }
  var h='<div class="pnav"><button class="pnv" id="pprev">‹ 前の銘柄</button>'
    +'<button class="pnv" id="pnext">次の銘柄 ›</button></div>'
    +trailH+'<div class="pn">'+esc(s.name)+'</div>'
    +'<div class="pc">'+s.code+' ・ '+esc(GROUPS[s.g]||'')+'</div>'
    +'<div class="pfacts">'+facts.map(function(f){return '<span class="pf">'+f+'</span>';}).join('')+mchip+exchip+'</div>'
    +'<div class="ph">◈ 発想が繋がる銘柄（似ている順）</div>';
  for(var e=0;e<fEdges.length;e++){
    var t=ST[fEdges[e].j];
    var tags=whyTags(fEdges[e].flags);
    var exwarn = t.ex? '<span class="wt" style="color:#ffc14d;background:rgba(255,193,77,.1);border-color:rgba(255,193,77,.3)">今夜の判定は除外・対象外</span>' : '';
    var nf=[];
    if(t.pbr!=null) nf.push('PBR '+t.pbr);
    if(t.score!=null) nf.push('総合 '+t.score+'点');
    if(t.close!=null) nf.push(t.close.toLocaleString()+'円');
    h+='<div class="nb" data-i="'+fEdges[e].j+'">'
      +'<span class="sim">'+fEdges[e].sim+'%</span>'
      +'<div class="nbn">'+esc(t.name)+'<small>'+esc(t.code)+'</small></div>'
      +(nf.length?'<div class="nbf">'+nf.join(' ・ ')+'</div>':'')
      +'<div class="why">'+tags.map(function(w,ix){return '<span class="wt'+(ix===0&&(fEdges[e].flags&16)?' mv':'')+'">'+esc(w)+'</span>';}).join('')+exwarn+'</div>'
      +'</div>';
  }
  h+='<div class="plinks">'
    +'<a class="plink" href="universe.html?q='+s.code+'">全銘柄台帳で見る</a>'
    +'<a class="plink" href="caps.html?c='+s.code+'">時価総額マップ</a>'
    +'<a class="plink y" href="https://finance.yahoo.co.jp/quote/'+s.code+'.T" target="_blank" rel="noopener">Yahoo! →</a>'
    +'</div>';
  var pc=document.getElementById('pcontent');
  pc.innerHTML=h;
  pc.querySelectorAll('.nb').forEach(function(el){
    el.addEventListener('click',function(){ focusOn(+el.dataset.i); });
  });
  pc.querySelectorAll('.tchip').forEach(function(el){
    el.addEventListener('click',function(){ focusOn(+el.dataset.i); });
  });
  document.getElementById('pprev').addEventListener('click',function(){ focusStep(-1); });
  document.getElementById('pnext').addEventListener('click',function(){ focusStep(1); });
  var pnl=document.getElementById('panel');
  pnl.classList.remove('ghost'); pnl.classList.add('show');
  document.body.classList.add('focused');
  setTimeout(resize,200);
}
function clearFocus(){
  focusI=-1; fEdges=[]; f2Edges=[]; fset2={};
  camGoal=null; lastPointer=Date.now();
  spinHoldUntil=Date.now()+8000;   /* カメラは今の場所のまま・自動回転もしばらく再開しない */
  document.body.classList.remove('focused');
  /* パネルは閉じずに「空間」として残す（showのまま中身だけ隠す）
     → キャンバスの寸法が一切変わらないので、点が引き伸ばされるバグが起きない */
  var pn=document.getElementById('panel');
  pn.classList.remove('show'); pn.classList.add('ghost');
}
document.getElementById('pclose').addEventListener('click',clearFocus);
/* フォーカスを前後の銘柄へ移す（キーボード矢印 と パネルの前へ/次へボタン） */
function focusStep(dir){
  if(!ST.length) return;
  var n=ST.length, i=(focusI>=0)?focusI:-1;
  for(var k=1;k<=n;k++){
    var j=((i+dir*k)%n+n)%n;
    var s=ST[j];
    if(isHidden(s)) continue;
    if(!SHOW_EX && s.ex) continue;
    focusOn(j);
    return;
  }
}
document.addEventListener('keydown',function(e){
  var ae=document.activeElement;
  if(ae&&(ae.tagName==='INPUT'||ae.tagName==='TEXTAREA')) return;
  if(e.key==='ArrowRight'||e.key==='ArrowDown'){ e.preventDefault(); focusStep(1); }
  else if(e.key==='ArrowLeft'||e.key==='ArrowUp'){ e.preventDefault(); focusStep(-1); }
  else if(e.key==='Escape'&&focusI>=0){ clearFocus(); }
});
function esc(t){return String(t).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}

/* ═══ search ═══ */
function normQ(s){
  var t=(s||'').normalize('NFKC').toLowerCase();
  t=t.replace(/[ぁ-ゖ]/g,function(ch){return String.fromCharCode(ch.charCodeAt(0)+0x60);});
  t=t.replace(/[\s\-・.,、。()（）\[\]「」『』&\/]/g,'');
  ['ホールディングス','ホールディング','グループ','株式会社','hd'].forEach(function(w){t=t.split(w).join('');});
  return t;
}
var qEl=document.getElementById('q'), sugg=document.getElementById('sugg');
qEl.addEventListener('input',function(){
  var v=normQ(qEl.value.trim());
  if(!v){ sugg.classList.remove('show'); return; }
  var hits=[];
  for(var i=0;i<ST.length;i++){
    if(ST[i].norm.indexOf(v)>=0||ST[i].code.indexOf(v)>=0) hits.push(i);
  }
  if(!hits.length){ sugg.innerHTML='<div class="cnt-it">0件（表記ゆれ・コードでもお試しを）</div>'; sugg.classList.add('show'); return; }
  var CAP=80;
  var h2='<div class="cnt-it">'+hits.length+'件ヒット'+(hits.length>CAP?('・先頭'+CAP+'件を表示（さらに絞り込めます）'):'')+'</div>';
  h2+=hits.slice(0,CAP).map(function(i){
    return '<div class="it" data-i="'+i+'">'+esc(ST[i].name)+'<small>'+ST[i].code+'</small></div>';
  }).join('');
  sugg.innerHTML=h2;
  sugg.classList.add('show');
  sugg.querySelectorAll('.it').forEach(function(el){
    el.addEventListener('click',function(){
      sugg.classList.remove('show'); qEl.value='';
      focusOn(+el.dataset.i);
    });
  });
});
qEl.addEventListener('keydown',function(e){
  if(e.key==='Enter'){
    var it=sugg.querySelector('.it');
    if(it){ sugg.classList.remove('show'); qEl.value=''; qEl.blur(); focusOn(+it.dataset.i); }
  }
});
document.addEventListener('click',function(e){
  if(!e.target.closest('.srchwrap')) sugg.classList.remove('show');
});

/* ═══ toolbar ═══ */
document.querySelectorAll('#modes button').forEach(function(b){
  b.addEventListener('click',function(){
    document.querySelectorAll('#modes button').forEach(function(x){x.classList.remove('on');});
    b.classList.add('on'); MODE=b.dataset.m; refreshColors();
  });
});
document.getElementById('spin').addEventListener('click',function(){
  SPIN=!SPIN; this.classList.toggle('on',SPIN); saveCam();
});
document.getElementById('reset').addEventListener('click',function(){
  rotY=0.6; rotX=0.32; zoom=1; ox=0; oy=0;
  velY=velX=velPx=velPy=0; camGoal=null;
});
document.getElementById('showex').addEventListener('click',function(){
  SHOW_EX=!SHOW_EX; this.classList.toggle('on',SHOW_EX);
});

/* hint: ✕で閉じる・20秒で自動で薄くなる */
(function(){
  var hx=document.getElementById('hintx'), hb=document.getElementById('hint');
  if(hx) hx.addEventListener('click',function(){ hb.style.display='none'; });
  setTimeout(function(){ if(hb) hb.style.opacity='0.35'; }, 20000);
})();

/* ═══ camera settings sheet ═══ */
var sheet=document.getElementById('camsheet');
function paintCam(){
  sheet.querySelectorAll('.seg').forEach(function(sg){
    var k=sg.dataset.k, cur=String(CAM[k]);
    sg.querySelectorAll('button').forEach(function(b){
      b.classList.toggle('on', b.dataset.v===cur);
    });
  });
}
sheet.querySelectorAll('.seg button').forEach(function(b){
  b.addEventListener('click',function(){
    var k=b.closest('.seg').dataset.k, v=b.dataset.v;
    CAM[k]=(v==='true')?true:(v==='false')?false:(isNaN(parseFloat(v))?v:parseFloat(v));
    if(k==='theme') applyTheme();
    saveCam(); paintCam();
  });
});
document.getElementById('camset').addEventListener('click',function(){
  paintCam(); sheet.style.display='flex';
});
var howEl=document.getElementById('howsheet');
document.getElementById('howbtn').addEventListener('click',function(){ howEl.style.display='flex'; });
document.getElementById('howclose').addEventListener('click',function(){ howEl.style.display='none'; });
howEl.addEventListener('click',function(e){ if(e.target===howEl) howEl.style.display='none'; });
document.getElementById('camclose').addEventListener('click',function(){ sheet.style.display='none'; });
sheet.addEventListener('click',function(e){ if(e.target===sheet) sheet.style.display='none'; });

/* キーボード: Esc=解除 ・ / =検索へ */
document.addEventListener('keydown',function(e){
  if(e.key==='Escape'){ clearFocus(); }
  else if(e.key==='/'&&document.activeElement!==qEl){ e.preventDefault(); qEl.focus(); }
});

/* ═══ intro ═══ */
var INTRO_KEY='kabuobaa_map_intro';
function maybeIntro(){
  var skip=false;
  try{ skip=localStorage.getItem(INTRO_KEY)==='1'; }catch(e){}
  if(!skip){ document.getElementById('intro').style.display='flex'; }
}
document.getElementById('gostart').addEventListener('click',function(){
  document.getElementById('intro').style.display='none';
});
document.getElementById('nointro').addEventListener('click',function(){
  try{ localStorage.setItem(INTRO_KEY,'1'); }catch(e){}
  document.getElementById('intro').style.display='none';
});

/* ═══ load ═══ */
fetch('map.json').then(function(r){
  if(!r.ok) throw new Error('map.jsonがまだ生成されていません');
  return r.json();
}).then(function(j){
  DATA=j; GROUPS=j.groups||[]; WHY=j.why||{}; AXES=j.axes||[]; MARKETS=j.markets||[];
  loadMine();
  if(j.dims){ var dn=document.getElementById('dimN'); if(dn) dn.textContent=j.dims; }
  document.getElementById('spin').classList.toggle('on',SPIN);
  ST=j.stocks.map(function(a,i){
    var szRaw=a[7]; var size=szRaw==null?0.5:Math.max(0.2,(szRaw-1.2)*0.75);
    return {code:a[0], name:a[1], g:a[2], m:a[3], x:a[4], y:a[5], z:a[6],
      size:size, msize:szRaw, pbr:a[8], score:a[9], tob:a[10], tri:a[11]===1,
      close:a[12], drop:a[13], status:a[14], nb:a[15]||[],
      ex:(a[14]==='dead'||a[14]==='skip'), norm:normQ(a[1]), col:'#888', px:0,py:0,pz:0,pf:1};
  });
  byCode={}; ST.forEach(function(s,i){byCode[s.code]=i;});
  order=ST.map(function(_,i){return i;});
  ST.forEach(function(s){ s.sn=(s.name.length>8)?(s.name.slice(0,8)+'…'):s.name; });
  prio=ST.map(function(_,i){return i;}).sort(function(a,b){
    return (ST[b].tri-ST[a].tri) || (ST[b].size-ST[a].size);
  });
  /* つながり線: 各銘柄の上位3近傍のうち類似度が高いペアだけ（多すぎる場合は自動で間引き） */
  (function(){
    var thr=(ST.length>1500)?60:50;
    while(true){
      EDGES=[]; var seen={};
      for(var i2=0;i2<ST.length;i2++){
        var nb2=ST[i2].nb, cnt3=0;
        for(var k2=0;k2<nb2.length&&cnt3<2;k2+=3){
          var j2=byCode[nb2[k2]];
          if(j2==null||nb2[k2+1]<thr) continue;
          var key2=(i2<j2)?(i2+'_'+j2):(j2+'_'+i2);
          if(seen[key2]) continue;
          seen[key2]=1; EDGES.push([i2,j2,nb2[k2+1]]); cnt3++;
        }
      }
      if(EDGES.length<=5000||thr>=95) break;
      thr+=5;
    }
  })();
  var gTime='';
  try{ var gd=new Date(j.generated_at); gTime=' ・ '+(gd.getMonth()+1)+'/'+gd.getDate()+' '+('0'+gd.getHours()).slice(-2)+':'+('0'+gd.getMinutes()).slice(-2)+'時点'; }catch(e){}
  document.getElementById('cnt').textContent=ST.length.toLocaleString()+' STOCKS'+gTime;
  document.getElementById('loading').style.display='none';
  refreshColors();
  resize();
  var mq=new URLSearchParams(location.search).get('c');
  if(mq&&byCode[mq]!=null){ focusOn(byCode[mq]); }
  else {
    /* URL指定がなければ、前回フォーカスしていた銘柄を初期表示として復元 */
    var last=null;
    try{ last=localStorage.getItem('kabuobaa_map_last'); }catch(e){}
    if(last&&byCode[last]!=null){ focusOn(byCode[last]); }
    else { maybeIntro(); }
  }
}).catch(function(e){
  document.getElementById('loading').textContent='⚠ '+e.message;
});
resize();
requestAnimationFrame(loop);
})();
</script>
</body>
</html>
"""


def render_map(map_n, dt):
    """関連銘柄マップページ（map.json を読む独立アプリ。ダーク宇宙テーマ）"""
    return MAP_TEMPLATE


# ------------------------------------------------------------
# 仮想実行: 「◎になったら翌日の始値で100株買い、+5000円の指値で売る」
# を過去1年の日足でなぞる（検証レポート用）
# ------------------------------------------------------------
SL_VARIANTS = [None, 3.0, 5.0, 8.0, 10.0, 15.0]  # 検証レポートで比較する損切り%（None=なし）


def simulate_variants(days, variants=None):
    """買いシグナルを1回計算し、損切り%のバリアントごとに取引リストを返す"""
    variants = variants or SL_VARIANTS
    n = CONFIG["RECENT_DAYS"]
    out = {v: [] for v in variants}
    if len(days) < n + 5:
        return out
    opens = [d["open"] for d in days]
    highs = [d["high"] for d in days]
    lows = [d["low"] for d in days]
    closes = [d["close"] for d in days]

    signal_days = set()
    for i in range(n, len(days) - 1):
        h20 = max(highs[i - n + 1:i + 1])
        c = closes[i]
        if h20 > 0 and c >= CONFIG["MIN_PRICE"] and (h20 - c) / h20 * 100 >= CONFIG["CHEAP_PCT"]:
            signal_days.add(i + 1)  # 翌営業日の始値で買う

    tp = CONFIG["TP_PCT"] / 100
    for v in variants:
        trades, pos = [], None
        for i in range(n, len(days)):
            if pos is None and i in signal_days:
                pos = {"buy_i": i, "buy": opens[i]}
            if pos is not None and i >= pos["buy_i"]:
                buy = pos["buy"]
                if v is not None and lows[i] <= buy * (1 - v / 100):
                    # 損切り（逆指値）成立を優先判定（保守的な仮定）
                    trades.append({"buy_date": days[pos["buy_i"]]["date"],
                                   "sell_date": days[i]["date"],
                                   "held": max(1, i - pos["buy_i"] + 1),
                                   "pnl": -buy * (v / 100) * 100, "stop": True})
                    pos = None
                elif highs[i] >= buy * (1 + tp):
                    trades.append({"buy_date": days[pos["buy_i"]]["date"],
                                   "sell_date": days[i]["date"],
                                   "held": max(1, i - pos["buy_i"] + 1),
                                   "pnl": buy * tp * 100, "stop": False})
                    pos = None
        if pos is not None:
            trades.append({"buy_date": days[pos["buy_i"]]["date"],
                           "sell_date": None,
                           "held": len(days) - 1 - pos["buy_i"],
                           "pnl": (closes[-1] - pos["buy"]) * 100, "stop": False})
        out[v] = trades
    return out



def fetch_tdnet(session, code, days_back=30, limit=3):
    """[{date, title, url}] を返す。取得できなければ空リスト（ページには出ない）"""
    try:
        url = f"https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.json"
        resp = session.get(url, params={"limit": 10}, timeout=15,
                           headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        items = (resp.json() or {}).get("items") or []
        cutoff = datetime.now(JST) - timedelta(days=days_back)
        out = []
        for it in items:
            td = it.get("Tdnet") or {}
            pub = td.get("pubdate", "")
            title = (td.get("title") or "").strip()
            doc = td.get("document_url") or ""
            try:
                dt2 = datetime.strptime(pub[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
            except ValueError:
                continue
            if dt2 >= cutoff and title:
                out.append({"date": f"{dt2.month}/{dt2.day}", "title": title, "url": doc})
            if len(out) >= limit:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------
# J-Quants（JPX公式）から財務素材（EPS・BPS・純資産・発行株数）を取得
# 認証はメール+パスワードから毎回トークンを自動発行（リフレッシュトークンの1週間期限に依存しない）
# 全銘柄分を1銘柄1リクエストで取ると重いので、日次でまとめて取れる date 指定を使い、
# 直近の決算開示日ぶんを走査して素材キャッシュ（fund_cache.json）に積み上げる
# ------------------------------------------------------------
def jquants_headers():
    """V2認証: APIキーをヘッダーに載せるだけ（期限切れなし）"""
    key = os.environ.get("JQUANTS_API_KEY", "").strip() or os.environ.get("JQUANTS_REFRESH_TOKEN", "").strip()
    if not key:
        return None
    return {"x-api-key": key}


def _to_float(v):
    try:
        return float(v) if v not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def _pick(d, *names):
    """複数候補のフィールド名から最初に存在するものの値を返す（V2略称・V1名の両対応）"""
    for n in names:
        if n in d and d[n] not in (None, "", "-"):
            return d[n]
    return None


# V2/V1 のフィールド名候補（応答の実名は初回ログで確認し、必要なら追加する）
JQ_F = {
    "assets":   ("TA", "TotalAssets"),
    "sales":    ("Sales", "NetSales"),
    "op":       ("OP", "OperatingProfit"),
    "np":       ("NP", "Profit", "NetIncome"),
    "div_fy":   ("DivFY", "ResultDividendPerShareAnnual"),
    "fdiv_fy":  ("FDivFY", "ForecastDividendPerShareAnnual"),
    "code":     ("Code", "LocalCode"),
    "disc":     ("DiscDate", "DisclosedDate", "DisclosureDate"),
    "eps":      ("EPS", "EarningsPerShare"),
    "bps":      ("BPS", "BookValuePerShare"),
    "equity":   ("Eq", "Equity", "NetAssets"),
    "shares":   ("ShOutFY", "ShOut", "IssuedShares", "AvgSh",
                 "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock"),
    "treasury": ("TrShFY", "TrSh", "TreasuryShares", "NumberOfTreasuryStockAtTheEndOfFiscalYear"),
    "fc_eps":   ("FEPS", "ForecastEPS", "ForecastEarningsPerShare"),
    "period":   ("CurPerEn", "CurrentPeriodEndDate"),
    "pertype":  ("CurPerTy", "TypeOfCurrentPeriod", "CurrentPeriodType"),
    "ordp":     ("OrdP", "OrdinaryProfit", "OrdinaryIncome"),
}


def jquants_fetch_materials(session, days_back=100):
    """V2 /fins/summary を開示日ごとに走査し {code: {eps,bvps,shares,equity,asof, hist}} を返す。
    hist = {期末日|期区分: {sales, op, ordp, np, ty, disc}} — 業績チャート用の履歴（蓄積される）。
    同一銘柄は最新の開示を採用。無料プランは12週遅延のため長めに走査する"""
    headers = jquants_headers()
    if not headers:
        return {}
    out = {}
    hist_out = {}
    today = datetime.now(JST).date()
    n_req = 0
    printed_fields = False
    for i in range(days_back):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        ds = d.strftime("%Y%m%d")  # V2は YYYYMMDD
        pagination = None
        while True:
            params = {"date": ds}
            if pagination:
                params["pagination_key"] = pagination
            try:
                r = session.get("https://api.jquants.com/v2/fins/summary",
                                params=params, headers=headers, timeout=60)
                n_req += 1
                if r.status_code == 429:
                    time.sleep(3)
                    continue
                if r.status_code in (401, 403):
                    print(f"  ! J-Quants認証エラー HTTP {r.status_code}: APIキー（JQUANTS_API_KEY）を確認してください "
                          f"{r.text[:120]}", file=sys.stderr)
                    return {}
                if r.status_code != 200:
                    break
                j = r.json()
            except Exception as e:  # noqa: BLE001
                print(f"  ! J-Quants取得エラー({ds}): {e}", file=sys.stderr)
                break
            rows = j.get("data") or j.get("statements") or []
            if rows and not printed_fields:
                print(f"  J-Quants応答フィールド例: {sorted(rows[0].keys())[:60]}")
                printed_fields = True
            for st in rows:
                code = str(_pick(st, *JQ_F["code"]) or "").strip()
                if len(code) == 5 and code[-1] == "0":
                    code = code[:-1]      # 5桁表記（末尾0）→4桁
                if not code:
                    continue
                disclosed = str(_pick(st, *JQ_F["disc"]) or ds)
                if len(disclosed) == 8 and disclosed.isdigit():
                    disclosed = f"{disclosed[:4]}-{disclosed[4:6]}-{disclosed[6:]}"
                prev = out.get(code)
                if prev and prev.get("asof", "") >= disclosed:
                    continue
                eps = _to_float(_pick(st, *JQ_F["eps"]))
                bvps = _to_float(_pick(st, *JQ_F["bps"]))
                equity = _to_float(_pick(st, *JQ_F["equity"]))
                shares = _to_float(_pick(st, *JQ_F["shares"]))
                treasury = _to_float(_pick(st, *JQ_F["treasury"]))
                if shares and treasury:
                    shares = shares - treasury
                fc_eps = _to_float(_pick(st, *JQ_F["fc_eps"]))
                assets = _to_float(_pick(st, *JQ_F["assets"]))
                sales = _to_float(_pick(st, *JQ_F["sales"]))
                op = _to_float(_pick(st, *JQ_F["op"]))
                np_ = _to_float(_pick(st, *JQ_F["np"]))
                div_fy = _to_float(_pick(st, *JQ_F["div_fy"]))
                fdiv_fy = _to_float(_pick(st, *JQ_F["fdiv_fy"]))
                if bvps is None and equity and shares:
                    bvps = equity / shares
                mat = {k: v for k, v in (("eps", eps), ("bvps", bvps), ("shares", shares),
                                          ("equity", equity), ("fc_eps", fc_eps),
                                          ("assets", assets), ("sales", sales), ("op", op),
                                          ("np", np_), ("div_fy", div_fy), ("fdiv_fy", fdiv_fy)) if v is not None}
                if mat:
                    mat["asof"] = disclosed
                    out[code] = mat
                # 業績履歴（チャート用）: 期末日と期区分ごとに売上・利益を記録
                per_end = str(_pick(st, *JQ_F["period"]) or "").strip()
                per_ty = str(_pick(st, *JQ_F["pertype"]) or "").strip()
                ordp = _to_float(_pick(st, *JQ_F["ordp"]))
                if per_end and (sales is not None or np_ is not None or op is not None):
                    if len(per_end) == 8 and per_end.isdigit():
                        per_end = f"{per_end[:4]}-{per_end[4:6]}-{per_end[6:]}"
                    hkey = f"{per_end}|{per_ty or '?'}"
                    hrec = {k: v for k, v in (("sales", sales), ("op", op),
                                              ("ordp", ordp), ("np", np_)) if v is not None}
                    if hrec:
                        hrec["ty"] = per_ty or "?"
                        hrec["disc"] = disclosed
                        prev_h = hist_out.setdefault(code, {}).get(hkey)
                        if not prev_h or prev_h.get("disc", "") <= disclosed:
                            hist_out[code][hkey] = hrec
            pagination = j.get("pagination_key")
            if not pagination:
                break
        time.sleep(0.15)
    n_hist = sum(len(v) for v in hist_out.values())
    for code, h in hist_out.items():
        out.setdefault(code, {"asof": ""})["hist"] = h
    print(f"  J-Quants: {n_req}リクエストで {len(out)}銘柄の財務素材（業績履歴 {n_hist}期分）を取得")
    return out


FUND_CACHE_PATH = DOCS / "fund_cache.json"


def yahoo_backfill_financials(codes, persisted):
    """一回限りの過去業績バックフィル（業績チャートの完成用）。

    J-Quants無料プランは約2年分しか遡れないため、それ以前の年次業績を
    Yahooのfundamentals-timeseries APIから取得して hist に補完する。
    - 年次の 売上高・営業利益・税引前利益(経常の代用)・純利益 を最大5年分程度
    - 同じ期のJ-Quants実測があればそちらを優先（上書きしない）
    - 取得済みの銘柄には yh_ts マーカーを付けて二度と再取得しない（新規上場だけ以後対象）
    """
    TYPES = ("annualTotalRevenue", "annualOperatingIncome",
             "annualPretaxIncome", "annualNetIncome")
    FMAP = {"annualTotalRevenue": "sales", "annualOperatingIncome": "op",
            "annualPretaxIncome": "ordp", "annualNetIncome": "np"}
    need = []
    for code in codes:
        prev = persisted.get(code) or {}
        if prev.get("yh_ts"):
            continue
        h = prev.get("hist") or {}
        n_fy = sum(1 for r in h.values() if (r.get("ty") or "").upper() in ("FY", "4Q", "Y"))
        if n_fy >= 5:  # 既に十分な年数があればマーカーだけ付けて完了扱い
            prev2 = dict(prev); prev2["yh_ts"] = "enough"; persisted[code] = prev2
            continue
        need.append(code)
    if not need:
        return 0
    print(f"  Yahoo業績バックフィル: {len(need)}銘柄の過去年次業績を取得します（銘柄ごとに一回限り）")
    import threading
    from concurrent.futures import ThreadPoolExecutor
    lock = threading.Lock()
    today = datetime.now(JST).date().isoformat()
    n_ok = [0]; n_done = [0]

    def one(code):
        url = ("https://query1.finance.yahoo.com/ws/fundamentals-timeseries/"
               f"v1/finance/timeseries/{code}.T")
        params = {"symbol": f"{code}.T", "type": ",".join(TYPES),
                  "period1": "946684800", "period2": str(int(time.time())),
                  "merge": "false", "padTimeSeries": "false"}
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        recs = {}
        try:
            for attempt in range(2):
                resp = _get_session().get(url, params=params, headers=headers, timeout=30)
                if resp.status_code == 429:
                    time.sleep(4 * (attempt + 1))
                    continue
                resp.raise_for_status()
                for res in (resp.json().get("timeseries", {}).get("result") or []):
                    t = (res.get("meta", {}).get("type") or [None])[0]
                    f = FMAP.get(t)
                    if not f:
                        continue
                    for row in (res.get(t) or []):
                        if not row:
                            continue
                        pe = row.get("asOfDate")
                        rv = (row.get("reportedValue") or {}).get("raw")
                        if not pe or rv is None:
                            continue
                        recs.setdefault(pe, {})[f] = rv
                break
        except Exception:  # noqa: BLE001
            return  # 失敗した銘柄はマーカーを付けず、次回の実行でまた試す
        with lock:
            prev = persisted.get(code) or {}
            h = dict(prev.get("hist") or {})
            added = 0
            for pe, vals in sorted(recs.items()):
                key = f"{pe}|FY"
                if key in h:  # J-Quantsの実測を優先
                    continue
                if "sales" not in vals and "np" not in vals:
                    continue
                h[key] = {**vals, "ty": "FY", "disc": "", "src": "yh"}
                added += 1
            prev2 = dict(prev)
            if h:
                keys = sorted(h.keys())[-24:]
                prev2["hist"] = {k: h[k] for k in keys}
            prev2["yh_ts"] = today
            persisted[code] = prev2
            if added:
                n_ok[0] += 1
            n_done[0] += 1
            if n_done[0] % 300 == 0:
                print(f"    バックフィル進捗 {n_done[0]}/{len(need)}...")
        time.sleep(0.1)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(one, need))
    print(f"  Yahoo業績バックフィル: {n_ok[0]}銘柄に過去年次を追加しました")
    return n_ok[0]


def load_fund_cache():
    """前回までに受け取った財務素材（EPS・BPS・発行株数・配当）を読む。決算ごとにしか変わらないので再利用できる"""
    try:
        return json.loads(FUND_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_fund_cache(cache):
    try:
        FUND_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def fetch_fundamentals(session, codes, closes=None):
    """財務指標は「素材（EPS・BPS・発行株数・配当）」を受け取り、指標そのものはこちらで計算する。
    - PER = 株価 ÷ EPS、PBR = 株価 ÷ BPS、時価総額 = 株価 × 発行株数、ROE = EPS ÷ BPS
    - 素材は決算ごとにしか変わらないため、今回の応答に無くても前回保存分で計算する
    - 相手が送ってきたPER/PBRは、素材が無いときの最後の手段としてのみ使う"""
    out = {}
    closes = closes or {}
    persisted = load_fund_cache()
    today = datetime.now(JST).date().isoformat()

    # J-Quants（あれば）: 決算開示ベースの素材を取り込み、保存分より新しければ上書き
    # 無料プランは12週遅延のため、直近だけの走査では「早い時期に決算発表した銘柄」（トヨタ等）が
    # 永久に拾えない。素材キャッシュが薄いうちは約1年分をさかのぼって全銘柄を取り込む
    n_with_hist = sum(1 for v in persisted.values() if len(v.get("hist") or {}) >= 3)
    deep = len(persisted) < 3000 or n_with_hist < 1500
    if deep and session is not None:
        print("  J-Quants: 蓄積が少ないため約2年分の決算開示を取り込みます（初回のみ・十数分）")
    jq = jquants_fetch_materials(session, days_back=(760 if deep else 110)) if session is not None else {}
    for code, mat in jq.items():
        prev = persisted.get(code) or {}
        new_hist = {**(prev.get("hist") or {}), **(mat.pop("hist", {}) or {})}
        if not prev or (mat.get("asof", "") >= prev.get("asof", "")):
            merged = {**prev, **mat}
        else:
            merged = prev
        if new_hist:
            # 業績履歴は消さずに蓄積（古い順に最大24期まで保持）
            keys = sorted(new_hist.keys())[-24:]
            merged["hist"] = {k: new_hist[k] for k in keys}
        persisted[code] = merged
    if jq:
        jq_keys = list(jq.keys())[:5]
        code_keys = list(codes)[:5]
        matched = sum(1 for c in codes if c in persisted)
        print(f"  診断: 素材コード例={jq_keys} / 株価コード例={code_keys} / "
              f"一致={matched}/{len(codes)} / 保存予定={len(persisted)}件")
        ex = next(iter(jq.values()))
        print(f"  診断: 素材の中身例={ex}")

    # 過去年次業績のバックフィル（各銘柄一回限り・済みマーカーで自動スキップ）
    if session is not None:
        try:
            yahoo_backfill_financials(list(codes), persisted)
            save_fund_cache(persisted)  # 途中経過も保存（後段で失敗しても消えない）
        except Exception as e:  # noqa: BLE001
            print(f"  ! 業績バックフィル失敗（次回再試行）: {e}", file=sys.stderr)

    for code in codes:
        fresh = _FUND_CACHE.get(code) or {}
        prev = persisted.get(code) or {}
        # 素材のマージ: 今回の値を優先、無ければ前回保存分
        mat = {}
        for k in ("eps", "bvps", "shares", "dy", "equity", "fc_eps",
                  "assets", "sales", "op", "np", "div_fy", "fdiv_fy"):
            v = fresh.get(k, prev.get(k))
            if v is not None:
                mat[k] = v
        if mat:
            mat["asof"] = today if any(k in fresh for k in ("eps", "bvps", "shares")) else prev.get("asof", today)
            # 蓄積データ（業績履歴・バックフィル済マーカー）は素材更新で消さない
            if prev.get("hist"):
                mat["hist"] = prev["hist"]
            if prev.get("yh_ts"):
                mat["yh_ts"] = prev["yh_ts"]
            persisted[code] = mat

        price = closes.get(code)
        entry = {}
        eps, bvps, shares = mat.get("eps"), mat.get("bvps"), mat.get("shares")

        # 自前計算（本線）
        if price and eps is not None and eps != 0:
            entry["per"] = round(price / eps, 1)
        if price and bvps and bvps > 0:
            entry["pbr"] = round(price / bvps, 2)
        # 発行株数のクロスチェック: 純利益÷EPS・純資産÷BPS（同じ決算内の整合）と突き合わせ、
        # 株式数フィールドの取り違え・分割ずれによる時価総額の桁違いを防ぐ
        np_v, eq_v = mat.get("np"), mat.get("equity")
        est = []
        if np_v and eps and abs(eps) > 1e-9 and np_v / eps > 0:
            est.append(np_v / eps)
        if eq_v and bvps and bvps > 0:
            est.append(eq_v / bvps)
        est_med = sorted(est)[len(est) // 2] if est else None
        shares_eff = shares if (shares and shares > 0) else est_med
        if shares and est_med and (max(shares, est_med) / max(1e-9, min(shares, est_med)) > 1.6):
            shares_eff = est_med
        if price and shares_eff:
            entry["mcap_oku"] = round(price * shares_eff / 100_000_000)
        if eps is not None and bvps and bvps > 0:
            entry["roe"] = round(eps / bvps * 100, 1)
        if mat.get("dy") is not None:
            entry["div_yield"] = round(mat["dy"] * 100, 2)
        # 配当利回り: 素材（実績年間配当 or 予想）と株価から算出（Yahoo値が無ければ）
        dps = mat.get("fdiv_fy") if mat.get("fdiv_fy") is not None else mat.get("div_fy")
        if "div_yield" not in entry and price and dps is not None and dps >= 0:
            entry["div_yield"] = round(dps / price * 100, 2)
        # 収益性・健全性
        assets, sales, op, np_ = mat.get("assets"), mat.get("sales"), mat.get("op"), mat.get("np")
        eq = mat.get("equity")
        if np_ is not None and assets and assets > 0:
            entry["roa"] = round(np_ / assets * 100, 1)
        if eq is not None and assets and assets > 0:
            entry["equity_ratio"] = round(eq / assets * 100, 1)
        if op is not None and sales and sales > 0:
            entry["op_margin"] = round(op / sales * 100, 1)
        if np_ is not None and sales and sales > 0:
            entry["net_margin"] = round(np_ / sales * 100, 1)
        if price and sales and shares and shares > 0:
            entry["psr"] = round(price / (sales / shares), 2)
        if dps is not None and eps and eps > 0:
            entry["payout"] = round(dps / eps * 100, 1)
        # PEG: PER ÷ 予想EPS成長率（%）
        fce = mat.get("fc_eps")
        if entry.get("per") and fce and eps and eps > 0 and fce > eps:
            growth = (fce / eps - 1) * 100
            if growth >= 1:
                entry["peg"] = round(entry["per"] / growth, 2)

        # 素材が無い項目だけ、相手のPER/PBR/時価総額で補う（最後の手段）
        if "per" not in entry and fresh.get("per") is not None:
            entry["per"] = round(fresh["per"], 1)
        if "pbr" not in entry and fresh.get("pbr") is not None:
            entry["pbr"] = round(fresh["pbr"], 2)
        if "mcap_oku" not in entry and fresh.get("mcap"):
            entry["mcap_oku"] = round(fresh["mcap"] / 100_000_000)
        if "roe" not in entry and entry.get("per") and entry.get("pbr") and entry["per"] > 0:
            entry["roe"] = round(entry["pbr"] / entry["per"] * 100, 1)

        if entry:
            entry["computed"] = bool(eps is not None or bvps)
            if prev.get("hist") or mat.get("hist"):
                entry["hist"] = prev.get("hist") or mat.get("hist")
            out[code] = entry

    save_fund_cache(persisted)
    print(f"  ファンダ計算: {len(out)}銘柄にPER/PBR等を付与（素材キャッシュ {len(persisted)}件を保存: {FUND_CACHE_PATH}）")
    return out


# ------------------------------------------------------------
# 代表取締役の異動（社長交代）検出: TDnetの日付別一覧を直近N日分スキャンし、
# 見出しに異動系キーワードを含む開示を銘柄コードに紐付ける（全銘柄対象・通信は日数分のみ）
# ------------------------------------------------------------
EXEC_KEYWORDS = ("代表取締役の異動", "代表取締役等の異動", "代表者の異動", "社長交代", "社長の交代",
                 "代表取締役社長の異動", "代表執行役の異動", "CEOの異動", "代表取締役および役員の異動",
                 "代表取締役の変更", "社長人事", "代表取締役社長交代")
EXEC_CACHE_PATH = DOCS / "exec_changes.json"
TOPIC_CACHE_PATH = DOCS / "topics.json"

# 注目開示トピックス: (キー, 表示名, 良し悪し pos/neg/warn, 含むキーワード, 除外キーワード)
# ※社長交代（EXEC_KEYWORDS）は別格扱いのため、ここには含めない
TOPIC_RULES = [
    ("up",       "上方修正",        "pos",  ("上方修正",), ()),
    ("down",     "下方修正",        "neg",  ("下方修正",), ()),
    ("zohai",    "増配",            "pos",  ("増配", "復配"), ()),
    ("genpai",   "減配・無配",      "neg",  ("減配", "無配"), ()),
    ("buyback",  "自社株買い",      "pos",  ("自己株式の取得", "自己株式取得", "自社株買い"),
     ("状況", "結果", "終了", "消却")),
    ("split",    "株式分割",        "pos",  ("株式分割",), ()),
    ("tob",      "TOB・MBO",        "warn", ("公開買付", "ＭＢＯ", "MBO"), ()),
    ("alliance", "資本業務提携",    "pos",  ("資本業務提携",), ()),
    ("delist",   "上場廃止・監理",  "neg",  ("上場廃止", "監理銘柄", "特設注意市場"), ()),
    ("scandal",  "不適切会計・調査", "neg",  ("不適切な会計", "不適切会計", "調査委員会", "第三者委員会"), ()),
]
TOPIC_LABEL = {k: (label, tone) for k, label, tone, _, _ in TOPIC_RULES}


def _norm_code(code5):
    c = str(code5 or "").strip()
    return c[:-1] if len(c) == 5 and c.endswith("0") else c


def _classify_topic(title):
    """開示タイトルをトピック分類。最初に合致したルール1つを返す（なければNone）"""
    for key, label, tone, inc, exc in TOPIC_RULES:
        if any(k in title for k in inc) and not any(k in title for k in exc):
            return key
    return None


def scan_disclosures(session, days_back=14):
    """TDnet日次一覧を1回のスキャンで走査し、
    (社長交代 {code:[...]}, 注目トピックス {code:[...]}) の2つを返す。
    失敗日は前回キャッシュで補う"""
    def _load(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    prev_exec = _load(EXEC_CACHE_PATH)
    prev_topic = _load(TOPIC_CACHE_PATH)
    found_exec, found_topic = {}, {}
    today = datetime.now(JST).date()
    for i in range(days_back):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        ds = d.strftime("%Y%m%d")
        try:
            resp = session.get(f"https://webapi.yanoshin.jp/webapi/tdnet/list/{ds}.json",
                               params={"limit": 3000}, timeout=30,
                               headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                raise RuntimeError(resp.status_code)
            items = (resp.json() or {}).get("items") or []
        except Exception:  # noqa: BLE001
            # その日は前回キャッシュから復元
            for prev, found in ((prev_exec, found_exec), (prev_topic, found_topic)):
                for code, lst in prev.items():
                    for it in lst:
                        if it.get("date") == d.isoformat():
                            found.setdefault(code, []).append(it)
            time.sleep(0.4)
            continue
        for it in items:
            td = it.get("Tdnet") or {}
            title = (td.get("title") or "").strip()
            if not title:
                continue
            code = _norm_code(td.get("company_code"))
            if not code:
                continue
            rec = {"date": d.isoformat(), "title": title,
                   "url": td.get("document_url") or "",
                   "company": td.get("company_name") or ""}
            if any(k in title for k in EXEC_KEYWORDS):
                found_exec.setdefault(code, []).append(rec)
                continue  # 社長交代は別格扱い（トピックスと二重計上しない）
            cat = _classify_topic(title)
            if cat:
                rec["cat"] = cat
                found_topic.setdefault(code, []).append(rec)
        time.sleep(0.4)
    # 重複除去・新しい順
    for found in (found_exec, found_topic):
        for code in found:
            seen, uniq = set(), []
            for it in sorted(found[code], key=lambda x: x["date"], reverse=True):
                k = (it["date"], it["title"])
                if k not in seen:
                    seen.add(k)
                    uniq.append(it)
            found[code] = uniq
    for path, found in ((EXEC_CACHE_PATH, found_exec), (TOPIC_CACHE_PATH, found_topic)):
        try:
            path.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    print(f"  代表異動の開示: 直近{days_back}日で {sum(len(v) for v in found_exec.values())}件 / {len(found_exec)}銘柄")
    print(f"  注目開示トピックス: 直近{days_back}日で {sum(len(v) for v in found_topic.values())}件 / {len(found_topic)}銘柄")
    return found_exec, found_topic


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
        # 上場約10年以上の銘柄のみ、全期間チャート用の月足を追加取得
        # （10年未満なら10年チャートが全期間を兼ねるため不要）
        if days and len(days) >= 2350:
            try:
                ah = fetch_all_history(_get_session(), stock["code"], stock["suffix"])
                if ah:
                    _ALLHIST_CACHE[stock["code"]] = ah
            except Exception:
                pass
            time.sleep(CONFIG["THROTTLE_SEC"])
        return stock, days

    candidates, dead_count, skip_count, fail_count = [], 0, 0, 0
    all_results = []
    map_series = {}  # 関連銘柄マップ用: 全銘柄の直近130営業日の終値
    sim_ohlc = {}    # シミュレーション用: 全銘柄の直近245営業日のOHLC＋出来高
    detail_map = {}
    done = 0
    with ThreadPoolExecutor(max_workers=CONFIG["WORKERS"]) as pool:
        futures = [pool.submit(task, s) for s in universe]
        for fut in as_completed(futures):
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(universe)} 取得済み...")
            stock, days_full = fut.result()
            days = days_full[-245:] if days_full else days_full  # 既存判定は直近1年
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
            long_m = compute_long_metrics(days_full, code=stock["code"])
            detail_map[stock["code"]] = {
                **stock, **m, "status": status, "reason": reason,
                "days": days[-10:], "long": long_m,
            }
            map_series[stock["code"]] = ([d["date"] for d in days[-130:]],
                                         [d["close"] for d in days[-130:]])
            sim_ohlc[stock["code"]] = (
                [d["date"] for d in days],
                [round(d["open"], 1) for d in days],
                [round(d["high"], 1) for d in days],
                [round(d["low"], 1) for d in days],
                [round(d["close"], 1) for d in days],
                [d.get("volume") or 0 for d in days])
            if status == "dead":
                dead_count += 1
                all_results.append({**base, "status": "dead", "reason": reason})
                continue
            if status == "skip":
                skip_count += 1
                all_results.append({**base, "status": "skip", "reason": reason})
                continue
            var_trades = simulate_variants(days, variants=[None])
            trades = var_trades[None]
            candidates.append({**stock, **m, "days": days[-10:],
                               "long": long_m,
                               "sim": sim_summary(trades),
                               "trades": trades[-6:]})
            all_results.append({**base, "status": "ok", "reason": ""})

    # ファンダメンタルはスコアの判断材料になるため、採点前に取得する
    import requests as _rq
    td_session = _rq.Session()
    fundamentals = fetch_fundamentals(
        td_session, list(detail_map.keys()),
        closes={code: e.get("close") for code, e in detail_map.items()})
    fund_ok = len(fundamentals) > 0
    for c in candidates:
        c["fund"] = fundamentals.get(c["code"])
    for code, e in detail_map.items():
        e["fund"] = fundamentals.get(code)

    for c in candidates:
        c["score"], c["reasons"] = score_stock(c)
        c["demerit"], c["demerit_hits"] = demerit_stock(c)
    # 減点方式: 詳細を持つ全銘柄（除外・対象外を含む）に適用し、全体順位を付ける
    for code, e in detail_map.items():
        if "demerit" not in e:
            e["demerit"], e["demerit_hits"] = demerit_stock(e)
        c_ent = next((c for c in candidates if c["code"] == code), None)
        if c_ent is not None:
            e["demerit"], e["demerit_hits"] = c_ent["demerit"], c_ent["demerit_hits"]
            e["score"] = c_ent["score"]
    all_by_demerit = sorted(detail_map.values(),
                            key=lambda s: (s["demerit"], -(s.get("score") or 0)))
    for i, e in enumerate(all_by_demerit, 1):
        e["demerit_rank"] = i
    demerit_rank_map = {e["code"]: e["demerit_rank"] for e in all_by_demerit}
    for c in candidates:
        c["demerit_rank"] = demerit_rank_map.get(c["code"])
    clean_ranked = all_by_demerit[:100]

    # ---- 三層選定: 安全（減点≦許容）× 質（上位）× タイミング（買い場あり）----
    SAFE_MAX = CONFIG.get("SAFE_MAX_DEMERIT", 12)   # 「重い」1個までは許容、「致命」は不可
    for c in candidates:
        c["safe_ok"] = c["demerit"] <= SAFE_MAX and not any(h[0] == "致命" for h in c["demerit_hits"])
        c["timing_ok"] = c["drop_pct"] >= CONFIG["CHEAP_PCT"] and c.get("stabilizing", True)
        c["tri"] = c["safe_ok"] and c["timing_ok"]
    # 帳簿の主候補: 三層合格を優先し、その中を総合スコア順。足りなければ従来順で補う
    tri = sorted([c for c in candidates if c["tri"]], key=lambda s: s["score"], reverse=True)
    rest = sorted([c for c in candidates if not c["tri"]], key=lambda s: s["score"], reverse=True)
    candidates[:] = tri + rest
    # 「まもなく」: 安全×質は合格だが、買い場（◎）にあと少し届かない銘柄
    q_threshold = sorted((c.get("q_score", 0) for c in candidates), reverse=True)
    q_cut = q_threshold[min(len(q_threshold) - 1, 150)] if q_threshold else 0
    soon = [c for c in candidates
            if c["safe_ok"] and not c["timing_ok"] and c.get("q_score", 0) >= q_cut
            and 0 < (CONFIG["CHEAP_PCT"] - c["drop_pct"]) <= 3.0]
    for c in soon:
        c["to_cheap_pct"] = round(CONFIG["CHEAP_PCT"] - c["drop_pct"], 1)
        c["trigger_price"] = round(c["high20"] * (1 - CONFIG["CHEAP_PCT"] / 100), 1)
    soon.sort(key=lambda s: (s["to_cheap_pct"], -s.get("q_score", 0)))
    soon_list = soon[:20]
    picked = candidates[:CONFIG["SHORTLIST_N"]]
    picked_codes = {s["code"] for s in picked}
    score_map = {c["code"]: c["score"] for c in candidates}
    rank_map = {s["code"]: i + 1 for i, s in enumerate(picked)}
    cand_by_code = {c["code"]: c for c in candidates}
    soon_codes = {c["code"] for c in soon_list}
    for r in all_results:
        if r["status"] == "ok":
            r["status"] = "picked" if r["code"] in picked_codes else "bench"
            r["score"] = round(score_map.get(r["code"], 0), 1)
            if r["status"] == "picked":
                r["cand_rank"] = rank_map.get(r["code"])
        e = detail_map.get(r["code"])
        if e is not None and "demerit" in e:
            r["demerit"] = e["demerit"]
            r["demerit_rank"] = e.get("demerit_rank")
        c = cand_by_code.get(r["code"])
        if c is not None:
            r["q_score"] = c.get("q_score")
            r["t_score"] = c.get("t_score")
            r["tri"] = c.get("tri", False)
            r["soon"] = r["code"] in soon_codes
        e_g = detail_map.get(r["code"])
        if e_g is not None:
            ag, rf, n_ev = all_green_flags(e_g)
            r["all_green"], r["red_free"], r["n_eval"] = ag, rf, n_ev
            e_g["all_green"], e_g["red_free"], e_g["n_eval"] = ag, rf, n_ev
        e2 = detail_map.get(r["code"])
        if e2 is not None and c is not None:
            e2["q_score"] = c.get("q_score"); e2["t_score"] = c.get("t_score")
            e2["tri"] = c.get("tri", False); e2["soon"] = r["code"] in soon_codes
            e2["safe_ok"] = c.get("safe_ok"); e2["timing_ok"] = c.get("timing_ok")

    stats = {
        "universe": len(universe),
        "dead_excluded": dead_count,
        "skipped": skip_count,
        "failed": fail_count,
        "cutoff_score": round(picked[-1]["score"], 1) if picked else 0,
    }

    # 地合い（日経平均の200日線と直近20日の変化）+ 市場全体の需給指標
    market = None
    mkt_days = fetch_daily(_get_session(), "^N225", "", range_="10y")
    if mkt_days and len(mkt_days) >= 210:
        mc = [d["close"] for d in mkt_days]
        ma200 = sum(mc[-200:]) / 200
        market = {"above200": mc[-1] > ma200,
                  "chg20": round((mc[-1] / mc[-21] - 1) * 100, 1),
                  "nikkei": round(mc[-1], 0)}
        # NT倍率（TOPIXが取れれば）
        tpx = (fetch_daily(_get_session(), "^TPX", "", range_="10y")
               or fetch_daily(_get_session(), "1306", ".T", range_="10y"))
        if tpx and len(tpx) >= 30:
            tc = tpx[-1]["close"]
            if tc and tc > 0:
                nt = mc[-1] / tc
                market["nt_ratio"] = round(nt, 2)
    # 騰落レシオ(25日)と新高値・新安値銘柄数（対象全銘柄の日足から集計）
    adv = dec = 0
    hi52 = lo52 = 0
    up25 = down25 = 0
    for e in detail_map.values():
        dd = e.get("days") or []
        if len(dd) >= 2:
            if dd[-1]["close"] > dd[-2]["close"]:
                adv += 1
            elif dd[-1]["close"] < dd[-2]["close"]:
                dec += 1
        if e.get("pos1y") is not None:
            if e["pos1y"] >= 0.98:
                hi52 += 1
            elif e["pos1y"] <= 0.02:
                lo52 += 1
    if market is None:
        market = {}
    market["adv"], market["dec"] = adv, dec
    market["adr_1d"] = round(adv / dec, 2) if dec else None
    market["new_high"], market["new_low"] = hi52, lo52

    print("TDnet適時開示を取得中...")
    exec_changes, topic_map = scan_disclosures(td_session)
    for code, e in detail_map.items():
        if code in exec_changes:
            e["exec_change"] = exec_changes[code]
        if code in topic_map:
            e["topics"] = topic_map[code]
    for c in candidates:
        if c["code"] in exec_changes:
            c["exec_change"] = exec_changes[c["code"]]
        if c["code"] in topic_map:
            c["topics"] = topic_map[c["code"]]
    for r in all_results:
        if r["code"] in exec_changes:
            r["exec_change"] = exec_changes[r["code"]]
        if r["code"] in topic_map:
            r["topics"] = topic_map[r["code"]]
    rank_map2 = {s["code"]: i + 1 for i, s in enumerate(picked)}
    for c in candidates:
        e = detail_map.get(c["code"])
        if e is not None:
            e["score"] = c.get("score")
            e["reasons"] = c.get("reasons", [])
            e["sim"] = c.get("sim")
            e["cand_rank"] = rank_map2.get(c["code"])
    for s in picked:
        s["disclosures"] = fetch_tdnet(td_session, s["code"])
        time.sleep(0.25)
    nikkei_days = ([(d["date"], d["close"]) for d in mkt_days[-470:]]
                   if mkt_days and len(mkt_days) >= 210 else [])
    extras = {"market": market,
              "fund_available": fund_ok, "detail_map": detail_map,
              "clean_ranked": clean_ranked, "soon": soon_list,
              "nikkei_days": nikkei_days,
              "map_series": map_series, "sim_ohlc": sim_ohlc,
              "clean_stats": {"screened": len(detail_map),
                              "flawless": sum(1 for e in detail_map.values() if e.get("demerit") == 0)}}
    return picked, stats, all_results, extras


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
                {"date": d,
                 "open": close * rng.uniform(0.98, 1.03),
                 "high": close * rng.uniform(1.03, 1.06),
                 "low": close * rng.uniform(0.95, 0.98),
                 "close": close * rng.uniform(0.98, 1.03),
                 "volume": 1_000_000}
                for d in ["2026-07-30", "2026-07-31", "2026-08-03", "2026-08-04",
                          "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10",
                          "2026-08-11", "2026-08-12"]
            ],
        })
    for s in picked:
        close = s["close"]  # 直前ループの残り値を使わない（銘柄間のデータ混線防止）
        s["sim"] = {"wins": rng.randint(0, 9),
                    "avg_held": rng.uniform(4, 28),
                    "open_loss": rng.random() < 0.3}
        s["worst_1d"] = rng.uniform(-6, -1)
        s["vol20"] = rng.uniform(0.8, 3.5)
        s["pos1y"] = rng.uniform(0.2, 0.7)
        s["concentration"] = rng.uniform(0.1, 0.8)
        s["stabilizing"] = True
        s["ma200_above"] = rng.random() < 0.6
        s["days"][-1]["close"] = close
        s["days"][-1]["open"] = close * 1.001
        s["days"][-1]["high"] = max(s["days"][-1]["high"], close * 1.004)
        s["days"][-1]["low"] = min(s["days"][-1]["low"], close * 0.996)
        s["disclosures"] = ([{"date": "8/7", "title": "2027年3月期 第1四半期決算短信〔日本基準〕（連結）",
                              "url": "https://www.release.tdnet.info/"}]
                            if rng.random() < 0.5 else [])
        base3 = close * rng.uniform(0.9, 1.2)
        spark = []
        p = base3
        from datetime import date as _date, timedelta as _td
        d0 = _date(2023, 8, 20)
        for k in range(150):
            p = max(close * 0.7, p * rng.uniform(0.975, 1.025))
            spark.append([( d0 + _td(days=k * 7)).isoformat(), round(p, 1)])
        spark[-1][1] = close
        zone_low = close * rng.uniform(0.88, 0.94)
        s["long"] = {
            "stoch": round(rng.uniform(10, 85), 1), "mfi": round(rng.uniform(15, 80), 1),
            "adx": round(rng.uniform(12, 40), 1), "di_plus_over": rng.random() < 0.6,
            "ichimoku": rng.choice(["above", "in", "below"]), "atr_pct": round(rng.uniform(1.2, 4.5), 2),
            "hv20": round(rng.uniform(18, 55), 1), "obv_trend": round(rng.uniform(-20, 25), 1),
            "rsi": round(rng.uniform(22, 55), 1),
            "gc": rng.random() < 0.6,
            "zone": {"zone_low": round(zone_low, 1),
                     "zone_top": round(zone_low * 1.05, 1),
                     "touches": rng.randint(2, 5),
                     "touch_dates": [spark[i][0] for i in rng.sample(range(20, 140), 3)],
                     "dist_pct": round(rng.uniform(-1, 6), 1)},
            "spark": spark,
            "spark1": spark[-52:],
            "spark10": ([[(d0 - _td(days=(150 - k) * 17)).isoformat(),
                          round(max(close * 0.4, base3 * (0.6 + 0.4 * k / 150) * rng.uniform(0.95, 1.05)), 1)]
                         for k in range(150)] + spark[::5]),
            "sparkall": ([[(d0 - _td(days=(160 - k) * 60)).isoformat(),
                           round(max(close * 0.15, base3 * (0.25 + 0.75 * k / 160) * rng.uniform(0.93, 1.07)), 1)]
                          for k in range(160)] + spark[::5]),
            "years_all": 28,
        }
        if rng.random() < 0.25:
            s["long"]["w_bottom"] = {"neck": round(close * 1.02, 1)}
        if rng.random() < 0.2:
            s["long"]["climax"] = {"date": "2026-08-08"}
        _per = round(rng.uniform(6, 35), 1)
        _pbr = round(rng.uniform(0.5, 4.0), 2)
        _sales0 = rng.uniform(3e10, 3e12)
        _hist = {}
        for yy in range(2022, 2027):
            _sales0 *= rng.uniform(0.98, 1.15)
            _opv = _sales0 * rng.uniform(0.03, 0.14)
            _hist[f"{yy}-03-31|FY"] = {"sales": round(_sales0), "op": round(_opv),
                                       "ordp": round(_opv * rng.uniform(0.9, 1.1)),
                                       "np": round(_opv * rng.uniform(0.55, 0.85) * (1 if rng.random() > 0.08 else -0.4)),
                                       "ty": "FY", "disc": f"{yy}-05-10"}
        _hist["2026-06-30|1Q"] = {"sales": round(_sales0 * 0.26), "op": round(_sales0 * 0.02),
                                  "np": round(_sales0 * 0.013), "ty": "1Q", "disc": "2026-08-05"}
        s["fund"] = {"per": _per, "pbr": _pbr, "hist": _hist,
                     "roe": round(_pbr / _per * 100, 1),
                     "mcap_oku": rng.randint(80, 40000),
                     "div_yield": round(rng.uniform(0, 4.5), 2),
                     "equity_ratio": round(rng.uniform(15, 75), 1), "op_margin": round(rng.uniform(-2, 18), 1),
                     "roa": round(rng.uniform(-1, 9), 1), "payout": round(rng.uniform(10, 120), 1),
                     "peg": round(rng.uniform(0.5, 3.5), 2)}
        s["vol_ratio"] = rng.uniform(0.5, 3.5)
        s["cheap_streak"] = rng.randint(0, 6)
        s["prev_change"] = rng.uniform(-4, 2)
        s["gap_avg"] = rng.uniform(0.3, 3.0)
        if rng.random() < 0.12:
            s["exec_change"] = [{"date": "2026-08-12", "title": "代表取締役の異動に関するお知らせ",
                                 "url": "https://www.release.tdnet.info/", "company": s["name"]}]
        if rng.random() < 0.3:
            _cat, _t = rng.choice([("up", "通期業績予想の上方修正に関するお知らせ"),
                                   ("buyback", "自己株式の取得に係る事項の決定に関するお知らせ"),
                                   ("zohai", "配当予想の修正（増配）に関するお知らせ"),
                                   ("down", "業績予想の下方修正に関するお知らせ"),
                                   ("split", "株式分割及び定款の一部変更に関するお知らせ")])
            s["topics"] = [{"date": "2026-08-18", "title": _t, "cat": _cat,
                            "url": "https://www.release.tdnet.info/", "company": s["name"]}]
        s["trades"] = [
            {"buy_date": "2026-05-11", "sell_date": "2026-05-18", "held": 6, "pnl": 12000.0, "stop": False},
            {"buy_date": "2026-06-02", "sell_date": "2026-07-13", "held": 28, "pnl": 9500.0, "stop": False},
            {"buy_date": "2026-07-30", "sell_date": None, "held": 9, "pnl": rng.uniform(-15000, 4000), "stop": False},
        ]
        s["macd_state"] = rng.choice(["golden_recent", "above", "below"])
        s["boll_sigma"] = rng.uniform(-2.5, 1.0)
        s["dev25"] = rng.uniform(-15, 3)
        s["score"], s["reasons"] = score_stock(s)
    picked.sort(key=lambda s: s["score"], reverse=True)
    stats = {"universe": 3912, "dead_excluded": 214, "skipped": 1480, "failed": 3,
             "cutoff_score": round(picked[-1]["score"], 1) if picked else 0}

    for s in picked:
        s["demerit"], s["demerit_hits"] = demerit_stock(s)
        s["safe_ok"] = s["demerit"] <= 12 and not any(h[0] == "致命" for h in s["demerit_hits"])
        s["timing_ok"] = s["drop_pct"] >= 5.0
        s["tri"] = s["safe_ok"] and s["timing_ok"]
    clean_ranked = sorted(picked, key=lambda s: (s["demerit"], -s["score"]))
    for i, s in enumerate(clean_ranked, 1):
        s["demerit_rank"] = i
    picked.sort(key=lambda s: (not s["tri"], -s["score"]))
    soon_list = []
    for s in picked[-4:]:
        s2 = dict(s); s2["drop_pct"] = rng.uniform(2.5, 4.8); s2["safe_ok"] = True; s2["timing_ok"] = False
        s2["to_cheap_pct"] = round(5.0 - s2["drop_pct"], 1); s2["trigger_price"] = round(s2["high20"] * 0.95, 1)
        soon_list.append(s2)
    all_results = []
    soon_codes_demo = {s2["code"] for s2 in soon_list}
    for i, s in enumerate(picked):
        all_results.append({"code": s["code"], "name": s["name"],
                            "market": s["market"], "sector": s["sector"],
                            "close": round(s["close"], 1),
                            "drop_pct": round(s["drop_pct"], 2),
                            "score": round(s["score"], 1), "cand_rank": i + 1,
                            "demerit": s.get("demerit"), "demerit_rank": s.get("demerit_rank"),
                            "q_score": s.get("q_score"), "t_score": s.get("t_score"), "tri": s.get("tri", False),
                            "soon": s["code"] in soon_codes_demo,
                            "all_green": all_green_flags(s)[0], "red_free": all_green_flags(s)[1], "n_eval": all_green_flags(s)[2],
                            "status": "picked", "reason": ""})
    all_results += [
        {"code": "9999", "name": "デモ圏外株", "market": "プライム", "sector": "サービス業",
         "close": 1200.0, "drop_pct": 1.2, "score": 46.0, "status": "bench", "reason": ""},
        {"code": "6800", "name": "デモ右肩下がり", "market": "スタンダード", "sector": "電気機器",
         "close": 300.0, "drop_pct": 8.0, "status": "dead", "reason": "1年高値から55%下落"},
        {"code": "7777", "name": "デモ流動性不足", "market": "グロース", "sector": "サービス業",
         "close": 800.0, "drop_pct": 4.0, "status": "skip", "reason": "流動性不足"},
        {"code": "8888", "name": "デモ取得失敗", "market": "プライム", "sector": "機械",
         "status": "fail", "reason": "データ取得失敗"},
    ]
    detail_map = {}
    for s in picked:
        detail_map[s["code"]] = {**s, "status": "picked",
                                 "reason": "", "cand_rank": 1}
    for r in all_results:
        src_s = next((s for s in picked if s["code"] == r["code"]), None)
        if src_s and src_s.get("exec_change"):
            r["exec_change"] = src_s["exec_change"]
        if src_s and src_s.get("topics"):
            r["topics"] = src_s["topics"]
    detail_map["6800"] = {"code": "6800", "name": "デモ右肩下がり", "market": "スタンダード",
                          "sector": "電気機器", "suffix": ".T", "status": "dead",
                          "reason": "1年高値から55%下落", "days": [], "long": {}}
    # マップ用の疑似値動き（同業種は共通ファクターで連動させ、相関類似が機能することを確認できるように）
    map_series = {}
    from datetime import date as _d2, timedelta as _td2
    _days130 = []
    _d = _d2(2026, 8, 21)
    while len(_days130) < 130:
        if _d.weekday() < 5:
            _days130.append(_d.isoformat())
        _d -= _td2(days=1)
    _days130.reverse()
    _mkt_path = [rng.gauss(0, 1) for _ in range(130)]
    _sec_factor = {}
    for s in picked:
        sec = s["sector"]
        if sec not in _sec_factor:
            _sec_factor[sec] = [rng.gauss(0, 1) for _ in range(130)]
        fpath = _sec_factor[sec]
        px = s["close"]
        ser = []
        for k in range(130):
            px *= 1 + 0.004 * _mkt_path[k] + 0.007 * fpath[k] + rng.gauss(0, 0.009)
            ser.append(px)
        scale = s["close"] / ser[-1]
        map_series[s["code"]] = (_days130, [round(v * scale, 1) for v in ser])
    # シミュレーション用の疑似OHLC（245営業日）
    sim_ohlc = {}
    _days245 = []
    _d = _d2(2026, 8, 21)
    while len(_days245) < 245:
        if _d.weekday() < 5:
            _days245.append(_d.isoformat())
        _d -= _td2(days=1)
    _days245.reverse()
    for s in picked:
        px = s["close"] * rng.uniform(0.8, 1.2)
        o_, h_, l_, c_, v_ = [], [], [], [], []
        for k in range(245):
            px *= 1 + rng.gauss(0.0004, 0.018)
            px = max(120.0, px)
            op = px * rng.uniform(0.99, 1.01)
            hi = max(op, px) * rng.uniform(1.0, 1.025)
            lo = min(op, px) * rng.uniform(0.975, 1.0)
            o_.append(round(op, 1)); h_.append(round(hi, 1)); l_.append(round(lo, 1)); c_.append(round(px, 1))
            v_.append(int(rng.uniform(3e5, 3e6)))
        scale2 = s["close"] / c_[-1]
        sim_ohlc[s["code"]] = (_days245,
                               [round(v * scale2, 1) for v in o_], [round(v * scale2, 1) for v in h_],
                               [round(v * scale2, 1) for v in l_], [round(v * scale2, 1) for v in c_], v_)
    # 日経の疑似系列（465日・序盤に下落局面→地合いフィルタが機能するか確認できる形）
    _days465 = []
    _d = _d2(2026, 8, 21)
    while len(_days465) < 465:
        if _d.weekday() < 5:
            _days465.append(_d.isoformat())
        _d -= _td2(days=1)
    _days465.reverse()
    _npx = 38000.0
    nikkei_days = []
    for k in range(465):
        drift = -0.0014 if 280 < k < 360 else 0.0006
        _npx *= 1 + drift + rng.gauss(0, 0.008)
        nikkei_days.append((_days465[k], round(_npx, 1)))
    extras = {
        "detail_map": detail_map,
        "map_series": map_series,
        "sim_ohlc": sim_ohlc,
        "nikkei_days": nikkei_days,
        "clean_ranked": clean_ranked, "soon": soon_list,
        "clean_stats": {"screened": 1480, "flawless": 3},
        "market": {"above200": True, "chg20": 2.4},
        "fund_available": True,
    }
    return picked, stats, all_results, extras


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
            "score": round(s.get("score", 0), 1),
            "reasons": s.get("reasons", []),
            "comment": make_comment(s),
            "prev_change": (round(s["prev_change"], 2) if s.get("prev_change") is not None else None),
            "demerit": s.get("demerit"),
            "demerit_rank": s.get("demerit_rank"),
            "demerit_hits": s.get("demerit_hits", []),
            "trades": s.get("trades", []),
            "exec_change": s.get("exec_change", []),
            "q_score": s.get("q_score"), "t_score": s.get("t_score"),
            "tri": s.get("tri", False), "safe_ok": s.get("safe_ok"), "timing_ok": s.get("timing_ok"),
            "macd_state": s.get("macd_state"),
            "boll_sigma": (round(s["boll_sigma"], 2) if s.get("boll_sigma") is not None else None),
            "dev25": (round(s["dev25"], 2) if s.get("dev25") is not None else None),
            "disclosures": s.get("disclosures", []),
            "fund": s.get("fund"),
            "long": {k: v for k, v in (s.get("long") or {}).items()
                     if k not in ("spark", "spark10", "spark1", "sparkall")},
            "spark": (s.get("long") or {}).get("spark", []),
            "spark10": (s.get("long") or {}).get("spark10", []),
            "spark1": (s.get("long") or {}).get("spark1", []),
            "sparkall": (s.get("long") or {}).get("sparkall", []),
            "years_all": (s.get("long") or {}).get("years_all"),
            "topics": s.get("topics", []),
            "cost": round(s["close"] * 100),
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
                   ("TOP_N", "SHORTLIST_N", "RECENT_DAYS", "CHEAP_PCT", "MILD_PCT", "TP_PCT",
                    "DEAD_DRAWDOWN", "DEAD_BELOW_MA_RATIO", "SAFE_MAX_DEMERIT",
                    "KNIFE_DROP_1D", "MAX_VOL20", "MIN_POS_1Y",
                    "MIN_RECORDS", "MIN_PRICE", "MIN_TURNOVER")},
        "stocks": stocks_out,
    }
    return data


def spark_svg(spark, long_info, width=320, height=110):
    """3年週足のミニチャートをSVG文字列で返す（支持帯・反発▲・現在地入り）"""
    if not spark or len(spark) < 10:
        return ""
    closes = [p[1] for p in spark]
    dates = [p[0] for p in spark]
    zone = (long_info or {}).get("zone")
    lo = min(closes)
    hi = max(closes)
    if zone:
        lo = min(lo, zone["zone_low"])
        hi = max(hi, zone["zone_top"])
    pad = (hi - lo) * 0.08 or 1
    lo -= pad
    hi += pad

    def x(i):
        return round(i / (len(closes) - 1) * (width - 8) + 4, 1)

    def y(v):
        return round(height - 6 - (v - lo) / (hi - lo) * (height - 12), 1)

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
             f'style="width:100%; height:auto; background:#fffdf6; border-radius:8px;">']
    if zone:
        parts.append(f'<rect x="0" y="{y(zone["zone_top"])}" width="{width}" '
                     f'height="{max(2, y(zone["zone_low"]) - y(zone["zone_top"]))}" '
                     f'fill="#c62f2f" opacity="0.12"/>')
    pts = " ".join(f"{x(i)},{y(v)}" for i, v in enumerate(closes))
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#1c1c1e" stroke-width="1.6"/>')
    if zone:
        # 反発地点（タッチ日を週足の最寄り位置へ）
        for td in zone.get("touch_dates", []):
            best_i = min(range(len(dates)), key=lambda i: abs(
                (datetime.fromisoformat(dates[i]) - datetime.fromisoformat(td)).days))
            parts.append(f'<path d="M {x(best_i)} {y(closes[best_i]) + 12} l 5 8 l -10 0 z" '
                         f'fill="#c62f2f"/>')
    parts.append(f'<circle cx="{x(len(closes) - 1)}" cy="{y(closes[-1])}" r="4" fill="#1c1c1e"/>')
    parts.append("</svg>")
    return "".join(parts)


def spark_block_html(spark3, spark10, long_info, spark1=None, sparkall=None, years_all=None):
    """1年/3年/10年/全期間を切り替えられる週足ミニチャート（切替JSは各ページのspSwap）"""
    svg3 = spark_svg(spark3, long_info)
    if not svg3:
        return ""
    variants = []  # (キー, ボタン名, svg)
    if spark1 and len(spark1) >= 10:
        variants.append(("1", "1年", spark_svg(spark1, long_info)))
    variants.append(("3", "3年", svg3))
    if spark10 and len(spark10) >= 10:
        variants.append(("10", "10年", spark_svg(spark10, long_info)))
    if sparkall and len(sparkall) >= 10:
        lab = f"全期間({years_all}年)" if years_all else "全期間"
        variants.append(("all", lab, spark_svg(sparkall, long_info)))
    z = (long_info or {}).get("zone")
    zline = (f'赤い帯=長期支持帯 {z["zone_low"]:,.0f}〜{z["zone_top"]:,.0f}円'
             f'（▲=過去の反発地点 ・ ●=いま）' if z else "●=いま")
    btns = ""
    if len(variants) > 1:
        btns = ('<span class="spbtns">'
                + "".join(f'<button type="button" class="spbtn{" on" if k == "3" else ""}" '
                          f'onclick="spSwap(this, \'{k}\')">{lab}</button>'
                          for k, lab, _s in variants)
                + '</span>')
    charts = "".join(
        f'<div class="spark spv" data-sp="{k}" style="display:{"" if k == "3" else "none"}">{s}</div>'
        for k, _lab, s in variants)
    return (f'<div class="spwrap"><div class="nhead">値動きと長期支持帯{btns}</div>'
            f'{charts}'
            f'<div class="discnote">{zline}。支持帯は直近3年の谷から自動検出（どの期間表示でも同じ帯）。</div></div>')


SPARK_JS = """<script>
function spSwap(btn, mode){
  var w = btn.closest('.spwrap');
  if (!w) return;
  w.querySelectorAll('.spbtn').forEach(function(b){ b.classList.remove('on'); });
  btn.classList.add('on');
  w.querySelectorAll('.spv').forEach(function(el){
    el.style.display = (el.dataset.sp === String(mode)) ? '' : 'none';
  });
}
</script>"""

SPARK_CSS = """
  .spbtns{float:right; display:inline-flex; gap:4px;}
  .spbtn{font-size:10px; font-weight:800; border:1px solid #d8d2c2; background:#fff; color:#6b6b70;
    border-radius:6px; padding:2px 8px; cursor:pointer;}
  .spbtn.on{background:#1c1c1e; color:#fff; border-color:#1c1c1e;}
"""


def _fin_fmt(v):
    """円→読みやすい表記（兆/億）。百万円単位らしき小さい値は円に換算"""
    av = abs(v)
    sign = "-" if v < 0 else ""
    if av >= 1e12:
        return f"{sign}{av / 1e12:,.2f}兆"
    if av >= 1e8:
        return f"{sign}{av / 1e8:,.0f}億"
    return f"{sign}{av / 1e4:,.0f}万"


def fin_chart_html(hist):
    """決算履歴 {期末日|区分: {sales,op,ordp,np,ty}} → 売上棒＋利益折れ線のSVGと期別テーブル"""
    if not hist:
        return ""
    recs = []
    for key, r in hist.items():
        pe = key.split("|")[0]
        if len(pe) >= 7:
            recs.append({"pe": pe, **r})
    if not recs:
        return ""
    recs.sort(key=lambda r: r["pe"])
    # 百万円単位らしき場合は円へ（売上の最大値で判定）
    mx = max((abs(r.get("sales") or 0) for r in recs), default=0)
    scale = 1e6 if 0 < mx < 1e7 else 1.0
    if scale != 1.0:
        for r in recs:
            for k in ("sales", "op", "ordp", "np"):
                if r.get(k) is not None:
                    r[k] = r[k] * scale
    fy = [r for r in recs if (r.get("ty") or "").upper() in ("FY", "4Q", "Y")]
    use = fy if len(fy) >= 2 else recs[-8:]
    use = use[-10:]
    if not use:
        return ""
    annual = use is fy or len(fy) >= 2

    def _plabel(r):
        y, m = r["pe"][2:4], r["pe"][5:7].lstrip("0")
        ty = (r.get("ty") or "").upper()
        if annual or ty in ("FY", "4Q", "Y"):
            return f"{y}/{m}期"
        q = {"1Q": "Q1", "2Q": "Q2", "3Q": "Q3"}.get(ty, "")
        return f"{y}/{m}{q}" if q else f"{y}/{m}"

    W, H = 340, 150
    PL, PR, PT, PB = 40, 40, 10, 20
    n = len(use)
    bw = min(34, (W - PL - PR) / n * 0.55)
    smax = max((r.get("sales") or 0) for r in use) or 1
    pvals = [v for r in use for v in (r.get("op"), r.get("ordp"), r.get("np")) if v is not None]
    pmin = min(0, min(pvals)) if pvals else 0
    pmax = max(pvals) if pvals else 1
    if pmax - pmin < 1:
        pmax = pmin + 1

    def X(i):
        return PL + (i + 0.5) * (W - PL - PR) / n

    def Ys(v):
        return H - PB - v / smax * (H - PT - PB) * 0.94

    def Yp(v):
        return H - PB - (v - pmin) / (pmax - pmin) * (H - PT - PB) * 0.94

    parts = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
             f'style="width:100%; height:auto; background:#fffdf6; border-radius:8px;">']
    if pmin < 0:
        parts.append(f'<line x1="{PL}" y1="{Yp(0):.1f}" x2="{W - PR}" y2="{Yp(0):.1f}" '
                     f'stroke="#e0d8c4" stroke-width="1" stroke-dasharray="3,3"/>')
    # 売上の棒
    for i, r in enumerate(use):
        s = r.get("sales")
        if s is None:
            continue
        parts.append(f'<rect x="{X(i) - bw / 2:.1f}" y="{Ys(s):.1f}" width="{bw:.1f}" '
                     f'height="{max(1, H - PB - Ys(s)):.1f}" rx="2" fill="#cfe0f5">'
                     f'<title>{_plabel(r)} 売上高 {_fin_fmt(s)}円</title></rect>')
    # 利益の折れ線
    lines = [("op", "#c9661f", "営業利益"), ("ordp", "#178a5b", "経常利益"), ("np", "#7d55c7", "純利益")]
    used_lines = []
    for k, col, lab in lines:
        pts = [(X(i), Yp(r[k])) for i, r in enumerate(use) if r.get(k) is not None]
        if len(pts) < 2:
            continue
        used_lines.append((k, col, lab))
        parts.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
                     + f'" fill="none" stroke="{col}" stroke-width="1.8"/>')
        for (x, y), r in zip(pts, [r for r in use if r.get(k) is not None]):
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="{col}">'
                         f'<title>{_plabel(r)} {lab} {_fin_fmt(r[k])}円</title></circle>')
    # x軸ラベル（重なる場合は間引き）
    step = max(1, n // 5)
    for i in range(0, n, step):
        parts.append(f'<text x="{X(i):.1f}" y="{H - 6}" font-size="8.5" fill="#a99a76" '
                     f'text-anchor="middle">{_plabel(use[i])}</text>')
    # 軸の目安
    parts.append(f'<text x="{W - PR + 3}" y="{Ys(smax) + 8:.1f}" font-size="8" fill="#7ba0cc">'
                 f'{_fin_fmt(smax)}</text>')
    parts.append(f'<text x="{W - PR + 3}" y="{H - PB + 2}" font-size="8" fill="#7ba0cc">売上</text>')
    parts.append(f'<text x="4" y="{Yp(pmax) + 8:.1f}" font-size="8" fill="#8a6f4d">{_fin_fmt(pmax)}</text>')
    parts.append(f'<text x="4" y="{H - PB + 2}" font-size="8" fill="#8a6f4d">利益</text>')
    parts.append('</svg>')
    legend = ('<div class="finlg"><span><i style="background:#cfe0f5"></i>売上高（右軸）</span>'
              + "".join(f'<span><i style="background:{col}"></i>{lab}</span>'
                        for _k, col, lab in used_lines) + '</div>')
    # 期別テーブル（直近5期・タップ不要で数字が見える）
    rows = []
    for r in reversed(use[-5:]):
        cells = "".join(
            f'<span class="fc">{_fin_fmt(r[k]) + "円" if r.get(k) is not None else "−"}</span>'
            for k in ("sales", "op", "np"))
        rows.append(f'<div class="finrow"><span class="fp">{_plabel(r)}</span>{cells}</div>')
    head = ('<div class="finrow finhead"><span class="fp"></span>'
            '<span class="fc">売上高</span><span class="fc">営業利益</span><span class="fc">純利益</span></div>')
    note = ("通期実績の推移" if annual else "四半期開示の推移（累計ベース）")
    yh_note = ""
    if any(r.get("src") == "yh" for r in use):
        yh_note = ("古い年度はYahoo Financeの年次データで補完しています"
                   "（この区間の経常利益は税引前利益で代用。決算短信の実測が入り次第、自動で置き換わります）。")
    return (f'<div class="nhead">業績の推移（{note}・決算短信より）</div>'
            f'<div class="finchart">{"".join(parts)}</div>{legend}{head}{"".join(rows)}'
            f'<div class="discnote">グラフの棒・点にカーソルを合わせると数値が出ます。'
            f'決算履歴は毎晩の実行で自動蓄積され、期間は時間とともに伸びていきます。{yh_note}</div>')


FIN_CSS = """
  .finchart{margin:4px 0 2px;}
  .finlg{display:flex; gap:10px; flex-wrap:wrap; padding:4px 2px;}
  .finlg span{display:flex; align-items:center; gap:4px; font-size:9.5px; color:#6e6e73; font-weight:700;}
  .finlg i{width:10px; height:6px; display:inline-block; border-radius:2px;}
  .finrow{display:flex; gap:6px; font-size:10.5px; padding:3px 0; border-bottom:1px dashed #f0ead9;}
  .finrow .fp{flex:none; width:64px; font-weight:800; color:#7a6a45;}
  .finrow .fc{flex:1; text-align:right; font-family:ui-monospace,Menlo,monospace;}
  .finhead .fc{color:#a99a76; font-weight:700; font-family:inherit;}
"""


def exec_card_html(ec):
    """代表取締役の異動 警告カード（方向判断はしない。原文へ1タップ）"""
    if not ec:
        return ""
    rows = "".join(
        f'<a class="execrow" href="{it["url"]}" target="_blank" rel="noopener">'
        f'<span class="num">{it["date"][5:].replace("-", "/")}</span> {html.escape(it["title"])} <span class="pdf">PDF ›</span></a>'
        for it in ec[:3])
    return (f'<div class="execcard"><div class="exech">⚠ 代表取締役の異動が開示されています（経営トップ交代）</div>'
            f'{rows}'
            f'<div class="execnote">サンリオ型（好転）かGENDA型（急落）かはシステムでは判定しません。'
            f'開示原文を読んで、新旧トップの背景・交代理由・市場の受け止めをご自身で判断してください。'
            f'この銘柄の採点には含めていません。</div></div>')


def topics_card_html(tp):
    """注目開示トピックスのカード（上方修正・自社株買いなど。社長交代は別カード）"""
    if not tp:
        return ""
    tone_cls = {"pos": "tpos", "neg": "tneg", "warn": "twarn"}
    rows = []
    for it in tp[:5]:
        label, tone = TOPIC_LABEL.get(it.get("cat"), ("開示", "warn"))
        rows.append(f'<a class="tprow" href="{it["url"]}" target="_blank" rel="noopener">'
                    f'<span class="tpbadge {tone_cls.get(tone, "twarn")}">{label}</span>'
                    f'<span class="num">{it["date"][5:].replace("-", "/")}</span> '
                    f'{html.escape(it["title"])} <span class="pdf">PDF ›</span></a>')
    more = f'<div class="tpmore">ほか{len(tp) - 5}件（直近14日）</div>' if len(tp) > 5 else ""
    return (f'<div class="tpcard"><div class="tph">📌 注目開示トピックス（株価に効きやすい適時開示）</div>'
            f'{"".join(rows)}{more}'
            f'<div class="tpnote">見出しの機械判定です。内容の良し悪し・織り込み済みかどうかは開示原文でご確認ください。'
            f'採点には含めていません。</div></div>')


def topic_badge(s):
    """行の名前の横に出す小さなトピックバッジ（最新1件＋件数）"""
    tp = s.get("topics") or []
    if not tp:
        return ""
    tone_cls = {"pos": "tpos", "neg": "tneg", "warn": "twarn"}
    label, tone = TOPIC_LABEL.get(tp[0].get("cat"), ("開示", "warn"))
    extra = f"+{len(tp) - 1}" if len(tp) > 1 else ""
    return f'<span class="tpbadge {tone_cls.get(tone, "twarn")}">{label}{extra}</span>'


EXEC_CSS = """
  .execbadge{display:inline-block; font-size:9px; font-weight:800; color:#fff; background:#c62f2f;
    border-radius:4px; padding:1px 6px; margin-left:4px; vertical-align:1px;}
  .execcard{background:#fdeeee; border:1.5px solid #e8a3a3; border-radius:10px; padding:10px 12px; margin:6px 0 10px;}
  .exech{font-size:12.5px; font-weight:800; color:#c62f2f; margin-bottom:6px;}
  .execrow{display:block; font-size:11.5px; color:#1c1c1e; text-decoration:none; padding:5px 0;
    border-top:1px dashed #f0c8c8; line-height:1.6;}
  .execrow .num{color:#c62f2f; font-weight:800; margin-right:4px;}
  .execrow .pdf{color:#2e4d7b; font-weight:700; margin-left:4px;}
  .execnote{font-size:10.5px; color:#8a3a3a; line-height:1.7; padding-top:6px;}
  .tpcard{background:#f4f8f3; border:1.5px solid #b9d3b4; border-radius:10px; padding:10px 12px; margin:6px 0 10px;}
  .tph{font-size:12.5px; font-weight:800; color:#1d5c38; margin-bottom:6px;}
  .tprow{display:block; font-size:11.5px; color:#1c1c1e; text-decoration:none; padding:5px 0;
    border-top:1px dashed #cfe0cb; line-height:1.6;}
  .tprow .num{color:#5a6b58; font-weight:700; margin-right:4px;}
  .tprow .pdf{color:#2e4d7b; font-weight:700; margin-left:4px;}
  .tpbadge{display:inline-block; font-size:9.5px; font-weight:800; color:#fff; border-radius:4px;
    padding:1px 6px; margin-right:5px; vertical-align:1px;}
  .tpbadge.tpos{background:#1d7a4f;}
  .tpbadge.tneg{background:#c62f2f;}
  .tpbadge.twarn{background:#8a6d1a;}
  .tpnote{font-size:10.5px; color:#4d6350; line-height:1.7; padding-top:6px;}
  .tpmore{font-size:10.5px; color:#5a6b58; padding-top:4px;}
"""


def meter_zones(s):
    """各指標がどのゾーンにいるかを返す: {指標名: 'G'|'B'|'Y'|'R'|'N'}（判定不能は含めない）"""
    fu = s.get("fund") or {}
    lg = s.get("long") or {}
    z = {}
    def zone(val, bands):
        for lo, hi, col in bands:
            if lo <= val < hi:
                return col
        return bands[-1][2] if val >= bands[-1][1] else bands[0][2]
    per = fu.get("per")
    if per is not None:
        z["PER"] = zone(per, [(0, 10, "B"), (10, 20, "G"), (20, 40, "Y"), (40, 1e9, "R")]) if per >= 0 else "R"
    pbr = fu.get("pbr")
    if pbr is not None:
        z["PBR"] = zone(pbr, [(0, 0.5, "Y"), (0.5, 1.5, "B"), (1.5, 3, "G"), (3, 1e9, "R")])
    roe = fu.get("roe")
    if roe is not None:
        z["ROE"] = zone(roe, [(-1e9, 3, "R"), (3, 5, "Y"), (5, 10, "G"), (10, 1e9, "B")])
    dy = fu.get("div_yield")
    if dy is not None:
        z["配当"] = zone(dy, [(0, 1, "N"), (1, 3, "G"), (3, 5, "B"), (5, 1e9, "Y")])
    rsi = lg.get("rsi")
    if rsi is not None:
        z["RSI"] = zone(rsi, [(0, 30, "G"), (30, 40, "Y"), (40, 60, "N"), (60, 70, "Y"), (70, 101, "R")])
    bs = s.get("boll_sigma")
    if bs is not None:
        z["ボリンジャー"] = zone(bs, [(-1e9, -2, "G"), (-2, -1, "Y"), (-1, 1, "N"), (1, 2, "Y"), (2, 1e9, "R")])
    dv = s.get("dev25")
    if dv is not None:
        z["25日線乖離"] = zone(dv, [(-1e9, -20, "R"), (-20, -8, "G"), (-8, 0, "N"), (0, 1e9, "N")])
    ms = s.get("macd_state")
    if ms:
        z["MACD"] = {"below": "R", "golden_recent": "Y", "above": "G"}[ms]
    gc = lg.get("gc")
    if gc is not None:
        z["50/200日線"] = "G" if gc else "R"
    zz = lg.get("zone")
    if zz:
        z["支持帯の試し"] = "G" if (zz.get("touches", 0) + 1) <= 3 else "R"
    return z


def all_green_flags(s):
    """(all_green, red_free, evaluated_count)。all_green=赤も黄も無し（緑/青/中立のみ）、red_free=赤なし"""
    z = meter_zones(s)
    cols = list(z.values())
    if not cols:
        return False, False, 0
    return ("R" not in cols and "Y" not in cols), ("R" not in cols), len(cols)


def stock_meters_html(s):
    """その銘柄の指標がいまどの圏内かを、指標タブと同じ配色のミニメーターで並べる"""
    fu = s.get("fund") or {}
    lg = s.get("long") or {}
    items = []

    def bar(label, zones, lo, hi, val, fmt, note=""):
        if val is None:
            return
        v = max(lo, min(hi, val))
        pos = (v - lo) / (hi - lo) * 100
        segs = "".join(
            f'<div class="mz" style="left:{(a - lo) / (hi - lo) * 100:.1f}%; width:{(b - a) / (hi - lo) * 100:.1f}%; background:{col}"></div>'
            for a, b, col in zones)
        items.append(
            f'<div class="mrow"><span class="ml">{label}</span>'
            f'<div class="mtrack">{segs}<div class="mpin" style="left:{pos:.1f}%"></div></div>'
            f'<span class="mv num">{fmt}</span></div>' + (f'<div class="mnote">{note}</div>' if note else ""))

    G, Y, R, B, N = "#b9dcc0", "#f5e6b3", "#f2c4a8", "#c9dcf3", "#eeeae0"
    # 価値系（PER/PBR）: 標準=緑(安心) / 割安=青(魅力・要確認) / 割高=黄→赤
    per = fu.get("per")
    if per is not None:
        bar("PER", [(0, 10, B), (10, 20, G), (20, 40, Y), (40, 60, R)], 0, 60, per, f"{per:.1f}倍")
    pbr = fu.get("pbr")
    if pbr is not None:
        bar("PBR", [(0, 0.5, Y), (0.5, 1.5, B), (1.5, 3, G), (3, 8, R)], 0, 8, pbr, f"{pbr:.2f}倍")
    roe = fu.get("roe")
    if roe is not None:
        bar("ROE", [(0, 3, R), (3, 5, Y), (5, 10, G), (10, 30, B)], 0, 30, roe, f"{roe:.1f}%")
    dy = fu.get("div_yield")
    if dy is not None:
        bar("配当利回り", [(0, 1, N), (1, 3, G), (3, 5, B), (5, 8, Y)], 0, 8, dy, f"{dy:.2f}%")
    rsi = lg.get("rsi")
    if rsi is not None:
        bar("RSI(14)", [(0, 30, G), (30, 40, Y), (40, 60, N), (60, 70, Y), (70, 100, R)], 0, 100, rsi, f"{rsi:.0f}")
    bs = s.get("boll_sigma")
    if bs is not None:
        bar("ボリンジャー", [(-3, -2, G), (-2, -1, Y), (-1, 1, N), (1, 2, Y), (2, 3, R)], -3, 3, bs, f"{bs:+.1f}σ")
    dv = s.get("dev25")
    if dv is not None:
        bar("25日線乖離", [(-30, -20, R), (-20, -8, G), (-8, 0, N), (0, 10, N)], -30, 10, dv, f"{dv:+.1f}%")
    ms = s.get("macd_state")
    if ms:
        pos_v = {"below": 0.5, "golden_recent": 1.5, "above": 2.5}[ms]
        lab = {"below": "下向き", "golden_recent": "買い転換", "above": "上向き"}[ms]
        bar("MACD", [(0, 1, R), (1, 2, Y), (2, 3, G)], 0, 3, pos_v, lab)
    gc = lg.get("gc")
    if gc is not None:
        bar("50/200日線", [(0, 1, R), (1, 2, G)], 0, 2, 1.5 if gc else 0.5, "GC" if gc else "DC")
    z = lg.get("zone")
    if z:
        t = z.get("touches", 0) + 1
        bar("支持帯の試し", [(0, 3, G), (3, 5, R)], 0, 5, min(t, 5), f"{t}回目")
    dp = s.get("drop_pct")
    if dp is not None:
        bar("20日高値から", [(0, 3, N), (3, 5, Y), (5, 8, G), (8, 15, Y)], 0, 15, dp, f"−{dp:.1f}%")
    n_primary = len(items)
    # ---- 準主要 ----
    st_ = lg.get("stoch")
    if st_ is not None:
        bar("ストキャス", [(0, 20, G), (20, 30, Y), (30, 70, N), (70, 80, Y), (80, 100, R)], 0, 100, st_, f"{st_:.0f}")
    mfi = lg.get("mfi")
    if mfi is not None:
        bar("MFI", [(0, 20, G), (20, 30, Y), (30, 70, N), (70, 80, Y), (80, 100, R)], 0, 100, mfi, f"{mfi:.0f}")
    adx = lg.get("adx")
    if adx is not None:
        dip = lg.get("di_plus_over")
        bar("ADX(トレンド強さ)", [(0, 20, N), (20, 25, Y), (25, 60, G if dip else R)], 0, 60, adx,
            f"{adx:.0f}{'↑' if dip else '↓'}")
    ich = lg.get("ichimoku")
    if ich:
        bar("一目・雲", [(0, 1, R), (1, 2, Y), (2, 3, G)], 0, 3, {"below": 0.5, "in": 1.5, "above": 2.5}[ich],
            {"below": "雲の下", "in": "雲の中", "above": "雲の上"}[ich])
    atrp = lg.get("atr_pct")
    if atrp is not None:
        bar("ATR(日々の値幅)", [(0, 2, G), (2, 3.5, N), (3.5, 5, Y), (5, 10, R)], 0, 10, atrp, f"{atrp:.1f}%")
    hv = lg.get("hv20")
    if hv is not None:
        bar("HV(年率変動)", [(0, 25, G), (25, 40, N), (40, 60, Y), (60, 120, R)], 0, 120, hv, f"{hv:.0f}%")
    obv_t = lg.get("obv_trend")
    if obv_t is not None:
        bar("OBV(20日)", [(-50, -10, R), (-10, 0, Y), (0, 10, N), (10, 50, G)], -50, 50, obv_t, f"{obv_t:+.0f}%")
    er = fu.get("equity_ratio")
    if er is not None:
        bar("自己資本比率", [(0, 20, R), (20, 35, Y), (35, 50, N), (50, 100, G)], 0, 100, er, f"{er:.0f}%")
    om = fu.get("op_margin")
    if om is not None:
        bar("営業利益率", [(-10, 0, R), (0, 5, Y), (5, 10, N), (10, 30, G)], -10, 30, om, f"{om:.1f}%")
    roa = fu.get("roa")
    if roa is not None:
        bar("ROA", [(-5, 0, R), (0, 2, Y), (2, 5, N), (5, 20, G)], -5, 20, roa, f"{roa:.1f}%")
    po = fu.get("payout")
    if po is not None:
        bar("配当性向", [(0, 30, N), (30, 60, G), (60, 100, Y), (100, 150, R)], 0, 150, po, f"{po:.0f}%")
    peg = fu.get("peg")
    if peg is not None:
        bar("PEG", [(0, 1, B), (1, 2, G), (2, 3, Y), (3, 5, R)], 0, 5, peg, f"{peg:.2f}")
    n_sec = len(items) - n_primary
    if n_sec > 0:
        items.insert(n_primary, '<div class="msub">準主要指標（補助的に見る）</div>')
    if not items:
        return ""
    ag, rf, n_ev = all_green_flags(s)
    tag = (' <span class="agtag">オールグリーン</span>' if ag else (' <span class="agtag rf">赤なし</span>' if rf else ""))
    cnt_label = f'主要{n_primary}' + (f'＋準主要{n_sec}' if n_sec else "") + '指標'
    return (f'<div class="nhead">いまの指標の位置（{cnt_label}）{tag}</div>'
            '<div class="mlegend"><span style="background:#b9dcc0">安心・標準</span>'
            '<span style="background:#c9dcf3">魅力あり（要確認）</span>'
            '<span style="background:#f5e6b3">注意</span><span style="background:#f2c4a8">警戒</span>'
            '<span style="background:#eeeae0">中立</span></div>'
            '<div class="meters">' + "".join(items) + '</div>'
            '<div class="discnote">ピンが現在値。PER・PBRなど価値系は「標準」が最も安心、割安は魅力だが「割安の罠」の確認が必要。'
            'RSI・ボリンジャーなど行きすぎ系は売られすぎ側が押し目買いに有利。読み方は「指標の読み方」ページへ。</div>')


METER_CSS = """
  .meters{margin:4px 0 6px;}
  .agtag{display:inline-block; font-size:9px; font-weight:800; color:#1a5c37; background:#b9dcc0; border-radius:4px; padding:1px 6px; margin-left:4px;}
  .agtag.rf{background:#e9f3ea;}
  .mlegend{display:flex; gap:4px; flex-wrap:wrap; margin:2px 0 6px;}
  .mlegend span{font-size:9px; font-weight:700; border-radius:4px; padding:2px 6px; color:#1c1c1e;}
  .mrow{display:flex; align-items:center; gap:8px; padding:3px 0;}
  .ml{flex:none; width:108px; font-size:10px; color:var(--ink2); font-weight:700; text-align:right; white-space:nowrap;}
  .mtrack{position:relative; flex:1; height:12px; border-radius:6px; overflow:hidden; background:#eeeae0;}
  .mz{position:absolute; top:0; height:100%;}
  .mpin{position:absolute; top:-2px; width:3px; height:16px; background:#1c1c1e; border-radius:2px; transform:translateX(-50%);}
  .mv{flex:none; width:58px; font-size:11px; font-weight:800; text-align:right;}
  .mnote{font-size:9.5px; color:var(--ink3); padding-left:104px;}
  .msub{font-size:9.5px; font-weight:800; color:#a99a76; letter-spacing:.06em; margin:6px 0 2px; padding-left:4px;}
"""


RANK_HIST_PATH = DOCS / "history" / "ranks.json"


def load_rank_history():
    try:
        return json.loads(RANK_HIST_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"days": []}   # days: [{"date": "YYYY-MM-DD", "ranks": {code: rank}, "names": {code: name}}]


def is_final_run(dt):
    """その日の「確定記帳」扱いにする実行か（15:35以降=大引け後 or 手動）。途中経過は履歴に積まない"""
    mins = dt.hour * 60 + dt.minute
    return dt.weekday() >= 5 or mins >= 15 * 60 + 35 or os.environ.get("TEST_LIMIT")


def save_rank_history(data, dt):
    hist = load_rank_history()
    if not is_final_run(dt):
        return
    today = dt.date().isoformat()
    entry = {"date": today,
             "ranks": {s["code"]: s["rank"] for s in data["stocks"]},
             "names": {s["code"]: s["name"] for s in data["stocks"]}}
    days = [d for d in hist.get("days", []) if d.get("date") != today]
    days.append(entry)
    days.sort(key=lambda d: d["date"])
    hist["days"] = days[-10:]
    RANK_HIST_PATH.parent.mkdir(exist_ok=True)
    RANK_HIST_PATH.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")


def attach_rank_moves(data, dt):
    """帳簿の各銘柄に prev_rank / move / rank_series（直近10営業日）を付与"""
    hist = load_rank_history()
    days = hist.get("days", [])
    today = dt.date().isoformat()
    past = [d for d in days if d["date"] != today]  # 「昨日」は当日以外の直近確定日
    prev = past[-1] if past else None
    series_days = (past + [{"date": today, "ranks": {s["code"]: s["rank"] for s in data["stocks"]}}])[-10:]
    for s in data["stocks"]:
        code = s["code"]
        pr = prev["ranks"].get(code) if prev else None
        s["prev_rank"] = pr
        if prev is None:
            s["move"] = None          # 履歴なし
        elif pr is None:
            s["move"] = "new"         # 昨日は候補外
        else:
            s["move"] = pr - s["rank"]  # 正=ランクアップ
        s["rank_series"] = [[d["date"][5:].replace("-", "/"), d["ranks"].get(code)] for d in series_days]
    data["prev_date"] = prev["date"] if prev else None


def make_comment(s):
    """自前データだけから作る1行コメント（事実のみ・最大3項目）"""
    bits = []
    vr = s.get("vol_ratio")
    if vr and vr >= 2:
        bits.append(f"出来高が普段の{vr:.1f}倍に急増")
    st = s.get("cheap_streak") or 0
    if st >= 3:
        bits.append(f"◎水準の安さが{st}日続く")
    pos = s.get("pos1y")
    if pos is not None:
        if pos <= 0.25:
            bits.append("1年レンジの安値寄り")
        elif pos >= 0.8:
            bits.append("1年の高値圏からの一服")
    pc = s.get("prev_change")
    if pc is not None and abs(pc) >= 3:
        bits.append(f"前日比{pc:+.1f}%と大きめの動き")
    if s.get("ma200_above") is False:
        bits.append("200日線の下")
    return " ・ ".join(bits[:3])


def yen(v):
    return f"{v:,.0f}"



# 持ち金設定（帳簿ページ用・端末内保存）
CAP_JS = '''<script>
const CAP_KEY = 'kabuobaa_capital';
const FAV_KEY = 'kabuobaa_favs';
const TOPN = __TOPN__;
const capIn = document.getElementById('cap');
const allChk = document.getElementById('showall');
let favs = new Set();
try { favs = new Set(JSON.parse(localStorage.getItem(FAV_KEY) || '[]')); } catch(e){}

function saveFavs(){ localStorage.setItem(FAV_KEY, JSON.stringify(Array.from(favs))); }

function applyCap(){
  const man = parseFloat(capIn.value) || 0;
  const cap = man * 10000;
  localStorage.setItem(CAP_KEY, capIn.value || '');
  const ledger = document.querySelector('.ledger');
  const rows = Array.from(document.querySelectorAll('details.drow'));

  // ★お気に入りを先頭に（スコア順は維持したまま並べ替え）
  const favRows = rows.filter(r => favs.has(r.querySelector('.fav').dataset.code));
  const rest = rows.filter(r => !favs.has(r.querySelector('.fav').dataset.code));
  const ordered = favRows.concat(rest);
  ordered.forEach(r => ledger.appendChild(r));

  let shown = 0;
  ordered.forEach(r => {
    const cost = parseFloat(r.dataset.cost);
    const afford = cap <= 0 || cost <= cap;
    const isFav = favs.has(r.querySelector('.fav').dataset.code);
    r.classList.toggle('over', !afford);
    let visible;
    if (allChk.checked){
      visible = true;
    } else if (isFav){
      visible = true;  // お気に入りは資金に関わらず常に表示
      shown++;
    } else {
      visible = afford && shown < TOPN;
      if (visible) shown++;
    }
    r.classList.toggle('caphidden', !visible);
  });
  let n = 0;
  ordered.forEach(r => {
    const st = r.querySelector('.fav');
    st.classList.toggle('on', favs.has(st.dataset.code));
    if (!r.classList.contains('caphidden')){
      n++;
      const rk = r.querySelector('.rk');
      if (rk) rk.textContent = n;
    }
  });
  const cnt = document.getElementById('showncnt');
  if (cnt) cnt.textContent = String(n);
}

document.querySelectorAll('.fav').forEach(b => b.addEventListener('click', e => {
  e.preventDefault();
  e.stopPropagation();
  const c = b.dataset.code;
  if (favs.has(c)) favs.delete(c); else favs.add(c);
  saveFavs();
  applyCap();
}));

if (capIn){
  capIn.value = localStorage.getItem(CAP_KEY) || '';
  capIn.addEventListener('input', applyCap);
  allChk.addEventListener('change', applyCap);
  applyCap();
}

</script>'''

# 銘柄コードのコピーとSBIアプリ起動（帳簿・全銘柄で共用）
SHARED_FN_JS = '''<script>
function copyCode(btn, code, e){
  if (e){ e.preventDefault(); e.stopPropagation(); }
  const done = () => {
    const orig = btn.textContent;
    btn.textContent = 'コピー済み';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = orig; btn.classList.remove('copied'); }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(code).then(done).catch(done);
  } else { done(); }
}

function openSBI(code, e){
  if (e){ e.preventDefault(); e.stopPropagation(); }
  const go = () => { location.href = 'shortcuts://run-shortcut?name=' + encodeURIComponent('SBIへ'); };
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(code).then(go).catch(go);
  } else { go(); }
}
</script>'''

# ページ内から夜間バッチ(GitHub Actions)を起動する「いま更新」ボタン
_REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "").strip() or "hashimotokannna/kabuobaa"

UPDATE_JS = ""  # 更新ボタンは単純なページ再読み込み（F5相当）に変更済み

UPDATE_CSS = """
  header{position:relative;}
  header .t{padding-right:88px;}
  .updbtn{position:absolute; right:0; top:4px; font-size:11px; font-weight:800; color:#2e4d7b;
    background:#e8eef8; border:none; border-radius:8px; padding:6px 10px; cursor:pointer;}
"""


def render_html(data):
    stocks = data["stocks"]
    stats = data["stats"]

    weekdays = "月火水木金土日"
    dt = datetime.fromisoformat(data["generated_at"])
    date_str = f"{dt.month}/{dt.day}（{weekdays[dt.weekday()]}）"
    # 平日9:00〜15:35の実行なら「取引時間中の途中経過」とみなす
    mins = dt.hour * 60 + dt.minute
    is_intraday = dt.weekday() < 5 and (9 * 60) <= mins <= (15 * 60 + 35)
    price_label = "現在値" if is_intraday else "終値"

    chip_class = {"プライム": "prime", "スタンダード": "std", "グロース": "growth"}
    cfg = data["config"]
    universe = stats.get("universe", 0)
    excluded = stats.get("dead_excluded", 0)
    soon = data.get("soon") or []
    soon_rows = "".join(
        f'<div class="srow"><span class="sn"><b>{html.escape(s["name"])}</b> '
        f'<span class="chip {chip_class.get(s["market"], "local")}">{html.escape(s["market"])}</span> '
        f'<span class="num" style="color:var(--ink2)">{s["code"]}</span></span>'
        f'<span class="sp num">あと <b>{s["to_cheap_pct"]:.1f}%</b>下げで◎ ・ 目安 {s["trigger_price"]:,.0f}円 '
        f'<small>（いま {s["close"]:,.0f}円・質{s["q_score"] or 0:.0f}・安{s["demerit"]}）</small></span></div>'
        for s in soon)
    soon_block = (f'<div class="soonbox"><div class="soonh">まもなく買い場（安全×質は合格・◎まであと3%以内）'
                  f'<span class="cnt">{len(soon)}銘柄</span></div>{soon_rows}'
                  f'<div class="capnote">おばあさんが雑誌で目星を付けて「下がるのを待った」動作を自動化。◎に達した日に上の厳選リストへ昇格します。'
                  f'目安の値段に指値を置くのも一手。</div></div>') if soon else ""
    ex_all = data.get("exec_all") or []
    if ex_all:
        items_ex = "".join(
            f'<a class="exitem" href="{it["url"]}" target="_blank" rel="noopener">'
            f'<span class="num">{it["date"][5:].replace("-", "/")}</span> {html.escape(it["company"] or it["code"])} '
            f'<small>{html.escape(it["title"][:28])}…</small></a>' for it in ex_all[:8])
        more = f'<div class="exmore">他 {len(ex_all) - 8}件は全銘柄の「社長交代」絞り込みへ</div>' if len(ex_all) > 8 else ""
        exec_banner = (f'<details class="exban"><summary>⚠ 直近14日の代表取締役の異動: {len(ex_all)}件（全銘柄・タップで一覧）</summary>'
                       f'<div class="exlist">{items_ex}{more}</div></details>')
    else:
        exec_banner = ""
    tp_all = data.get("topics_all") or []
    if tp_all:
        tone_cls = {"pos": "tpos", "neg": "tneg", "warn": "twarn"}
        by_cat = {}
        for it in tp_all:
            by_cat.setdefault(it.get("cat"), []).append(it)
        cat_parts = []
        for key, label, tone, _, _ in TOPIC_RULES:
            lst = by_cat.get(key)
            if not lst:
                continue
            rows_t = "".join(
                f'<a class="exitem" href="{it["url"]}" target="_blank" rel="noopener">'
                f'<span class="num">{it["date"][5:].replace("-", "/")}</span> {html.escape(it["company"] or it["code"])} '
                f'<small>{html.escape(it["title"][:26])}…</small></a>' for it in lst[:6])
            more_t = f'<div class="exmore">他 {len(lst) - 6}件は全銘柄の「注目開示」絞り込みへ</div>' if len(lst) > 6 else ""
            cat_parts.append(f'<div class="tpcath"><span class="tpbadge {tone_cls.get(tone, "twarn")}">{label}</span>'
                             f'<small>{len(lst)}件</small></div>{rows_t}{more_t}')
        topics_banner = (f'<details class="exban tpban"><summary>📌 直近3営業日の注目開示: {len(tp_all)}件'
                         f'（上方修正・自社株買いなど・タップで一覧）</summary>'
                         f'<div class="exlist">{"".join(cat_parts)}</div></details>')
    else:
        topics_banner = ""
    mkt = data.get("market")
    if mkt and mkt.get("above200") is not None:
        extra = []
        if mkt.get("adr_1d") is not None:
            extra.append(f'値上がり{mkt["adv"]:,}／値下がり{mkt["dec"]:,}')
        if mkt.get("new_high") is not None:
            extra.append(f'新高値{mkt["new_high"]}・新安値{mkt["new_low"]}')
        if mkt.get("nt_ratio"):
            extra.append(f'NT倍率{mkt["nt_ratio"]:.2f}')
        extra_s = ("<br><small>" + " ・ ".join(extra) + "</small>") if extra else ""
        if mkt["above200"]:
            market_banner = (f'<div class="mkt ok">地合い: 上昇基調（日経平均が200日線の上・'
                             f'直近20日 {mkt["chg20"]:+.1f}%）{extra_s}</div>')
        else:
            market_banner = (f'<div class="mkt ng">地合い警戒: 日経平均が200日線の下（直近20日 '
                             f'{mkt["chg20"]:+.1f}%）。全体が下落基調の間は、押し目買いの成功率が'
                             f'下がります。買いは普段より慎重に{extra_s}</div>')
    else:
        market_banner = ""

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

    def trades_html(s):
        tr = s.get("trades") or []
        if not tr:
            return ""
        rows_t = []
        for t in tr:
            bd = t["buy_date"][5:].replace("-", "/")
            if t["sell_date"]:
                sd = t["sell_date"][5:].replace("-", "/")
                res = ("損切り" if t.get("stop") else "利確")
                cls = "minus" if t.get("stop") else "plus"
                rows_t.append(f'<div class="hrow"><span class="num">{bd} 買 → {sd} {res}（{t["held"]}日）</span>'
                              f'<span class="num {cls}">{t["pnl"]:+,.0f}円</span></div>')
            else:
                cls = "plus" if t["pnl"] >= 0 else "minus"
                rows_t.append(f'<div class="hrow"><span class="num">{bd} 買 → 持ち越し中（{t["held"]}日）</span>'
                              f'<span class="num {cls}">{t["pnl"]:+,.0f}円</span></div>')
        return (f'<div class="nhead">この銘柄で同じ買い方をしていたら（この1年・直近{len(tr)}回）</div>'
                + "".join(rows_t)
                + '<div class="discnote">◎で翌日始値100株買い→+5%で売り（損切りなし）の仮想記録。'
                  '「勝ち癖」の有無を個別に確認できます。</div>')

    def demerit_xref(s):
        dm = s.get("demerit")
        if dm is None:
            return ""
        rk = s.get("demerit_rank")
        hits = s.get("demerit_hits") or []
        sev_cls = {"致命": "sevA", "重い": "sevB", "軽い": "sevC"}
        head = (f'<div class="nhead">減点方式では: 全体{rk:,}位' if rk else '<div class="nhead">減点方式では: ') \
            + (f'（合計 −{dm}点）</div>' if dm else '（<b class="okc">無傷</b>）</div>')
        body = "".join(
            f'<div class="hit"><span class="sev {sev_cls[h[0]]}">{h[0]}</span>{html.escape(h[1])}'
            f'<span class="num hp">−{h[2]}</span></div>' for h in hits) \
            or '<div class="hit ok">買ってはいけない条件に一つも該当なし</div>'
        return head + body + '<div class="discnote">加点で光っていても減点が重い銘柄は「魅力はあるが欠点もある」銘柄。'\
                             '両方の物差しで見てください（一覧は「無傷」タブ）。</div>'

    def move_html(s):
        mv = s.get("move")
        if mv is None:
            return ""
        if mv == "new":
            return '<span class="mvb new">NEW</span>'
        if mv > 0:
            return f'<span class="mvb up">↑{mv}</span>'
        if mv < 0:
            return f'<span class="mvb dn">↓{abs(mv)}</span>'
        return '<span class="mvb flat">→</span>'

    def rank_series_html(s):
        rs = s.get("rank_series") or []
        if len(rs) < 2:
            return ""
        cells = "".join(
            f'<div class="rc"><div class="rd">{d}</div><div class="rr num">{("−" if r is None else f"{r}位")}</div></div>'
            for d, r in rs)
        return (f'<div class="nhead">順位の推移（直近{len(rs)}営業日・確定記帳ベース）</div>'
                f'<div class="rseries">{cells}</div>'
                '<div class="discnote">「−」は候補圏外だった日。連日上位に居続ける銘柄は根拠が安定、'
                '急浮上はその日の下げで安さが増した銘柄。</div>')

    def pc_html(s):
        pc = s.get("prev_change")
        if pc is None:
            return ""
        cls = "up" if pc > 0 else "dn" if pc < 0 else "flat"
        return f'<div class="p3 num {cls}">前日比 {pc:+.1f}%</div>'

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
    for s in stocks:
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
        reasons_html = "".join(
            f'<div class="reason">・{html.escape(r)}</div>' for r in s.get("reasons", []))
        fund_html = ""
        fu = s.get("fund")
        if fu:
            frows = []
            if fu.get("per") is not None:
                tag = ("割安圏" if fu["per"] < 10 else "標準的" if fu["per"] <= 20 else "割高圏")
                frows.append(f'<div class="fact"><span>PER（株価収益率）</span>'
                             f'<span class="num">{fu["per"]:.1f}倍 <small>{tag}</small></span></div>')
            if fu.get("pbr") is not None:
                tag = ("解散価値割れ" if fu["pbr"] < 1 else "標準的" if fu["pbr"] <= 3 else "割高圏")
                frows.append(f'<div class="fact"><span>PBR（株価純資産倍率）</span>'
                             f'<span class="num">{fu["pbr"]:.2f}倍 <small>{tag}</small></span></div>')
            if fu.get("roe") is not None:
                tag = ("高収益" if fu["roe"] >= 10 else "標準的" if fu["roe"] >= 5 else "低収益")
                frows.append(f'<div class="fact"><span>ROE（自己資本利益率）</span>'
                             f'<span class="num">{fu["roe"]:.1f}% <small>{tag}</small></span></div>')
            if fu.get("div_yield") is not None:
                frows.append(f'<div class="fact"><span>配当利回り（実績）</span>'
                             f'<span class="num">{fu["div_yield"]:.2f}%</span></div>')
            if fu.get("mcap_oku"):
                frows.append(f'<div class="fact"><span>時価総額</span>'
                             f'<span class="num">{fu["mcap_oku"]:,}億円</span></div>')
            if frows:
                src_note = ("PER・PBR・ROE・時価総額は、決算短信の数字（EPS・BPS・発行株数／JPX J-Quants）と最新株価からこのシステムが算出"
                            if fu.get("computed") else "この銘柄は決算数字が取得できず、配信元の指標値を参照")
                fund_html = ('<div class="nhead">ファンダメンタル指標</div>' + "".join(frows)
                             + f'<div class="discnote">{src_note}。読み方は「指標の読み方」ページ参照。低PER・低PBRには'
                               '業績悪化を織り込んだ「割安の罠」もあるため、単独では判断しないこと。</div>')
        tech_html = spark_block_html(s.get("spark"), s.get("spark10"), s.get("long"),
                                     spark1=s.get("spark1"), sparkall=s.get("sparkall"),
                                     years_all=s.get("years_all"))
        disc_html = ""
        if s.get("disclosures"):
            rows_d = "".join(
                f'<a class="disc" href="{d["url"]}" target="_blank" rel="noopener">'
                f'<span class="num">{d["date"]}</span> {html.escape(d["title"])}</a>'
                for d in s["disclosures"])
            disc_html = (f'<div class="nhead">会社からの発表（TDnet適時開示・直近30日）</div>{rows_d}'
                         f'<div class="discnote">決算・業績修正などの発表直後は値動きが大きくなりがちです。'
                         f'見出しをタップすると原文（PDF）が開きます。</div>')
        rows_html.append(f"""
      <details class="drow" data-cost="{s["cost"]}">
      <summary class="row">
        <div class="rk num">{s["rank"]}</div>
        <div class="nm">
          <div class="n1">{html.escape(s["name"])} <span class="chip {chip}">{html.escape(s["market"])}</span>{new_mark}{'<span class="tri3badge">安全×質×時</span>' if s.get("tri") else ""}{move_html(s)}{'<span class="execbadge">社長交代</span>' if s.get("exec_change") else ""}{topic_badge(s)}</div>
          <div class="n2 num"><button class="codebtn" onclick="copyCode(this, '{s["code"]}', event)">{s["code"]} ⧉</button> ・ {html.escape(s["group"])} ・ 100株 {s["cost"] / 10000:,.1f}万円 ・ {s["score"]:.0f}点<span class="nofund">資金不足</span></div>
          {f'<div class="cmt">{html.escape(s["comment"])}</div>' if s.get("comment") else ""}
        </div>
        <div class="px">
          <div class="p1 num"><small>{price_label}</small> {yen(s["close"])}<small>円</small></div>
          <div class="p2 num {"athigh" if s["drop_pct"] < 0.05 else "drop"}">{"20日高値を更新中" if s["drop_pct"] < 0.05 else f'高値から −{s["drop_pct"]:.1f}%'}</div>
          {pc_html(s)}
        </div>
        <div>{badge}</div>
        <button class="fav" data-code="{s["code"]}" aria-label="お気に入り">★</button>
        <div class="chev">›</div>
      </summary>
      <div class="notebox">
        {exec_card_html(s.get("exec_change"))}
        {topics_card_html(s.get("topics"))}
        {stock_meters_html(s)}
        <div class="nhead">選ばれた根拠（総合 {s["score"]:.0f}点 ＝ 質 {s.get("q_score") or 0:.0f} + タイミング {s.get("t_score") or 0:.0f}）</div>
        {reasons_html}
        {rank_series_html(s)}
        {demerit_xref(s)}
        {trades_html(s)}
        {tech_html}
        {fund_html}
        {fin_chart_html((s.get("fund") or {}).get("hist"))}
        {disc_html}
        {latest_block(s)}
        <div class="fact"><span>100株の必要資金</span><span class="num">{s["cost"] / 10000:,.1f}万円</span></div>
        <div class="fact"><span>普段の値段（20日平均）</span><span class="num">{yen(s["usual"])}円</span></div>
        <div class="fact"><span>直近の高値（20日）</span><span class="num">{yen(s["high20"])}円</span></div>
        <div class="fact"><span>高値からの下げ</span><span class="num drop">−{yen(s["drop_yen"])}円（−{s["drop_pct"]:.1f}%）</span></div>
        {range1y}
        <div class="nhead">ノート（新しい順）</div>
        {day_rows(s)}
        <div class="linkrow">
          <a class="ylink" href="{yahoo_url}" target="_blank" rel="noopener">Yahoo!ファイナンス →</a>
          <button class="ylink sbi" onclick="openSBI('{s["code"]}', event)">SBI証券アプリで見る</button>
          <a class="ylink" href="map.html?c={s["code"]}">🗺 関連マップで見る</a>
        </div>
      </div>
      </details>""")

    body_rows = "\n".join(rows_html)
    excluded = stats.get("dead_excluded", 0)
    universe = stats.get("universe", 0)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="robots" content="noindex, nofollow">
<link rel="apple-touch-icon" href="icon.png">
<link rel="icon" type="image/png" href="icon.png">
<style>html,body{{touch-action:pan-x pan-y;}}</style>
<script>
document.addEventListener('gesturestart',function(e){{e.preventDefault();}});
document.addEventListener('gesturechange',function(e){{e.preventDefault();}});
</script>
<title>今夜の厳選{cfg["TOP_N"]}銘柄 ｜ 株ノート</title>
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
  .n1{{font-size:13.5px; font-weight:700; line-height:1.5;}}
  .n2{{font-size:10.5px; color:var(--ink2); margin-top:2px;}}
  .px{{text-align:right; flex:none;}}
  .p1{{font-size:14.5px; font-weight:700;}}
  .p1 small{{font-size:10px; font-weight:600; color:var(--ink2);}}
  .p2{{font-size:10.5px; margin-top:2px;}}
  .drop{{color:var(--cheap); font-weight:700;}}
  .athigh{{color:#2e5fa8; font-weight:800;}}
__METERCSS__
__EXECCSS__
  html, body{{overflow-x:hidden; max-width:100%;}}
  body{{overscroll-behavior-x:none;}}
  *{{min-width:0;}}
  img, svg{{max-width:100%;}}
  .topnav, .chips, .filters, .rseries{{overflow-x:auto; overscroll-behavior-x:contain; -webkit-overflow-scrolling:touch;}}
  .card, .hcard, .ledger, .list, .flatlist, .soonbox, .capcard, details, .notebox, .ubody{{max-width:100%; overflow-wrap:anywhere;}}
  input, select{{max-width:100%;}}

  .hrow{{display:flex; justify-content:space-between; font-size:11px; padding:4px 0;
    border-bottom:1px dashed #f0ead9;}}
  .plus{{color:#2e7d32;}} .minus{{color:var(--cheap);}}
  .hit{{display:flex; align-items:center; gap:6px; font-size:11px; padding:4px 0;
    border-bottom:1px dashed #f0ead9;}}
  .hit.ok{{color:#1a5c37; font-weight:700;}} .okc{{color:#1a5c37;}}
  .hp{{margin-left:auto; font-weight:800; color:#8a5a17;}}
  .sev{{flex:none; font-size:9px; font-weight:800; border-radius:4px; padding:1px 5px;}}
  .sevA{{background:#fdeeee; color:#c62f2f;}} .sevB{{background:var(--mild-bg); color:#b06a00;}}
  .sevC{{background:#eef0f4; color:#4b4f57;}}
  .p3{{font-size:10px; margin-top:1px; font-weight:700;}}
  .p3.up{{color:#c62f2f;}} .p3.dn{{color:#2e5fa8;}} .p3.flat{{color:var(--ink3);}}
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
  .linkrow{{display:flex; gap:8px; margin-top:12px;}}
  .ylink{{flex:1; display:block; font-size:12px; font-weight:700; color:#2e4d7b; font-family:inherit;
    text-decoration:none; text-align:center; background:#eef2f8; border-radius:9px;
    padding:9px; border:none; cursor:pointer;}}
  .ylink.sbi{{color:#1a5c37; background:#e9f3ea;}}
  .ylink.buy{{color:#fff; background:#1c1c1e;}}
  .linkrow{{flex-wrap:wrap;}} .linkrow .ylink{{min-width:45%;}}
  .codebtn{{font-family:inherit; font-size:inherit; color:#2e4d7b; background:none;
    border:none; border-bottom:1px dashed #9db3cc; padding:0 1px; cursor:pointer;}}
  .codebtn.copied{{color:#1a5c37; border-bottom-color:#1a5c37;}}
__NAVCSS__
  .pnav{{display:flex; gap:8px; padding:0 0 10px;}}
  .pnav a{{flex:1; font-size:12px; font-weight:700; color:#4a3f28; text-decoration:none;
    background:#f4eedd; border-radius:10px; padding:9px 10px; text-align:center;}}
  details.crit{{background:#fff; border-radius:12px; margin-bottom:12px;
    box-shadow:0 1px 3px rgba(0,0,0,.05);}}
  details.crit summary{{list-style:none; cursor:pointer; font-size:12px; font-weight:800;
    color:#4a3f28; padding:11px 14px; display:flex; justify-content:space-between; align-items:center;}}
  details.crit summary::-webkit-details-marker{{display:none;}}
  details.crit .chev{{color:#c9bd9d; transition:transform .15s;}}
  details.crit[open] .chev{{transform:rotate(90deg);}}
  .critbody{{padding:0 14px 12px; border-top:1px solid #f0ead9;}}
  .step{{font-size:11.5px; line-height:1.7; color:var(--ink2); padding:7px 0;
    border-bottom:1px dashed #f0ead9;}}
  .step:last-child{{border-bottom:none;}}
  .step b{{color:var(--ink);}}
  details.gsec summary.sec{{list-style:none; cursor:pointer;}}
  details.gsec summary.sec::-webkit-details-marker{{display:none;}}
  .gchev{{display:inline-block; color:#c9bd9d; font-weight:700; margin-left:4px;
    transition:transform .15s;}}
  details.gsec[open] .gchev{{transform:rotate(90deg);}}
  .capcard{{background:#fff; border-radius:12px; padding:11px 14px; margin-bottom:12px;
    box-shadow:0 1px 3px rgba(0,0,0,.05);}}
  .caprow{{font-size:13px; font-weight:700; display:flex; align-items:center; gap:6px; flex-wrap:wrap;}}
  .capin{{width:70px; font-size:15px; font-weight:700; padding:5px 8px;
    border:1.5px solid #d9d2bf; border-radius:8px; background:#fff; text-align:right;}}
  .caponly{{font-size:11.5px; font-weight:600; color:var(--ink2); margin-left:auto;
    display:flex; align-items:center; gap:4px;}}
  .capnote{{font-size:10.5px; color:var(--ink3); line-height:1.6; margin-top:6px;}}
  .reason{{font-size:11px; color:var(--ink2); line-height:1.7; padding:2px 0;}}
  .nofund{{display:none; color:#fff; background:#b06a00; font-size:9px; font-weight:800;
    border-radius:4px; padding:1px 4px; margin-left:6px; vertical-align:1px;}}
  details.drow.over summary.row{{opacity:.45;}}
  details.drow.over .nofund{{display:inline;}}
  .caphidden{{display:none !important;}}
  .cmt{{font-size:10px; color:#8a5a17; margin-top:2px; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis;}}
  .fav{{background:none; border:none; font-size:18px; color:#ddd3ba; padding:0 2px;
    cursor:pointer; line-height:1;}}
  .fav.on{{color:#e0a300;}}
  .disc{{display:block; font-size:11px; line-height:1.6; color:#2e4d7b; text-decoration:none;
    padding:4px 0; border-bottom:1px dashed #f0ead9;}}
  .disc .num{{color:var(--ink2); font-weight:700; margin-right:4px;}}
  .discnote{{font-size:10px; color:var(--ink3); line-height:1.6; padding:5px 0 2px;}}
  .spark{{margin:4px 0 2px;}}
  .mkt{{font-size:11.5px; font-weight:700; border-radius:10px; padding:9px 12px;
    margin-bottom:10px; line-height:1.6;}}
  .mkt.ok{{background:#e9f3ea; color:#3a5a40;}}
  .mkt.ng{{background:var(--mild-bg); color:#8a5a17;}}
  .mvb{{display:inline-block; font-size:9.5px; font-weight:800; border-radius:4px; padding:1px 5px; margin-left:4px; vertical-align:1px;}}
  .mvb.up{{color:#c62f2f; background:#fdeeee;}} .mvb.dn{{color:#2e5fa8; background:#e8eef8;}}
  .mvb.flat{{color:var(--ink3); background:#f0f0f4;}} .mvb.new{{color:#b06a00; background:var(--mild-bg);}}
  .rseries{{display:flex; gap:4px; overflow-x:auto; padding:4px 0;}}
  .rc{{flex:none; min-width:44px; text-align:center; background:#fff; border-radius:8px; padding:5px 3px;}}
  .rd{{font-size:9px; color:var(--ink3);}} .rr{{font-size:12px; font-weight:800;}}
  .tri3badge{{display:inline-block; font-size:9px; font-weight:800; color:#fff; background:#3a5a40;
    border-radius:4px; padding:1px 5px; margin-left:4px; vertical-align:1px;}}
  details.exban{{background:#fdeeee; border:1.5px solid #e8a3a3; border-radius:12px; margin-bottom:10px;}}
  details.exban summary{{list-style:none; cursor:pointer; font-size:12px; font-weight:800; color:#c62f2f; padding:10px 14px;}}
  details.exban summary::-webkit-details-marker{{display:none;}}
  .exlist{{padding:0 14px 10px;}}
  .exitem{{display:block; font-size:11.5px; color:#1c1c1e; text-decoration:none; padding:5px 0; border-top:1px dashed #f0c8c8; line-height:1.6;}}
  .exitem .num{{color:#c62f2f; font-weight:800; margin-right:4px;}} .exitem small{{color:var(--ink2);}}
  .exmore{{font-size:10.5px; color:#8a3a3a; padding-top:6px;}}
  details.tpban{{background:#f4f8f3; border-color:#b9d3b4;}}
  details.tpban summary{{color:#1d5c38;}}
  details.tpban .exitem{{border-top-color:#cfe0cb;}}
  details.tpban .exitem .num{{color:#5a6b58;}}
  details.tpban .exmore{{color:#4d6350;}}
  .tpcath{{padding-top:8px; font-weight:800;}} .tpcath small{{color:#4d6350; font-weight:600; margin-left:4px;}}
  .soonbox{{background:#fff; border-radius:14px; padding:4px 0 10px; margin-top:12px; box-shadow:0 1px 3px rgba(0,0,0,.06);}}
  .soonh{{font-size:12px; font-weight:800; color:#7a6a45; letter-spacing:.06em; padding:12px 14px 6px;
    display:flex; justify-content:space-between; align-items:baseline;}}
  .soonh .cnt{{font-weight:600; color:#a99a76; font-size:10.5px;}}
  .srow{{display:flex; justify-content:space-between; align-items:center; gap:8px; padding:8px 14px;
    border-top:1px solid #f0ead9; font-size:12px;}}
  .sn{{flex:1; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}}
  .sp{{flex:none; text-align:right; font-size:11px; color:var(--ink2);}} .sp b{{color:#b06a00;}}
  .soonbox .capnote{{padding:8px 14px 0;}}
</style>
</head>
<body>
__NAV__
<header>
  <button type="button" class="updbtn" onclick="location.reload()">🔄 更新</button>
  <div class="t">今夜の厳選<span id="showncnt">{cfg["TOP_N"]}</span>銘柄</div>
  <div class="s">{date_str} {dt.hour:02d}:{dt.minute:02d} 記帳{"（取引時間中・当日分は途中経過）" if is_intraday else ""} ・ 根拠スコア順{f' ・ ↑↓は {data["prev_date"][5:].replace("-", "/")} 確定記帳との比較' if data.get("prev_date") else ""} ・ 判断はご自身で</div>
</header>
{market_banner}
{exec_banner}
{topics_banner}
<details class="crit">
  <summary>この厳選{cfg["TOP_N"]}銘柄の選定基準（タップで開閉）<span class="chev">›</span></summary>
  <div class="critbody">
    <div class="step"><b>1. 対象</b> 東証プライム・スタンダード・グロースの全銘柄（{universe:,}銘柄）</div>
    <div class="step"><b>2. 土俵に上げない</b> 上場から日足{cfg["MIN_RECORDS"]}日未満 ／ 株価{cfg["MIN_PRICE"]}円未満 ／ 直近{cfg["RECENT_DAYS"]}日の平均売買代金{int(cfg["MIN_TURNOVER"]/10000):,}万円未満（売りたい時に売れない銘柄を避ける）</div>
    <div class="step"><b>3. 危ない下げ方を除外</b> ①1年高値から{int(cfg["DEAD_DRAWDOWN"]*100)}%以上下落・長期の下落トレンド継続（終わった株） ②直近10日に1日{cfg["KNIFE_DROP_1D"]:.0f}%超の急落（決算ミス等の材料落ち=落ちるナイフ） ③日々の値動きが±{cfg["MAX_VOL20"]:.1f}%超の荒い銘柄 ④1年安値圏を更新中 ⑤下げ止まり未確認（前日から安値切り下げ中）——本日計{excluded:,}銘柄を除外</div>
    <div class="step"><b>「高値から −◯%」の定義</b> 直近{cfg["RECENT_DAYS"]}営業日の最高値に対して、いまの値段が何%下にあるか。最高値はいまの値段を含む期間の天井なので、この数字は<b>0%が上限でプラスにはなりません</b>（今日が最高値なら「20日高値を更新中」と青で表示）。「今日の勢い」は別途、前日比で併記します</div>
    <div class="step"><b>4. 三層で選ぶ</b> <b>安全</b>（減点方式の合計が{cfg["SAFE_MAX_DEMERIT"]}以下・致命なし）× <b>質</b>（財務・トレンド・実績・長期テクニカルの加点）× <b>タイミング</b>（◎の安さ・下げ止まり・RSI/MACD/ボリンジャー等）。三層すべて合格の銘柄を先頭に、その中を総合スコア順に。安全×質は合格でも◎にあと少しの銘柄は「まもなく買い場」に</div>
    <div class="step"><b>4-2. 根拠スコアで採点</b> 残った銘柄を「いまの安さ」「下げの質（じわ下げか急落か・値動きの穏やかさ）」「トレンドの地合い（200日線の上の押し目か・1年レンジ内の位置）」「過去1年でこの買い方が利確+{cfg["TP_PCT"]:.0f}%を取れた実績」「10年データの長期テクニカル（支持帯の反発実績と試行回数・ゴールデンクロス・RSI・MACD・ボリンジャーバンド・25日線乖離・W底・セリクラ兆候、補助としてストキャス・DMI/ADX・一目の雲・OBV/MFI・ATR）」「売買のしやすさ」の6観点で採点し、上位{cfg["SHORTLIST_N"]}銘柄を候補に</div>
    <div class="step"><b>5. 厳選{cfg["TOP_N"]}銘柄</b> 候補のうち、持ち金設定があれば「100株買える銘柄」だけを対象に、スコア上位{cfg["TOP_N"]}銘柄を表示。各銘柄の点数の内訳はタップで確認できます</div>
    <div class="step"><b>6. 目安ラベル</b> ◎=高値から{cfg["CHEAP_PCT"]:.0f}%以上安い ／ ○={cfg["MILD_PCT"]:.0f}%以上安い ／ 「普段の値段」={cfg["RECENT_DAYS"]}日の終値平均</div>
    <div class="step"><b>7. 財務の健全性（自動判定）</b> PER（20倍以下+・60倍超−）、PBR（0.5〜1.5倍+・8倍超−）、ROE（10%以上+・3%未満−）、赤字（PERマイナス −20点）、配当利回り3%以上（+）、時価総額（小型−・中大型+）に加え、補助として自己資本比率（50%以上+・20%未満−）、営業利益率（10%以上+・営業赤字−）、ROA（5%以上+）、配当性向100%超（−）、PEG1倍以下（+）を固定基準で採点。財務データはJPX公式（J-Quants）の決算短信。取得できなかった項目は判定をスキップします</div>
    <div class="step"><b>8. 投資スタイル調整</b> この採点は「夜1回の判断・短期回転（数日〜数週間）・50万円規模」向けに調整。夜間ギャップ（翌朝の窓開け）が小さい銘柄を加点、利確まで平均25日超の資金拘束銘柄を減点</div>
    <div class="step" style="color:#8a5a17;">基準は毎回の実行時点の設定で、この文章も自動で追随します。個々の銘柄の判定理由は「全銘柄の判定一覧」で確認できます。</div>
  </div>
</details>
<div class="capcard">
  <div class="caprow">持ち金 <input id="cap" class="capin num" type="number" inputmode="numeric"
    placeholder="50"> 万円
    <label class="caponly"><input id="showall" type="checkbox"> 候補{len(stocks)}銘柄すべて表示</label></div>
  <div class="capnote">この端末にだけ保存。「100株買える銘柄」の中からスコア上位{cfg["TOP_N"]}銘柄が選ばれます。
  財務の健全性（PER・PBR・ROE・配当・時価総額）はシステムが固定基準で自動判定し、
  各銘柄のスコアと根拠に反映済みです。</div>
</div>
<div class="ledger">
{body_rows}
</div>
{soon_block}
<footer>
  対象 {universe:,}銘柄 ／ 右肩下がり・急落直後・荒い値動き等で除外 {excluded:,}銘柄<br>
  ◎=直近{data["config"]["RECENT_DAYS"]}日高値から{data["config"]["CHEAP_PCT"]:.0f}%以上安い ・ ○={data["config"]["MILD_PCT"]:.0f}%以上安い<br>
  データ: Yahoo Finance ・ JPX公式（J-Quants） ・ TDnet ・ このページは判断材料の表示のみ
</footer>
__CAPJS__
</body>
</html>
""".replace("__CAPJS__", CAP_JS.replace("__TOPN__", str(cfg["TOP_N"])) + SHARED_FN_JS + SPARK_JS + UPDATE_JS + NAV_JS) \
       .replace("__NAVCSS__", NAV_CSS) \
       .replace("__METERCSS__", METER_CSS) \
       .replace("__EXECCSS__", EXEC_CSS + SPARK_CSS + UPDATE_CSS + FIN_CSS) \
       .replace("__NAV__", nav_html("index"))


# 全ページ共通のナビゲーション
NAV_CSS = """
  .topnav{display:flex; gap:6px; overflow-x:auto; padding:2px 0 12px;
    -webkit-overflow-scrolling:touch;}
  .topnav a{flex:none; font-size:12.5px; font-weight:700; color:#4a3f28;
    text-decoration:none; background:#f4eedd; border-radius:10px; padding:8px 14px;}
  .topnav a.act{background:#1c1c1e; color:#fff;}
  .topnav a.subtag{background:none; color:#8a5a17; font-weight:800; padding-left:2px;}
"""

NAV_ITEMS = [
    ("guide.html", "はじめに", "guide"),
    ("indicators.html", "指標の読み方", "indicators"),
    ("universe.html", "全銘柄台帳", "universe"),
    ("index.html", "今夜の厳選", "index"),
    ("map.html", "銘柄マップ", "map"),
    ("caps.html", "時価総額マップ", "caps"),
]


NAV_JS = """<script>
(function(){
  const order = ['guide.html', 'indicators.html', 'universe.html', 'index.html', 'map.html', 'caps.html'];
  let here = location.pathname.split('/').pop();
  if (!here) here = 'index.html';
  const idx = order.indexOf(here);
  if (idx < 0) return;
  let sx = 0, sy = 0, st = 0;
  document.addEventListener('touchstart', e => {
    const t = e.touches[0]; sx = t.clientX; sy = t.clientY; st = Date.now();
  }, {passive: true});
  document.addEventListener('touchend', e => {
    const t = e.changedTouches[0];
    const dx = t.clientX - sx, dy = t.clientY - sy;
    if (Date.now() - st > 600) return;
    if (Math.abs(dx) < 80 || Math.abs(dy) > 50 || Math.abs(dx) < Math.abs(dy) * 2) return;
    if (e.target.closest('.topnav, .chips, .filters, input, textarea, .spark')) return;
    const j = dx < 0 ? idx + 1 : idx - 1;
    if (j >= 0 && j < order.length) location.href = order[j];
  }, {passive: true});
})();
</script>"""


def nav_html(active):
    parts = []
    sub = active in ("tob", "sim")  # 銘柄マップ・時価総額マップはメインタブ
    for href, label, key in NAV_ITEMS:
        cls = ' class="act"' if (key == active or (sub and key == "guide")) else ""
        parts.append(f'<a href="{href}"{cls}>{label}</a>')
    if sub:
        parts.append('<a class="subtag">› その他の機能</a>')
    return '<div class="topnav">' + "".join(parts) + "</div>"


# ------------------------------------------------------------
# サブページ共通の枠（cream帳簿デザイン）
# ------------------------------------------------------------
SUBPAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1, user-scalable=no">
<meta name="robots" content="noindex, nofollow">
<link rel="apple-touch-icon" href="icon.png">
<link rel="icon" type="image/png" href="icon.png">
<style>html,body{touch-action:pan-x pan-y;}</style>
<script>
document.addEventListener('gesturestart',function(e){e.preventDefault();});
document.addEventListener('gesturechange',function(e){e.preventDefault();});
</script>
<title>__TITLE__ ｜ 株ノート</title>
<style>
  :root{--ink:#1c1c1e; --ink2:#6e6e73; --ink3:#aeaeb2; --paper:#faf6ec;
    --paper-line:#e7e0cf; --bg:#f2f2f7; --line:#e5e5ea; --cheap:#c62f2f; --cheap-bg:#fdeeee;
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
__NAVCSS__
  html, body{overflow-x:hidden; max-width:100%;}
  body{overscroll-behavior-x:none;}
  *{min-width:0;}
  img, svg{max-width:100%;}
  .topnav, .chips, .filters{overflow-x:auto; overscroll-behavior-x:contain; -webkit-overflow-scrolling:touch;}
  .card, .hcard, .ledger, .list, .flatlist, .soonbox, .capcard, details, .notebox, .ubody{max-width:100%; overflow-wrap:anywhere;}
  input, select{max-width:100%;}

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
__NAV__
<header>__HEADBTN__<div class="t">__TITLE__</div><div class="s">__SUBTITLE__</div></header>
__BODY__
<div class="note">__FOOTNOTE__</div>
__SCRIPT__
__NAVJS__
</body>
</html>
"""


STATUS_DEF = {
    "picked": ("候補", "#fdeeee", "#c62f2f", "根拠スコア上位の厳選候補（「今夜の厳選」に表示）"),
    "bench":  ("圏外", "#eef0f4", "#4b4f57", "対象内だがスコアが候補圏に届かず"),
    "dead":   ("除外", "#efe6f5", "#6b4487", "終わった株・急落直後・荒い値動き・下げ止まり未確認で除外"),
    "skip":   ("対象外", "#fdf3e3", "#b06a00", "土俵に上げない条件に該当"),
    "fail":   ("失敗", "#e8e8e8", "#666", "データ取得失敗"),
}


def render_universe(all_results, stats, dt):
    """全銘柄の判定一覧ページ（なぜ対象外かが後から分かる台帳）"""
    counts = {}
    for r in all_results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    chips = ['<div class="chips"><button class="fbtn on" data-f="all">すべて '
             f'{len(all_results):,}</button>']
    for key, (label, bg, fg, _desc) in STATUS_DEF.items():
        if counts.get(key):
            chips.append(f'<button class="fbtn" data-f="{key}" style="background:{bg}; color:{fg}">'
                         f'{label} {counts[key]:,}</button>')
    n_flaw = sum(1 for r in all_results if r.get("demerit") == 0)
    n_soon = sum(1 for r in all_results if r.get("soon"))
    if n_flaw:
        chips.append(f'<button class="fbtn" data-f="__flaw" style="background:#e9f3ea; color:#1a5c37">無傷 {n_flaw:,}</button>')
    if n_soon:
        chips.append(f'<button class="fbtn" data-f="__soon" style="background:#fdf3e3; color:#b06a00">まもなく {n_soon:,}</button>')
    n_ag = sum(1 for r in all_results if r.get("all_green"))
    n_rf = sum(1 for r in all_results if r.get("red_free"))
    chips.append(f'<button class="fbtn" data-f="__ag" style="background:#b9dcc0; color:#1a5c37">オールグリーン {n_ag:,}</button>')
    chips.append(f'<button class="fbtn" data-f="__rf" style="background:#e9f3ea; color:#3a5a40">赤なし {n_rf:,}</button>')
    n_exec = sum(1 for r in all_results if r.get("exec_change"))
    if n_exec:
        chips.append(f'<button class="fbtn" data-f="__exec" style="background:#c62f2f; color:#fff">社長交代 {n_exec:,}</button>')
    n_tp = sum(1 for r in all_results if r.get("topics"))
    if n_tp:
        chips.append(f'<button class="fbtn" data-f="__tp" style="background:#1d7a4f; color:#fff">注目開示 {n_tp:,}</button>')
    chips.append('<button class="fbtn" data-f="__fav" style="background:#fff8e0; color:#a06f00">★お気に入り</button>')
    chips.append('<button class="fbtn" data-f="__hold" style="background:#e8eef8; color:#2e4d7b">持ち株</button>')
    chips.append("</div>")
    chips.append('<div class="sortrow"><input id="q" class="search" type="search" placeholder="銘柄名・コードで検索">'
                 '<select id="sort" class="sortsel">'
                 '<option value="code">コード順</option>'
                 '<option value="name">名前順（文字コード）</option>'
                 '<option value="dm">安全（減点が少ない）順</option>'
                 '<option value="dm_desc">減点が多い順</option>'
                 '<option value="q">質スコアが高い順</option>'
                 '<option value="tm">タイミングスコアが高い順</option>'
                 '<option value="sc">総合スコアが高い順</option>'
                 '<option value="tri">三層合格（今夜の厳選入り）を先頭に</option>'
                 '<option value="fav">★お気に入り・持ち株を先頭に</option>'
                 '<option value="exec">社長交代の開示ありを先頭に</option>'
                 '<option value="tp">注目開示（上方修正等）を先頭に</option>'
                 '<option value="tob">TOBされやすい順（素地スコア）</option>'
                 '<option value="ag">オールグリーンを先頭に</option>'
                 '<option value="drop">高値からの下落率が大きい順</option>'
                 '<option value="close">株価が高い順</option>'
                 '<option value="close_asc">株価が安い順</option>'
                 '<option value="mkt">市場別</option>'
                 '</select></div>')

    # 業種カテゴリでまとめる（メイン帳簿と同じ分類）
    chip_class = {"プライム": "prime", "スタンダード": "std", "グロース": "growth"}
    groups = {}
    for r in all_results:
        g = SECTOR_GROUPS.get(r.get("sector", ""), DEFAULT_GROUP)
        groups.setdefault(g, []).append(r)
    ordered_groups = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)

    def _norm(s):
        import unicodedata, re as _re
        t = unicodedata.normalize("NFKC", s or "").lower()
        # ひらがな→カタカナ統一
        t = "".join(chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ゖ" else ch for ch in t)
        # 空白・記号・法人格の定型語を除去
        t = _re.sub(r"[\s\-\u30fb・．.,、。()（）\[\]「」『』&＆/／]", "", t)
        for w in ("ホールディングス", "ホールディング", "グループ", "株式会社", "hd", "ｈｄ"):
            t = t.replace(w, "")
        return t

    def row_html(r):
        label, bg, fg, _d = STATUS_DEF[r["status"]]
        mchip = chip_class.get(r.get("market", ""), "local")
        close = f'{r["close"]:,.0f}円' if r.get("close") is not None else "−"
        drop = f'−{r["drop_pct"]:.1f}%' if r.get("drop_pct") is not None else ""
        cut = stats.get("cutoff_score", 0)
        if r["status"] == "picked" and r.get("cand_rank"):
            why = f'候補{r["cand_rank"]}位 ・ スコア{r.get("score", 0):.0f}点'
        elif r["status"] == "bench" and r.get("score") is not None:
            why = f'スコア{r["score"]:.0f}点（候補ライン{cut:.0f}点に届かず）'
        else:
            why = r.get("reason") or ""
        reason_html = (f'<span class="why">{html.escape(why)}</span>' if why else "")
        def _n(v, default):
            return default if v is None else v
        return (
            f'<details class="udet" data-s="{r["status"]}" data-code="{r["code"]}" '
            f'data-t="{html.escape(_norm(r["name"]))} {html.escape(r["name"].lower())} {r["code"]}" '
            f'data-name="{html.escape(r["name"])}" '
            f'data-dm="{_n(r.get("demerit"), 9999)}" data-sc="{_n(r.get("score"), -1)}" '
            f'data-q="{_n(r.get("q_score"), -1)}" data-tm="{_n(r.get("t_score"), -1)}" '
            f'data-tri="{1 if r.get("tri") else 0}" data-soon="{1 if r.get("soon") else 0}" '
            f'data-exec="{1 if r.get("exec_change") else 0}" '
            f'data-tp="{len(r.get("topics") or [])}" '
            f'data-tob="{_n(r.get("tob"), -1)}" '
            f'data-ag="{1 if r.get("all_green") else 0}" data-rf="{1 if r.get("red_free") else 0}" data-nev="{r.get("n_eval", 0)}" '
            f'data-close="{_n(r.get("close"), -1)}" data-drop="{_n(r.get("drop_pct"), -1)}" '
            f'data-mkt="{html.escape(r.get("market", ""))}" data-sec="{html.escape(r.get("sector", ""))}">'
            f'<summary class="urow">'
            f'<span class="st" style="background:{bg}; color:{fg}">{label}</span>'
            f'<span class="un"><b>{html.escape(r["name"])}</b> '
            f'<span class="chip {mchip}">{html.escape(r.get("market", "") or "−")}</span> '
            + ('<span class="mark tri">帳簿</span>' if r.get("tri") and r["status"] == "picked" else "")
            + ('<span class="mark soon">まもなく</span>' if r.get("soon") else "")
            + ('<span class="mark exec">社長交代</span>' if r.get("exec_change") else "")
            + topic_badge(r)
            + ('<span class="mark ag">オールグリーン</span>' if r.get("all_green") else "")
            + f'<button class="uhold" data-code="{r["code"]}" aria-label="持ち株">持</button>'
            + f'<span class="num uc">{r["code"]}</span>{reason_html}</span>'
            f'<button class="ufav" data-code="{r["code"]}" aria-label="お気に入り">★</button>'
            f'<span class="up num">{close}<small>{drop}</small>'
            + (f'<small class="tri3">'
               f'<b class="{"zero" if r.get("demerit") == 0 else ""}">安{r["demerit"]}</b> '
               f'質{r.get("q_score") or 0:.0f} 時{r.get("t_score") or 0:.0f}</small>'
               if r.get("demerit") is not None and r.get("q_score") is not None
               else (f'<small class="dmv{" zero" if r.get("demerit") == 0 else ""}">安{r["demerit"]}</small>' if r.get("demerit") is not None else ""))
            + f'</span></summary>'
            f'<div class="ubody">読み込み中…</div></details>')

    rows = []
    for g, members in ordered_groups:
        rows.append(f'<details class="gsec" open><summary class="gh">{html.escape(g)}'
                    f'<span class="gcnt">{len(members):,}銘柄 <span class="gchev">›</span></span></summary>')
        rows.extend(row_html(r) for r in sorted(members, key=lambda x: x["code"]))
        rows.append("</details>")

    legend = "".join(
        f'<div class="fact"><span><span class="st" style="background:{bg}; color:{fg}">{label}</span></span>'
        f'<span style="font-size:11.5px; color:var(--ink2)">{desc}</span></div>'
        for label, bg, fg, desc in STATUS_DEF.values())

    extra_css = """
  .chips{display:flex; gap:6px; flex-wrap:wrap; padding:2px 0 8px;}
  .fbtn{font-size:11.5px; font-weight:700; border:none; border-radius:14px;
    padding:5px 11px; background:#fff; color:var(--ink2); cursor:pointer;}
  .fbtn.on{outline:2px solid var(--ink);}
  .sortrow{display:flex; gap:6px; margin-bottom:10px;}
  .search{flex:1; min-width:0; font-size:14px; padding:9px 12px; border:1.5px solid #d9d2bf;
    border-radius:10px; background:#fff;}
  .sortsel{flex:none; max-width:44%; min-width:0; font-size:12px; font-weight:700; padding:8px 6px;
    border:1.5px solid #d9d2bf; border-radius:10px; background:#fff; color:var(--ink);}
  .flatlist{background:var(--paper); border-radius:14px; padding:2px 0;}
  .flatlist details.udet:first-child summary.urow{border-top:none;}
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
  .dmv{color:#8a5a17 !important; font-weight:800;} .dmv.zero{color:#1a5c37 !important;}
  .tri3{display:block; font-size:9.5px; color:var(--ink2) !important; font-weight:600;}
  .tri3 b{color:#8a5a17;} .tri3 b.zero{color:#1a5c37;}
  .mark{display:inline-block; font-size:9px; font-weight:800; border-radius:4px; padding:1px 5px; margin-right:3px; vertical-align:1px;}
  .mark.tri{background:#1c1c1e; color:#fff;} .mark.soon{background:#fdf3e3; color:#b06a00;}
  .mark.exec{background:#c62f2f; color:#fff;}
  .mark.ag{background:#b9dcc0; color:#1a5c37;}
""" + EXEC_CSS + """
  .uhold{display:inline-block; font-size:9px; font-weight:800; border-radius:4px; padding:1px 5px; margin-right:3px;
    vertical-align:1px; border:1px solid #d9d2bf; background:#fff; color:#c9bd9d; cursor:pointer; line-height:1.4;}
  .uhold.on{background:#2e4d7b; color:#fff; border-color:#2e4d7b;}
  .ufav{flex:none; background:none; border:none; font-size:17px; color:#ddd3ba; padding:0 2px; cursor:pointer; line-height:1;}
  .ufav.on{color:#e0a300;}
""" + METER_CSS + """
  .hit{display:flex; align-items:center; gap:6px; font-size:11px; padding:4px 0; border-bottom:1px dashed #f0ead9;}
  .hit.ok{color:#1a5c37; font-weight:700;} .hp{margin-left:auto; font-weight:800; color:#8a5a17;}
  .sev{flex:none; font-size:9px; font-weight:800; border-radius:4px; padding:1px 5px;}
  .sevA{background:#fdeeee; color:#c62f2f;} .sevB{background:#fdf3e3; color:#b06a00;} .sevC{background:#eef0f4; color:#4b4f57;}
  details.udet summary.urow{list-style:none; cursor:pointer;}
  details.udet summary.urow::-webkit-details-marker{display:none;}
  details.udet[open] summary.urow{background:#f4eedd;}
  .ubody{background:#fffdf6; border-top:1px dashed var(--paper-line); padding:10px 14px 14px;}
  .nhead{font-size:10.5px; font-weight:800; color:#7a6a45; letter-spacing:.06em; margin:10px 0 4px;}
  .reason{font-size:11px; color:var(--ink2); line-height:1.7; padding:2px 0;}
  .nrow{display:flex; gap:10px; font-size:11.5px; padding:4px 0;}
  .nrow .nd{width:58px; font-weight:700; flex:none;}
  .spark{margin:4px 0 2px;}
  .discnote{font-size:10px; color:var(--ink3); line-height:1.6; padding:5px 0 2px;}
  .ylink{display:block; margin-top:10px; font-size:12px; font-weight:700; color:#2e4d7b;
    text-decoration:none; text-align:center; background:#eef2f8; border-radius:9px; padding:9px;}
  .chip{display:inline-block; font-size:9px; font-weight:600; border-radius:5px;
    padding:1.5px 5px; vertical-align:1px;}
  .chip.prime{background:#e8eef8; color:#2e4d7b;}
  .chip.std{background:#e9f3ea; color:#3a5a40;}
  .chip.growth{background:#f4ecf9; color:#6b4487;}
  .chip.local{background:#f7efe4; color:#8a5a17;}
  details.gsec summary.gh{list-style:none; cursor:pointer; font-size:12px; font-weight:800;
    color:#7a6a45; letter-spacing:.05em; padding:12px 12px 6px;
    display:flex; justify-content:space-between; align-items:baseline;}
  details.gsec summary.gh::-webkit-details-marker{display:none;}
  .gcnt{font-weight:600; color:#a99a76; font-size:10.5px;}
  .gchev{display:inline-block; color:#c9bd9d; font-weight:700; transition:transform .15s;}
  details.gsec[open] .gchev{transform:rotate(90deg);}
  details.uguide{background:#fffdf6; border:1.5px solid #e0d8c4; border-radius:14px; margin-bottom:10px;}
  details.uguide summary{list-style:none; cursor:pointer; font-size:12px; font-weight:800; color:#7a6a45;
    padding:11px 14px; display:flex; justify-content:space-between; align-items:center; gap:8px;}
  details.uguide summary::-webkit-details-marker{display:none;}
  .ugchev{color:#c9bd9d; font-weight:700; transition:transform .15s;}
  details.uguide[open] .ugchev{transform:rotate(90deg);}
  .ugbody{padding:0 14px 12px;}
  .ugh{font-size:11px; font-weight:800; color:#7a6a45; letter-spacing:.06em; margin:14px 0 6px;
    padding-top:10px; border-top:1px dashed #e7e0cf;}
  .ugbody .ugh:first-child{border-top:none; padding-top:0; margin-top:4px;}
  .ugp{font-size:11.5px; color:var(--ink2); line-height:1.8; padding:2px 0 6px;}
  .ugrow{display:flex; gap:8px; align-items:flex-start; font-size:11.5px; color:var(--ink2);
    line-height:1.7; padding:4px 0;}
  .ugrow .ugk{flex:none; font-size:10px; font-weight:800; border-radius:5px; padding:2px 7px; margin-top:1px;}
  .ugrow .ugk2{flex:none; width:86px; font-size:10.5px; font-weight:800; color:#4a4a4f; margin-top:1px;}
  .linkrow{display:flex; gap:8px; margin-top:12px; flex-wrap:wrap;}
  .linkrow .ylink{flex:1; min-width:45%; margin-top:0; border:none; cursor:pointer; font-family:inherit;}
  .ylink.sbi{color:#1a5c37; background:#e9f3ea;}
  .ylink.buy{color:#fff; background:#1c1c1e;}
  .simrow{display:flex; align-items:center; gap:7px; font-size:11.5px; color:var(--ink);
    text-decoration:none; padding:6px 0; border-bottom:1px dashed #f0ead9;}
  .simrow b{flex:none; max-width:46%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
  .simrow .simw{flex:1; min-width:0; font-size:9.5px; color:var(--ink3); overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; text-align:right;}
  .simrow .simp{flex:none; font-weight:800; color:#2e4d7b;}
  .simex{flex:none; font-size:9px; font-weight:800; color:#b06a00; background:#fdf3e3;
    border-radius:4px; padding:1px 5px;}
""" + SPARK_CSS + UPDATE_CSS + FIN_CSS + """
"""
    script = """<script>
const rows = Array.from(document.querySelectorAll('details.udet'));
let filter = 'all';
function normQ(s){
  let t = (s || '').normalize('NFKC').toLowerCase();
  t = t.replace(/[\u3041-\u3096]/g, ch => String.fromCharCode(ch.charCodeAt(0) + 0x60));
  t = t.replace(/[\s\-・.,、。()（）\[\]「」『』&\/]/g, '');
  for (const w of ['ホールディングス','ホールディング','グループ','株式会社','hd']) t = t.split(w).join('');
  return t;
}
function apply(){
  const raw = document.getElementById('q').value.trim();
  const terms = raw.split(/[\s　]+/).filter(Boolean).map(normQ).filter(Boolean);
  for (const r of rows){
    const hay = r.dataset.t;
    const okQ = terms.length === 0 || terms.every(t => hay.includes(t));
    const okF = (filter === 'all' || r.dataset.s === filter
                 || (filter === '__flaw' && r.dataset.dm === '0')
                 || (filter === '__soon' && r.dataset.soon === '1')
                 || (filter === '__exec' && r.dataset.exec === '1')
                 || (filter === '__tp' && r.dataset.tp !== '0')
                 || (filter === '__ag' && r.dataset.ag === '1')
                 || (filter === '__rf' && r.dataset.rf === '1')
                 || (filter === '__fav' && r.dataset.fav === '1')
                 || (filter === '__hold' && r.dataset.hold === '1'));
    r.classList.toggle('hidden', !(okF && okQ));
  }
  if (!flat){
    for (const g of document.querySelectorAll('details.gsec')){
      const visible = g.querySelectorAll('details.udet:not(.hidden)').length;
      g.classList.toggle('hidden', visible === 0);
    }
  }
}
// ★お気に入り（帳簿と同じ保存先を共有）と持ち株印
const FAV_KEY = 'kabuobaa_favs', H_KEY = 'kabuobaa_holdings', HM_KEY = 'kabuobaa_holdmarks';
let favs = new Set(); try { favs = new Set(JSON.parse(localStorage.getItem(FAV_KEY) || '[]')); } catch(e){}
// 持ち株印: 手動トグル（HM_KEY）。持ち株管理に登録済みの銘柄は初回だけ自動で点灯させて取り込む
let holdMarks = new Set(); try { holdMarks = new Set(JSON.parse(localStorage.getItem(HM_KEY) || '[]')); } catch(e){}
try {
  const reg = JSON.parse(localStorage.getItem(H_KEY) || '[]').map(h => h.code);
  reg.forEach(c => holdMarks.add(c));
} catch(e){}
function paintMarks(){
  rows.forEach(r => {
    const code = r.dataset.code;
    const b = r.querySelector('.ufav'); if (b) b.classList.toggle('on', favs.has(code));
    const hb = r.querySelector('.uhold'); if (hb) hb.classList.toggle('on', holdMarks.has(code));
    r.dataset.fav = favs.has(code) ? '1' : '0';
    r.dataset.hold = holdMarks.has(code) ? '1' : '0';
  });
}
document.querySelectorAll('.ufav').forEach(b => b.addEventListener('click', e => {
  e.preventDefault(); e.stopPropagation();
  const c = b.dataset.code;
  if (favs.has(c)) favs.delete(c); else favs.add(c);
  localStorage.setItem(FAV_KEY, JSON.stringify(Array.from(favs)));
  paintMarks(); apply();
}));
document.querySelectorAll('.uhold').forEach(b => b.addEventListener('click', e => {
  e.preventDefault(); e.stopPropagation();
  const c = b.dataset.code;
  if (holdMarks.has(c)) holdMarks.delete(c); else holdMarks.add(c);
  localStorage.setItem(HM_KEY, JSON.stringify(Array.from(holdMarks)));
  paintMarks(); apply();
}));
paintMarks();

// タップで銘柄別詳細をオンデマンド読み込み
rows.forEach(r => r.addEventListener('toggle', () => {
  if (!r.open || r.dataset.loaded) return;
  r.dataset.loaded = '1';
  const body = r.querySelector('.ubody');
  fetch('details/' + r.dataset.code + '.json')
    .then(resp => { if (!resp.ok) throw new Error(); return resp.json(); })
    .then(j => { body.innerHTML = j.html; })
    .catch(() => { body.textContent = 'この銘柄の詳細データはありません（取得失敗銘柄など）'; });
}));
document.querySelectorAll('.fbtn').forEach(c => c.addEventListener('click', () => {
  document.querySelectorAll('.fbtn').forEach(x => x.classList.remove('on'));
  c.classList.add('on'); filter = c.dataset.f; apply();
}));
document.getElementById('q').addEventListener('input', apply);

// ---- ソート ----
const listEl = document.querySelector('.list');
const groups = Array.from(document.querySelectorAll('details.gsec'));
const homeGroup = new Map();  // 行 → 元の業種グループ
groups.forEach(g => g.querySelectorAll('details.udet').forEach(r => homeGroup.set(r, g)));
let flat = null;
const cmp = {
  code:      (a, b) => a.dataset.code.localeCompare(b.dataset.code, 'ja'),
  name:      (a, b) => a.dataset.name.localeCompare(b.dataset.name, 'ja'),
  dm:        (a, b) => (+a.dataset.dm - +b.dataset.dm) || (+b.dataset.sc - +a.dataset.sc),
  dm_desc:   (a, b) => (+b.dataset.dm - +a.dataset.dm) || (+b.dataset.sc - +a.dataset.sc),
  sc:        (a, b) => +b.dataset.sc - +a.dataset.sc,
  q:         (a, b) => +b.dataset.q - +a.dataset.q,
  tm:        (a, b) => +b.dataset.tm - +a.dataset.tm,
  tri:       (a, b) => (+b.dataset.tri - +a.dataset.tri) || (+b.dataset.soon - +a.dataset.soon) || (+b.dataset.sc - +a.dataset.sc),
  fav:       (a, b) => (+b.dataset.fav - +a.dataset.fav) || (+b.dataset.hold - +a.dataset.hold) || (+b.dataset.sc - +a.dataset.sc),
  exec:      (a, b) => (+b.dataset.exec - +a.dataset.exec) || (+b.dataset.sc - +a.dataset.sc),
  tp:        (a, b) => (+b.dataset.tp - +a.dataset.tp) || (+b.dataset.sc - +a.dataset.sc),
  tob:       (a, b) => (+b.dataset.tob - +a.dataset.tob) || (+b.dataset.sc - +a.dataset.sc),
  ag:        (a, b) => (+b.dataset.ag - +a.dataset.ag) || (+b.dataset.rf - +a.dataset.rf) || (+b.dataset.nev - +a.dataset.nev) || (+b.dataset.sc - +a.dataset.sc),
  drop:      (a, b) => +b.dataset.drop - +a.dataset.drop,
  close:     (a, b) => +b.dataset.close - +a.dataset.close,
  close_asc: (a, b) => +a.dataset.close - +b.dataset.close,
  mkt:       (a, b) => a.dataset.mkt.localeCompare(b.dataset.mkt, 'ja') || a.dataset.code.localeCompare(b.dataset.code),
};
function applySort(){
  const key = document.getElementById('sort').value;
  localStorage.setItem('kabuobaa_usort', key);
  if (key === 'code'){
    // 業種グループ表示に戻す
    if (flat){ flat.remove(); flat = null; }
    groups.forEach(g => { g.classList.remove('hidden'); listEl.appendChild(g); });
    rows.forEach(r => homeGroup.get(r).appendChild(r));
    groups.forEach(g => Array.from(g.querySelectorAll('details.udet')).sort(cmp.code).forEach(r => g.appendChild(r)));
  } else {
    // フラット表示で全体ソート
    groups.forEach(g => g.classList.add('hidden'));
    if (!flat){ flat = document.createElement('div'); flat.className = 'flatlist'; listEl.appendChild(flat); }
    rows.slice().sort(cmp[key]).forEach(r => flat.appendChild(r));
  }
  apply();
}
document.getElementById('sort').addEventListener('change', applySort);
const savedSort = localStorage.getItem('kabuobaa_usort');
if (savedSort && cmp[savedSort]){ document.getElementById('sort').value = savedSort; applySort(); }
// マップ等からの遷移: ?q=コード で検索欄に自動入力して該当銘柄を開く
const qp = new URLSearchParams(location.search).get('q');
if (qp){ const qe=document.getElementById('q'); qe.value=qp; apply();
  const hit=rows.find(r=>r.dataset.code===qp);
  if(hit){ hit.open=true; setTimeout(()=>hit.scrollIntoView({block:'center'}), 150); } }
</script>"""

    weekdays = "月火水木金土日"
    subtitle = (f"{dt.month}/{dt.day}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d} 判定 ・ "
                f"全{len(all_results):,}銘柄の扱いと理由の台帳 ・ "
                f"候補ライン（{CONFIG['SHORTLIST_N']}位のスコア）: {stats.get('cutoff_score', 0):.0f}点")
    guide_card = f"""
<details class="uguide">
<summary>❓ この台帳の見方（絞り込み・並べ替え・数字の意味）— 初めての方はここ<span class="ugchev">›</span></summary>
<div class="ugbody">

<div class="ugh">各行の右端に出る「安・質・時」の3つの数字</div>
<div class="ugp">このシステムは全銘柄を<b>3つのものさし</b>で毎回採点しています。おばあさんの手法でいうと——</div>
<div class="ugrow"><span class="ugk" style="background:#e9f3ea; color:#1a5c37">安</span><span><b>安全（減点方式・小さいほど良い）</b>。「買ってはいけない条件」に当てはまるたびに減点した合計。<b>0=無傷</b>（緑色で表示）。12以下で「致命」該当なしなら安全圏＝雑誌選び段階の「危ない銘柄をはじく」目利きに相当</span></div>
<div class="ugrow"><span class="ugk" style="background:#e8eef8; color:#2e4d7b">質</span><span><b>質（加点方式・大きいほど良い）</b>。財務の健全さ（PER・PBR・ROEなど）、上昇トレンドか、過去1年この買い方で勝てた実績、長期チャートの形などの合計点＝「そもそも良い会社・良いチャートか」</span></div>
<div class="ugrow"><span class="ugk" style="background:#fdf3e3; color:#b06a00">時</span><span><b>タイミング（加点方式・大きいほど良い）</b>。いま普段より安いか、下げ止まったか、RSI・MACD・ボリンジャーの位置など＝「<b>今夜</b>買いたい度」。質が高くても時が低い銘柄は「良い会社だが今は買い場でない」</span></div>
<div class="ugp">帳簿（今夜の厳選）は、この<b>安全に合格 × 質とタイミングの合計（総合スコア）が高い順</b>に選ばれています。</div>

<div class="ugh">「◎」と「○」の意味（2重丸マーク）</div>
<div class="ugp"><b>◎＝直近{CONFIG["RECENT_DAYS"]}営業日の高値から{CONFIG["CHEAP_PCT"]:.0f}%以上安い</b>（おばあさんの「いつもより安い日に買う」の基準に相当）。<b>○＝{CONFIG["MILD_PCT"]:.0f}%以上安い</b>（もう一声）。◎はタイミング点の中心的な加点で、「まもなく」は◎まであと3%以内の待機組です。</div>

<div class="ugh">絞り込みボタン（チップ）の意味</div>
<div class="ugp">上の「判定の凡例」5種（候補・圏外・除外・対象外・失敗）は毎回の判定結果そのもの。残りは横断の注目条件です。</div>
<div class="ugrow"><span class="ugk" style="background:#e9f3ea; color:#1a5c37">無傷</span><span>減点0（安=0）。買ってはいけない条件に一つも該当しない銘柄</span></div>
<div class="ugrow"><span class="ugk" style="background:#fdf3e3; color:#b06a00">まもなく</span><span>安全と質は合格だが、◎の安さまであと3%以内。下がれば帳簿に昇格する待機組</span></div>
<div class="ugrow"><span class="ugk" style="background:#c62f2f; color:#fff">社長交代</span><span>直近14日に「代表取締役の異動」を開示した銘柄。株価が大きく動きうる別格情報なので単独で表示（採点には含めません）</span></div>
<div class="ugrow"><span class="ugk" style="background:#1d7a4f; color:#fff">注目開示</span><span>直近14日に株価に効きやすい適時開示（上方修正・自社株買い・増配・株式分割・TOBなど＝緑、下方修正・減配・上場廃止・不適切会計など＝赤）があった銘柄。詳細を開くと見出しと原文リンクが見られます（「今夜の厳選」トップのバナーは直近3営業日ぶんの速報）</span></div>
<div class="ugrow"><span class="ugk" style="background:#b9dcc0; color:#1a5c37">オールグリーン</span><span>指標メーターに赤（警戒）も黄（注意）も無い銘柄。ただし判定できた指標だけで見るため、指標が少ない銘柄ほど該当しやすい点に注意</span></div>
<div class="ugrow"><span class="ugk" style="background:#e9f3ea; color:#3a5a40">赤なし</span><span>警戒（赤）だけが無い銘柄。オールグリーンより緩い基準</span></div>
<div class="ugrow"><span class="ugk" style="background:#fff8e0; color:#a06f00">★お気に入り</span><span>行の★を押した銘柄。帳簿の★と共通で、この端末にだけ保存</span></div>
<div class="ugrow"><span class="ugk" style="background:#e8eef8; color:#2e4d7b">持ち株</span><span>行の「持」を押した銘柄。銘柄マップの「マイ銘柄」モードで自分の地図としても見られます</span></div>

<div class="ugh">並べ替え——先頭に来るのは「何がすごい」銘柄か</div>
<div class="ugrow"><span class="ugk2">コード順</span><span>基本の表示。業種カテゴリごとにまとまります（これ以外はカテゴリを外して全体で並べ替え）</span></div>
<div class="ugrow"><span class="ugk2">名前順</span><span>銘柄名の文字コード順。名前で目視で探したいときに</span></div>
<div class="ugrow"><span class="ugk2">安全順</span><span>先頭ほど減点が少ない＝<b>大負けしにくい</b>。同点なら総合スコア順。「減点が多い順」は逆に危ない銘柄の点検用</span></div>
<div class="ugrow"><span class="ugk2">質順</span><span>先頭ほど<b>会社と長期チャートが良い</b>。今夜のタイミングは問わないので、下がるのを待つ監視リスト作りに向く</span></div>
<div class="ugrow"><span class="ugk2">タイミング順</span><span>先頭ほど<b>今夜が買い場に近い</b>。安全・質を見ずにタイミングだけで並ぶ点に注意</span></div>
<div class="ugrow"><span class="ugk2">総合スコア順</span><span>質＋タイミングの合計。帳簿の並びとほぼ同じ基準（帳簿はさらに安全合格が条件）</span></div>
<div class="ugrow"><span class="ugk2">三層合格を先頭に</span><span>安全×質×タイミングすべて合格＝<b>帳簿入りの銘柄</b>が先頭。次に「まもなく」</span></div>
<div class="ugrow"><span class="ugk2">★・社長交代・注目開示・オールグリーン</span><span>それぞれの該当銘柄を先頭に集めます（★は持ち株印も一緒に・同順位は総合スコア順）</span></div>
<div class="ugrow"><span class="ugk2">TOBされやすい順</span><span>買収・非公開化されやすい「体質」の素地スコア順（PBR・時価総額・ため込み度など9観点）。詳しくは「TOB素地ランキング」ページへ</span></div>
<div class="ugrow"><span class="ugk2">下落率・株価・市場別</span><span>単純な数値・区分の並べ替え。「高値からの下落率が大きい順」は安い順ですが、下げには理由があることも多いので減点・除外理由も必ず確認</span></div>

<div class="ugh">操作のコツ</div>
<div class="ugp">行をタップすると、その銘柄の<b>指標メーター・採点根拠・減点内訳・チャート・財務・開示</b>がその場で開きます。検索はひらがな／カタカナ／全角半角どちらでもOK、銘柄コードでも探せます。</div>
</div>
</details>"""
    body = ('<div class="card"><h2>判定の凡例</h2>' + legend + "</div>"
            + guide_card
            + "".join(chips)
            + '<div class="list">' + "\n".join(rows) + "</div>")
    footnote = ("「オールグリーン」は各銘柄の指標メーターに赤（警戒）も黄（注意）も無い銘柄（判定できた指標のみで評価）、"
                "「赤なし」は警戒だけが無い銘柄。指標が少ない銘柄ほど該当しやすい点に注意し、詳細を開いて評価済み指標の数も確認してください。"
                "「対象外」は上場間もない・株価100円未満・売買代金が少ない、のいずれか。"
                "「除外」は終わった株（1年高値から大幅下落・長期下落トレンド）に加え、"
                "直近の急落（落ちるナイフ）・荒すぎる値動き・1年安値圏更新中・下げ止まり未確認を含みます。"
                "各行に個別の理由を表示。判定は毎回の実行で更新されます。")
    return (SUBPAGE_TEMPLATE
            .replace("__NAVCSS__", NAV_CSS)
            .replace("__HEADBTN__", '<button type="button" class="updbtn" onclick="location.reload()">🔄 更新</button>')
            .replace("__NAVJS__", NAV_JS)
            .replace("__NAV__", nav_html("universe"))
            .replace("__TITLE__", "全銘柄台帳 — 約4,000銘柄の判定と根拠")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", body)
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", script + SHARED_FN_JS + SPARK_JS + UPDATE_JS))


def render_guide(dt):
    """使い方ページ（整理版）: 色分けした短いカード。指標の解説は「指標」タブへ"""
    c = CONFIG
    body = f"""
<div class="gcard c-green"><div class="gh">🟢 これは何？</div>
<div class="gt">おじいさま・おばあさまの株手法（毎日の四本値記録・普段より安くなったら買い・決めたルールで売る）をWeb化。
<b>下準備はシステムが自動、買う・売るの判断と注文はあなた</b>。注文機能はありません。</div></div>

<div class="gcard c-blue"><div class="gh">🔵 毎晩の流れ（1〜2分）</div>
<ol class="steps">
<li><b>「今夜の厳選」を開く</b> ホーム画面のアイコン → 合言葉（記憶した端末は自動）</li>
<li><b>厳選{c["TOP_N"]}銘柄を見る</b> タップで根拠・チャート・ノート・会社の発表が開く</li>
<li><b>買うなら</b> 銘柄内の「買った→持ち株に登録」を押してから証券会社アプリで注文</li>
<li><b>売りはルールで機械的に</b> 推奨は利確+10%・損切り−5%のIFDOCO注文。このルールの実力は「IFDOCOシミュレーション」（このページ最下部）で毎晩検証されています</li>
</ol></div>

<div class="gcard c-paper"><div class="gh">📒 タブの役割</div>
<div class="tabrow"><span class="tb">今夜の厳選</span>安全×質×タイミングの三層で選んだ厳選{c["TOP_N"]}銘柄＋「まもなく買い場」の待ち銘柄。持ち金・★・根拠</div>
<div class="tabrow"><span class="tb">全銘柄台帳</span>約4,000銘柄の台帳。安全（減点）・質・タイミングの3スコア、無傷／まもなく絞り込み、並べ替え、タップで全詳細（指標メーター付き）</div>
<div class="tabrow"><span class="tb">指標の読み方</span>PER・RSIなど全指標の図解。数字の読み方はここ</div>
<div class="tabrow"><span class="tb">銘柄マップ</span>全銘柄を財務×テクニカル×値動きでベクトル化した3D空間。似ている銘柄が近くに並び、タップで「発想が繋がる銘柄」へ光の糸が伸びる</div>
<div class="tabrow"><span class="tb">時価総額マップ</span>株価×発行株式数の平面に全銘柄を配置。右上ほど時価総額が大きく、100億〜10兆円の等高線で市場の全体像がひと目でわかる</div></div>

<div class="gcard c-yellow"><div class="gh">🟡 選定の仕組み（要約）</div>
<div class="gt">全銘柄 → 対象外（流動性不足・低位株・上場浅）→ 除外（終わった株・急落直後・荒い値動き・下げ止まり未確認）→
残りを<b>6観点で採点</b>（安さ／下げの質／トレンド／過去1年の利確実績／長期テクニカル／財務健全性）→ 上位{c["SHORTLIST_N"]}が候補 → 持ち金で買える{c["TOP_N"]}銘柄を表示。
詳細は「今夜の厳選」の「選定基準」カードと各銘柄の根拠。</div></div>

<div class="gcard c-red"><div class="gh">🔴 大事な前提</div>
<div class="gt">・持ち金・売りルール・★・持ち株・売却履歴・合言葉の記憶は<b>すべてこの端末の中だけ</b>に保存（iPhoneとMacで別）<br>
・データはJPX公式・Yahoo Finance・TDnet。表示は毎時の「写真」で、リアルタイムはYahooリンクで<br>
・このサイトは判断材料の表示のみ。投資判断は自己責任で</div></div>

<div class="gcard c-sub"><div class="gh">🧩 その他の機能（サブシステム）</div>
<div class="gt">メインの4タブとは別に、使いたい人向けの補助機能です。</div>
<a class="subbtn" href="sim.html"><b>IFDOCOシミュレーション</b><span>「毎晩の厳選1位を前日終値で買い、+10%指値と−5%成行のOCOで売る」を過去1年ぶん再現し、以後は毎晩の実際の1位で自動記帳。合計損益・勝率・塩漬け株・日次の全記録</span></a>
<a class="subbtn" href="tob.html"><b>TOB素地ランキング</b><span>買収・非公開化されやすい「体質」を診断士の定石（PBR・時価総額・ため込み度など9観点）で全銘柄採点した順位表。確率の予測ではなく、最後の確認は人間の役割</span></a>
</div>

<div class="gcard c-gray"><div class="gh">⚙️ 操作のコツ・初期設定</div>
<div class="gt"><b>スワイプ</b>で左右のタブへ移動 ／ 銘柄コード（例 2489 ⧉）を<b>タップでコピー</b> ／
<b>SBIアプリ連携</b>は1回だけ設定：ショートカットApp →「＋」→「Appを開く」→ SBI証券 株 → 名前を「SBIへ」に</div>
<div class="gt" style="margin-top:6px"><b>更新タイミング</b> 平日 9:40〜14:40毎時・15:45・20:30（夜が入れ替え基準）。実行に30〜60分かかるため表示は最大1時間前の値です</div>
<div class="gt" style="margin-top:6px"><b>「🔄 更新」ボタン</b>（「今夜の厳選」と「全銘柄台帳」の右上）はページの再読み込み（F5と同じ）。スマホでも最新の記帳に切り替えられます</div></div>
"""
    extra_css = """
  .gcard{border-radius:14px; padding:13px 15px; margin-bottom:12px; border-left:6px solid;}
  .c-green{background:#eef6ef; border-color:#3a5a40;} .c-blue{background:#eaf1fb; border-color:#2e5fa8;}
  .c-paper{background:#faf6ec; border-color:#a99a76;} .c-yellow{background:#fdf6e6; border-color:#c9a227;}
  .c-red{background:#fdeeee; border-color:#c62f2f;} .c-gray{background:#f0f0f4; border-color:#6e6e73;}
  .c-sub{background:#fff; border-color:#8a5a17;}
  .subbtn{display:block; text-decoration:none; color:inherit; background:#faf6ec; border-radius:10px;
    padding:10px 12px; margin-top:8px;}
  .subbtn b{display:block; font-size:13px; color:#4a3f28; margin-bottom:3px;}
  .subbtn span{font-size:11.5px; line-height:1.7; color:var(--ink2);}
  .gh{font-size:14px; font-weight:800; margin-bottom:8px;}
  .gt{font-size:12.5px; line-height:1.85;}
  .steps{padding-left:20px; font-size:12.5px; line-height:1.9;} .steps li{margin-bottom:3px;}
  .tabrow{display:flex; gap:8px; align-items:baseline; font-size:12px; line-height:1.7; padding:4px 0; border-bottom:1px dashed #e7e0cf;}
  .tb{flex:none; font-size:11px; font-weight:800; color:#fff; background:#1c1c1e; border-radius:6px; padding:2px 8px;}
"""
    weekdays = "月火水木金土日"
    subtitle = f"役割分担・毎晩の流れ・タブの意味 ・ {dt.month}/{dt.day}（{weekdays[dt.weekday()]}）時点の仕様"
    footnote = "仕様を変えるとこのページも自動で追随します。数字の読み方は「指標の読み方」ページへ。"
    return (SUBPAGE_TEMPLATE
            .replace("__NAVCSS__", NAV_CSS)
            .replace("__HEADBTN__", "")
            .replace("__NAVJS__", NAV_JS)
            .replace("__NAV__", nav_html("guide"))
            .replace("__TITLE__", "はじめに — この株ノートの使い方")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", body)
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", ""))


def render_tob(tob_ranked, n_total, dt):
    """TOB素地ランキングページ: 買収・非公開化されやすい「体質」の順位表"""
    judged = len(tob_ranked)
    announced = [e for e in tob_ranked if e.get("tob_announced")]
    candidates = [e for e in tob_ranked if not e.get("tob_announced")][:50]
    max_score = max((e["tob"] for e in candidates), default=1) or 1
    chip_class = {"プライム": "prime", "スタンダード": "std", "グロース": "growth"}

    rows = []
    for e in candidates:
        fu = e.get("fund") or {}
        mchip = chip_class.get(e.get("market", ""), "local")
        barw = e["tob"] / max_score * 100
        facts = []
        if fu.get("pbr") is not None:
            facts.append(f'PBR {fu["pbr"]:.2f}倍')
        if fu.get("mcap_oku"):
            facts.append(f'時価総額 {fu["mcap_oku"]:,}億円')
        if fu.get("equity_ratio") is not None:
            facts.append(f'自己資本 {fu["equity_ratio"]:.0f}%')
        if fu.get("roe") is not None:
            facts.append(f'ROE {fu["roe"]:.1f}%')
        hits_html = "".join(
            f'<div class="thit"><span>{html.escape(label)}</span>'
            f'<span class="num tp{"p" if pts >= 0 else "m"}">{pts:+d}</span></div>'
            for label, pts in (e.get("tob_hits") or []))
        yahoo_url = f'https://finance.yahoo.co.jp/quote/{e["code"]}{e.get("suffix", ".T")}'
        rows.append(f"""
<details class="trow">
<summary class="tsum">
  <span class="trk num">{e["tob_rank"]}</span>
  <span class="tnm"><b>{html.escape(e["name"])}</b> <span class="chip {mchip}">{html.escape(e.get("market", "") or "−")}</span>
    <span class="num tuc">{e["code"]}</span>
    <span class="tfact num">{" ・ ".join(facts)}</span></span>
  <span class="tsc num">{e["tob"]}<small>点</small></span>
  <span class="tchev">›</span>
</summary>
<div class="tbody">
  <div class="tbar"><div class="tbarfill" style="width:{barw:.0f}%"></div></div>
  {hits_html}
  <div class="linkrow">
    <a class="ylink" href="{yahoo_url}" target="_blank" rel="noopener">Yahoo!ファイナンス →</a>
    <button type="button" class="ylink sbi" onclick="openSBI('{e["code"]}', event)">SBI証券アプリで見る</button>
  </div>
</div>
</details>""")

    if announced:
        ann_rows = "".join(
            f'<div class="fact"><span><b>{html.escape(e["name"])}</b> <span class="num">{e["code"]}</span>'
            f'<span class="tpbadge twarn">TOB・MBO発表済み</span></span>'
            f'<span class="v num">素地 {e["tob"]}点・全体{e["tob_rank"]:,}位</span></div>'
            for e in announced[:15])
        ann_card = (f'<div class="card"><h2>📌 答え合わせ: 実際にTOB・MBOが発表された銘柄（直近14日）</h2>'
                    f'{ann_rows}'
                    f'<div class="note">発表当日は株価が跳ねてPBR等が上がるため、上の点数は発表の影響を受けた後の値です。'
                    f'このランキングの実力は「発表<b>前</b>に何位だったか」で測るのが正しく、'
                    f'毎晩の実行を重ねるほど答え合わせの精度が上がっていきます。</div></div>')
    else:
        ann_card = ('<div class="card"><h2>📌 答え合わせ（実績照合）</h2>'
                    '<div class="note">直近14日にTOB・MBOの開示があった銘柄が現れると、ここに'
                    '「その銘柄が本ランキングで何位だったか」を表示します。実績が貯まるほど、'
                    'この物差し自体の当たり外れを検証できます。</div></div>')

    body = f"""
<div class="card" style="border-left:5px solid #6b4487;">
  <h2>これは何？（確率の予測ではありません）</h2>
  <div class="gt">M&Aアドバイザーや中小企業診断士が「この会社は買収・非公開化の対象になりやすい」と見るときの
  <b>定石を固定基準にして、全銘柄を機械的に採点した順位表</b>です。
  「TOBが起きる」という予告ではなく、<b>買収側から見て魅力的な体質かどうか</b>の素地スコアです。
  役割分担: 体質での絞り込みは機械が毎晩やる → 最後の確認と判断は人間がやる。</div>
</div>

<details class="crit2">
<summary>採点の物差し（9つの観点・タップで開閉）<span class="tchev">›</span></summary>
<div class="critbody2">
  <div class="cr"><b>① 解散価値との比較（最大22点）</b> PBR1倍割れ、特に0.6倍未満は「会社を丸ごと買って資産を得た方が安い」状態。買収の最重要シグナル</div>
  <div class="cr"><b>② 買える大きさか（最大15点）</b> TOB・MBOの主戦場は時価総額100〜1000億円。大型すぎると資金面で非現実的（減点）</div>
  <div class="cr"><b>③ ため込み体質（最大18点）</b> 自己資本比率が高いのにROEが低い＝現金と資産を抱えて活かせていない。ファンド・アクティビストが最も好む型</div>
  <div class="cr"><b>④ 稼ぐ力はあるか（最大8点）</b> 営業黒字が前提。壊れた会社は買われない——「安いのに健全」の組み合わせが核心</div>
  <div class="cr"><b>⑤ 還元の渋さ（5点）</b> 黒字なのに配当性向が低い＝ため込みの傍証</div>
  <div class="cr"><b>⑥ 株価の放置（最大6点）</b> 1年レンジ下位に放置されているほど、プレミアムを乗せた買付けがしやすい</div>
  <div class="cr"><b>⑦ 出来高の薄さ（4点）</b> 市場の目が届かない銘柄ほど割安の歪みが残る</div>
  <div class="cr"><b>⑧ 市場区分（4点）</b> スタンダード・グロースは非公開化しやすい。プライムのPBR1倍割れは東証の改善要請が直撃</div>
  <div class="cr"><b>⑨ 利益面の割安（4点）</b> PER10倍未満はおまけの加点</div>
</div>
</details>

<div class="card warncard2">
  <h2>⚠ このスコアの限界（正直な注意書き）</h2>
  <div class="gt">{TOB_MISSING_NOTE}</div>
  <div class="gt" style="margin-top:6px">また「TOB狙い」の投資は、<b>発表がなければ延々と塩漬け</b>になりやすい戦法です。
  上位銘柄はどれも「安くて健全」ではあるので、TOBが来なくても持てる銘柄かを通常の三層判定（安全・質・タイミング）でも確認してください。</div>
</div>

{ann_card}

<div class="card" style="padding:4px 0 8px;">
  <h2 style="padding:10px 14px 2px;">素地スコア上位50銘柄（判定 {judged:,}銘柄中）</h2>
  {"".join(rows)}
</div>
"""
    extra_css = """
  .gt{font-size:12.5px; line-height:1.85;}
  details.crit2{background:#faf6ec; border-radius:14px; margin-bottom:12px; box-shadow:0 1px 3px rgba(0,0,0,.06);}
  details.crit2 summary{list-style:none; cursor:pointer; font-size:13px; font-weight:800; color:#7a6a45;
    padding:12px 14px; display:flex; justify-content:space-between; align-items:center;}
  details.crit2 summary::-webkit-details-marker{display:none;}
  .critbody2{padding:0 14px 12px;}
  .cr{font-size:12px; line-height:1.8; padding:6px 0; border-top:1px dashed #e7e0cf; color:var(--ink2);}
  .cr b{color:#4a3f28;}
  .tchev{color:#c9bd9d; font-weight:700; transition:transform .15s;}
  details[open] > summary .tchev{transform:rotate(90deg);}
  .warncard2{background:#fdf6e6 !important; border-left:5px solid #b06a00;}
  .warncard2 h2{color:#8a5a17;}
  details.trow{border-top:1px solid var(--paper-line);}
  .tsum{list-style:none; cursor:pointer; display:flex; align-items:center; gap:8px; padding:8px 14px;}
  .tsum::-webkit-details-marker{display:none;}
  details.trow[open] .tsum{background:#f4eedd;}
  .trk{flex:none; width:26px; text-align:center; font-size:13px; font-weight:800; color:#6b4487;}
  .tnm{flex:1; min-width:0; font-size:12.5px; line-height:1.5;}
  .tuc{color:var(--ink2); font-size:11px; margin-left:2px;}
  .tfact{display:block; font-size:10px; color:var(--ink2);}
  .tsc{flex:none; font-size:15px; font-weight:800; color:#6b4487;}
  .tsc small{font-size:9px; font-weight:600; color:var(--ink3);}
  .tbody{background:#fffdf6; border-top:1px dashed var(--paper-line); padding:10px 14px 14px;}
  .tbar{height:10px; background:#f0ead9; border-radius:5px; overflow:hidden; margin-bottom:8px;}
  .tbarfill{height:100%; background:#9a7ab5; border-radius:5px;}
  .thit{display:flex; justify-content:space-between; gap:10px; font-size:11.5px; line-height:1.7;
    padding:4px 0; border-bottom:1px dashed #f0ead9;}
  .thit .tpp{font-weight:800; color:#6b4487; flex:none;}
  .thit .tpm{font-weight:800; color:#b06a00; flex:none;}
  .tpbadge{display:inline-block; font-size:9.5px; font-weight:800; color:#fff; border-radius:4px;
    padding:1px 6px; margin-left:5px; vertical-align:1px;}
  .tpbadge.twarn{background:#8a6d1a;}
  .chip{display:inline-block; font-size:9px; font-weight:600; border-radius:5px; padding:1.5px 5px; vertical-align:1px;}
  .chip.prime{background:#e8eef8; color:#2e4d7b;}
  .chip.std{background:#e9f3ea; color:#3a5a40;}
  .chip.growth{background:#f4ecf9; color:#6b4487;}
  .chip.local{background:#f7efe4; color:#8a5a17;}
  .linkrow{display:flex; gap:8px; margin-top:12px; flex-wrap:wrap;}
  .linkrow .ylink{flex:1; min-width:45%;}
  .ylink{display:block; font-size:12px; font-weight:700; color:#2e4d7b; font-family:inherit;
    text-decoration:none; text-align:center; background:#eef2f8; border-radius:9px; padding:9px;
    border:none; cursor:pointer;}
  .ylink.sbi{color:#1a5c37; background:#e9f3ea;}
"""
    weekdays = "月火水木金土日"
    subtitle = (f"{dt.month}/{dt.day}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d} 判定 ・ "
                f"素材の揃った{judged:,}銘柄（全{n_total:,}銘柄中）を採点 ・ 確率の予測ではなく「体質」の順位")
    footnote = ("採点はJPX公式（J-Quants）の決算数字と株価から毎回自動計算され、基準はこのページの「採点の物差し」の通りです。"
                "TOB・MBOの発生を保証するものではありません。全銘柄台帳の並べ替え「TOBされやすい順」でも全銘柄ぶんを確認できます。"
                "投資判断はご自身で。")
    return (SUBPAGE_TEMPLATE
            .replace("__NAVCSS__", NAV_CSS)
            .replace("__HEADBTN__", "")
            .replace("__NAVJS__", NAV_JS)
            .replace("__NAV__", nav_html("tob"))
            .replace("__TITLE__", "TOB素地ランキング — 買収されやすい体質の順位")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", body)
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", SHARED_FN_JS))


def render_caps(n_stocks, dt):
    """時価総額マップ: 株価×発行株式数の対数平面（ズーム・パン対応）＋全銘柄ランキング"""
    body = r"""
<div class="card">
  <h2>この図の見方</h2>
  <div class="gt">横軸=<b>株価</b>、縦軸=<b>発行株式数</b>（どちらも対数目盛）。掛け算が時価総額なので、
  <b>右上に行くほど時価総額が大きい</b>会社です。斜めの点線は「同じ時価総額」のライン（100億〜10兆円）。
  点をタップすると銘柄名と時価総額・順位が出ます。<b>ドラッグで移動・ピンチ/ホイールで拡大</b>できるので、
  密集した小型株ゾーンにも潜れます。</div>
  <div class="dirrow"><b>→ 右へ行くほど</b> 1株の値段が高い「値がさ株」。100株の必要資金も大きい</div>
  <div class="dirrow"><b>↑ 上へ行くほど</b> 発行株式数が多い。売買はしやすい一方、利益は多くの株に薄く分配される</div>
  <div class="dirrow"><b>↗ 右上の隅</b> 時価総額の最大級（トヨタ・メガバンク級）。<b>↙ 左下</b>は小型・新興。同じ時価総額でも「高い株価×少ない株数」か「安い株価×大量の株数」かで性格が違います</div>
</div>

<div class="card" style="padding:10px 10px 6px;">
  <div class="csearchrow"><input id="cq" class="csearch" type="search" placeholder="銘柄名・コードで探す（図の中で光ります）"></div>
  <div class="ctools">
    <span class="cmode on" data-cm="tier">時価総額の階級</span>
    <span class="cmode" data-cm="mkt">市場区分</span>
    <span class="creset" id="creset">⛶ 全体表示に戻す</span>
  </div>
  <div id="cwrap" style="position:relative">
    <canvas id="ccv" style="width:100%; display:block; border-radius:10px; background:#fffdf6; touch-action:none;"></canvas>
    <div id="csugg" class="csugg"></div>
  </div>
  <div class="tierlg" id="lgtier">
    <span><i style="background:#6b4487"></i>10兆円以上</span>
    <span><i style="background:#2e4d7b"></i>1兆〜10兆</span>
    <span><i style="background:#3a5a40"></i>1000億〜1兆</span>
    <span><i style="background:#b06a00"></i>100億〜1000億</span>
    <span><i style="background:#8e8e93"></i>100億円未満</span>
  </div>
  <div class="tierlg" id="lgmkt" style="display:none">
    <span><i style="background:#2e4d7b"></i>プライム</span>
    <span><i style="background:#3a5a40"></i>スタンダード</span>
    <span><i style="background:#6b4487"></i>グロース</span>
    <span><i style="background:#b06a00"></i>札幌・福岡</span>
  </div>
  <div id="selcard" class="selcard" style="display:none"></div>
</div>

<div class="card" id="statcard">
  <h2>市場全体のものさし</h2>
  <div id="capstats"></div>
</div>

<div class="card">
  <h2 style="display:flex; justify-content:space-between; align-items:center;">時価総額ランキング（全銘柄）
    <span class="rsort"><button class="rsbtn on" data-d="desc">大きい順</button><button class="rsbtn" data-d="asc">小さい順</button></span></h2>
  <div id="rankall"></div>
  <div class="rmore-row"><button id="rmore" class="rmorebtn">さらに表示</button></div>
  <div class="note">時価総額 = 株価 × 発行株式数。決算短信（J-Quants）の株式数と最新株価から毎回自動計算。行をタップすると図でその銘柄が光ります。</div>
</div>
"""
    extra_css = """
  .gt{font-size:12.5px; line-height:1.85;}
  .dirrow{font-size:11.5px; line-height:1.8; color:var(--ink2); padding:5px 0; border-top:1px dashed #f0ead9;}
  .dirrow b{color:#4a3f28;}
  .tierlg{display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; padding:0 2px;}
  .tierlg span{display:flex; align-items:center; gap:5px; font-size:10.5px; color:var(--ink2); font-weight:700;}
  .tierlg i{width:9px; height:9px; border-radius:50%; display:inline-block;}
  .ctools{display:flex; gap:6px; align-items:center; padding:0 2px 8px; flex-wrap:wrap;}
  .cmode{font-size:11px; font-weight:800; color:var(--ink2); background:#fff; border:1.5px solid #d9d2bf;
    border-radius:12px; padding:4px 11px; cursor:pointer;}
  .cmode.on{background:#1c1c1e; color:#fff; border-color:#1c1c1e;}
  .creset{margin-left:auto; font-size:11px; font-weight:800; color:#2e4d7b; background:#e8eef8;
    border-radius:12px; padding:4px 11px; cursor:pointer;}
  .csearchrow{padding:2px 2px 8px;}
  .csearch{width:100%; font-size:14px; padding:9px 12px; border:1.5px solid #d9d2bf; border-radius:10px; background:#fff;}
  .csugg{position:absolute; left:8px; top:8px; z-index:5; background:#fff; border:1.5px solid #d9d2bf;
    border-radius:10px; max-height:220px; overflow-y:auto; display:none; box-shadow:0 8px 24px rgba(0,0,0,.12); min-width:220px;}
  .csugg.show{display:block;}
  .csugg .it{padding:7px 12px; font-size:12px; cursor:pointer; border-top:1px solid #f0ead9;}
  .csugg .it:first-child{border-top:none;}
  .csugg .it small{color:var(--ink3); margin-left:6px;}
  .selcard{margin-top:10px; background:#fffdf6; border:1.5px solid #e0d8c4; border-radius:10px; padding:10px 12px;}
  .selname{font-size:14px; font-weight:800;}
  .selname small{font-weight:600; color:var(--ink2); margin-left:6px;}
  .selfacts{display:flex; gap:6px; flex-wrap:wrap; margin:6px 0;}
  .self{font-size:10.5px; font-weight:700; color:var(--ink2); background:#f4f1e8; border-radius:6px; padding:3px 8px;}
  .self b{color:#1c1c1e;}
  .sellinks{display:flex; gap:8px; margin-top:6px; flex-wrap:wrap;}
  .sellinks a{flex:1; min-width:30%; text-align:center; text-decoration:none; font-size:11.5px; font-weight:700;
    color:#2e4d7b; background:#eef2f8; border-radius:8px; padding:8px 4px;}
  .statgridc{display:grid; grid-template-columns:repeat(2,1fr); gap:8px;}
  .stc{background:#fffdf6; border-radius:10px; padding:8px 10px;}
  .stc .k{font-size:10px; color:var(--ink3); font-weight:700;}
  .stc .v{font-size:15px; font-weight:800; color:#4a3f28;}
  .stc .v small{font-size:10px; color:var(--ink2); font-weight:600;}
  .stbar{display:flex; height:14px; border-radius:7px; overflow:hidden; margin:8px 0 4px;}
  .stbar i{display:block; height:100%;}
  .stbarnote{font-size:10px; color:var(--ink3); line-height:1.6;}
  .rsort{display:flex; border:1.5px solid #d9d2bf; border-radius:8px; overflow:hidden;}
  .rsbtn{border:none; background:#fff; color:var(--ink2); font-size:11px; font-weight:800; padding:5px 10px; cursor:pointer;}
  .rsbtn.on{background:#1c1c1e; color:#fff;}
  .rrow{display:flex; align-items:center; gap:8px; padding:5px 0; border-bottom:1px dashed #f0ead9;
    font-size:12px; cursor:pointer;}
  .rrk{flex:none; width:34px; text-align:right; font-weight:800; color:#7a6a45; font-size:11px;}
  .rnm{flex:none; width:37%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:700;}
  .rbarw{flex:1; height:10px; background:#f0ead9; border-radius:5px; overflow:hidden;}
  .rbar{height:100%; background:#a58ec4; border-radius:5px;}
  .rmc{flex:none; width:74px; text-align:right; font-weight:800; color:#4a3f28; font-size:11px;}
  .rmore-row{text-align:center; padding:10px 0 2px;}
  .rmorebtn{border:1.5px solid #d9d2bf; background:#fff; color:#2e4d7b; font-size:12px; font-weight:800;
    border-radius:10px; padding:8px 22px; cursor:pointer;}
"""
    script = r"""<script>
(function(){
'use strict';
var STK=[], sel=-1, W=0, H=0, DPR=1, CMODE='tier';
var cv=document.getElementById('ccv'), ctx=cv.getContext('2d');
var EXT={x:[0,1],y:[0,1]};              /* 全体の範囲(log) */
var VX=[0,1], VY=[0,1];                 /* 現在のビュー(log) */
var PAD={l:46,r:12,t:14,b:34};
var MKC={'プライム':'#2e4d7b','スタンダード':'#3a5a40','グロース':'#6b4487','札幌':'#b06a00','福岡':'#b06a00'};
function tierCol(m){
  if(m>=100000) return '#6b4487';
  if(m>=10000) return '#2e4d7b';
  if(m>=1000) return '#3a5a40';
  if(m>=100) return '#b06a00';
  return '#8e8e93';
}
function colOf(s){ return CMODE==='mkt' ? (MKC[s.mkt]||'#8e8e93') : tierCol(s.m); }
function fmtM(m){
  if(m>=10000) return (m/10000).toFixed(2).replace(/\.00$/,'')+'兆円';
  return Math.round(m).toLocaleString()+'億円';
}
function fmtShares(sh){
  if(sh>=1e8) return (sh/1e8).toFixed(1)+'億株';
  return Math.round(sh/1e4).toLocaleString()+'万株';
}
function X(lx){ return PAD.l+(lx-VX[0])/(VX[1]-VX[0])*(W-PAD.l-PAD.r); }
function Y(ly){ return H-PAD.b-(ly-VY[0])/(VY[1]-VY[0])*(H-PAD.t-PAD.b); }
function invX(px){ return VX[0]+(px-PAD.l)/(W-PAD.l-PAD.r)*(VX[1]-VX[0]); }
function invY(py){ return VY[0]+(H-PAD.b-py)/(H-PAD.t-PAD.b)*(VY[1]-VY[0]); }
function clampView(){
  var minSpan=0.18;
  var sx=VX[1]-VX[0], sy=VY[1]-VY[0];
  if(sx<minSpan){ var cx=(VX[0]+VX[1])/2; VX=[cx-minSpan/2,cx+minSpan/2]; }
  if(sy<minSpan){ var cy=(VY[0]+VY[1])/2; VY=[cy-minSpan/2,cy+minSpan/2]; }
  var over=0.4;
  if(VX[0]<EXT.x[0]-over){ var d=EXT.x[0]-over-VX[0]; VX[0]+=d; VX[1]+=d; }
  if(VX[1]>EXT.x[1]+over){ var d2=VX[1]-(EXT.x[1]+over); VX[0]-=d2; VX[1]-=d2; }
  if(VY[0]<EXT.y[0]-over){ var d3=EXT.y[0]-over-VY[0]; VY[0]+=d3; VY[1]+=d3; }
  if(VY[1]>EXT.y[1]+over){ var d4=VY[1]-(EXT.y[1]+over); VY[0]-=d4; VY[1]-=d4; }
}
function resize(){
  DPR=Math.min(2.5,window.devicePixelRatio||1);
  W=cv.clientWidth; H=Math.max(340,Math.min(540,Math.round(W*0.9)));
  cv.style.height=H+'px';
  cv.width=Math.round(W*DPR); cv.height=Math.round(H*DPR);
  ctx.setTransform(DPR,0,0,DPR,0,0);
  draw();
}
window.addEventListener('resize',resize);
function gridStep(span){ return span>1.4?1:(span>0.7?0.5:(span>0.3?0.25:0.1)); }
function fmtPrice(v){
  if(v>=10000) return (v/10000).toLocaleString(undefined,{maximumFractionDigits:1})+'万円';
  return Math.round(v).toLocaleString()+'円';
}
function fmtShAxis(v){
  if(v>=1e8) return (v/1e8).toLocaleString(undefined,{maximumFractionDigits:1})+'億株';
  if(v>=1e4) return Math.round(v/1e4).toLocaleString()+'万株';
  return Math.round(v).toLocaleString()+'株';
}
function draw(){
  if(!STK.length) return;
  ctx.clearRect(0,0,W,H);
  ctx.font='10px ui-monospace,Menlo,monospace';
  ctx.strokeStyle='#eee7d6'; ctx.fillStyle='#a99a76'; ctx.lineWidth=1;
  var stx=gridStep(VX[1]-VX[0]);
  for(var e=Math.ceil(VX[0]/stx)*stx;e<=VX[1];e+=stx){
    var gx=X(e);
    if(gx<PAD.l-1||gx>W-PAD.r+1) continue;
    ctx.globalAlpha=(Math.abs(e-Math.round(e))<1e-6)?1:0.45;
    ctx.beginPath(); ctx.moveTo(gx,PAD.t); ctx.lineTo(gx,H-PAD.b); ctx.stroke();
    ctx.textAlign='center';
    ctx.fillText(fmtPrice(Math.pow(10,e)), gx, H-PAD.b+14);
  }
  var sty=gridStep(VY[1]-VY[0]);
  for(var e2=Math.ceil(VY[0]/sty)*sty;e2<=VY[1];e2+=sty){
    var gy=Y(e2);
    if(gy<PAD.t-1||gy>H-PAD.b+1) continue;
    ctx.globalAlpha=(Math.abs(e2-Math.round(e2))<1e-6)?1:0.45;
    ctx.beginPath(); ctx.moveTo(PAD.l,gy); ctx.lineTo(W-PAD.r,gy); ctx.stroke();
    ctx.textAlign='right';
    ctx.fillText(fmtShAxis(Math.pow(10,e2)), PAD.l-5, gy+3);
  }
  ctx.globalAlpha=1;
  ctx.textAlign='left';
  ctx.fillText('株価 →', W-PAD.r-42, H-6);
  ctx.save(); ctx.translate(10,PAD.t+64); ctx.rotate(-Math.PI/2);
  ctx.fillText('発行株式数 →',0,0); ctx.restore();
  /* 四隅の意味づけ（全体表示に近いときだけ） */
  if(VX[1]-VX[0] > (EXT.x[1]-EXT.x[0])*0.75){
    ctx.font='700 10px "Hiragino Sans",sans-serif';
    ctx.fillStyle='rgba(122,106,69,.5)';
    ctx.fillText('↖ 低位・大量発行', PAD.l+6, PAD.t+14);
    ctx.textAlign='right';
    ctx.fillText('超大型（トヨタ級）↗', W-PAD.r-4, PAD.t+14);
    ctx.fillText('値がさ株（株数少なめ）↘', W-PAD.r-4, H-PAD.b-8);
    ctx.textAlign='left';
    ctx.fillText('↙ 小型・新興', PAD.l+6, H-PAD.b-8);
    ctx.font='10px ui-monospace,Menlo,monospace';
  }
  /* 等時価総額線 */
  var isos=[[10,'10億円'],[100,'100億円'],[1000,'1000億円'],[10000,'1兆円'],[100000,'10兆円']];
  ctx.setLineDash([4,4]);
  for(var i2=0;i2<isos.length;i2++){
    var logM=Math.log10(isos[i2][0]*1e8);
    var p1y=logM-VX[0], p2y=logM-VX[1];
    if(Math.max(p1y,p2y)<VY[0]||Math.min(p1y,p2y)>VY[1]) continue;
    ctx.strokeStyle='rgba(138,90,23,.32)';
    ctx.beginPath(); ctx.moveTo(X(VX[0]),Y(p1y)); ctx.lineTo(X(VX[1]),Y(p2y)); ctx.stroke();
    var lx=Math.min(VX[1]-0.24, Math.max(VX[0]+0.1, logM-VY[1]+0.2));
    var ly=logM-lx;
    if(ly>VY[0]&&ly<VY[1]){
      ctx.fillStyle='#8a5a17';
      ctx.fillText(isos[i2][1], X(lx)+3, Y(ly)-4);
    }
  }
  ctx.setLineDash([]);
  /* 点（細め・ビュー内のみ） */
  var vis=[];
  for(var i=0;i<STK.length;i++){
    var s=STK[i];
    s.px=X(s.lx); s.py=Y(s.ly);
    if(s.px<PAD.l-4||s.px>W-PAD.r+4||s.py<PAD.t-4||s.py>H-PAD.b+4) continue;
    vis.push(i);
    /* 全銘柄おなじ小さな点（大きさは時価総額と無関係。位置だけが情報） */
    ctx.globalAlpha=0.8;
    ctx.fillStyle=colOf(s);
    ctx.beginPath(); ctx.arc(s.px,s.py,1.6,0,Math.PI*2); ctx.fill();
  }
  ctx.globalAlpha=1;
  /* ビュー内の時価総額上位にラベル（ズームするほどその場の顔ぶれが見える） */
  ctx.font='700 10px "Hiragino Sans",sans-serif';
  var placed={}, labeled=0;
  for(var j=0;j<vis.length&&labeled<14;j++){
    var t=STK[vis[j]];   /* STKは時価総額降順なのでvisも降順 */
    var key=Math.floor(t.px/72)+'_'+Math.floor(t.py/16);
    if(placed[key]) continue; placed[key]=1; labeled++;
    ctx.fillStyle='#4a3f28';
    ctx.fillText(t.name.length>7?t.name.slice(0,7)+'…':t.name, t.px+5, t.py-4);
  }
  if(sel>=0){
    var ss=STK[sel];
    if(ss.px>PAD.l-30&&ss.px<W+30&&ss.py>-30&&ss.py<H+30){
      ctx.strokeStyle='#c62f2f'; ctx.lineWidth=2;
      ctx.beginPath(); ctx.arc(ss.px,ss.py,7,0,Math.PI*2); ctx.stroke();
      ctx.font='800 12px "Hiragino Sans",sans-serif';
      ctx.fillStyle='#c62f2f';
      ctx.fillText(ss.name, Math.min(ss.px+8,W-90), Math.max(14,ss.py-9));
    }
  }
}
/* ── ズーム・パン（1本指=移動 / ピンチ・ホイール=拡大 / ダブルタップ=ズーム） ── */
var PTRS=new Map(), moved=0, lastP=null, pinch0=0, lastTapT=0;
function cXY(e){ var r=cv.getBoundingClientRect(); return [e.clientX-r.left, e.clientY-r.top]; }
function zoomAt(px,py,k){
  var ax=invX(px), ay=invY(py);
  VX=[ax-(ax-VX[0])/k, ax+(VX[1]-ax)/k];
  VY=[ay-(ay-VY[0])/k, ay+(VY[1]-ay)/k];
  clampView(); draw();
}
cv.addEventListener('pointerdown',function(e){
  var xy=cXY(e);
  PTRS.set(e.pointerId,{x:xy[0],y:xy[1]});
  try{ cv.setPointerCapture(e.pointerId); }catch(err){}
  if(PTRS.size===1){ moved=0; lastP=xy; }
  else { pinch0=0; lastP=null; }
});
cv.addEventListener('pointermove',function(e){
  if(!PTRS.has(e.pointerId)) return;
  var xy=cXY(e);
  PTRS.set(e.pointerId,{x:xy[0],y:xy[1]});
  if(PTRS.size===1&&lastP){
    var dx=xy[0]-lastP[0], dy=xy[1]-lastP[1];
    moved+=Math.abs(dx)+Math.abs(dy);
    var sx=(VX[1]-VX[0])/(W-PAD.l-PAD.r), sy=(VY[1]-VY[0])/(H-PAD.t-PAD.b);
    VX[0]-=dx*sx; VX[1]-=dx*sx;
    VY[0]+=dy*sy; VY[1]+=dy*sy;
    clampView(); draw();
    lastP=xy;
  } else if(PTRS.size>=2){
    var arr=[]; PTRS.forEach(function(p){arr.push(p);});
    var d=Math.hypot(arr[0].x-arr[1].x,arr[0].y-arr[1].y);
    var cx=(arr[0].x+arr[1].x)/2, cy=(arr[0].y+arr[1].y)/2;
    if(pinch0>0&&d>0) zoomAt(cx,cy,d/pinch0);
    pinch0=d; moved=99;
  }
});
function endP(e){
  var xy=cXY(e);
  PTRS.delete(e.pointerId);
  if(PTRS.size===1){ PTRS.forEach(function(p){lastP=[p.x,p.y];}); pinch0=0; }
  if(PTRS.size===0){
    if(moved<7){
      var now=Date.now();
      if(now-lastTapT<330){ zoomAt(xy[0],xy[1],1.7); lastTapT=0; }
      else { selectAt(xy[0],xy[1]); lastTapT=now; }
    }
    lastP=null; pinch0=0;
  }
}
cv.addEventListener('pointerup',endP);
cv.addEventListener('pointercancel',function(e){ PTRS.delete(e.pointerId); lastP=null; pinch0=0; });
cv.addEventListener('wheel',function(e){
  e.preventDefault();
  var xy=cXY(e);
  zoomAt(xy[0],xy[1],Math.pow(1.0015,-e.deltaY));
},{passive:false});
document.getElementById('creset').addEventListener('click',function(){
  VX=EXT.x.slice(); VY=EXT.y.slice(); draw();
});
function selectAt(x,y){
  var best=-1, bd=330;
  for(var i=0;i<STK.length;i++){
    var d=(STK[i].px-x)*(STK[i].px-x)+(STK[i].py-y)*(STK[i].py-y);
    if(d<bd){ bd=d; best=i; }
  }
  if(best>=0) select(best,true);
}
function esc(t){return String(t).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function select(i,scroll){
  sel=i; draw();
  var s=STK[i];
  var pct=((i+1)/STK.length*100);
  var pctS=pct<=50?('上位 '+(pct<1?pct.toFixed(1):Math.round(pct))+'%'):('下位 '+Math.round(100-pct)+'%');
  var el=document.getElementById('selcard');
  el.style.display='block';
  el.innerHTML='<div class="selname">'+esc(s.name)+'<small>'+s.code+' ・ '+esc(s.mkt||'')+'</small></div>'
    +'<div class="selfacts">'
    +'<span class="self">時価総額 <b>'+fmtM(s.m)+'</b>（'+(i+1)+'位 / '+STK.length.toLocaleString()+'銘柄・'+pctS+'）</span>'
    +'<span class="self">株価 <b>'+s.c.toLocaleString()+'円</b></span>'
    +'<span class="self">発行株式数 <b>'+fmtShares(s.sh)+'</b></span>'
    +'<span class="self">100株の資金 <b>'+Math.round(s.c*100/10000).toLocaleString()+'万円</b></span>'
    +'</div>'
    +'<div class="sellinks">'
    +'<a href="universe.html?q='+s.code+'">台帳で判定</a>'
    +'<a href="map.html?c='+s.code+'">銘柄マップ</a>'
    +'<a href="https://finance.yahoo.co.jp/quote/'+s.code+'.T" target="_blank" rel="noopener">Yahoo! →</a>'
    +'</div>';
  if(scroll) el.scrollIntoView({block:'nearest',behavior:'smooth'});
}
function normQ(s){
  var t=(s||'').normalize('NFKC').toLowerCase();
  t=t.replace(/[ぁ-ゖ]/g,function(ch){return String.fromCharCode(ch.charCodeAt(0)+0x60);});
  t=t.replace(/[\s\-・.,、。()（）\[\]「」『』&\/]/g,'');
  ['ホールディングス','ホールディング','グループ','株式会社','hd'].forEach(function(w){t=t.split(w).join('');});
  return t;
}
var cq=document.getElementById('cq'), csugg=document.getElementById('csugg');
cq.addEventListener('input',function(){
  var v=normQ(cq.value.trim());
  if(!v){ csugg.classList.remove('show'); return; }
  var hits=[];
  for(var i=0;i<STK.length&&hits.length<40;i++){
    if(STK[i].norm.indexOf(v)>=0||STK[i].code.indexOf(v)>=0) hits.push(i);
  }
  if(!hits.length){ csugg.classList.remove('show'); return; }
  csugg.innerHTML=hits.map(function(i){
    return '<div class="it" data-i="'+i+'">'+esc(STK[i].name)+'<small>'+STK[i].code+' ・ '+fmtM(STK[i].m)+'</small></div>';
  }).join('');
  csugg.classList.add('show');
  csugg.querySelectorAll('.it').forEach(function(el){
    el.addEventListener('click',function(){
      csugg.classList.remove('show'); cq.value='';
      select(+el.dataset.i,false);
    });
  });
});
document.addEventListener('click',function(e){
  if(!e.target.closest('#cwrap')&&!e.target.closest('.csearchrow')) csugg.classList.remove('show');
});
/* 色モード */
document.querySelectorAll('.cmode').forEach(function(b){
  b.addEventListener('click',function(){
    document.querySelectorAll('.cmode').forEach(function(x){x.classList.remove('on');});
    b.classList.add('on'); CMODE=b.dataset.cm;
    document.getElementById('lgtier').style.display=CMODE==='tier'?'flex':'none';
    document.getElementById('lgmkt').style.display=CMODE==='mkt'?'flex':'none';
    draw();
  });
});
/* ── 全銘柄ランキング（段階表示・大小ソート） ── */
var RDIR='desc', RSHOWN=0, RSTEP=100;
var rk=document.getElementById('rankall'), rmore=document.getElementById('rmore');
function rowHtml(idxInStk, dispRank, maxlm){
  var s=STK[idxInStk];
  var w=Math.max(3,(Math.log10(Math.max(1.5,s.m))-0)/(maxlm)*100);
  return '<div class="rrow" data-i="'+idxInStk+'"><span class="rrk">'+dispRank+'</span>'
    +'<span class="rnm">'+esc(s.name)+'</span>'
    +'<span class="rbarw"><span class="rbar" style="display:block;width:'+w.toFixed(0)+'%"></span></span>'
    +'<span class="rmc">'+fmtM(s.m)+'</span></div>';
}
function renderMore(){
  var maxlm=Math.log10(Math.max(2,STK[0].m));
  var frag=[], N=STK.length;
  var end=Math.min(N, RSHOWN+ (RSHOWN===0?RSTEP:RSTEP*5));
  for(var k=RSHOWN;k<end;k++){
    var idx=(RDIR==='desc')?k:(N-1-k);
    frag.push(rowHtml(idx, (RDIR==='desc')?(k+1):(N-k), maxlm));
  }
  rk.insertAdjacentHTML('beforeend', frag.join(''));
  RSHOWN=end;
  rmore.textContent=(RSHOWN>=N)?'すべて表示済み（'+N.toLocaleString()+'銘柄）':'さらに表示（あと'+(N-RSHOWN).toLocaleString()+'銘柄）';
  rmore.disabled=RSHOWN>=N;
}
rk.addEventListener('click',function(e){
  var row=e.target.closest('.rrow');
  if(!row) return;
  select(+row.dataset.i,false);
  cv.scrollIntoView({block:'center',behavior:'smooth'});
});
rmore.addEventListener('click',renderMore);
document.querySelectorAll('.rsbtn').forEach(function(b){
  b.addEventListener('click',function(){
    document.querySelectorAll('.rsbtn').forEach(function(x){x.classList.remove('on');});
    b.classList.add('on'); RDIR=b.dataset.d;
    rk.innerHTML=''; RSHOWN=0; renderMore();
  });
});
/* ── 市場全体のものさし ── */
function renderStats(){
  var N=STK.length, total=0, tiers=[0,0,0,0,0];
  for(var i=0;i<N;i++){
    var m=STK[i].m; total+=m;
    tiers[m>=100000?0:(m>=10000?1:(m>=1000?2:(m>=100?3:4)))]++;
  }
  var top10=0;
  for(var j=0;j<Math.min(10,N);j++) top10+=STK[j].m;
  var med=STK[Math.floor(N/2)].m;
  var cols=['#6b4487','#2e4d7b','#3a5a40','#b06a00','#8e8e93'];
  var labs=['10兆+','1兆+','1000億+','100億+','未満'];
  var bar='', note=[];
  for(var t2=0;t2<5;t2++){
    bar+='<i style="width:'+Math.max(1,tiers[t2]/N*100).toFixed(1)+'%;background:'+cols[t2]+'"></i>';
    note.push(labs[t2]+' '+tiers[t2].toLocaleString()+'社');
  }
  document.getElementById('capstats').innerHTML=
    '<div class="statgridc">'
    +'<div class="stc"><div class="k">全銘柄の時価総額 合計</div><div class="v">'+fmtM(total)+'</div></div>'
    +'<div class="stc"><div class="k">上位10社の占有率</div><div class="v">'+(top10/total*100).toFixed(1)+'%<small> ← 集中度</small></div></div>'
    +'<div class="stc"><div class="k">ちょうど真ん中の会社</div><div class="v">'+fmtM(med)+'<small>（中央値）</small></div></div>'
    +'<div class="stc"><div class="k">対象銘柄数</div><div class="v">'+N.toLocaleString()+'<small>銘柄</small></div></div>'
    +'</div>'
    +'<div class="stbar">'+bar+'</div>'
    +'<div class="stbarnote">'+note.join(' ・ ')+'（社数の割合）。合計や集中度は毎晩の実行で更新されます。</div>';
}
fetch('caps.json').then(function(r){
  if(!r.ok) throw new Error('caps.jsonがまだ生成されていません（次回の実行で作られます）');
  return r.json();
}).then(function(j){
  STK=j.stocks.map(function(a){
    var sh=a[5]*1e8/a[4];
    return {code:a[0], name:a[1], mkt:a[3], c:a[4], m:a[5], sh:sh,
            lx:Math.log10(a[4]), ly:Math.log10(sh),
            norm:normQ(a[1]), px:0, py:0};
  });
  STK.sort(function(a,b){return b.m-a.m;});
  var lxs=STK.map(function(s){return s.lx;});
  var lys=STK.map(function(s){return s.ly;});
  EXT.x=[Math.min.apply(null,lxs)-0.12, Math.max.apply(null,lxs)+0.25];
  EXT.y=[Math.min.apply(null,lys)-0.15, Math.max.apply(null,lys)+0.3];
  VX=EXT.x.slice(); VY=EXT.y.slice();
  resize();
  renderStats();
  renderMore();
  var qp=new URLSearchParams(location.search).get('c');
  if(qp){ for(var i=0;i<STK.length;i++) if(STK[i].code===qp){ select(i,false); break; } }
}).catch(function(e){
  document.getElementById('selcard').style.display='block';
  document.getElementById('selcard').textContent='⚠ '+e.message;
});
setTimeout(resize,50);
})();
</script>"""
    weekdays = "月火水木金土日"
    subtitle = (f"{dt.month}/{dt.day}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d} 時点 ・ "
                f"時価総額を計算できた{n_stocks:,}銘柄 ・ 株価×発行株式数＝時価総額の全体地図")
    footnote = ("株価・株式数は毎回の実行時点の値です。点の色は時価総額の階級（または市場区分）、斜めの点線は同じ時価総額のライン。"
                "個別の判定・指標は全銘柄台帳へ、似ている銘柄の探索は銘柄マップへ。")
    return (SUBPAGE_TEMPLATE
            .replace("__NAVCSS__", NAV_CSS)
            .replace("__HEADBTN__", "")
            .replace("__NAVJS__", NAV_JS)
            .replace("__NAV__", nav_html("caps"))
            .replace("__TITLE__", "時価総額マップ — 株価×株式数の全体地図")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", body)
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", script))


STATUS_LABEL = {"picked": "厳選候補", "ok": "候補", "bench": "圏外",
                "dead": "除外", "skip": "対象外", "fail": "取得失敗"}


def render_stock_detail(e):
    """1銘柄の詳細HTML断片（全銘柄一覧のタップ展開用）"""
    parts = [exec_card_html(e.get("exec_change")), topics_card_html(e.get("topics")),
             stock_meters_html(e)]
    status = e.get("status", "")
    if e.get("cand_rank"):
        parts.append(f'<div class="nhead">候補{e["cand_rank"]}位 ・ スコア {e.get("score", 0):.0f}点</div>')
        for r in e.get("reasons", []):
            parts.append(f'<div class="reason">・{html.escape(r)}</div>')
    elif e.get("score") is not None:
        parts.append(f'<div class="nhead">スコア {e["score"]:.0f}点（候補圏外）</div>')
        for r in e.get("reasons", []):
            parts.append(f'<div class="reason">・{html.escape(r)}</div>')
    elif status == "dead":
        parts.append(f'<div class="nhead">除外理由</div>'
                     f'<div class="reason">・{html.escape(e.get("reason", ""))}</div>')
    elif status == "skip":
        parts.append(f'<div class="nhead">対象外の理由</div>'
                     f'<div class="reason">・{html.escape(e.get("reason", ""))}</div>')

    if e.get("demerit") is not None:
        sev_cls = {"致命": "sevA", "重い": "sevB", "軽い": "sevC"}
        rk = e.get("demerit_rank")
        dm = e["demerit"]
        parts.append(f'<div class="nhead">減点方式: 全体{rk:,}位' + (f'（−{dm}点）</div>' if dm else '（無傷）</div>'))
        hits = e.get("demerit_hits") or []
        if hits:
            for h in hits:
                parts.append(f'<div class="hit"><span class="sev {sev_cls[h[0]]}">{h[0]}</span>'
                             f'{html.escape(h[1])}<span class="num hp">−{h[2]}</span></div>')
        else:
            parts.append('<div class="hit ok">買ってはいけない条件に一つも該当なし</div>')

    if e.get("tob") is not None:
        ann = ('<span class="tpbadge twarn">TOB・MBO発表済み</span>' if e.get("tob_announced") else "")
        parts.append(f'<div class="nhead">TOB素地スコア: {e["tob"]}点（全体{e.get("tob_rank", 0):,}位）{ann}</div>')
        for label, pts in (e.get("tob_hits") or []):
            parts.append(f'<div class="reason">・{html.escape(label)}（{pts:+d}）</div>')
        parts.append('<div class="discnote">買収・非公開化されやすい「体質」の順位で、発生の予測ではありません。'
                     '全体像と注意点は「TOB素地ランキング」ページ（使い方 › その他の機能）へ。</div>')

    fu = e.get("fund")
    if fu and fu.get("hist"):
        parts.append(fin_chart_html(fu["hist"]))
    if fu:
        parts.append('<div class="nhead">ファンダメンタル指標</div>')
        if fu.get("per") is not None:
            parts.append(f'<div class="fact"><span>PER</span><span class="num">{fu["per"]:.1f}倍</span></div>')
        if fu.get("pbr") is not None:
            parts.append(f'<div class="fact"><span>PBR</span><span class="num">{fu["pbr"]:.2f}倍</span></div>')
        if fu.get("roe") is not None:
            parts.append(f'<div class="fact"><span>ROE</span><span class="num">{fu["roe"]:.1f}%</span></div>')
        if fu.get("div_yield") is not None:
            parts.append(f'<div class="fact"><span>配当利回り</span><span class="num">{fu["div_yield"]:.2f}%</span></div>')
        if fu.get("mcap_oku"):
            parts.append(f'<div class="fact"><span>時価総額</span><span class="num">{fu["mcap_oku"]:,}億円</span></div>')

    lg = e.get("long") or {}
    sb = spark_block_html(lg.get("spark"), lg.get("spark10"), lg,
                          spark1=lg.get("spark1"), sparkall=lg.get("sparkall"),
                          years_all=lg.get("years_all"))
    if sb:
        parts.append(sb)

    days = e.get("days") or []
    if days:
        weekdays = "月火水木金土日"
        parts.append('<div class="nhead">ノート（新しい順）</div>')
        for d in reversed(days):
            dt2 = datetime.fromisoformat(d["date"])
            label = f"{dt2.month}/{dt2.day}({weekdays[dt2.weekday()]})"
            parts.append(f'<div class="nrow num"><span class="nd">{label}</span>'
                         f'<span>始:{d["open"]:,.0f} 高:{d["high"]:,.0f} '
                         f'安:{d["low"]:,.0f} 終:{d["close"]:,.0f}</span></div>')

    sims = e.get("similar") or []
    if sims:
        parts.append('<div class="nhead">発想が繋がる・似ている銘柄（マップの近傍）</div>')
        for sv in sims:
            why = "・".join(sv.get("why") or []) or "総合的に近い"
            exmark = '<span class="simex">除外中</span>' if sv.get("ex") else ""
            parts.append(f'<a class="simrow" href="map.html?c={sv["code"]}">'
                         f'<b>{html.escape(sv["name"])}</b> <span class="num">{sv["code"]}</span>{exmark}'
                         f'<span class="simw">{html.escape(why)}</span>'
                         f'<span class="num simp">{sv["sim"]}%</span></a>')
        parts.append('<div class="discnote">「財務体質 × テクニカル × 値動きの連動」の高次元ベクトルで近い銘柄。'
                     'タップすると関連銘柄マップの3D空間で、その銘柄を中心とした繋がりが開きます。'
                     '「除外中」は今夜の判定で除外・対象外になっている銘柄（推奨ではありません）。</div>')

    yahoo_url = f'https://finance.yahoo.co.jp/quote/{e["code"]}{e.get("suffix", ".T")}'
    parts.append(f'<div class="linkrow">'
                 f'<a class="ylink" href="{yahoo_url}" target="_blank" rel="noopener">Yahoo!ファイナンス →</a>'
                 f'<button type="button" class="ylink sbi" onclick="openSBI(\'{e["code"]}\', event)">SBI証券アプリで見る</button>'
                 f'<a class="ylink" href="map.html?c={e["code"]}">🗺 関連マップで見る</a>'
                 f'</div>')
    return "".join(parts)


def write_details(detail_map):
    ddir = DOCS / "details"
    ddir.mkdir(exist_ok=True)
    for code, e in detail_map.items():
        try:
            payload = {"html": render_stock_detail(e)}
            (ddir / f"{code}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
    print(f"  銘柄別詳細: {len(detail_map):,}件を書き出し")


def render_clean(clean_ranked, clean_stats, dt):
    """減点方式ランキング「無傷」ページ（帳簿と同じ見た目・持ち金連動・お気に入り連動）"""
    weekdays = "月火水木金土日"
    chip_class = {"プライム": "prime", "スタンダード": "std", "グロース": "growth"}
    sev_cls = {"致命": "sevA", "重い": "sevB", "軽い": "sevC"}

    rows = []
    for i, s in enumerate(clean_ranked, 1):
        chip = chip_class.get(s.get("market", ""), "local")
        cost = s["close"] * 100
        dm = s.get("demerit", 0)
        hits = s.get("demerit_hits", [])
        badge = ('<span class="dm zero">無傷</span>' if dm == 0
                 else f'<span class="dm">−{dm}</span>')
        hit_html = "".join(
            f'<div class="hit"><span class="sev {sev_cls[h[0]]}">{h[0]}</span>'
            f'{html.escape(h[1])}<span class="num hp">−{h[2]}</span></div>' for h in hits
        ) or '<div class="hit ok">減点項目なし。買ってはいけない条件に一つも該当しません</div>'
        fu = s.get("fund") or {}
        fund_line = " ・ ".join(x for x in [
            f'PER {fu["per"]:.1f}倍' if fu.get("per") is not None else "",
            f'PBR {fu["pbr"]:.2f}倍' if fu.get("pbr") is not None else "",
            f'ROE {fu["roe"]:.1f}%' if fu.get("roe") is not None else "",
        ] if x)
        yahoo_url = f'https://finance.yahoo.co.jp/quote/{s["code"]}{s.get("suffix", ".T")}'
        rows.append(f"""
      <details class="drow" data-cost="{cost:.0f}">
      <summary class="row">
        <div class="rk num">{i}</div>
        <div class="nm">
          <div class="n1">{html.escape(s["name"])} <span class="chip {chip}">{html.escape(s.get("market", ""))}</span></div>
          <div class="n2 num">{s["code"]} ・ 100株 {cost / 10000:,.1f}万円 ・ 加点{s.get("score", 0):.0f}点{" ・ " + fund_line if fund_line else ""}<span class="nofund">資金不足</span></div>
        </div>
        <div class="px"><div class="p1 num">{s["close"]:,.0f}<small>円</small></div>
          <div class="p2 num drop">高値から −{s["drop_pct"]:.1f}%</div></div>
        <div>{badge}</div>
        <div class="chev">›</div>
      </summary>
      <div class="notebox">
        <div class="nhead">減点の内訳（合計 −{dm}点）</div>
        {hit_html}
        <a class="ylink" href="{yahoo_url}" target="_blank" rel="noopener">Yahoo!ファイナンスで詳細を見る →</a>
      </div>
      </details>""")

    rules = "".join(
        f'<div class="hit"><span class="sev {sev_cls[a]}">{a}</span>{html.escape(b)}'
        f'<span class="num hp">−{c}</span></div>' for a, b, c in DEMERIT_RULES_DOC)

    body = f"""
<div class="capcard">
  <div class="caprow">持ち金 <input id="cap" class="capin num" type="number" inputmode="numeric" placeholder="50"> 万円
    <label class="caponly"><input id="showall" type="checkbox"> 資金不足も表示</label></div>
  <div class="capnote">帳簿と同じ設定を共有。100株買える銘柄だけを表示します。</div>
</div>
<details class="crit">
  <summary>減点ルール一覧（{len(DEMERIT_RULES_DOC)}項目・タップで開閉）<span class="chev">›</span></summary>
  <div class="critbody">
    <div class="step">加点方式の帳簿とは逆の発想です。「絶対に買ってはいけない条件」を全部並べ、該当した重さを減点。
    <b>減点ゼロ＝無傷</b>の銘柄が上位に来ます。株価・PER・PBRなどは毎日動くため、顔ぶれは毎回入れ替わります。
    対象はデータの取れた<b>全銘柄</b>（帳簿で除外・対象外になった銘柄も含めて全部採点。全体順位は各銘柄の詳細と全銘柄一覧で確認可）。減点が同じなら加点スコアの高い順。</div>
    {rules}
  </div>
</details>
<div class="ledger">
{"".join(rows)}
</div>
"""
    extra_css = """
  .capcard{background:#fff; border-radius:12px; padding:11px 14px; margin-bottom:12px;
    box-shadow:0 1px 3px rgba(0,0,0,.05);}
  .caprow{font-size:13px; font-weight:700; display:flex; align-items:center; gap:6px; flex-wrap:wrap;}
  .capin{width:70px; font-size:15px; font-weight:700; padding:5px 8px;
    border:1.5px solid #d9d2bf; border-radius:8px; background:#fff; text-align:right;}
  .caponly{font-size:11.5px; font-weight:600; color:var(--ink2); margin-left:auto;
    display:flex; align-items:center; gap:4px;}
  .capnote{font-size:10.5px; color:var(--ink3); line-height:1.6; margin-top:6px;}
  details.crit{background:#fff; border-radius:12px; margin-bottom:12px;
    box-shadow:0 1px 3px rgba(0,0,0,.05);}
  details.crit summary{list-style:none; cursor:pointer; font-size:12px; font-weight:800;
    color:#4a3f28; padding:11px 14px; display:flex; justify-content:space-between; align-items:center;}
  details.crit summary::-webkit-details-marker{display:none;}
  .critbody{padding:0 14px 12px; border-top:1px solid #f0ead9;}
  .step{font-size:11.5px; line-height:1.7; color:var(--ink2); padding:7px 0;}
  .ledger{background:var(--paper); border-radius:14px; padding:4px 0; box-shadow:0 1px 3px rgba(0,0,0,.06);}
  details.drow summary.row{list-style:none; cursor:pointer;}
  details.drow summary.row::-webkit-details-marker{display:none;}
  details[open] summary.row{background:#f4eedd;}
  .row{display:flex; align-items:center; gap:9px; padding:9px 14px; border-top:1px solid var(--paper-line);}
  .rk{width:22px; font-size:12px; color:#a99a76; font-weight:700; text-align:right; flex:none;}
  .nm{flex:1; min-width:0;} .n1{font-size:13.5px; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .n2{font-size:10.5px; color:var(--ink2); margin-top:2px;}
  .px{text-align:right; flex:none;} .p1{font-size:14.5px; font-weight:700;} .p1 small{font-size:10px; color:var(--ink2);}
  .p2{font-size:10.5px; margin-top:2px;} .drop{color:var(--cheap); font-weight:700;}
  .chip{display:inline-block; font-size:9.5px; font-weight:600; border-radius:5px; padding:1.5px 5px; vertical-align:1px;}
  .chip.prime{background:#e8eef8; color:#2e4d7b;} .chip.std{background:#e9f3ea; color:#3a5a40;}
  .chip.growth{background:#f4ecf9; color:#6b4487;} .chip.local{background:#f7efe4; color:#8a5a17;}
  .chev{color:#c9bd9d; font-size:16px; font-weight:700; transition:transform .15s;}
  details[open] .chev{transform:rotate(90deg);}
  .dm{font-size:11px; font-weight:800; border-radius:6px; padding:3px 7px; color:#8a5a17; background:var(--mild-bg);}
  .dm.zero{color:#1a5c37; background:#e9f3ea;}
  .notebox{background:#fffdf6; border-top:1px dashed var(--paper-line); padding:10px 14px 14px;}
  .nhead{font-size:10.5px; font-weight:800; color:#7a6a45; letter-spacing:.06em; margin:4px 0 6px;}
  .hit{display:flex; align-items:center; gap:6px; font-size:11.5px; padding:5px 0;
    border-bottom:1px dashed #f0ead9; color:var(--ink);}
  .hit.ok{color:#1a5c37; font-weight:700;}
  .hp{margin-left:auto; font-weight:800; color:#8a5a17;}
  .sev{flex:none; font-size:9px; font-weight:800; border-radius:4px; padding:1px 5px;}
  .sevA{background:#fdeeee; color:#c62f2f;} .sevB{background:var(--mild-bg); color:#b06a00;}
  .sevC{background:#eef0f4; color:#4b4f57;}
  .ylink{display:block; margin-top:10px; font-size:12px; font-weight:700; color:#2e4d7b;
    text-decoration:none; text-align:center; background:#eef2f8; border-radius:9px; padding:9px;}
  .nofund{display:none; color:#fff; background:#b06a00; font-size:9px; font-weight:800;
    border-radius:4px; padding:1px 4px; margin-left:6px; vertical-align:1px;}
  details.drow.over summary.row{opacity:.45;} details.drow.over .nofund{display:inline;}
  .caphidden{display:none !important;}
"""
    script = """<script>
const CAP_KEY = 'kabuobaa_capital';
const capIn = document.getElementById('cap'), allChk = document.getElementById('showall');
function applyCap(){
  const cap = (parseFloat(capIn.value) || 0) * 10000;
  localStorage.setItem(CAP_KEY, capIn.value || '');
  let n = 0;
  document.querySelectorAll('details.drow').forEach(r => {
    const over = cap > 0 && parseFloat(r.dataset.cost) > cap;
    r.classList.toggle('over', over);
    const hide = over && !allChk.checked;
    r.classList.toggle('caphidden', hide);
    if (!hide){ n++; r.querySelector('.rk').textContent = n; }
  });
}
capIn.value = localStorage.getItem(CAP_KEY) || '';
capIn.addEventListener('input', applyCap); allChk.addEventListener('change', applyCap); applyCap();
</script>"""
    n_flaw = clean_stats.get("flawless", 0)
    subtitle = (f"{dt.month}/{dt.day}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d} 判定 ・ "
                f"対象{clean_stats.get('screened', 0):,}銘柄のうち<b>無傷 {n_flaw}銘柄</b> ・ 減点の少ない順に上位{len(clean_ranked)}")
    footnote = ("減点方式は「欠点のなさ」のランキングで、加点方式の帳簿（光る点の多さ）とは別の物差しです。"
                "両方に載る銘柄は、光る点があり欠点も少ない銘柄。判断材料の表示のみで、投資判断はご自身で。")
    return (SUBPAGE_TEMPLATE
            .replace("__NAVCSS__", NAV_CSS)
            .replace("__HEADBTN__", "")
            .replace("__NAVJS__", NAV_JS)
            .replace("__NAV__", nav_html("clean"))
            .replace("__TITLE__", "無傷ランキング（減点方式）")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", body)
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", script))


# ------------------------------------------------------------
# 「指標」ページ: 各指標をメーター図と具体例で図解（子供にも大人にも）
# ------------------------------------------------------------
def meter_svg(zones, ticks, marker=None, unit=""):
    """横長メーター。zones=[(start,end,color,label)] ticks=[(value,label)]"""
    W, H = 640, 92
    lo = zones[0][0]; hi = zones[-1][1]
    def x(v):
        v = max(lo, min(hi, v))
        return 20 + (v - lo) / (hi - lo) * (W - 40)
    parts = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" style="width:100%; height:auto;">']
    for s, e, col, lab in zones:
        parts.append(f'<rect x="{x(s):.1f}" y="30" width="{x(e) - x(s):.1f}" height="22" rx="4" fill="{col}"/>')
        parts.append(f'<text x="{(x(s) + x(e)) / 2:.1f}" y="45" font-size="12" font-weight="700" fill="#1c1c1e" text-anchor="middle">{lab}</text>')
    for v, lab in ticks:
        parts.append(f'<line x1="{x(v):.1f}" y1="52" x2="{x(v):.1f}" y2="60" stroke="#6e6e73" stroke-width="1.5"/>')
        parts.append(f'<text x="{x(v):.1f}" y="74" font-size="11" fill="#6e6e73" text-anchor="middle">{lab}{unit}</text>')
    if marker is not None:
        mv, mlab = marker
        parts.append(f'<path d="M {x(mv):.1f} 28 l -7 -12 l 14 0 z" fill="#1c1c1e"/>')
        parts.append(f'<text x="{x(mv):.1f}" y="12" font-size="11" font-weight="800" fill="#1c1c1e" text-anchor="middle">{mlab}</text>')
    parts.append("</svg>")
    return "".join(parts)


G, Y, R, B, N = "#b9dcc0", "#f5e6b3", "#f2c4a8", "#c9dcf3", "#eeeae0"  # 良い/注意/悪い/情報/中立

INDICATORS = [
    {
        "key": "eps", "name": "EPS（1株あたり利益）", "tag": "PERの材料",
        "one": "会社の1年の利益を発行株数で割った「1株が稼いだ金額」。株価 ÷ EPS ＝ PER。",
        "kid": "クラス全員でお店をやって100万円もうかったら、40人で割ってひとり2.5万円。これが「1株あたりの取り分」＝EPS。",
        "meter": "",
        "cases": [
            ("EPS 150円", G, "株価1,500円ならPER10倍。EPSが毎年伸びている会社は、株価が同じなら年々割安になっていく。"),
            ("EPS 横ばい", N, "金額の大小そのものに良し悪しはない（株数で変わるため）。大事なのは「伸びているか」と株価との比率（PER）。"),
            ("EPS マイナス", R, "赤字。PERは計算不能（マイナス）になり、このシステムでは大きく減点。"),
        ],
        "trap": "EPSの金額そのものを会社同士で比べても意味がない（株の分割で何倍にも変わる）。比べるのは「同じ会社の去年と今年」か、株価と割ったPER。自社株買いで株数が減るとEPSは利益が同じでも上がる。",
        "score": "単独では採点せず、株価÷EPSでPERを算出して採点。決算短信（J-Quants）の実績値を使用。予想EPSがあればPEG（成長性）にも使用。",
    },
    {
        "key": "bps", "name": "BPS（1株あたり純資産）", "tag": "PBRの材料",
        "one": "会社の純資産（資産−借金）を発行株数で割った「1株の持ち分の中身」。株価 ÷ BPS ＝ PBR。",
        "kid": "会社をいま解散してぜんぶ売り払い、借金を返して残ったお金を株数で山分けしたら1株いくらか、という金額。",
        "meter": "",
        "cases": [
            ("BPS 2,000円 ／ 株価 1,600円", B, "PBR0.8倍。持ち分の中身より安く買える状態。中身（資産の質・稼ぐ力）の確認が前提で加点。"),
            ("BPS 500円 ／ 株価 1,500円", N, "PBR3倍。成長企業なら普通。BPS単独では判断しない。"),
            ("BPSが毎年減っている", R, "赤字の垂れ流しや大きな損失で持ち分が痩せている合図。要警戒。"),
        ],
        "trap": "帳簿上の資産が実際に売れるとは限らない（古い工場・在庫など）。BPSが高くても「中身」の質はわからないため、ROE（その資産で稼げているか）とセットで見る。",
        "score": "単独では採点せず、株価÷BPSでPBRを算出して採点。決算短信（J-Quants）の実績値を使用。ROE＝EPS÷BPSの算出にも使用。",
    },
    {
        "key": "per", "name": "PER（株価収益率）", "tag": "割安・割高",
        "one": "株価が「1年分の利益の何年分」か。低いほど利益に対して安い。",
        "kid": "たとえば1年で100円もうかるお店を1,000円で買ったらPER10倍。10年で元が取れる、ということ。",
        "meter": meter_svg([(0, 10, B, "割安圏（要確認）"), (10, 20, G, "標準（安心）"), (20, 40, Y, "割高圏"), (40, 60, R, "異常")],
                           [(0, "0"), (10, "10"), (20, "20"), (40, "40"), (60, "60")], marker=(15, "例: 15倍"), unit="倍"),
        "cases": [
            ("8倍", G, "利益に対して株価が安い。同業比較で確認し、業績が落ちていなければ「割安」で加点。"),
            ("15倍", N, "日本株の平均的な水準（標準＝安心圏）。これ単独では判断材料にならず、他の指標を見る。"),
            ("70倍", R, "利益の70年分。成長期待が先行しており、期待が剥げると下げが深い。減点。"),
        ],
        "trap": "業績が悪化して利益が減ると見かけのPERは「上がる」。逆に一時的な特別利益で「下がる」。マイナス＝赤字。業種で水準がまったく違うので同業比較が基本。",
        "score": "20倍以下 +8点 ／ 60倍超 −10点 ／ 赤字（マイナス）−20点。減点方式では60倍超−15、赤字−30。",
    },
    {
        "key": "pbr", "name": "PBR（株価純資産倍率）", "tag": "資産に対する値段",
        "one": "株価が「会社の純資産の何倍」か。1倍未満は理論上、会社を解散した方が高い水準。",
        "kid": "1,000円の貯金が入った貯金箱を800円で売っている状態がPBR0.8倍。お得に見えるけど、貯金箱に穴が開いていないか（稼ぐ力）は要確認。",
        "meter": meter_svg([(0, 0.5, Y, "注意"), (0.5, 1.5, B, "割安圏（要確認）"), (1.5, 3, G, "標準（安心）"), (3, 6, Y, "割高"), (6, 8, R, "過熱")],
                           [(0.5, "0.5"), (1, "1.0"), (1.5, "1.5"), (3, "3"), (6, "6"), (8, "8+")], marker=(1.2, "例: 1.2倍"), unit="倍"),
        "cases": [
            ("0.7倍", B, "純資産より安く売られている。ROEが低くなければ「割安の罠」ではなく本物の割安。要確認のうえ加点。"),
            ("2.0倍", G, "標準的。成長企業ならこのくらいは普通。"),
            ("9倍", R, "資産の9倍の値段。IT・バイオ系に多いが、資産面の下値支えがなく減点。"),
        ],
        "trap": "低PBRが何年も放置される「割安の罠」がある。理由は「稼ぐ力がない」か「資産の中身が悪い」。必ずROEとセットで見る。",
        "score": "0.5〜1.5倍 +6点 ／ 8倍超 −8点。",
    },
    {
        "key": "roe", "name": "ROE（自己資本利益率）", "tag": "稼ぐ力",
        "one": "株主のお金（純資産）で、1年にどれだけ利益を出したか。会社の「稼ぐ力」そのもの。",
        "kid": "100万円のお小遣い元手で1年に10万円もうけたらROE10%。同じ元手で3万円しかもうからない会社より優秀。",
        "meter": meter_svg([(0, 3, R, "弱い"), (3, 5, Y, "注意"), (5, 10, G, "標準（安心）"), (10, 20, B, "優良"), (20, 30, B, "非常に高い")],
                           [(0, "0"), (3, "3"), (5, "5"), (10, "10"), (20, "20")], marker=(8.5, "日本平均 約8.5%"), unit="%"),
        "cases": [
            ("15%", G, "資本効率が高い。低PBRと組み合わさっていれば「安くて稼ぐ」理想形。加点。"),
            ("6%", N, "標準的。可もなく不可もなし。"),
            ("1.5%", R, "元手のわりに稼げていない。低PBRの理由がこれなら「割安の罠」。減点。"),
        ],
        "trap": "借金を増やすとROEは見かけ上がる（レバレッジ）。極端に高いROE（40%超など）は自己資本が薄いだけの可能性もある。",
        "score": "10%以上 +8点 ／ 3%未満 −5点。減点方式では3%未満−12。",
    },
    {
        "key": "div", "name": "配当利回り", "tag": "持っているだけの収入",
        "one": "株価に対する年間配当の割合。3〜4%は高配当の部類。",
        "kid": "1,000円の株で年30円もらえたら利回り3%。銀行預金よりずっと高いが、株価は上下する。",
        "meter": meter_svg([(0, 1, N, "低い"), (1, 3, G, "標準"), (3, 5, B, "高配当"), (5, 8, Y, "高すぎ注意")],
                           [(0, "0"), (1, "1"), (3, "3"), (5, "5"), (8, "8")], marker=(3.2, "例: 3.2%"), unit="%"),
        "cases": [
            ("3.5%", G, "配当が下値を支えやすい。押し目買いの安心材料。加点。"),
            ("1.2%", Y, "成長投資に回す会社に多い。配当は判断材料にならない。"),
            ("7%", R, "株価急落で見かけの利回りが跳ねているか、減配前の可能性。要警戒。"),
        ],
        "trap": "利回り＝配当÷株価なので、株価が下がるほど利回りは上がる。高利回りが「安さの結果」なのか「業績悪化の前兆」なのか見極めが必要。",
        "score": "3%以上 +5点。",
    },
    {
        "key": "rsi", "name": "RSI（14日）", "tag": "過熱感",
        "one": "直近14日の値動きから「買われすぎ・売られすぎ」を0〜100で示す。",
        "kid": "ボールを地面に強く叩きつけるほど跳ね返る、の「叩きつけ度」。30以下は強く叩きつけられた状態。",
        "meter": meter_svg([(0, 30, G, "売られすぎ"), (30, 40, Y, "やや売られすぎ"), (40, 60, N, "中立"), (60, 70, Y, "やや買われすぎ"), (70, 100, R, "買われすぎ")],
                           [(0, "0"), (30, "30"), (50, "50"), (70, "70"), (100, "100")], marker=(28, "例: 28")),
        "cases": [
            ("25", G, "売られすぎ。押し目買い手法との相性が最も良い状態。加点。"),
            ("50", N, "中立。判断材料にならない。"),
            ("78", R, "買われすぎ。ここから買うのは高値掴みになりやすい。減点方式で減点。"),
        ],
        "trap": "強い下落トレンド中はRSI30以下が「続く」ことがある（張り付き）。だからこそこのシステムは「下げ止まり確認」とセットで使う。",
        "score": "30以下 +10点 ／ 40以下 +5点。減点方式では70超−6。",
    },
    {
        "key": "macd", "name": "MACD", "tag": "勢いの転換",
        "one": "短期と中期の平均線の差で「勢いの向き」を見る。マイナス圏からの買い転換が最も広く使われるサイン。",
        "kid": "坂道を下っていた自転車が、ペダルを漕ぎ始めた瞬間を捉える指標。「まだ下り坂だけど勢いは上向き」がわかる。",
        "meter": meter_svg([(0, 1, R, "下向き"), (1, 2, Y, "転換直後"), (2, 3, G, "上向き継続")],
                           [(0.5, "シグナル線の下"), (1.5, "買い転換"), (2.5, "上")], marker=(1.5, "例: 買い転換")),
        "cases": [
            ("買い転換（直近5日）", G, "下げの勢いが尽きて反転し始めた。押し目買いのタイミングとして最良。+8点。"),
            ("上向き継続", G, "勢いは上。追随買いは可だが押し目としては遅い。+4点。"),
            ("下向き", R, "まだ下げの勢いが残る。ナイフの落下中。加点なし。"),
        ],
        "trap": "横ばい相場ではダマシ（転換→すぐ戻る）が多い。単独では使わず、支持帯やRSIと重ねる。",
        "score": "直近5日以内の買い転換 +8点 ／ 上向き継続 +4点。",
    },
    {
        "key": "boll", "name": "ボリンジャーバンド", "tag": "統計的な行きすぎ",
        "one": "過去20日の値動きの「ばらつき（σ）」で普通の範囲を測る。−2σ以下は統計上約2%しか起きない売られすぎ。",
        "kid": "身長の平均から極端に外れた人が珍しいのと同じ。−2σは「クラスで一番背が低い」レベルの珍しさ。珍しい状態は長続きしにくい。",
        "meter": meter_svg([(-3, -2, G, "売られすぎ"), (-2, -1, Y, "下限付近"), (-1, 1, N, "普通の範囲"), (1, 2, Y, "上限付近"), (2, 3, R, "買われすぎ")],
                           [(-2, "−2σ"), (-1, "−1σ"), (0, "中心"), (1, "+1σ"), (2, "+2σ")], marker=(-2.1, "例: −2.1σ")),
        "cases": [
            ("−2.3σ", G, "統計的な売られすぎ。平均への回帰（戻り）が期待できる。+8点。"),
            ("−0.5σ", N, "普通の範囲。特に材料なし。"),
            ("+2.2σ", R, "買われすぎ。過熱。減点方式で減点。"),
        ],
        "trap": "急落局面ではバンド自体が広がって「−2σに触れたまま下げ続ける」（バンドウォーク）ことがある。下げ止まり確認とセットで。",
        "score": "−2σ以下 +8点 ／ −1.5σ以下 +4点。減点方式では+2σ超−6。",
    },
    {
        "key": "dev", "name": "25日移動平均乖離率", "tag": "逆張りの定番",
        "one": "25日平均線から何%離れているか。−8%を超える下方乖離は逆張りの定番圏。",
        "kid": "ゴムひもを引っ張るほど戻る力が強くなる。ただし引っ張りすぎるとゴムが切れる（−20%超は異常事態）。",
        "meter": meter_svg([(-30, -20, R, "異常乖離"), (-20, -8, G, "逆張り圏"), (-8, 0, N, "通常"), (0, 10, N, "通常（上）")],
                           [(-30, "−30"), (-20, "−20"), (-8, "−8"), (0, "0"), (10, "+10")], marker=(-11, "例: −11%"), unit="%"),
        "cases": [
            ("−12%", G, "平均線から大きく下に離れ、戻りやすい位置。+6点。"),
            ("−3%", N, "通常範囲。押し目としては浅い。"),
            ("−25%", R, "乖離しすぎ。決算ミス等の「何か」が起きている可能性。−5点。"),
        ],
        "trap": "乖離率の「戻りやすい」は平均への回帰であって、上昇トレンド復帰の保証ではない。",
        "score": "−8〜−20% +6点 ／ −20%超 −5点。減点方式でも−20%超は−10。",
    },
    {
        "key": "gc", "name": "ゴールデンクロス（50日/200日線）", "tag": "長期の地合い",
        "one": "50日平均線が200日平均線の上にある＝長期の上昇形。押し目買い手法との相性が最も良い環境。",
        "kid": "最近1ヶ月の平均点が、1年の平均点より高い生徒。「調子が上向きの子」の悪い日を狙うのが押し目買い。",
        "meter": meter_svg([(0, 1, R, "デッドクロス（50日線が下）"), (1, 2, G, "ゴールデンクロス（50日線が上）")],
                           [(0.5, "長期は調整形"), (1.5, "長期は上昇形")], marker=(1.5, "例: GC中")),
        "cases": [
            ("GC継続中の押し目", G, "上昇トレンド中の一時的な下げ。この手法の理想形。+20点（200日線の上での下げ）+10点。"),
            ("DC中の反発", R, "下落トレンド中の一時的な戻り。上値が重く、利確に届きにくい。減点方式で−10。"),
        ],
        "trap": "クロスの瞬間は「遅行」する（トレンドがだいぶ進んでから点灯）。タイミングではなく環境判断に使う。",
        "score": "200日線の上での下げ +20点 ／ GC中 +10点。減点方式ではDC中−10。",
    },
    {
        "key": "zone", "name": "長期支持帯とタッチ回数", "tag": "過去に買いが入った価格帯",
        "one": "3年の谷を集めた「何度も反発した価格帯」。定石は「〜3回目までは支持されやすく、4回目以降は割れやすい」。",
        "kid": "同じ場所で3回転んだら、4回目はそこが崩れているかもしれない。逆に2回しか転んでいない場所はまだ丈夫。",
        "meter": meter_svg([(0, 3, G, "1〜3回目の試し（信頼圏）"), (3, 5, R, "4回目以降（割れやすい）")],
                           [(1, "1回目"), (2, "2回目"), (3, "3回目"), (4, "4回目"), (5, "5回目")], marker=(3, "例: 3回目")),
        "cases": [
            ("過去2回反発・今3回目", G, "支持帯の直上での押し目。定石の信頼圏内。+12点。"),
            ("過去4回反発・今5回目", R, "試されすぎ。次は割れる可能性が高まる。−5点・減点方式−8。"),
            ("帯を明確に割った後", R, "支持帯は無効。このシステムは自動で候補から外す。"),
        ],
        "trap": "支持帯は「絶対の壁」ではなく「買い手が多かった記憶」。出来高を伴って割れたら素直に諦める。",
        "score": "〜3回目 +12点 ／ 4回目以降 −5点。",
    },
    {
        "key": "mkt", "name": "地合い（日経平均の200日線）", "tag": "市場全体の風向き",
        "one": "日経平均が200日線の上か下か。下にある間は市場全体が下落基調で、個別の押し目買いの成功率も落ちる。",
        "kid": "みんなが下り坂を歩いているときに、一人だけ上り坂を歩くのは大変。風向きを見てから歩き出す。",
        "meter": meter_svg([(0, 1, R, "200日線の下（警戒）"), (1, 2, G, "200日線の上（順風）")],
                           [(0.5, "全体が下落基調"), (1.5, "全体が上昇基調")], marker=(1.5, "例: 順風")),
        "cases": [
            ("上昇基調", G, "帳簿上部に緑のバナー。普段通りに押し目を拾ってよい。"),
            ("下落基調", R, "オレンジの警戒バナー。買いは普段より慎重に、株数を減らす・見送るなどの判断を。"),
        ],
        "trap": "地合いは「傾向」であって個別銘柄の保証にはならない。逆に、悪い地合いで下げ止まっている銘柄は強い銘柄。",
        "score": "スコアには入れず、帳簿上部のバナーで注意喚起。",
    },
]


SECONDARY_INDICATORS = [
    {"key": "stoch", "name": "ストキャスティクス（スロー）", "tag": "過熱感（RSIの相棒）",
     "one": "直近14日の値幅の中で、いまの終値がどの位置か（0〜100）。20以下=売られすぎ、80以上=買われすぎ。",
     "kid": "教室の身長順で、今日の自分が「一番低い側」にいるかを見る指標。RSIより反応が早い。",
     "meter": meter_svg([(0, 20, G, "売られすぎ"), (20, 30, Y, "やや"), (30, 70, N, "中立"), (70, 80, Y, "やや"), (80, 100, R, "買われすぎ")],
                        [(0, "0"), (20, "20"), (50, "50"), (80, "80"), (100, "100")], marker=(15, "例: 15")),
     "cases": [("12", G, "売られすぎ。RSIとセットで低ければ押し目の信頼度が上がる。+4点。"),
               ("50", N, "中立。材料にならない。"),
               ("88", R, "買われすぎ。追いかけ買いは危険。")],
     "trap": "強いトレンド中は80以上・20以下に「張り付く」。単独では使わず、下げ止まり確認と併用。",
     "score": "20以下 +4点。"},
    {"key": "adx", "name": "DMI / ADX", "tag": "トレンドの強さと向き",
     "one": "ADXは「トレンドの強さ」（向きは示さない）。+DIと−DIのどちらが上かで向きが分かる。ADX25以上で「トレンドあり」。",
     "kid": "風の強さ（ADX）と風向き（+DI/−DI）。強い追い風の中の一休み（押し目）が理想、強い向かい風なら見送り。",
     "meter": meter_svg([(0, 20, N, "トレンドなし"), (20, 25, Y, "弱い"), (25, 60, G, "トレンドあり（向きは±DIで）")],
                        [(0, "0"), (20, "20"), (25, "25"), (40, "40"), (60, "60")], marker=(28, "例: 28")),
     "cases": [("ADX30・+DI優勢", G, "上昇トレンドに勢いがある中の押し目。+4点。"),
               ("ADX15", N, "方向感なし。レンジ相場。"),
               ("ADX30・−DI優勢", R, "下降トレンドに勢い。ナイフの落下中。−4点。")],
     "trap": "ADXは遅行する。上がり始めた時点でトレンドはかなり進んでいる。環境認識用。",
     "score": "ADX25以上かつ+DI優勢 +4点／−DI優勢 −4点。"},
    {"key": "ichimoku", "name": "一目均衡表（雲）", "tag": "中期の地合い",
     "one": "先行スパンで作る「雲」に対して株価が上か中か下か。雲の上=中期強気、雲の下=中期弱気、雲の中=方向感なし。",
     "kid": "雲の上を飛んでいる飛行機は安定、雲の中は視界不良、雲の下は雨。",
     "meter": meter_svg([(0, 1, R, "雲の下"), (1, 2, Y, "雲の中"), (2, 3, G, "雲の上")],
                        [(0.5, "弱気"), (1.5, "もみ合い"), (2.5, "強気")], marker=(2.5, "例: 雲の上")),
     "cases": [("雲の上での押し目", G, "中期の支えがある下げ。+4点。"),
               ("雲の中", Y, "方向感なし。他の指標で判断。"),
               ("雲の下", R, "中期は弱い。反発しても雲が上値抵抗になりやすい。−3点。")],
     "trap": "雲のねじれ（先行スパンの交差）付近は転換点になりやすく、判断が難しい。",
     "score": "雲の上 +4点／雲の下 −3点。"},
    {"key": "atr", "name": "ATR（アベレージ・トゥルー・レンジ）", "tag": "値幅・損切り幅の目安",
     "one": "1日にどれくらい動く銘柄か（株価比%）。損切り幅・利確幅を「その銘柄の普段の動き」に合わせるのに使う。",
     "kid": "その人の歩幅。歩幅の小さい人は少しの距離で判断できるが、大股の人は同じ距離だとすぐ踏み越える。",
     "meter": meter_svg([(0, 2, G, "穏やか"), (2, 3.5, N, "普通"), (3.5, 5, Y, "大きめ"), (5, 10, R, "荒い")],
                        [(0, "0"), (2, "2"), (3.5, "3.5"), (5, "5"), (10, "10")], marker=(1.8, "例: 1.8%"), unit="%"),
     "cases": [("1.5%", G, "日々の値幅が小さく、−8%損切りは十分な余裕。+3点。"),
               ("3%", N, "普通。損切りは−8%で妥当。"),
               ("6%", R, "1日で損切りに届く。損切り幅を広げるか、そもそも見送り。−3点。")],
     "trap": "ATRが小さすぎる銘柄は動かないので短期回転には向かない。「小さければ良い」ではない。",
     "score": "2%以下 +3点／5%以上 −3点。損切り%の妥当性チェックにも。"},
    {"key": "obvmfi", "name": "OBV / MFI（出来高系）", "tag": "お金の流れ",
     "one": "OBVは「上げた日の出来高を足し、下げた日を引く」累積線。株価が下がっているのにOBVが上向きなら「誰かが買い集めている」。MFIは出来高込みのRSI。",
     "kid": "お店の売上（株価）は落ちているのに、常連客（出来高）が増えている——それは復活の前兆かもしれない。",
     "meter": meter_svg([(0, 20, G, "MFI売られすぎ"), (20, 30, Y, "やや"), (30, 70, N, "中立"), (70, 80, Y, "やや"), (80, 100, R, "買われすぎ")],
                        [(0, "0"), (20, "20"), (50, "50"), (80, "80"), (100, "100")], marker=(18, "例: MFI 18")),
     "cases": [("OBV上向き＋株価下落", G, "下げの中で買い集め。反発の芽。+3点。"),
               ("MFI 15", G, "出来高込みの売られすぎ。+3点。"),
               ("OBV下向き＋株価横ばい", R, "静かに売り抜けられている可能性。")],
     "trap": "出来高の少ない銘柄では大口1件で歪む。売買代金の大きい銘柄で意味を持つ。",
     "score": "OBV20日で+5%超 +3点／MFI20以下 +3点。"},
    {"key": "hv", "name": "ヒストリカル・ボラティリティ（HV）", "tag": "値動きの荒さ（年率）",
     "one": "過去20日の値動きから計算した年率換算の変動率。日本株の平均は25〜35%程度。",
     "kid": "ジェットコースターの高低差。高いほどスリルはあるが、目的地（利確）に着く前に振り落とされやすい。",
     "meter": meter_svg([(0, 25, G, "穏やか"), (25, 40, N, "普通"), (40, 60, Y, "高め"), (60, 120, R, "非常に高い")],
                        [(0, "0"), (25, "25"), (40, "40"), (60, "60"), (120, "120")], marker=(30, "例: 30%"), unit="%"),
     "cases": [("22%", G, "落ち着いた銘柄。夜1回判断のスタイルと相性が良い。"),
               ("35%", N, "普通。"),
               ("75%", R, "荒すぎ。翌朝の窓開けも大きくなる。減点方式で減点対象。")],
     "trap": "HVが低い＝安全ではない。決算前などに急に跳ねる。",
     "score": "採点には直接使わず、ATR・夜間ギャップで代替。メーター表示のみ。"},
    {"key": "eqratio", "name": "自己資本比率", "tag": "財務の厚み（倒産しにくさ）",
     "one": "総資産のうち返さなくていいお金（自己資本）の割合。高いほど不況に強い。目安: 50%以上=厚い、20%未満=借入依存。",
     "kid": "家の値段のうち自分のお金で払った割合。ローンだらけの家は金利が上がると危ない。",
     "meter": meter_svg([(0, 20, R, "借入依存"), (20, 35, Y, "やや薄い"), (35, 50, N, "標準"), (50, 100, G, "厚い")],
                        [(0, "0"), (20, "20"), (35, "35"), (50, "50"), (100, "100")], marker=(55, "例: 55%"), unit="%"),
     "cases": [("60%", G, "財務が厚い。下げても倒産リスクが低く、安心して押し目を拾える。+4点。"),
               ("40%", N, "標準的。"),
               ("12%", R, "借入依存。金利上昇や業績悪化に脆い。−5点。")],
     "trap": "銀行・リース・商社などは業種の性質上、低くて正常。同業比較が必須。",
     "score": "50%以上 +4点／20%未満 −5点。"},
    {"key": "opmargin", "name": "営業利益率 / ROA", "tag": "本業の稼ぐ力・資産効率",
     "one": "営業利益率＝売上のうち本業で残る利益の割合（10%以上=高収益）。ROA＝総資産に対する利益（5%以上=効率良い）。ROEと違い借金で水増しされない。",
     "kid": "1,000円のお弁当を売って本業でいくら残るか。100円残れば営業利益率10%。",
     "meter": meter_svg([(-10, 0, R, "営業赤字"), (0, 5, Y, "薄利"), (5, 10, N, "標準"), (10, 30, G, "高収益")],
                        [(-10, "−10"), (0, "0"), (5, "5"), (10, "10"), (30, "30")], marker=(12, "例: 12%"), unit="%"),
     "cases": [("営業利益率15%", G, "本業が強い。株価が下げても業績が支える。+3点。"),
               ("5%", N, "標準。"),
               ("−3%（営業赤字）", R, "本業で損している。下げても戻りが鈍い。−5点。")],
     "trap": "利益率は業種差が大きい（小売は低く、ソフトは高い）。同業比較で見る。",
     "score": "営業利益率10%以上 +3点／営業赤字 −5点／ROA5%以上 +3点。"},
    {"key": "payout", "name": "配当性向 / PEG", "tag": "配当の持続性・成長との割安度",
     "one": "配当性向＝利益のうち配当に回す割合（30〜60%が健全、100%超は利益以上に配っており減配リスク）。PEG＝PER÷利益成長率（1倍以下なら成長に対して割安）。",
     "kid": "配当性向はお小遣いのうち貯金せず使う割合。100%超は貯金を切り崩している。PEGは「伸び盛りの子の月謝が割安か」。",
     "meter": meter_svg([(0, 30, N, "低め"), (30, 60, G, "健全"), (60, 100, Y, "高め"), (100, 150, R, "無理している")],
                        [(0, "0"), (30, "30"), (60, "60"), (100, "100"), (150, "150")], marker=(45, "例: 45%"), unit="%"),
     "cases": [("配当性向40%", G, "余裕を持って配当。持続性が高い。"),
               ("配当性向120%", R, "利益以上に配当。減配の予備軍。−3点。"),
               ("PEG 0.8倍", B, "成長率に対して株価が割安。+4点。")],
     "trap": "PEGは予想EPSに依存する。会社予想は保守的にも楽観的にもなる。",
     "score": "配当性向100%超 −3点／PEG1倍以下 +4点。"},
    {"key": "breadth", "name": "騰落レシオ・新高値新安値・NT倍率", "tag": "市場全体の体温",
     "one": "騰落レシオ＝値上がり銘柄数÷値下がり銘柄数（25日累計で120%超は過熱、70%未満は売られすぎ）。新高値・新安値の銘柄数は相場の広がり。NT倍率＝日経平均÷TOPIX（高いほど値がさハイテク偏重）。",
     "kid": "クラス全員の機嫌。半分以上が笑顔なら地合いは良い。数人だけ笑って残りが泣いていたら偏った相場。",
     "meter": meter_svg([(0, 70, G, "売られすぎ（逆張り好機）"), (70, 100, N, "普通"), (100, 120, N, "やや強い"), (120, 160, R, "過熱")],
                        [(0, "0"), (70, "70"), (100, "100"), (120, "120"), (160, "160")], marker=(95, "例: 95%"), unit="%"),
     "cases": [("騰落レシオ65%", G, "市場全体が売られすぎ。個別の押し目が効きやすい局面。"),
               ("新安値銘柄が急増", R, "全体が崩れている。個別で良く見えても慎重に。"),
               ("NT倍率が急上昇", Y, "一部の値がさ株だけが相場を引っ張っている。裾野が狭い。")],
     "trap": "全体指標は「傾向」。個別の勝負を保証しない。帳簿上部の地合いバナーで毎日確認。",
     "score": "採点には入れず、地合いバナーに表示。"},
]

GLOSSARY = [
    ("バリュエーション", [
        ("EPS / BPS / SPS / CFPS / DPS", "1株あたりの利益・純資産・売上・キャッシュフロー・配当。PER・PBR・PSR・PCFR・利回りの分母になる基礎数字。"),
        ("PSR（株価売上高倍率）", "株価÷1株売上。赤字の成長企業をPERで測れないときの代替。1倍未満は割安寄りだが業種差が大きい。"),
        ("PCFR（株価キャッシュフロー倍率）", "株価÷1株CF。利益より操作されにくいCFで見た割安度。"),
        ("EV/EBITDA", "企業価値÷（営業利益+減価償却）。買収の値付けで使う指標。8倍以下は割安寄り。個人の短期売買では出番は少ない。"),
    ]),
    ("財務健全性・効率性", [
        ("流動比率 / 当座比率", "1年以内に払う負債に対し、1年以内に現金化できる資産がどれだけあるか。流動比率200%以上・当座比率100%以上が安心の目安。"),
        ("D/Eレシオ", "有利子負債÷自己資本。1倍以下が健全の目安。"),
        ("インタレスト・カバレッジ", "営業利益÷支払利息。利払いの余裕。10倍以上なら安心。"),
        ("フリーキャッシュフロー（FCF）", "本業で稼いだ現金から投資を引いた残り。プラスが続く会社は自力で成長・還元できる。"),
        ("粗利益率 / 経常利益率 / 純利益率", "売上に対する各段階の利益率。粗利は商品力、経常は財務込み、純利益は最終。"),
        ("総資産回転率 / 棚卸資産回転率", "資産・在庫がどれだけ効率よく売上に変わっているか。低下は在庫過剰の兆し。"),
        ("ROIC", "投下資本利益率。事業に投じたお金に対する本業の利益率。WACC（資本コスト）を上回っていれば価値を生んでいる。"),
    ]),
    ("テクニカル（トレンド系）", [
        ("EMA / WMA / HMA / ALMA", "直近を重視した移動平均の変種。SMAより反応が速い。当システムは50日/200日SMAとMACD（EMAベース）で代替。"),
        ("パラボリックSAR", "トレンドの転換点をドットで示す。追随には向くが、レンジ相場でダマシが多い。"),
        ("エンベロープ / ケルトナー / ドンチャン", "移動平均や高値安値からの一定幅のバンド。ボリンジャーバンドの兄弟。"),
        ("GMMA", "短期6本+長期6本の移動平均の束。束の広がりでトレンドの強さを見る。"),
        ("スーパートレンド", "ATRベースのトレンド追随線。損切りラインの置き場にも使われる。"),
        ("ジグザグ", "細かい動きを消して山と谷だけを結ぶ。過去の波の把握用で、直近は確定しない（後から変わる）。"),
    ]),
    ("テクニカル（オシレーター系）", [
        ("CCI / ウィリアムズ%R / ROC / モメンタム", "いずれも「勢い」や「行きすぎ」を測る指標。RSI・ストキャスと役割が重なるため、当システムでは二重加点を避けて採用せず。"),
        ("RCI（順位相関指数）", "日付と価格の順位相関。−80以下で売られすぎ。日本の個人投資家に人気。"),
        ("アルティメット / デマーカー / TRIX / CMO / AO", "各種の派生オシレーター。基本はRSI・MACD・ストキャスで足りる。"),
        ("サイコロジカルライン", "直近12日のうち上げた日の割合。25%以下は売られすぎ。"),
    ]),
    ("出来高・ボラティリティ", [
        ("VWAP", "出来高加重平均価格。その日の「平均的な約定値段」。日中トレード向けで、夜1回判断では出番が少ない。"),
        ("VR / CMF / A/Dライン", "出来高で買い圧力・売り圧力を測る指標群。OBV・MFIと同系統。"),
        ("価格帯別出来高", "どの値段で最も多く売買されたか。支持帯・抵抗帯の裏付けになる。当システムの「長期支持帯」の考え方に近い。"),
    ]),
    ("市場全体・需給・センチメント", [
        ("空売り比率 / 信用倍率 / 投資部門別売買", "需給の本丸。信用買い残の積み上がりは「将来の売り圧力」。J-Quants Standard以上で取得可（未導入）。"),
        ("日経VI / VIX / プット・コール・レシオ", "恐怖指数。急騰時は投げ売り一巡の目安になることも。"),
        ("マクレラン・オシレーター / ヒンデンブルグ・オーメン", "騰落データから作る市場全体のシグナル。後者は暴落の前兆として有名だが的中率は議論あり。"),
    ]),
    ("クオンツ・成績評価", [
        ("β / α / R²", "市場との連動性・超過収益・説明力。ポートフォリオ全体の性格を測る指標。"),
        ("シャープ / ソルティノ / インフォメーション・レシオ", "取ったリスクに対してどれだけ稼いだか。IFDOCOシミュレーションの成績を「リスク調整後」で見る次の段階で導入予定。"),
        ("最大ドローダウン / VaR / 標準偏差", "最悪期の落ち込み・想定損失・ばらつき。IFDOCOシミュレーションに最大ドローダウンを追加するのが次の候補。"),
    ]),
]


def _ind_card(ind, cls="ind"):
    cases = "".join(
        f'<div class="case"><span class="cv" style="background:{col}">{v}</span><span>{html.escape(t)}</span></div>'
        for v, col, t in ind["cases"])
    return f"""
<details class="{cls}" id="{ind["key"]}">
  <summary><span class="itag">{html.escape(ind["tag"])}</span><b>{html.escape(ind["name"])}</b><span class="chev">›</span></summary>
  <div class="ibody">
    <div class="one">{html.escape(ind["one"])}</div>
    <div class="kid">🧒 {html.escape(ind["kid"])}</div>
    {f'<div class="meter">{ind["meter"]}</div>' if ind.get("meter") else ""}
    <div class="ihead">具体例：この数字ならこう見る</div>
    {cases}
    <div class="ihead">落とし穴</div>
    <div class="trap">{html.escape(ind["trap"])}</div>
    <div class="ihead">このシステムでの扱い</div>
    <div class="sc">{html.escape(ind["score"])}</div>
  </div>
</details>"""


def render_indicators(dt):
    cards = []
    for ind in INDICATORS:
        cards.append(_ind_card(ind))
    sec_cards = [_ind_card(ind, "ind sec") for ind in SECONDARY_INDICATORS]
    gl_parts = []
    for cat, terms in GLOSSARY:
        rows_g = "".join(f'<div class="gterm"><b>{html.escape(n)}</b><span>{html.escape(d)}</span></div>' for n, d in terms)
        gl_parts.append(f'<details class="gcat"><summary>{html.escape(cat)}<span class="chev">›</span></summary><div class="gbody">{rows_g}</div></details>')
    glossary = ('<details class="tier3"><summary><b>参考: 用語集（その他の指標 約40）</b>'
                '<span class="chev">›</span></summary><div class="tier3body">'
                '<div class="note">採点には使っていないが、投資家として知っておくと判断が深まる指標。'
                '「なぜ採用していないか」も添えています。</div>' + "".join(gl_parts) + '</div></details>')

    legend = ('<div class="card"><h2>色の意味（全指標共通）</h2><div class="lg">'
              f'<span style="background:{G}">安心・標準（迷わず見られる）</span>'
              f'<span style="background:{B}">魅力あり・要確認（割安の罠などを確認）</span>'
              f'<span style="background:{Y}">注意・様子見</span>'
              f'<span style="background:{R}">警戒</span>'
              f'<span style="background:{N}">中立</span></div>'
              '<div class="note">価値系（PER・PBR・ROE）は「標準」が最も安心な位置で、「割安」は魅力だが理由の確認が要る位置。'
              '行きすぎ系（RSI・ボリンジャー等）は売られすぎ側が押し目買いに有利。▲は具体例の位置。'
              '各指標をタップすると、子供向けのたとえ・メーター・数値別の見方・落とし穴・採点での扱いが開きます。</div></div>')

    extra_css = f"""
  .lg{{display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px;}}
  .lg span{{font-size:11px; font-weight:700; border-radius:6px; padding:4px 9px;}}
  details.ind{{background:#fff; border-radius:14px; margin-bottom:10px; box-shadow:0 1px 3px rgba(0,0,0,.05);}}
  details.ind summary{{list-style:none; cursor:pointer; display:flex; align-items:center; gap:8px;
    padding:13px 14px; font-size:14px;}}
  details.ind summary::-webkit-details-marker{{display:none;}}
  .itag{{flex:none; font-size:9.5px; font-weight:800; color:#4a3f28; background:#f4eedd; border-radius:5px; padding:2px 6px;}}
  .chev{{margin-left:auto; color:#c9bd9d; font-size:16px; font-weight:700; transition:transform .15s;}}
  details[open] .chev{{transform:rotate(90deg);}}
  .ibody{{padding:0 14px 14px; border-top:1px solid #f0ead9;}}
  .one{{font-size:13px; line-height:1.8; padding:10px 0 4px; font-weight:700;}}
  .kid{{font-size:12.5px; line-height:1.8; color:#3a5a40; background:#eef6ef; border-radius:10px; padding:9px 12px; margin:6px 0 10px;}}
  .meter{{margin:6px 0 4px;}}
  .ihead{{font-size:10.5px; font-weight:800; color:#7a6a45; letter-spacing:.06em; margin:12px 0 6px;}}
  .case{{display:flex; gap:8px; align-items:flex-start; font-size:12px; line-height:1.7; padding:5px 0; border-bottom:1px dashed #f0ead9;}}
  .cv{{flex:none; min-width:64px; text-align:center; font-weight:800; border-radius:6px; padding:2px 6px; font-size:11.5px;}}
  .trap{{font-size:12px; line-height:1.8; color:#8a5a17; background:#fdf6e6; border-radius:10px; padding:8px 12px;}}
  .sc{{font-size:12px; line-height:1.7; color:var(--ink2);}}
  .tierh{{font-size:12px; font-weight:800; color:#4a3f28; letter-spacing:.06em; margin:16px 0 8px; padding-left:4px;
    border-left:4px solid #3a5a40;}}
  .tierh.sec{{border-left-color:#a99a76; color:#7a6a45;}}
  details.ind.sec summary{{font-size:13px;}} details.ind.sec .itag{{background:#f0f0f4; color:#6e6e73;}}
  details.tier3{{background:#fff; border-radius:14px; margin-top:16px; box-shadow:0 1px 3px rgba(0,0,0,.05);}}
  details.tier3 > summary{{list-style:none; cursor:pointer; display:flex; align-items:center; padding:13px 14px; font-size:13px; color:#4a3f28;}}
  details.tier3 > summary::-webkit-details-marker{{display:none;}}
  .tier3body{{padding:0 10px 10px;}}
  details.gcat{{background:#faf6ec; border-radius:10px; margin:6px 0;}}
  details.gcat summary{{list-style:none; cursor:pointer; display:flex; align-items:center; padding:9px 12px; font-size:12.5px; font-weight:800; color:#7a6a45;}}
  details.gcat summary::-webkit-details-marker{{display:none;}}
  .gbody{{padding:0 12px 8px;}}
  .gterm{{padding:6px 0; border-top:1px dashed #e7e0cf; font-size:11.5px; line-height:1.7;}}
  .gterm b{{display:block; color:#1c1c1e; font-size:12px;}} .gterm span{{color:var(--ink2);}}
"""
    weekdays = "月火水木金土日"
    subtitle = f"数字を「読める」ようになるための図解 ・ タップで開閉 ・ {dt.month}/{dt.day}（{weekdays[dt.weekday()]}）時点の採点基準"
    footnote = "各指標の閾値はこのシステムの現在の設定に基づきます。相場環境や業種で最適値は変わるため、絶対的な基準ではなく「よく使われる目安」としてご覧ください。"
    return (SUBPAGE_TEMPLATE
            .replace("__NAVCSS__", NAV_CSS)
            .replace("__HEADBTN__", "")
            .replace("__NAVJS__", NAV_JS)
            .replace("__NAV__", nav_html("indicators"))
            .replace("__TITLE__", "指標の読み方 — 数字を判断に変える図解")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", legend
                     + '<div class="tierh">主要指標（採点の中心・{}）</div>'.format(len(INDICATORS)) + "".join(cards)
                     + '<div class="tierh sec">準主要指標（補助的に加点・{}）</div>'.format(len(SECONDARY_INDICATORS)) + "".join(sec_cards)
                     + glossary)
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", ""))


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
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1, user-scalable=no">
<meta name="robots" content="noindex, nofollow">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="apple-touch-icon" href="icon.png">
<link rel="icon" type="image/png" href="icon.png">
<style>html,body{touch-action:pan-x pan-y;}</style>
<script>
document.addEventListener('gesturestart',function(e){e.preventDefault();});
document.addEventListener('gesturechange',function(e){e.preventDefault();});
</script>
<title>Kabuobaa</title>
<style>
  *{box-sizing:border-box; margin:0; padding:0;}
  html, body{overflow-x:hidden; max-width:100%;}
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


def render_sim(payload, dt):
    """IFDOCOシミュレーションページ（紙テイスト・sim.jsonを読む）"""
    sm = payload.get("summary", {})
    body = r"""
<div class="card" style="border-left:5px solid #2e4d7b;">
  <h2>このシミュレーションのルール（毎晩自動で再計算）</h2>
  <div class="gt"><b>毎営業日、「今夜の厳選」1位の銘柄をIFDOCO注文で機械的に売買したら？</b>を過去1年ぶん実行した結果です。</div>
  <div class="simrule">① 買い: 1位銘柄をその日の終値×(1−0.05%)の指値で翌営業日に発注。寄り付きが指値以下なら寄りで約定、当日安値が届かなければ<b>不成立→破棄</b>（記録は残す）</div>
  <div class="simrule">② 売り(OCO): 買値<b>+10%の指値</b> と 買値<b>−5%の逆指値成行</b>。窓開けで設定値を飛び越えた日は<b>寄り付き価格で約定</b>（現実の注文挙動を再現）。同日に両方へ届いた場合は保守的に損切り優先</div>
  <div class="simrule">③ <b>同じ銘柄を保有中は重ね買いしない</b>（1位が連日同じ銘柄でもスキップ・記録は残す）</div>
  <div class="simrule">④ どちらにも届かないまま残った株は<b>塩漬け株</b>として保有し続け、最新終値で評価。資金は無制限・100株ずつ</div>
  <div class="gt" style="margin-top:6px;"><span class="ssrc">復元</span> = 過去の1位を価格由来の要素（安さ・下げ止まり・トレンド・RSI・流動性）で復元した区間（過去時点の財務は取得不能のため質スコアは現在値で固定）。
  <span class="ssrc live">実測</span> = システムが毎晩実際に選んだ1位。日が経つほど実測の比率が上がり、検証の信頼度が上がります。<br>
  <b>いま「復元」ばかりなのは異常ではありません。</b>実測の蓄積はこの機能を追加した日から始まったばかりで、過去1年の大半はどうしても復元になります。
  実測期間が始まる前の区間は今後もずっと「復元」のまま残り、これから毎晩1日ずつ「実測」が増えていきます（1年後には全区間が実測になります）。</div>
</div>

<div class="card">
  <h2>成績サマリー（過去1年）</h2>
  <div id="simstats" class="sgrid"></div>
  <div class="note" id="simnote"></div>
</div>

<div class="card" style="border-left:5px solid #6b4487;">
  <h2>並走シミュレーション（本線は凍結・影で検証中）</h2>
  <div class="gt">同じ日々の選定・同じ値動きに対して、ルール違いの変種を裏で同時に走らせています。
  <b>本線のルールは一切変えていません</b>。実測データが貯まってから（決済30回未満は参考扱い）、勝ち続けた変種だけを採用候補にします。</div>
  <div id="varstable"></div>
  <canvas id="vcv" style="width:100%; display:block; background:#fffdf6; border-radius:10px; margin-top:8px;"></canvas>
  <div class="note" id="varsnote">線=各ルールの累積確定損益。</div>
</div>

<div class="card">
  <h2>累積損益カーブ と 投下資金</h2>
  <canvas id="scv" style="width:100%; display:block; background:#fffdf6; border-radius:10px;"></canvas>
  <div class="note">緑/赤の線=確定損益の累積。うすい茶色の面=その日に市場へ投じていた資金（保有ポジションの取得額合計）。</div>
</div>

<div class="card">
  <h2>取引の時系列図（1行=1取引・横線が保有期間）</h2>
  <canvas id="gcv" style="width:100%; display:block; background:#fffdf6; border-radius:10px;"></canvas>
  <div class="glg"><span><i style="background:#2e7d32"></i>利確で終了</span>
  <span><i style="background:#c62f2f"></i>損切りで終了</span>
  <span><i style="background:#b06a00"></i>保有中（塩漬け）</span></div>
  <div class="note">下から古い順。線の左端=買った日、右端=売れた日（保有中は右端まで伸び続けます）。線が長い=資金が拘束されていた期間です。</div>
</div>

<div class="card">
  <h2>塩漬け株（未決済・含み損益）</h2>
  <div id="simopen"></div>
</div>

<div class="card">
  <h2 style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px;">全取引の記録（新しい順）
    <span class="rsort"><button class="sfbtn on" data-f="all">すべて</button><button class="sfbtn" data-f="tp">利確</button><button class="sfbtn" data-f="sl">損切</button><button class="sfbtn" data-f="open">保有中</button><button class="sfbtn" data-f="no">不成立/スキップ</button></span></h2>
  <div id="simrec"></div>
  <div class="rmore-row"><button id="smore" class="rmorebtn">さらに表示</button></div>
</div>
"""
    extra_css = """
  .gt{font-size:12.5px; line-height:1.85;}
  .simrule{font-size:11.5px; line-height:1.8; color:var(--ink2); padding:5px 0; border-top:1px dashed #f0ead9;}
  .simrule b{color:#4a3f28;}
  .sgrid{display:grid; grid-template-columns:repeat(2,1fr); gap:8px;}
  .sg{background:#fffdf6; border-radius:10px; padding:9px 11px;}
  .sg .k{font-size:10px; color:var(--ink3); font-weight:700;}
  .sg .v{font-size:16px; font-weight:800;}
  .sg .v small{font-size:10.5px; font-weight:600; color:var(--ink2);}
  .sg .v.plus{color:#2e7d32;} .sg .v.minus{color:#c62f2f;}
  .rsort{display:flex; border:1.5px solid #d9d2bf; border-radius:8px; overflow:hidden;}
  .sfbtn{border:none; background:#fff; color:var(--ink2); font-size:10.5px; font-weight:800; padding:5px 9px; cursor:pointer;}
  .sfbtn.on{background:#1c1c1e; color:#fff;}
  .trow2{padding:7px 0; border-bottom:1px dashed #f0ead9; font-size:12px;}
  .tr1{display:flex; align-items:center; gap:7px;}
  .sev{flex:none; font-size:9.5px; font-weight:800; border-radius:4px; padding:2px 7px;}
  .sev.tp{background:#e9f3ea; color:#1a5c37;} .sev.sl{background:#fdeeee; color:#c62f2f;}
  .sev.open{background:#fdf3e3; color:#b06a00;} .sev.nofill{background:#f0f0f4; color:#6e6e73;}
  .sev.skip{background:#f0f0f4; color:#6e6e73;}
  .snm{flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:700;}
  .snm small{color:var(--ink3); font-weight:600;}
  .spnl{flex:none; font-weight:800; font-size:12px;}
  .spnl.plus{color:#2e7d32;} .spnl.minus{color:#c62f2f;}
  .ssrc{display:inline-block; font-size:9px; font-weight:700; color:#8a5a17; background:#fdf6e6; border-radius:4px; padding:1px 5px;}
  .ssrc.live{color:#1a5c37; background:#e9f3ea;}
  .tr2{font-size:10.5px; color:var(--ink2); padding:3px 0 0 2px; font-family:ui-monospace,Menlo,monospace;}
  .tr2 b{color:#1c1c1e;}
  .vrow{display:flex; align-items:center; gap:7px; padding:6px 0; border-bottom:1px dashed #f0ead9; font-size:11.5px;}
  .vdot{flex:none; width:10px; height:10px; border-radius:50%;}
  .vlab{flex:1; min-width:0; font-weight:700; line-height:1.5;}
  .vnum{flex:none; text-align:right; font-weight:800; font-size:12px;}
  .vnum small{display:block; font-weight:600; color:var(--ink3); font-size:9px;}
  .vnum.plus{color:#2e7d32;} .vnum.minus{color:#c62f2f;}
  .vsub{font-size:9.5px; color:var(--ink3); font-weight:600;}
  .glg{display:flex; gap:12px; flex-wrap:wrap; padding:8px 2px 0;}
  .glg span{display:flex; align-items:center; gap:5px; font-size:10.5px; color:var(--ink2); font-weight:700;}
  .glg i{width:14px; height:3px; display:inline-block; border-radius:2px;}
  .orow{display:flex; align-items:center; gap:8px; padding:6px 0; border-bottom:1px dashed #f0ead9; font-size:12px;}
  .onm{flex:1; min-width:0; font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
  .onm small{color:var(--ink3); font-weight:600;}
  .oinfo{flex:none; text-align:right; font-size:10.5px; color:var(--ink2);}
  .opnl{flex:none; width:84px; text-align:right; font-weight:800; font-size:12px;}
  .opnl.plus{color:#2e7d32;} .opnl.minus{color:#c62f2f;}
  .rmore-row{text-align:center; padding:10px 0 2px;}
  .rmorebtn{border:1.5px solid #d9d2bf; background:#fff; color:#2e4d7b; font-size:12px; font-weight:800;
    border-radius:10px; padding:8px 22px; cursor:pointer;}
"""
    script = r"""<script>
(function(){
'use strict';
var D=null, FILT='all', SHOWN=0, DIDX={};
function yen(v){ return (v<0?'−':'+')+Math.abs(Math.round(v)).toLocaleString()+'円'; }
function man(v){ return Math.round(v/10000).toLocaleString()+'万円'; }
function esc(t){return String(t).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function cls(v){ return v>=0?'plus':'minus'; }
function md(d){ return d? d.slice(2).replace(/-/g,'/') : ''; }
function stats(){
  var s=D.summary;
  var total=s.total_pnl+s.unrealized;
  document.getElementById('simstats').innerHTML=
    '<div class="sg"><div class="k">確定損益（1年合計）</div><div class="v '+cls(s.total_pnl)+'">'+yen(s.total_pnl)+'</div></div>'
    +'<div class="sg"><div class="k">含み損益（塩漬け'+s.open+'銘柄）</div><div class="v '+cls(s.unrealized)+'">'+yen(s.unrealized)+'</div></div>'
    +'<div class="sg"><div class="k">確定+含みの通算</div><div class="v '+cls(total)+'">'+yen(total)+'</div></div>'
    +'<div class="sg"><div class="k">勝率（決済済みのみ）</div><div class="v">'+(s.win_rate==null?'−':s.win_rate+'%')+'<small> 利確'+s.tp+'・損切'+s.sl+'</small></div></div>'
    +'<div class="sg"><div class="k">この戦略に必要だった資金</div><div class="v">'+man(s.max_invested)+'<small> 最大時（同時'+s.max_positions+'銘柄）</small></div></div>'
    +'<div class="sg"><div class="k">平均投下資金</div><div class="v">'+man(s.avg_invested)+'<small> / 営業日平均</small></div></div>'
    +'<div class="sg"><div class="k">売買成立</div><div class="v">'+s.fills+'<small>回 ／ 不成立'+s.nofills+'・保有中スキップ'+s.skips+'</small></div></div>'
    +'<div class="sg"><div class="k">実測1位でのデータ日数</div><div class="v">'+s.live_days+'<small> / '+s.days+'営業日（残りは復元）</small></div></div>';
  document.getElementById('simnote').textContent=
    '1回の売買は100株。「必要だった資金」は保有ポジションの取得額合計のピーク＝この戦略を回すのに実際に要した余力の実測値です。';
}
function curveDraw(){
  var cv=document.getElementById('scv'), ctx=cv.getContext('2d');
  var W=cv.clientWidth, H=Math.max(190,Math.round(W*0.44));
  var DPR=Math.min(2.5,window.devicePixelRatio||1);
  cv.style.height=H+'px'; cv.width=W*DPR; cv.height=H*DPR;
  ctx.setTransform(DPR,0,0,DPR,0,0);
  var c=D.curve;
  if(!c.length) return;
  var vals=c.map(function(p){return p[1];});
  var invs=c.map(function(p){return p[2]||0;});
  var lo=Math.min(0,Math.min.apply(null,vals)), hi=Math.max(0,Math.max.apply(null,vals));
  var ihi=Math.max.apply(null,invs)||1;
  if(hi-lo<1) hi=lo+1;
  var P={l:14,r:66,t:12,b:20};
  function X(i){ return P.l+i/(c.length-1)*(W-P.l-P.r); }
  function Y(v){ return H-P.b-(v-lo)/(hi-lo)*(H-P.t-P.b); }
  function Yi(v){ return H-P.b-v/ihi*(H-P.t-P.b)*0.85; }
  ctx.clearRect(0,0,W,H);
  /* 投下資金（面） */
  ctx.beginPath(); ctx.moveTo(X(0),H-P.b);
  for(var k=0;k<c.length;k++) ctx.lineTo(X(k),Yi(invs[k]));
  ctx.lineTo(X(c.length-1),H-P.b); ctx.closePath();
  ctx.fillStyle='rgba(169,154,118,.18)'; ctx.fill();
  /* 0ライン */
  ctx.strokeStyle='#d9d2bf'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(P.l,Y(0)); ctx.lineTo(W-P.r,Y(0)); ctx.stroke();
  ctx.font='10px ui-monospace,Menlo,monospace'; ctx.fillStyle='#a99a76';
  ctx.fillText('±0', W-P.r+4, Y(0)+3);
  ctx.fillText(Math.round(hi/10000)+'万円', W-P.r+4, Y(hi)+8);
  ctx.fillText(Math.round(lo/10000)+'万円', W-P.r+4, Y(lo)-2);
  ctx.fillText('資金'+Math.round(ihi/10000)+'万円', W-P.r+4, Yi(ihi)+10);
  var last=vals[vals.length-1];
  var col=last>=0?'#2e7d32':'#c62f2f';
  ctx.beginPath();
  for(var j=0;j<c.length;j++){ if(j===0) ctx.moveTo(X(j),Y(vals[j])); else ctx.lineTo(X(j),Y(vals[j])); }
  ctx.strokeStyle=col; ctx.lineWidth=2; ctx.stroke();
  ctx.fillStyle='#a99a76';
  ctx.fillText(md(c[0][0]), P.l, H-6);
  ctx.textAlign='right';
  ctx.fillText(md(c[c.length-1][0])+' ('+yen(last)+')', W-P.r, H-6);
  ctx.textAlign='left';
}
function ganttDraw(){
  var cv=document.getElementById('gcv'), ctx=cv.getContext('2d');
  var rows=D.trades.filter(function(t){return t.ev==='tp'||t.ev==='sl'||t.ev==='open';});
  var W=cv.clientWidth;
  var rh=Math.max(2.2,Math.min(5,420/Math.max(1,rows.length)));
  var H=Math.max(200,Math.min(560,Math.round(rows.length*rh)+46));
  var DPR=Math.min(2.5,window.devicePixelRatio||1);
  cv.style.height=H+'px'; cv.width=W*DPR; cv.height=H*DPR;
  ctx.setTransform(DPR,0,0,DPR,0,0);
  ctx.clearRect(0,0,W,H);
  var dates=D.dates, N=dates.length;
  DIDX={}; for(var i=0;i<N;i++) DIDX[dates[i]]=i;
  var P={l:10,r:10,t:8,b:26};
  function X(di){ return P.l+di/(N-1)*(W-P.l-P.r); }
  /* 月の目盛 */
  ctx.font='9.5px ui-monospace,Menlo,monospace'; ctx.fillStyle='#a99a76';
  ctx.strokeStyle='#f0ead9'; ctx.lineWidth=1;
  var lastM='';
  for(var i2=0;i2<N;i2++){
    var m=dates[i2].slice(0,7);
    if(m!==lastM){
      lastM=m;
      var gx=X(i2);
      ctx.beginPath(); ctx.moveTo(gx,P.t); ctx.lineTo(gx,H-P.b); ctx.stroke();
      ctx.fillText(m.slice(2).replace('-','/'), gx+2, H-12);
    }
  }
  /* 取引の線分（下=古い） */
  var colmap={tp:'#2e7d32', sl:'#c62f2f', open:'#b06a00'};
  for(var r=0;r<rows.length;r++){
    var t=rows[r];
    var y=H-P.b-(r+0.5)*((H-P.t-P.b)/rows.length);
    var x1=X(DIDX[t.buy_date]||0);
    var x2=(t.ev==='open')? X(N-1) : X(DIDX[t.sell_date]||N-1);
    ctx.strokeStyle=colmap[t.ev]; ctx.lineWidth=Math.max(1.4,rh*0.55);
    ctx.globalAlpha=0.8;
    ctx.beginPath(); ctx.moveTo(x1,y); ctx.lineTo(Math.max(x2,x1+2),y); ctx.stroke();
    if(t.ev!=='open'){
      ctx.globalAlpha=1; ctx.fillStyle=colmap[t.ev];
      ctx.beginPath(); ctx.arc(Math.max(x2,x1+2),y,Math.max(1.6,rh*0.45),0,Math.PI*2); ctx.fill();
    }
  }
  ctx.globalAlpha=1;
}
function openList(){
  var el=document.getElementById('simopen');
  if(!D.positions.length){ el.innerHTML='<div class="note">塩漬け株はありません（全ポジション決済済み）</div>'; return; }
  el.innerHTML=D.positions.map(function(p){
    return '<div class="orow"><span class="onm">'+esc(p.name)+' <small>'+p.code+'</small></span>'
      +'<span class="oinfo">'+md(p.buy_date)+'買 '+p.buy.toLocaleString()+'円<br>'
      +'現在 '+p.last.toLocaleString()+'円 ・ '+p.held+'日目</span>'
      +'<span class="opnl '+cls(p.pnl)+'">'+yen(p.pnl)+'</span></div>';
  }).join('');
}
var EVL={tp:'利確', sl:'損切', open:'保有中', nofill:'不成立', skip:'スキップ'};
function recRows(){
  var all=D.trades.slice().reverse();
  return all.filter(function(t){
    if(FILT==='tp') return t.ev==='tp';
    if(FILT==='sl') return t.ev==='sl';
    if(FILT==='open') return t.ev==='open';
    if(FILT==='no') return t.ev==='nofill'||t.ev==='skip';
    return true;
  });
}
function renderRec(reset){
  var el=document.getElementById('simrec');
  if(reset){ el.innerHTML=''; SHOWN=0; }
  var rows=recRows();
  var end=Math.min(rows.length, SHOWN+100);
  var h='';
  for(var i=SHOWN;i<end;i++){
    var t=rows[i];
    var line2='';
    if(t.ev==='tp'||t.ev==='sl'){
      line2='<b>'+md(t.buy_date)+'</b> '+t.buy.toLocaleString()+'円で買付 → <b>'+md(t.sell_date)+'</b> '
        +t.sell.toLocaleString()+'円で売却 ・ 保有'+t.held+'営業日';
    } else if(t.ev==='open'){
      line2='<b>'+md(t.buy_date)+'</b> '+t.buy.toLocaleString()+'円で買付 → 保有中（現在 '+(t.last!=null?t.last.toLocaleString():'−')+'円・'+(t.held||0)+'日目）';
    } else if(t.ev==='nofill'){
      line2='<b>'+md(t.buy_date)+'</b> '+t.buy.toLocaleString()+'円の指値に届かず破棄';
    } else if(t.n>1){
      line2='<b>'+md(t.buy_date)+'〜'+md(t.to)+'</b> の'+t.n+'営業日連続で1位だが、同銘柄を保有中のため重ね買いせず（1行に圧縮表示）';
    } else {
      line2='<b>'+md(t.buy_date)+'</b> 同銘柄を保有中のため重ね買いせず';
    }
    h+='<div class="trow2"><div class="tr1">'
      +'<span class="sev '+t.ev+'">'+EVL[t.ev]+'</span>'
      +'<span class="snm">'+esc(t.name)+' <small>'+t.code+'</small></span>'
      +(t.pnl!=null?'<span class="spnl '+cls(t.pnl)+'">'+yen(t.pnl)+'</span>':'')
      +'<span class="ssrc'+(t.src==='live'?' live':'')+'">'+(t.src==='live'?'実測':'復元')+'</span>'
      +'</div><div class="tr2">'+line2+'</div></div>';
  }
  el.insertAdjacentHTML('beforeend',h);
  SHOWN=end;
  var btn=document.getElementById('smore');
  btn.textContent=(SHOWN>=rows.length)?'すべて表示済み（'+rows.length+'件）':'さらに表示（あと'+(rows.length-SHOWN)+'件）';
  btn.disabled=SHOWN>=rows.length;
}
document.getElementById('smore').addEventListener('click',function(){ renderRec(false); });
document.querySelectorAll('.sfbtn').forEach(function(b){
  b.addEventListener('click',function(){
    document.querySelectorAll('.sfbtn').forEach(function(x){x.classList.remove('on');});
    b.classList.add('on'); FILT=b.dataset.f; renderRec(true);
  });
});
function varsDraw(){
  if(!D.variants){ return; }
  var el=document.getElementById('varstable');
  el.innerHTML=D.variants.map(function(v){
    var s=v.summary;
    var tot=s.total_pnl+s.unrealized;
    if(v.na) return '<div class="vrow"><span class="vdot" style="background:'+v.color+'"></span>'
      +'<span class="vlab">'+esc(v.label)+'<div class="vsub">日経データ不足のため今回は本線と同一</div></span></div>';
    return '<div class="vrow"><span class="vdot" style="background:'+v.color+'"></span>'
      +'<span class="vlab">'+esc(v.label)
      +'<div class="vsub">勝率'+(s.win_rate==null?'−':s.win_rate+'%')+'（利確'+s.tp+'/損切'+s.sl+'）'
      +' ・ 塩漬け'+s.open+' ・ 最大資金'+man(s.max_invested)+' ・ 最大DD '+man(s.max_dd)+'</div></span>'
      +'<span class="vnum '+cls(tot)+'">'+yen(tot)+'<small>確定'+yen(s.total_pnl)+'・含み'+yen(s.unrealized)+'</small></span></div>';
  }).join('');
  var cv=document.getElementById('vcv'), ctx=cv.getContext('2d');
  var W=cv.clientWidth, H=Math.max(160,Math.round(W*0.36));
  var DPR=Math.min(2.5,window.devicePixelRatio||1);
  cv.style.height=H+'px'; cv.width=W*DPR; cv.height=H*DPR;
  ctx.setTransform(DPR,0,0,DPR,0,0);
  ctx.clearRect(0,0,W,H);
  var allv=[];
  D.variants.forEach(function(v){ v.curve.forEach(function(p){ allv.push(p[1]); }); });
  if(!allv.length) return;
  var lo=Math.min(0,Math.min.apply(null,allv)), hi=Math.max(0,Math.max.apply(null,allv));
  if(hi-lo<1) hi=lo+1;
  var P={l:10,r:56,t:8,b:8};
  function Y(v){ return H-P.b-(v-lo)/(hi-lo)*(H-P.t-P.b); }
  ctx.strokeStyle='#e8e1cf'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(P.l,Y(0)); ctx.lineTo(W-P.r,Y(0)); ctx.stroke();
  ctx.font='10px ui-monospace,Menlo,monospace'; ctx.fillStyle='#a99a76';
  ctx.fillText('±0', W-P.r+4, Y(0)+3);
  D.variants.forEach(function(v){
    var c=v.curve, n=c.length;
    if(n<2) return;
    ctx.beginPath();
    for(var i=0;i<n;i++){
      var x=P.l+i/(n-1)*(W-P.l-P.r);
      if(i===0) ctx.moveTo(x,Y(c[i][1])); else ctx.lineTo(x,Y(c[i][1]));
    }
    ctx.strokeStyle=v.color; ctx.lineWidth=v.key==='main'?2.2:1.4;
    ctx.globalAlpha=v.key==='main'?1:0.85;
    ctx.stroke();
  });
  ctx.globalAlpha=1;
}
window.addEventListener('resize',function(){ if(D){ curveDraw(); ganttDraw(); varsDraw(); } });
fetch('sim.json').then(function(r){
  if(!r.ok) throw new Error('sim.jsonがまだ生成されていません（次回の実行で作られます）');
  return r.json();
}).then(function(j){
  D=j; stats(); varsDraw(); curveDraw(); ganttDraw(); openList(); renderRec(true);
}).catch(function(e){
  document.getElementById('simstats').innerHTML='<div class="note">⚠ '+e.message+'</div>';
});
})();
</script>"""
    weekdays = "月火水木金土日"
    subtitle = (f"{dt.month}/{dt.day}（{weekdays[dt.weekday()]}）時点 ・ "
                f"「毎晩の1位をIFDOCOで機械売買したら」の1年検証 ・ "
                f"決済{sm.get('closed', 0)}回 / 塩漬け{sm.get('open', 0)}銘柄 / "
                f"最大投下資金 {sm.get('max_invested', 0) / 10000:,.0f}万円")
    footnote = ("約定はすべて仮定（買い: 前日終値×(1−0.05%)指値 ／ 利確: +10%指値 ／ 損切り: −5%逆指値成行。"
                "窓開け時は寄り付き価格で約定）。出来高・板の厚みは考慮していません。"
                "同日にTP/SL両方へ到達した場合は損切り優先の保守的計上。手数料・税金は含みません。投資判断はご自身で。")
    return (SUBPAGE_TEMPLATE
            .replace("__NAVCSS__", NAV_CSS)
            .replace("__HEADBTN__", "")
            .replace("__NAVJS__", NAV_JS)
            .replace("__NAV__", nav_html("sim"))
            .replace("__TITLE__", "IFDOCOシミュレーション — 1位を機械売買した成績")
            .replace("__SUBTITLE__", subtitle)
            .replace("__FOOTNOTE__", footnote)
            .replace("__BODY__", body)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", script))


# ------------------------------------------------------------
# IFDOCOシミュレーション:
#   毎晩の「今夜の厳選1位」を前日終値×(1-0.05%)の指値で翌営業日に買い、
#   +10%指値 / −5%逆指値成行(約定は設定値×(1-0.05%)) のOCOで売る。
#   過去1年は価格由来の要素で当時の1位を復元、当夜以降は実際の1位を蓄積。
# ------------------------------------------------------------
SIM_TP_PCT = 10.0
SIM_SL_PCT = 5.0
SIM_SLIP = 0.0005          # 約定時の不利方向 0.05%
SIM_SHARES = 100
SIM_STATE_PATH = DOCS / "history" / "simstate.json"


def _sim_reconstruct_picks(sim_ohlc, qmap):
    """各営業日の上位2銘柄を復元する {date: [code1, code2]}。
    価格由来の要素（安さ・下げ止まり・トレンド・RSI・流動性・危険な下げ方の除外）は
    その日時点で再計算し、質スコアは現在値で固定（過去の財務は取得不能のため）"""
    import pandas as pd
    import numpy as np
    cols_c, cols_h, cols_l, cols_v = {}, {}, {}, {}
    for code, tup in sim_ohlc.items():
        dates, _o, h, l, c, v = tup
        if len(dates) < 80:
            continue
        idx = pd.Index(dates)
        cols_c[code] = pd.Series(c, index=idx, dtype="float64")
        cols_h[code] = pd.Series(h, index=idx, dtype="float64")
        cols_l[code] = pd.Series(l, index=idx, dtype="float64")
        cols_v[code] = pd.Series(v, index=idx, dtype="float64")
    if len(cols_c) < 3:
        return {}
    df_c = pd.DataFrame(cols_c).sort_index()
    df_h = pd.DataFrame(cols_h).reindex(df_c.index)
    df_l = pd.DataFrame(cols_l).reindex(df_c.index)
    df_v = pd.DataFrame(cols_v).reindex(df_c.index)

    h20 = df_h.rolling(20, min_periods=15).max()
    drop = (h20 - df_c) / h20 * 100
    ma200 = df_c.rolling(200, min_periods=120).mean()
    ret = df_c.pct_change(fill_method=None)
    vol20 = ret.rolling(20, min_periods=15).std() * 100
    turnover20 = (df_c * df_v).rolling(20, min_periods=10).mean()
    knife10 = ret.rolling(10, min_periods=5).min() * 100
    runmax = df_c.cummax()
    dd = (runmax - df_c) / runmax
    runmin = df_c.cummin()
    rng_ = (runmax - runmin).replace(0, np.nan)
    pos1y = (df_c - runmin) / rng_
    stab = df_l >= df_l.shift(1)
    delta = df_c.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=10).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=10).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    t = (np.where(drop >= 5, 25.0, np.clip(drop * 3, 0, 15))
         + np.where(stab, 10.0, 0.0)
         + np.where(df_c.to_numpy() > ma200.to_numpy(), 8.0, 0.0)
         + np.where(rsi.to_numpy() < 40, 8.0, 0.0))
    elig = ((df_c >= 100) & (turnover20 >= 5e7) & (knife10 > -8)
            & (vol20 <= 4.5) & (dd <= 0.40) & (pos1y >= 0.12) & stab
            & (drop >= 3.0))
    qv = np.array([float(qmap.get(code, 0.0)) for code in df_c.columns])
    score = np.where(elig.to_numpy() & np.isfinite(t), t + qv[None, :], -1e9)
    order2 = np.argsort(-score, axis=1)[:, :2]
    picks = {}
    codes = list(df_c.columns)
    for i, d in enumerate(df_c.index):
        if i < 60:
            continue
        lst = [codes[j] for j in order2[i] if score[i, j] > -1e8]
        if lst:
            picks[str(d)] = lst
    return picks


def _exec_ifdoco(all_dates, bar_at, picks_of, namemap, sim_ohlc,
                 use_second=False, allow_day=None):
    """IFDOCO実行器（全変種で共通）。
    use_second: 1位を保有中なら2位を買う ／ allow_day: 新規買いを出してよい日の判定関数"""
    positions, trades, curve = [], [], []
    date_i = {d: i for i, d in enumerate(all_dates)}
    cum = 0.0
    tp_n = sl_n = fill_n = nofill_n = skip_n = gate_n = 0
    skip_runs = {}  # code -> (skip行, 最後にスキップした日のindex) 連続スキップの圧縮用
    max_inv = 0.0
    max_pos = 0
    inv_sum = 0.0
    peak = 0.0
    max_dd = 0.0
    pend = None
    for i, d in enumerate(all_dates):
        # 1) OCO判定（同日両到達は損切り優先・窓開けは寄り付き約定）
        still = []
        for p in positions:
            bar = bar_at(p["code"], d)
            if bar is None:
                still.append(p)
                continue
            op, hi, lo, _c = bar
            sl_line = p["buy"] * (1 - SIM_SL_PCT / 100)
            tp_line = p["buy"] * (1 + SIM_TP_PCT / 100)
            if lo <= sl_line:
                sell = min(op, sl_line) * (1 - SIM_SLIP)
                pnl = (sell - p["buy"]) * SIM_SHARES
                cum += pnl
                sl_n += 1
                p["trade"].update({"ev": "sl", "sell_date": d, "sell": round(sell, 1),
                                   "pnl": round(pnl), "held": max(1, i - p["i"])})
            elif hi >= tp_line:
                sell = max(op, tp_line)
                pnl = (sell - p["buy"]) * SIM_SHARES
                cum += pnl
                tp_n += 1
                p["trade"].update({"ev": "tp", "sell_date": d, "sell": round(sell, 1),
                                   "pnl": round(pnl), "held": max(1, i - p["i"])})
            else:
                still.append(p)
        positions = still
        # 2) 買付（保有中の銘柄は重ね買いしない。use_secondなら2位に切替）
        if pend is not None:
            held = {p["code"] for p in positions}
            cand = None
            for c0 in pend["cands"]:
                if c0 not in held:
                    cand = c0
                    break
                if not use_second:
                    break
            if cand is None:
                skip_n += 1
                c0 = pend["cands"][0]
                # 連続する同一銘柄のスキップは1行に圧縮（buy_date=開始日, to=最終日, n=日数）
                run = skip_runs.get(c0)
                if run is not None and run[1] == i - 1:
                    run[0]["to"] = d
                    run[0]["n"] = run[0].get("n", 1) + 1
                    skip_runs[c0] = (run[0], i)
                else:
                    t_skip = {"code": c0, "name": namemap.get(c0, c0), "src": pend["src"],
                              "buy_date": d, "buy": round(pend["prices"].get(c0, 0), 1),
                              "ev": "skip", "n": 1}
                    trades.append(t_skip)
                    skip_runs[c0] = (t_skip, i)
            else:
                price = pend["prices"].get(cand)
                bar = bar_at(cand, d)
                if price and bar is not None and (bar[0] <= price or bar[2] <= price):
                    fill = min(price, bar[0])
                    trade = {"code": cand, "name": namemap.get(cand, cand), "src": pend["src"],
                             "buy_date": d, "buy": round(fill, 1), "ev": "open",
                             "alt": cand != pend["cands"][0]}
                    trades.append(trade)
                    positions.append({"code": cand, "buy": fill, "date": d, "i": i,
                                      "src": pend["src"], "trade": trade})
                    fill_n += 1
                else:
                    nofill_n += 1
                    trades.append({"code": cand, "name": namemap.get(cand, cand), "src": pend["src"],
                                   "buy_date": d, "buy": round(price or 0, 1), "ev": "nofill"})
            pend = None
        # 3) 今夜の選定 → 翌営業日の注文（地合いフィルタはここで判定）
        pk = picks_of(d)
        if pk:
            cands, src_ = pk
            if allow_day is not None and not allow_day(d):
                gate_n += 1
            else:
                prices = {}
                for c1 in cands:
                    bar = bar_at(c1, d)
                    if bar is not None:
                        prices[c1] = bar[3] * (1 - SIM_SLIP)
                cands = [c1 for c1 in cands if c1 in prices]
                if cands:
                    pend = {"cands": cands, "prices": prices, "src": src_}
        inv = sum(p["buy"] for p in positions) * SIM_SHARES
        max_inv = max(max_inv, inv)
        max_pos = max(max_pos, len(positions))
        inv_sum += inv
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        curve.append([d, round(cum), round(inv)])

    unreal = 0.0
    pos_out = []
    for p in positions:
        last_c = sim_ohlc[p["code"]][4][-1]
        upnl = (last_c - p["buy"]) * SIM_SHARES
        unreal += upnl
        p["trade"]["last"] = round(last_c, 1)
        p["trade"]["pnl"] = round(upnl)
        p["trade"]["held"] = max(1, len(all_dates) - 1 - date_i.get(p["date"], 0))
        pos_out.append({"code": p["code"], "name": namemap.get(p["code"], p["code"]),
                        "buy": round(p["buy"], 1), "buy_date": p["date"],
                        "last": round(last_c, 1), "shares": SIM_SHARES,
                        "pnl": round(upnl), "held": p["trade"]["held"], "src": p["src"]})
    pos_out.sort(key=lambda x: x["pnl"])
    closed = tp_n + sl_n
    summary = {
        "total_pnl": round(cum), "unrealized": round(unreal),
        "tp": tp_n, "sl": sl_n, "closed": closed,
        "win_rate": round(tp_n / closed * 100, 1) if closed else None,
        "fills": fill_n, "nofills": nofill_n, "skips": skip_n, "gated": gate_n,
        "open": len(pos_out), "days": len(all_dates),
        "max_invested": round(max_inv),
        "avg_invested": round(inv_sum / max(1, len(all_dates))),
        "max_positions": max_pos,
        "max_dd": round(max_dd),
    }
    return {"summary": summary, "curve": curve, "trades": trades, "positions": pos_out}


def run_simulation(picked, detail_map, sim_ohlc, dt, demo=False, nikkei_days=None):
    """本線＋並走変種（2位買い・地合いフィルタ）を同一データで実行し docs/sim.json を書く。
    本線ルールは凍結（変種は影の検証のみ）"""
    SIM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(SIM_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        state = {"live_picks": {}}
    live_picks = state.get("live_picks") or {}
    # 旧形式（date→code文字列）→ 新形式（date→[1位,2位]）へ移行
    live_picks = {d: (v if isinstance(v, list) else [v]) for d, v in live_picks.items()}

    # 今夜の実際の1位・2位を蓄積（確定記帳のみ。デモは常に記録）
    if picked and (demo or is_final_run(dt)):
        top_date = picked[0].get("date")
        if top_date:
            live_picks[str(top_date)] = [s["code"] for s in picked[:2]]
    all_dates = sorted({d for tup in sim_ohlc.values() for d in tup[0]})
    if all_dates:
        live_picks = {d: c for d, c in live_picks.items() if d >= all_dates[0]}
    state["live_picks"] = live_picks

    qmap = {}
    namemap = {}
    for code, e in detail_map.items():
        namemap[code] = e.get("name", code)
        if e.get("q_score") is not None:
            qmap[code] = e["q_score"]
        elif e.get("score") is not None:
            qmap[code] = e["score"] * 0.5
    recon = _sim_reconstruct_picks(sim_ohlc, qmap)
    n_live = 0

    def picks_of(d):
        nonlocal n_live
        if d in live_picks:
            lst = [c for c in live_picks[d] if c in sim_ohlc]
            if lst:
                n_live += 1
                return lst, "live"
        if d in recon:
            return recon[d], "restored"
        return None

    bars = {}
    for code, tup in sim_ohlc.items():
        dates, o, h, l, c, _v = tup
        bars[code] = ({dd: i for i, dd in enumerate(dates)}, o, h, l, c)

    def bar_at(code, d):
        b = bars.get(code)
        if b is None:
            return None
        i = b[0].get(d)
        if i is None:
            return None
        return (b[1][i], b[2][i], b[3][i], b[4][i])

    # 地合い（日経の200日線）: 日付→線の上かどうか
    above200 = {}
    if nikkei_days and len(nikkei_days) >= 210:
        ncl = [c for _d, c in nikkei_days]
        ndt = [_d for _d, c in nikkei_days]
        csum = [0.0]
        for c in ncl:
            csum.append(csum[-1] + c)
        for i in range(len(ncl)):
            if i >= 199:
                ma = (csum[i + 1] - csum[i - 199]) / 200
                above200[ndt[i]] = ncl[i] > ma

    def allow_mkt(d):
        return above200.get(d, True)   # データが無い日は許可（保守的に本線と同じ挙動）

    # ── 本線＋並走変種を同一データで実行 ──
    main = _exec_ifdoco(all_dates, bar_at, picks_of, namemap, sim_ohlc)
    v2nd = _exec_ifdoco(all_dates, bar_at, picks_of, namemap, sim_ohlc, use_second=True)
    vmkt = _exec_ifdoco(all_dates, bar_at, picks_of, namemap, sim_ohlc,
                        allow_day=(allow_mkt if above200 else None))

    payload = {
        "generated_at": datetime.now(JST).isoformat(),
        "rules": {"tp": SIM_TP_PCT, "sl": SIM_SL_PCT, "slip": SIM_SLIP * 100, "shares": SIM_SHARES},
        "summary": {**main["summary"], "live_days": n_live // 3 if n_live else 0},
        "dates": all_dates,
        "curve": main["curve"][-260:],
        "positions": main["positions"],
        "trades": main["trades"][-500:],
        "variants": [
            {"key": "main", "label": "本線（現行ルール・凍結）", "color": "#2e7d32",
             "summary": main["summary"], "curve": [[p[0], p[1]] for p in main["curve"][-260:]]},
            {"key": "v2nd", "label": "変種A: 1位を保有中なら2位を買う", "color": "#2e5fa8",
             "summary": v2nd["summary"], "curve": [[p[0], p[1]] for p in v2nd["curve"][-260:]]},
            {"key": "vmkt", "label": "変種B: 日経200日線割れの日は新規買い停止", "color": "#8a5a17",
             "summary": vmkt["summary"], "curve": [[p[0], p[1]] for p in vmkt["curve"][-260:]],
             "na": not above200},
        ],
    }
    (DOCS / "sim.json").write_text(json.dumps(payload, ensure_ascii=False,
                                              separators=(",", ":")), encoding="utf-8")
    try:
        SIM_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ms = main["summary"]
    print(f"  シミュレーション: {len(all_dates)}営業日 / 本線: 成立{ms['fills']}・利確{ms['tp']}・損切{ms['sl']}"
          f"・塩漬け{ms['open']}・損益{ms['total_pnl']:+,}円 / "
          f"変種A {v2nd['summary']['total_pnl']:+,}円 / 変種B {vmkt['summary']['total_pnl']:+,}円 / "
          f"実測日 {len(live_picks)}")
    return payload


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
        picked, stats, all_results, extras = make_demo_data()
    else:
        picked, stats, all_results, extras = run_screening()

    data = build_output(picked, stats)
    data["market"] = extras.get("market")
    if args.demo:
        # デモ用の疑似順位履歴（3営業日分）
        import random as _r
        rr = _r.Random(3)
        codes = [s["code"] for s in data["stocks"]]
        hist = {"days": []}
        for k, d in enumerate(["2026-08-08", "2026-08-11", "2026-08-12"]):
            shuffled = codes[:]; rr.shuffle(shuffled)
            hist["days"].append({"date": d, "ranks": {c: i + 1 for i, c in enumerate(shuffled[:20])}, "names": {}})
        RANK_HIST_PATH.parent.mkdir(exist_ok=True)
        RANK_HIST_PATH.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    attach_rank_moves(data, datetime.fromisoformat(data["generated_at"]))
    ex_all = []
    for code, e in (extras.get("detail_map") or {}).items():
        for it in (e.get("exec_change") or []):
            ex_all.append({**it, "code": code, "company": it.get("company") or e.get("name", "")})
    ex_all.sort(key=lambda x: x["date"], reverse=True)
    data["exec_all"] = ex_all
    # 注目開示トピックス（直近3営業日ぶんをトップのバナーに出す）
    tp_all = []
    recent_days = sorted({it["date"]
                          for e in (extras.get("detail_map") or {}).values()
                          for it in (e.get("topics") or [])}, reverse=True)[:3]
    for code, e in (extras.get("detail_map") or {}).items():
        for it in (e.get("topics") or []):
            if it["date"] in recent_days:
                tp_all.append({**it, "code": code, "company": it.get("company") or e.get("name", "")})
    tp_all.sort(key=lambda x: x["date"], reverse=True)
    data["topics_all"] = tp_all

    # TOB素地スコア（全銘柄に計算 → 全体順位付け）
    detail_map_all = extras.get("detail_map") or {}
    for code, e in detail_map_all.items():
        e["tob"], e["tob_hits"] = tob_score(e)
        e["tob_announced"] = any(t.get("cat") == "tob" for t in (e.get("topics") or []))
    tob_ranked = sorted([e for e in detail_map_all.values() if e.get("tob") is not None],
                        key=lambda x: -x["tob"])
    for i, e in enumerate(tob_ranked, 1):
        e["tob_rank"] = i
    for r in all_results:
        e = detail_map_all.get(r["code"])
        if e is not None and e.get("tob") is not None:
            r["tob"] = e["tob"]
            r["tob_announced"] = e.get("tob_announced", False)

    # 関連銘柄マップ（高次元ベクトル化 → 3D埋め込み → 類似度グラフ）
    try:
        map_n = build_stock_map(detail_map_all, extras.get("map_series"))
    except Exception as _map_ex:  # noqa: BLE001
        print(f"  関連銘柄マップの生成に失敗（他のページは継続）: {_map_ex}")
        map_n = 0
    data["soon"] = [{
        "code": s["code"], "name": s["name"], "market": s.get("market", ""),
        "close": round(s["close"], 1), "cost": round(s["close"] * 100),
        "drop_pct": round(s["drop_pct"], 2), "to_cheap_pct": s.get("to_cheap_pct"),
        "trigger_price": s.get("trigger_price"), "q_score": s.get("q_score"),
        "demerit": s.get("demerit"), "suffix": s.get("suffix", ".T"),
    } for s in (extras.get("soon") or [])]

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
    # 旧サブシステム（手法の検証レポート・持ち株管理）は廃止: 公開側の残骸も消す
    for _old in ("backtest.html", "holdings.html", "prices.json"):
        try:
            (DOCS / _old).unlink()
        except FileNotFoundError:
            pass
    (DOCS / "guide.html").write_text(render_guide(dt_now), encoding="utf-8")
    (DOCS / "indicators.html").write_text(render_indicators(dt_now), encoding="utf-8")
    (DOCS / "tob.html").write_text(
        render_tob(tob_ranked, len(detail_map_all), dt_now), encoding="utf-8")
    (DOCS / "map.html").write_text(render_map(map_n, dt_now), encoding="utf-8")
    # 時価総額マップ用データ（株価と時価総額から株式数を復元して描画）
    caps_rows = []
    for e in detail_map_all.values():
        fu_c = e.get("fund") or {}
        if fu_c.get("mcap_oku") and e.get("close"):
            try:
                caps_rows.append([e["code"], e["name"],
                                  SECTOR_GROUPS.get(e.get("sector", ""), DEFAULT_GROUP),
                                  e.get("market") or "",
                                  round(float(e["close"]), 1), round(float(fu_c["mcap_oku"]), 1)])
            except (TypeError, ValueError):
                continue
    caps_rows.sort(key=lambda r: -r[5])
    (DOCS / "caps.json").write_text(json.dumps(
        {"generated_at": datetime.now(JST).isoformat(), "stocks": caps_rows},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DOCS / "caps.html").write_text(render_caps(len(caps_rows), dt_now), encoding="utf-8")
    # 無傷ランキングは全銘柄一覧（絞り込み「無傷」・ソート「安全順」）に統合したため単独ページは廃止
    write_details(extras.get("detail_map") or {})

    # 選定履歴（1行/実行の軽量ログ。公開ブランチ上で引き継がれる）
    hdir = DOCS / "history"
    hdir.mkdir(exist_ok=True)
    with open(hdir / "picks.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "at": data["generated_at"][:16],
            "picked": [s["code"] for s in data["stocks"][:CONFIG["TOP_N"]]],
            "cutoff": stats.get("cutoff_score"),
        }, ensure_ascii=False) + "\n")

    # 順位履歴（日次・確定記帳のみ）: 候補全体の順位を営業日ごとに保持（直近10営業日）
    save_rank_history(data, dt_now)

    # IFDOCOシミュレーション（過去1年の復元＋毎晩の実測ウォッチ）
    try:
        sim_summary_out = run_simulation(picked, detail_map_all,
                                         extras.get("sim_ohlc") or {}, dt_now,
                                         demo=args.demo,
                                         nikkei_days=extras.get("nikkei_days") or [])
        (DOCS / "sim.html").write_text(render_sim(sim_summary_out, dt_now), encoding="utf-8")
    except Exception as _sim_ex:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  シミュレーション生成に失敗（他のページは継続）: {_sim_ex}")

    print(f"完了: {len(data['stocks'])}銘柄を選定 "
          f"(除外 {stats.get('dead_excluded', 0)}銘柄) → docs/index.html"
          f" + universe.html + sim.html ほか")


if __name__ == "__main__":
    main()
