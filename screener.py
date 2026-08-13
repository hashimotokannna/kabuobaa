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
    "SHORTLIST_N": 60,       # 候補として用意する数（持ち金・PER等の基準で外れる分の予備を含む）
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
def fetch_daily(session, code, suffix=".T"):
    """日足1年分を [{date, open, high, low, close, volume}] (古い順) で返す"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}"
    params = {"range": "10y", "interval": "1d"}
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
    """厳選の根拠スコア。(点数, 根拠の文リスト) を返す"""
    reasons = []
    score = 0.0

    # 1. いま安いか（主基準だが、深すぎる下げは加点を頭打ちに）
    capped = min(s["drop_pct"], 8.0)
    pt = capped * 8
    score += pt
    reasons.append(f"直近20日高値から −{s['drop_pct']:.1f}%（安さの主基準 +{pt:.0f}点・8%で頭打ち）")
    if s.get("usual"):
        vs = (s["usual"] - s["close"]) / s["usual"] * 100
        if vs > 0:
            pt = min(vs * 4, 20)
            score += pt
            reasons.append(f"普段の値段（20日平均）より {vs:.1f}%安い（+{pt:.0f}点）")

    # 2. 下げの「質」: じわ下げ か 崩落か
    conc = s.get("concentration", 0)
    if conc < 0.45:
        score += 15
        reasons.append("下げが複数日に分散した「じわ下げ」で、1日の急落で作られた安さではない（+15点）")
    elif conc < 0.7:
        score += 5
        reasons.append("下げはやや急だが崩落型ではない（+5点）")
    else:
        reasons.append("下げの大半が特定の1日に集中（急落型・加点なし）")
    vol = s.get("vol20", 0)
    if 0 < vol <= 2.5:
        score += 10
        reasons.append(f"日々の値動きが±{vol:.1f}%と穏やかで読みやすい（+10点）")

    # 3. トレンドの地合い: 上昇トレンド中の押し目が理想形
    if s.get("ma200_above"):
        score += 20
        reasons.append("200日平均線より上での下げ＝上昇トレンド中の一時的な押し目（+20点）")
    dd = s["drawdown_1y"] * 100
    if dd <= 15:
        score += 12
        reasons.append(f"1年高値からの下落 −{dd:.0f}% で長期トレンド健全（+12点）")
    elif dd <= 25:
        score += 6
        reasons.append(f"1年高値からの下落 −{dd:.0f}%（+6点）")
    pos = s.get("pos1y")
    if pos is not None and 0.25 <= pos <= 0.65:
        score += 10
        reasons.append(f"1年の値幅の中腹（下から{pos*100:.0f}%）での下げ。底抜けでも高値掴みでもない位置（+10点）")

    # 4. この銘柄で手法が効いてきた実績（安く買って+TP_PCT%で売る、の再現性）
    t = s.get("sim") or {}
    wins = t.get("wins", 0)
    tp = CONFIG["TP_PCT"]
    if wins > 0:
        base_pt = min(wins, 10) * 5
        score += base_pt
        reasons.append(f"過去1年、同じ買い方で {wins}回 利確ライン（+{tp:.0f}%）に到達（+{base_pt:.0f}点）")
        ah = t.get("avg_held")
        if ah is not None:
            if ah <= 7:
                score += 15
                reasons.append(f"利確まで平均 {ah:.0f}営業日。短期回転スタイルに最適（+15点）")
            elif ah <= 15:
                score += 8
                reasons.append(f"利確まで平均 {ah:.0f}営業日（+8点）")
            elif ah > 25:
                score -= 5
                reasons.append(f"利確まで平均 {ah:.0f}営業日と資金拘束が長め。"
                               f"短期回転スタイルでは機会損失（−5点）")
    else:
        reasons.append("過去1年はこの買い方の成立実績なし（加点なし）")
    if t.get("open_loss"):
        score -= 15
        reasons.append("ただし直近の仮想買いが塩漬け中（−15点）")

    # 5. 長期テクニカル（10年データからの定石）
    lg = s.get("long") or {}
    z = lg.get("zone")
    if z:
        if z["touches"] <= 2 and z["dist_pct"] >= -1:
            pt = 12
            score += pt
            reasons.append(f"長期支持帯 {z['zone_low']:,.0f}〜{z['zone_top']:,.0f}円の直上。"
                           f"過去{z['touches']}回反発し、今回が{z['touches'] + 1}回目の試し"
                           f"（〜3回目までは支持されやすいという定石の圏内 +{pt}点）")
        else:
            score -= 5
            reasons.append(f"長期支持帯 {z['zone_top']:,.0f}円付近は過去{z['touches']}回試されており、"
                           f"今回で{z['touches'] + 1}回目。支持線は試されるほど割れやすい（−5点・警戒）")
    if lg.get("gc") is True:
        score += 10
        reasons.append("50日線が200日線の上（ゴールデンクロス継続中の長期上昇形 +10点）")
    elif lg.get("gc") is False:
        reasons.append("50日線が200日線の下（長期は調整形・加点なし）")
    rsi = lg.get("rsi")
    if rsi is not None:
        if rsi <= 30:
            score += 10
            reasons.append(f"RSI(14)={rsi:.0f} の売られすぎ水準（+10点）")
        elif rsi <= 40:
            score += 5
            reasons.append(f"RSI(14)={rsi:.0f} でやや売られすぎ（+5点）")
    if lg.get("w_bottom"):
        score += 8
        reasons.append(f"W底（ダブルボトム）を形成しネックライン{lg['w_bottom']['neck']:,.0f}円を上抜け（+8点）")
    if lg.get("climax"):
        score += 6
        reasons.append("直近に出来高急増+長い下ヒゲ（セリングクライマックス=投げ売り一巡の兆候 +6点）")

    # 5.3 補助テクニカル指標（MACD・ボリンジャー・移動平均乖離）
    ms = s.get("macd_state")
    if ms == "golden_recent":
        score += 8
        reasons.append("MACDが直近5日以内に買い転換（下げの勢いが尽きた定番シグナル +8点）")
    elif ms == "above":
        score += 4
        reasons.append("MACDがシグナル線の上で上向き（+4点）")
    bs = s.get("boll_sigma")
    if bs is not None:
        if bs <= -2:
            score += 8
            reasons.append(f"ボリンジャーバンド−2σ以下（統計的な売られすぎ圏 +8点）")
        elif bs <= -1.5:
            score += 4
            reasons.append(f"ボリンジャーバンド−{abs(bs):.1f}σと下限付近（+4点）")
    dv = s.get("dev25")
    if dv is not None:
        if -20 < dv <= -8:
            score += 6
            reasons.append(f"25日移動平均線から{dv:.1f}%下方乖離（逆張りの定番圏 +6点）")
        elif dv <= -20:
            score -= 5
            reasons.append(f"25日線から{dv:.1f}%と乖離しすぎ（異常事態の可能性 −5点）")

    # 5.4 ファンダメンタルの健全性（システムが固定基準で自動判定）
    fu = s.get("fund")
    if fu:
        per = fu.get("per")
        pbr = fu.get("pbr")
        roe = fu.get("roe")
        dy = fu.get("div_yield")
        mcap = fu.get("mcap_oku")
        if per is None or per < 0:
            score -= 20
            reasons.append("赤字の可能性（PER算出不能）。業績不振の銘柄は下げても戻りが鈍い（−20点）")
        elif per <= 20:
            score += 8
            reasons.append(f"PER {per:.1f}倍と利益に対して妥当〜割安の水準（+8点）")
        elif per > 60:
            score -= 10
            reasons.append(f"PER {per:.1f}倍と利益に対して異常な割高。期待剥落時の下げが深い（−10点）")
        if pbr is not None:
            if 0.5 <= pbr <= 1.5:
                score += 6
                reasons.append(f"PBR {pbr:.2f}倍と資産に対して割安圏。下値が固くなりやすい（+6点）")
            elif pbr > 8:
                score -= 8
                reasons.append(f"PBR {pbr:.2f}倍と資産比で過熱気味（−8点）")
        if roe is not None and per is not None and per > 0:
            if roe >= 10:
                score += 8
                reasons.append(f"ROE {roe:.1f}%と資本効率が高く、稼ぐ力のある会社（+8点）")
            elif roe < 3:
                score -= 5
                reasons.append(f"ROE {roe:.1f}%と収益力が弱い（−5点）")
        if dy is not None and dy >= 3:
            score += 5
            reasons.append(f"配当利回り{dy:.1f}%。配当が下値を支えやすい（+5点）")
        if mcap:
            if mcap < 50:
                score -= 5
                reasons.append(f"時価総額{mcap:,}億円と小型で、値が飛びやすい（−5点）")
            elif mcap >= 1000:
                score += 3
                reasons.append(f"時価総額{mcap:,}億円の中大型で値動きが安定しやすい（+3点）")
    else:
        reasons.append("ファンダ指標が本日取得できず中立扱い（加減点なし）")

    # 5.5 夜1回の判断スタイルとの相性（翌朝の窓開けの小ささ）
    ga = s.get("gap_avg")
    if ga is not None:
        if ga <= 1.0:
            score += 8
            reasons.append(f"夜間ギャップ（翌朝の窓開け）平均±{ga:.1f}%と小さく、"
                           f"夜に決めた指値が翌朝も有効に働きやすい（+8点）")
        elif ga >= 2.5:
            score -= 5
            reasons.append(f"夜間ギャップ平均±{ga:.1f}%と大きく、夜の判断が翌朝ズレやすい（−5点）")

    # 6. 売り買いのしやすさ
    tv = s.get("turnover", 0)
    if tv >= 1_000_000_000:
        score += 12
        reasons.append(f"1日平均 {tv/100_000_000:.0f}億円の売買があり注文が通りやすい（+12点）")
    elif tv >= 100_000_000:
        score += 6
        reasons.append(f"1日平均 {tv/100_000_000:.1f}億円の売買（+6点）")

    return score, reasons


# ------------------------------------------------------------
# 長期テクニカル分析（10年データから計算する定石の技法）
#  - 長期支持帯とタッチ回数（支持線は試されるほど割れやすい=4回目警戒）
#  - ゴールデンクロス状態（50日/200日移動平均）
#  - RSI(14) の売られすぎ
#  - W底（ダブルボトム）形成
#  - セリングクライマックス兆候（出来高急増+長い下ヒゲ）
# ------------------------------------------------------------
def compute_long_metrics(days_full):
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

    # 週足スパークライン用（3年・5日おき）
    closes3 = closes[-n3:]
    out["spark"] = [[dates3[i], round(closes3[i], 1)] for i in range(0, n3, 5)]
    return out


def measure_factor_lift(days_full, days1y, factor_stats):
    """この銘柄の過去1年の全買いシグナルについて、シグナル時点の要因と
    その後の結果（40営業日以内に+TP_PCT%到達=勝ち）を集計に加算する"""
    n = CONFIG["RECENT_DAYS"]
    if len(days1y) < n + 10:
        return
    closes_f = [d["close"] for d in days_full]
    offset = len(days_full) - len(days1y)
    # 50/200日移動平均（累積和で高速化）
    prefix = [0.0]
    for c in closes_f:
        prefix.append(prefix[-1] + c)

    def sma(fi, w):
        if fi + 1 < w:
            return None
        return (prefix[fi + 1] - prefix[fi + 1 - w]) / w

    # RSI(14) を全期間ぶん前計算
    rsi_arr = [None] * len(closes_f)
    gains = losses = 0.0
    for i in range(1, len(closes_f)):
        ch = closes_f[i] - closes_f[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
        if i > 14:
            ch_old = closes_f[i - 14] - closes_f[i - 15]
            gains -= max(ch_old, 0)
            losses -= max(-ch_old, 0)
        if i >= 14:
            rsi_arr[i] = 100 - 100 / (1 + gains / losses) if losses > 0 else 100.0

    opens = [d["open"] for d in days1y]
    highs = [d["high"] for d in days1y]
    closes = [d["close"] for d in days1y]
    tp = 1 + CONFIG["TP_PCT"] / 100
    for i in range(n, len(days1y) - 2):
        h20 = max(highs[i - n + 1:i + 1])
        c = closes[i]
        if h20 <= 0 or c < CONFIG["MIN_PRICE"]:
            continue
        if (h20 - c) / h20 * 100 < CONFIG["CHEAP_PCT"]:
            continue
        buy = opens[i + 1]
        horizon = highs[i + 1:i + 1 + 40]
        win = bool(horizon) and max(horizon) >= buy * tp

        fi = offset + i
        s50, s200 = sma(fi, 50), sma(fi, 200)
        facts = {}
        if s50 is not None and s200 is not None:
            facts["gc"] = s50 > s200
        if rsi_arr[fi] is not None:
            facts["rsi"] = rsi_arr[fi] <= 40
        # じわ下げ: 直近10日に1日-4%超の急落がない
        worst = min((closes[j] - closes[j - 1]) / closes[j - 1] * 100
                    for j in range(max(1, i - 9), i + 1))
        facts["gradual"] = worst > -4.0

        for key, val in facts.items():
            bucket = factor_stats[key]["with" if val else "without"]
            bucket[0] += 1
            if win:
                bucket[1] += 1


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



# 検証レポートで比較する持ち金の段階
SIM_BUDGETS = [300_000, 500_000, 1_000_000, 2_000_000, None]  # None=無制限


def simulate_portfolio(sim_universe):
    """資金制約付きで「◎買い→+5000円売り」を1年なぞる。持ち金の段階別に成績を返す"""
    n = CONFIG["RECENT_DAYS"]
    events_by_date = {}
    stock_info = {}
    for s in sim_universe:
        dates, opens, highs, closes = s["dates"], s["opens"], s["highs"], s["closes"]
        if len(dates) < n + 5:
            continue
        stock_info[s["code"]] = {
            "highmap": dict(zip(dates, highs)),
            "openmap": dict(zip(dates, opens)),
            "last_close": closes[-1],
            "dates": dates,
        }
        for i in range(n, len(dates) - 1):
            h20 = max(highs[i - n + 1:i + 1])
            c = closes[i]
            if h20 > 0 and c >= CONFIG["MIN_PRICE"]:
                drop = (h20 - c) / h20 * 100
                if drop >= CONFIG["CHEAP_PCT"]:
                    events_by_date.setdefault(dates[i + 1], []).append((drop, s["code"]))

    calendar = sorted({d for info in stock_info.values() for d in info["dates"]})
    results = []
    for budget in SIM_BUDGETS:
        cash = float(budget) if budget else 0.0
        unlimited = budget is None
        positions = {}
        realized = 0.0
        wins = skipped = max_pos = 0
        for d in calendar:
            for code in list(positions):
                info = stock_info[code]
                h = info["highmap"].get(d)
                target = positions[code]["buy"] * (1 + CONFIG["TP_PCT"] / 100)
                if h is not None and h >= target:
                    profit = positions[code]["cost"] * CONFIG["TP_PCT"] / 100
                    realized += profit
                    wins += 1
                    if not unlimited:
                        cash += positions[code]["cost"] + profit
                    del positions[code]
            for drop, code in sorted(events_by_date.get(d, []), reverse=True):
                if code in positions:
                    continue
                op = stock_info[code]["openmap"].get(d)
                if op is None:
                    continue
                cost = op * 100.0
                if not unlimited and cost > cash:
                    skipped += 1
                    continue
                if not unlimited:
                    cash -= cost
                positions[code] = {"buy": op, "cost": cost}
            max_pos = max(max_pos, len(positions))
        unrealized = sum((stock_info[c]["last_close"] - p["buy"]) * 100.0
                         for c, p in positions.items())
        results.append({
            "budget": budget, "realized": realized, "wins": wins,
            "skipped": skipped, "open_count": len(positions),
            "unrealized": unrealized, "max_positions": max_pos,
        })
    return results


# ------------------------------------------------------------
# TDnet適時開示（東証公式の一次情報）の見出し取得
# 候補銘柄だけを対象に、TDnet配信API経由で直近の開示タイトルを取る
# ------------------------------------------------------------
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


def fetch_fundamentals(session, codes):
    """PER/PBR/時価総額/配当利回りをまとめて取得。取得不可なら空dict（表示側で非表示）"""
    out = {}
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    for i in range(0, len(codes), 50):
        batch = codes[i:i + 50]
        symbols = ",".join(f"{c}.T" for c in batch)
        try:
            resp = session.get(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                params={"symbols": symbols,
                        "fields": "trailingPE,priceToBook,marketCap,"
                                  "trailingAnnualDividendYield,epsTrailingTwelveMonths"},
                headers=headers, timeout=20)
            if resp.status_code != 200:
                continue
            for q in (resp.json().get("quoteResponse", {}).get("result") or []):
                code = (q.get("symbol") or "").replace(".T", "")
                per = q.get("trailingPE")
                pbr = q.get("priceToBook")
                mcap = q.get("marketCap")
                dy = q.get("trailingAnnualDividendYield")
                entry = {}
                if per is not None:
                    entry["per"] = round(per, 1)
                if pbr is not None:
                    entry["pbr"] = round(pbr, 2)
                if mcap:
                    entry["mcap_oku"] = round(mcap / 100_000_000)
                if dy is not None:
                    entry["div_yield"] = round(dy * 100, 2)
                if per is not None and pbr is not None and per > 0 and pbr > 0:
                    entry["roe"] = round(pbr / per * 100, 1)
                if entry:
                    out[code] = entry
        except Exception:  # noqa: BLE001
            continue
        time.sleep(0.5)
    return out


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
    all_results, sim_records, sim_universe = [], [], []
    slagg = {v: {"sl": v, "tp_count": 0, "sl_count": 0, "realized": 0.0,
                 "open": 0, "open_pnl": 0.0} for v in SL_VARIANTS}
    factor_stats = {k: {"with": [0, 0], "without": [0, 0]}
                    for k in ("gc", "rsi", "gradual")}
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
            long_m = compute_long_metrics(days_full)
            detail_map[stock["code"]] = {
                **stock, **m, "status": status, "reason": reason,
                "days": days[-10:], "long": long_m,
            }
            if status == "dead":
                dead_count += 1
                all_results.append({**base, "status": "dead", "reason": reason})
                continue
            if status == "skip":
                skip_count += 1
                all_results.append({**base, "status": "skip", "reason": reason})
                continue
            measure_factor_lift(days_full, days, factor_stats)
            var_trades = simulate_variants(days)
            trades = var_trades[None]
            candidates.append({**stock, **m, "days": days[-10:],
                               "long": long_m,
                               "sim": sim_summary(trades)})
            all_results.append({**base, "status": "ok", "reason": ""})
            if trades:
                sim_records.append({"code": stock["code"],
                                    "name": stock["name"], "trades": trades})
            for v, tl in var_trades.items():
                agg = slagg[v]
                for t in tl:
                    if t["sell_date"] is None:
                        agg["open"] += 1
                        agg["open_pnl"] += t["pnl"]
                    elif t.get("stop"):
                        agg["sl_count"] += 1
                        agg["realized"] += t["pnl"]
                    else:
                        agg["tp_count"] += 1
                        agg["realized"] += t["pnl"]
            sim_universe.append({
                "code": stock["code"], "name": stock["name"],
                "dates": [d["date"] for d in days],
                "opens": [d["open"] for d in days],
                "highs": [d["high"] for d in days],
                "closes": [d["close"] for d in days],
            })

    # ファンダメンタルはスコアの判断材料になるため、採点前に取得する
    import requests as _rq
    td_session = _rq.Session()
    fundamentals = fetch_fundamentals(td_session, list(detail_map.keys()))
    fund_ok = len(fundamentals) > 0
    for c in candidates:
        c["fund"] = fundamentals.get(c["code"])
    for code, e in detail_map.items():
        e["fund"] = fundamentals.get(code)

    for c in candidates:
        c["score"], c["reasons"] = score_stock(c)
    candidates.sort(key=lambda s: s["score"], reverse=True)
    picked = candidates[:CONFIG["SHORTLIST_N"]]
    picked_codes = {s["code"] for s in picked}
    score_map = {c["code"]: c["score"] for c in candidates}
    rank_map = {s["code"]: i + 1 for i, s in enumerate(picked)}
    for r in all_results:
        if r["status"] == "ok":
            r["status"] = "picked" if r["code"] in picked_codes else "bench"
            r["score"] = round(score_map.get(r["code"], 0), 1)
            if r["status"] == "picked":
                r["cand_rank"] = rank_map.get(r["code"])

    stats = {
        "universe": len(universe),
        "dead_excluded": dead_count,
        "skipped": skip_count,
        "failed": fail_count,
        "cutoff_score": round(picked[-1]["score"], 1) if picked else 0,
    }
    print("資金別シミュレーションを計算中...")
    portfolio = simulate_portfolio(sim_universe)
    slstats = [slagg[v] for v in SL_VARIANTS]

    # 地合い（日経平均の200日線と直近20日の変化）
    market = None
    mkt_days = fetch_daily(_get_session(), "^N225", "")
    if mkt_days and len(mkt_days) >= 210:
        mc = [d["close"] for d in mkt_days]
        ma200 = sum(mc[-200:]) / 200
        market = {"above200": mc[-1] > ma200,
                  "chg20": round((mc[-1] / mc[-21] - 1) * 100, 1)}

    print("TDnet適時開示を取得中...")
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
    extras = {"factor_stats": factor_stats, "market": market,
              "fund_available": fund_ok, "detail_map": detail_map}
    return picked, stats, all_results, sim_records, portfolio, slstats, extras


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
            "rsi": round(rng.uniform(22, 55), 1),
            "gc": rng.random() < 0.6,
            "zone": {"zone_low": round(zone_low, 1),
                     "zone_top": round(zone_low * 1.05, 1),
                     "touches": rng.randint(2, 5),
                     "touch_dates": [spark[i][0] for i in rng.sample(range(20, 140), 3)],
                     "dist_pct": round(rng.uniform(-1, 6), 1)},
            "spark": spark,
        }
        if rng.random() < 0.25:
            s["long"]["w_bottom"] = {"neck": round(close * 1.02, 1)}
        if rng.random() < 0.2:
            s["long"]["climax"] = {"date": "2026-08-08"}
        _per = round(rng.uniform(6, 35), 1)
        _pbr = round(rng.uniform(0.5, 4.0), 2)
        s["fund"] = {"per": _per, "pbr": _pbr,
                     "roe": round(_pbr / _per * 100, 1),
                     "mcap_oku": rng.randint(80, 40000),
                     "div_yield": round(rng.uniform(0, 4.5), 2)}
        s["vol_ratio"] = rng.uniform(0.5, 3.5)
        s["cheap_streak"] = rng.randint(0, 6)
        s["prev_change"] = rng.uniform(-4, 2)
        s["gap_avg"] = rng.uniform(0.3, 3.0)
        s["macd_state"] = rng.choice(["golden_recent", "above", "below"])
        s["boll_sigma"] = rng.uniform(-2.5, 1.0)
        s["dev25"] = rng.uniform(-15, 3)
        s["score"], s["reasons"] = score_stock(s)
    picked.sort(key=lambda s: s["score"], reverse=True)
    stats = {"universe": 3912, "dead_excluded": 214, "skipped": 1480, "failed": 3,
             "cutoff_score": round(picked[-1]["score"], 1) if picked else 0}

    all_results = []
    for i, s in enumerate(picked):
        all_results.append({"code": s["code"], "name": s["name"],
                            "market": s["market"], "sector": s["sector"],
                            "close": round(s["close"], 1),
                            "drop_pct": round(s["drop_pct"], 2),
                            "score": round(s["score"], 1), "cand_rank": i + 1,
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
    sim_records = []
    for s in picked[:10]:
        sim_records.append({"code": s["code"], "name": s["name"], "trades": [
            {"buy_date": "2026-05-11", "sell_date": "2026-05-18", "held": 6, "pnl": 5000.0},
            {"buy_date": "2026-06-02", "sell_date": "2026-07-13", "held": 28, "pnl": 5000.0},
            {"buy_date": "2026-07-30", "sell_date": None, "held": 9,
             "pnl": rng.uniform(-15000, 4000)},
        ]})
    portfolio = [
        {"budget": 300_000, "realized": 65000, "wins": 13, "skipped": 172,
         "open_count": 1, "unrealized": -8000, "max_positions": 2},
        {"budget": 500_000, "realized": 110000, "wins": 22, "skipped": 121,
         "open_count": 2, "unrealized": -21000, "max_positions": 3},
        {"budget": 1_000_000, "realized": 195000, "wins": 39, "skipped": 60,
         "open_count": 4, "unrealized": -47000, "max_positions": 6},
        {"budget": 2_000_000, "realized": 300000, "wins": 60, "skipped": 18,
         "open_count": 7, "unrealized": -92000, "max_positions": 11},
        {"budget": None, "realized": 390000, "wins": 78, "skipped": 0,
         "open_count": 12, "unrealized": -160000, "max_positions": 19},
    ]
    slstats = [
        {"sl": None, "tp_count": 610, "sl_count": 0, "realized": 4200000,
         "open": 180, "open_pnl": -2900000},
        {"sl": 3.0, "tp_count": 420, "sl_count": 380, "realized": 1150000,
         "open": 40, "open_pnl": -260000},
        {"sl": 5.0, "tp_count": 480, "sl_count": 260, "realized": 1900000,
         "open": 60, "open_pnl": -420000},
        {"sl": 8.0, "tp_count": 540, "sl_count": 150, "realized": 2600000,
         "open": 85, "open_pnl": -700000},
        {"sl": 10.0, "tp_count": 560, "sl_count": 110, "realized": 2900000,
         "open": 100, "open_pnl": -1050000},
        {"sl": 15.0, "tp_count": 585, "sl_count": 55, "realized": 3400000,
         "open": 140, "open_pnl": -2000000},
    ]
    detail_map = {}
    for s in picked:
        detail_map[s["code"]] = {**s, "status": "picked",
                                 "reason": "", "cand_rank": 1}
    detail_map["6800"] = {"code": "6800", "name": "デモ右肩下がり", "market": "スタンダード",
                          "sector": "電気機器", "suffix": ".T", "status": "dead",
                          "reason": "1年高値から55%下落", "days": [], "long": {}}
    extras = {
        "detail_map": detail_map,
        "factor_stats": {
            "gc": {"with": [420, 260], "without": [380, 170]},
            "rsi": {"with": [310, 195], "without": [490, 235]},
            "gradual": {"with": [520, 320], "without": [280, 110]},
        },
        "market": {"above200": True, "chg20": 2.4},
        "fund_available": True,
    }
    return picked, stats, all_results, sim_records, portfolio, slstats, extras


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
            "disclosures": s.get("disclosures", []),
            "fund": s.get("fund"),
            "long": {k: v for k, v in (s.get("long") or {}).items() if k != "spark"},
            "spark": (s.get("long") or {}).get("spark", []),
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
                    "DEAD_DRAWDOWN", "DEAD_BELOW_MA_RATIO",
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
    mkt = data.get("market")
    if mkt:
        if mkt["above200"]:
            market_banner = (f'<div class="mkt ok">地合い: 上昇基調（日経平均が200日線の上・'
                             f'直近20日 {mkt["chg20"]:+.1f}%）</div>')
        else:
            market_banner = (f'<div class="mkt ng">地合い警戒: 日経平均が200日線の下（直近20日 '
                             f'{mkt["chg20"]:+.1f}%）。全体が下落基調の間は、押し目買いの成功率が'
                             f'下がります。買いは普段より慎重に</div>')
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
                fund_html = ('<div class="nhead">ファンダメンタル指標</div>' + "".join(frows)
                             + '<div class="discnote">読み方は「使い方」ページ参照。低PER・低PBRには'
                               '業績悪化を織り込んだ「割安の罠」もあるため、単独では判断しないこと。</div>')
        tech_html = ""
        svg = spark_svg(s.get("spark"), s.get("long"))
        if svg:
            z = (s.get("long") or {}).get("zone")
            zline = (f'赤い帯=長期支持帯 {z["zone_low"]:,.0f}〜{z["zone_top"]:,.0f}円'
                     f'（▲=過去の反発地点 ・ ●=いま）' if z else "●=いま（3年週足）")
            tech_html = (f'<div class="nhead">3年の値動きと支持帯</div>'
                         f'<div class="spark">{svg}</div>'
                         f'<div class="discnote">{zline}</div>')
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
          <div class="n1">{html.escape(s["name"])} <span class="chip {chip}">{html.escape(s["market"])}</span>{new_mark}</div>
          <div class="n2 num"><button class="codebtn" onclick="copyCode(this, '{s["code"]}', event)">{s["code"]} ⧉</button> ・ {html.escape(s["group"])} ・ 100株 {s["cost"] / 10000:,.1f}万円 ・ {s["score"]:.0f}点<span class="nofund">資金不足</span></div>
          {f'<div class="cmt">{html.escape(s["comment"])}</div>' if s.get("comment") else ""}
        </div>
        <div class="px">
          <div class="p1 num"><small>{price_label}</small> {yen(s["close"])}<small>円</small></div>
          <div class="p2 num drop">高値から −{s["drop_pct"]:.1f}%</div>
        </div>
        <div>{badge}</div>
        <button class="fav" data-code="{s["code"]}" aria-label="お気に入り">★</button>
        <div class="chev">›</div>
      </summary>
      <div class="notebox">
        <div class="nhead">選ばれた根拠（スコア {s["score"]:.0f}点）</div>
        {reasons_html}
        {tech_html}
        {fund_html}
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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="robots" content="noindex, nofollow">
<link rel="apple-touch-icon" href="icon.png">
<link rel="icon" type="image/png" href="icon.png">
<title>Kabuobaa - 今夜の厳選{cfg["TOP_N"]}銘柄</title>
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
  .linkrow{{display:flex; gap:8px; margin-top:12px;}}
  .ylink{{flex:1; display:block; font-size:12px; font-weight:700; color:#2e4d7b;
    text-decoration:none; text-align:center; background:#eef2f8; border-radius:9px;
    padding:9px; border:none; cursor:pointer;}}
  .ylink.sbi{{color:#1a5c37; background:#e9f3ea;}}
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
</style>
</head>
<body>
<header>
  <div class="t">今夜の厳選<span id="showncnt">{cfg["TOP_N"]}</span>銘柄</div>
  <div class="s">{date_str} {dt.hour:02d}:{dt.minute:02d} 記帳{"（取引時間中・当日分は途中経過）" if is_intraday else ""} ・ 根拠スコア順 ・ タップで根拠とノート ・ 判断はご自身で</div>
</header>
__NAV__
{market_banner}
<details class="crit">
  <summary>この厳選{cfg["TOP_N"]}銘柄の選定基準（タップで開閉）<span class="chev">›</span></summary>
  <div class="critbody">
    <div class="step"><b>1. 対象</b> 東証プライム・スタンダード・グロースの全銘柄（{universe:,}銘柄）</div>
    <div class="step"><b>2. 土俵に上げない</b> 上場から日足{cfg["MIN_RECORDS"]}日未満 ／ 株価{cfg["MIN_PRICE"]}円未満 ／ 直近{cfg["RECENT_DAYS"]}日の平均売買代金{int(cfg["MIN_TURNOVER"]/10000):,}万円未満（売りたい時に売れない銘柄を避ける）</div>
    <div class="step"><b>3. 危ない下げ方を除外</b> ①1年高値から{int(cfg["DEAD_DRAWDOWN"]*100)}%以上下落・長期の下落トレンド継続（終わった株） ②直近10日に1日{cfg["KNIFE_DROP_1D"]:.0f}%超の急落（決算ミス等の材料落ち=落ちるナイフ） ③日々の値動きが±{cfg["MAX_VOL20"]:.1f}%超の荒い銘柄 ④1年安値圏を更新中 ⑤下げ止まり未確認（前日から安値切り下げ中）——本日計{excluded:,}銘柄を除外</div>
    <div class="step"><b>4. 根拠スコアで採点</b> 残った銘柄を「いまの安さ」「下げの質（じわ下げか急落か・値動きの穏やかさ）」「トレンドの地合い（200日線の上の押し目か・1年レンジ内の位置）」「過去1年でこの買い方が利確+{cfg["TP_PCT"]:.0f}%を取れた実績」「10年データの長期テクニカル（支持帯の反発実績と試行回数・ゴールデンクロス・RSI・MACD・ボリンジャーバンド・25日線乖離・W底・セリクラ兆候）」「売買のしやすさ」の6観点で採点し、上位{cfg["SHORTLIST_N"]}銘柄を候補に</div>
    <div class="step"><b>5. 厳選{cfg["TOP_N"]}銘柄</b> 候補のうち、持ち金設定があれば「100株買える銘柄」だけを対象に、スコア上位{cfg["TOP_N"]}銘柄を表示。各銘柄の点数の内訳はタップで確認できます</div>
    <div class="step"><b>6. 目安ラベル</b> ◎=高値から{cfg["CHEAP_PCT"]:.0f}%以上安い ／ ○={cfg["MILD_PCT"]:.0f}%以上安い ／ 「普段の値段」={cfg["RECENT_DAYS"]}日の終値平均</div>
    <div class="step"><b>7. 財務の健全性（自動判定）</b> PER（20倍以下+・60倍超−）、PBR（0.5〜1.5倍+・8倍超−）、ROE（10%以上+・3%未満−）、赤字の疑い（−20点）、配当利回り3%以上（+）、時価総額（小型−・中大型+）を固定基準で採点。各銘柄の根拠に点数付きで明示されます</div>
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
<footer>
  対象 {universe:,}銘柄 ／ 右肩下がり・急落直後・荒い値動き等で除外 {excluded:,}銘柄<br>
  ◎=直近{data["config"]["RECENT_DAYS"]}日高値から{data["config"]["CHEAP_PCT"]:.0f}%以上安い ・ ○={data["config"]["MILD_PCT"]:.0f}%以上安い<br>
  データ: Yahoo Finance ・ このページは判断材料の表示のみ
</footer>
__CAPJS__
</body>
</html>
""".replace("__CAPJS__", CAP_JS.replace("__TOPN__", str(cfg["TOP_N"])) + NAV_JS) \
       .replace("__NAVCSS__", NAV_CSS) \
       .replace("__NAV__", nav_html("index"))


# 全ページ共通のナビゲーション
NAV_CSS = """
  .topnav{display:flex; gap:6px; overflow-x:auto; padding:2px 0 12px;
    -webkit-overflow-scrolling:touch;}
  .topnav a{flex:none; font-size:12.5px; font-weight:700; color:#4a3f28;
    text-decoration:none; background:#f4eedd; border-radius:10px; padding:8px 14px;}
  .topnav a.act{background:#1c1c1e; color:#fff;}
"""

NAV_ITEMS = [
    ("index.html", "帳簿", "index"),
    ("holdings.html", "持ち株", "holdings"),
    ("universe.html", "全銘柄", "universe"),
    ("backtest.html", "検証", "backtest"),
    ("guide.html", "使い方", "guide"),
]


NAV_JS = """<script>
(function(){
  const order = ['index.html', 'holdings.html', 'universe.html', 'backtest.html', 'guide.html'];
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
    for href, label, key in NAV_ITEMS:
        cls = ' class="act"' if key == active else ""
        parts.append(f'<a href="{href}"{cls}>{label}</a>')
    return '<div class="topnav">' + "".join(parts) + "</div>"


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
<header><div class="t">__TITLE__</div><div class="s">__SUBTITLE__</div></header>
__BODY__
<div class="note">__FOOTNOTE__</div>
__SCRIPT__
__NAVJS__
</body>
</html>
"""


def render_backtest(sim_records, dt, portfolio=None, slstats=None, factor_stats=None):
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
    worst = sorted(open_losers, key=lambda t: t["pnl"])[:15]
    n_all = n_closed + len(open_pos)
    win_rate = (n_closed / n_all * 100) if n_all else 0

    # 月別の成立回数
    monthly = {}
    for t in closed:
        monthly[t["sell_date"][:7]] = monthly.get(t["sell_date"][:7], 0) + 1

    def yen2(v):
        sign = "+" if v >= 0 else "−"
        return f"{sign}{abs(v):,.0f}円"

    body = []
    if portfolio:
        body.append('<div class="card"><h2>持ち金別・現実シミュレーション（この1年）</h2>')
        body.append('<div class="capset">あなたの持ち金 <input id="cap" class="capin num" type="number" '
                    'inputmode="numeric" placeholder="50"> 万円（この端末にだけ保存）</div>')
        for p in portfolio:
            label = "無制限（参考）" if p["budget"] is None else f'{int(p["budget"]/10000)}万円'
            bid = "inf" if p["budget"] is None else str(p["budget"])
            cls = "plus" if p["unrealized"] >= 0 else "minus"
            sign = "+" if p["unrealized"] >= 0 else "−"
            body.append(
                f'<div class="tier" data-budget="{bid}">'
                f'<div class="tname num">{label}</div>'
                f'<div class="tfacts num">確定 <b class="plus">+{p["realized"]:,.0f}円</b>（{p["wins"]}勝）'
                f' ／ 資金不足で見送り {p["skipped"]:,}回'
                f' ／ 持ち越し{p["open_count"]}件 <b class="{cls}">{sign}{abs(p["unrealized"]):,.0f}円</b>'
                f' ／ 最大同時{p["max_positions"]}銘柄</div></div>')
        body.append('<div class="tiernote">持ち金を増やすと「見送り」が減って確定益が伸びる一方、'
                    '持ち越しの含み損も増えます。次の段の伸び幅が小さければ、まだ増額の必要はない、'
                    'という読み方ができます。</div></div>')
    if slstats:
        totals = [a["realized"] + a["open_pnl"] for a in slstats]
        best_i = max(range(len(slstats)), key=lambda i: totals[i])
        max_abs = max(abs(t) for t in totals) or 1
        body.append('<div class="card"><h2>損切りルールの効果比較（利確+'
                    f'{CONFIG["TP_PCT"]:.0f}%共通・資金無制限）</h2>')
        for idx, a in enumerate(slstats):
            label = "損切りなし（おばあさん流）" if a["sl"] is None else f'損切り −{a["sl"]:.0f}%'
            key = "none" if a["sl"] is None else f'{a["sl"]:.0f}'
            total = totals[idx]
            closed_n = a["tp_count"] + a["sl_count"]
            winrate = a["tp_count"] / closed_n * 100 if closed_n else 0
            tcls = "plus" if total >= 0 else "minus"
            tsign = "+" if total >= 0 else "−"
            osign = "+" if a["open_pnl"] >= 0 else "−"
            barw = abs(total) / max_abs * 100
            barcls = "barp" if total >= 0 else "barm"
            best = '<span class="best">★ この1年の最適</span>' if idx == best_i else ""
            body.append(
                f'<div class="tier" data-sl="{key}">'
                f'<div class="tname">{label}{best}</div>'
                f'<div class="barwrap"><div class="{barcls}" style="width:{barw:.0f}%"></div>'
                f'<span class="barv num {tcls}">{tsign}{abs(total):,.0f}円</span></div>'
                f'<div class="tfacts num">勝率{winrate:.0f}%（利確{a["tp_count"]:,}・損切り{a["sl_count"]:,}）'
                f' ／ 持ち越し{a["open"]:,}件 {osign}{abs(a["open_pnl"]):,.0f}円</div></div>')
        body.append('<div class="tiernote">棒の長さ＝トータル損益（確定+含み）。★が過去1年での最適設定です。'
                    'ただし過去1年に最適だった数字が来年も最適とは限らないため、★と自分の設定（印付き）が'
                    '大きくズレていないかを確認する使い方が健全です。</div></div>')

    if factor_stats:
        labels = {"gc": "ゴールデンクロス中（50日線＞200日線）",
                  "rsi": "RSI(14)が40以下（売られすぎ）",
                  "gradual": "じわ下げ（直近に1日4%超の急落なし）"}
        body.append('<div class="card"><h2>どの根拠が本当に効いているか（この1年の全買いシグナル実測）</h2>')
        for key, label in labels.items():
            f = factor_stats.get(key)
            if not f:
                continue
            wn, ww = f["with"]
            on, ow = f["without"]
            wr = ww / wn * 100 if wn else 0
            orate = ow / on * 100 if on else 0
            diff = wr - orate
            cls = "plus" if diff >= 0 else "minus"
            body.append(
                f'<div class="fact"><span>{label}</span>'
                f'<span class="v num">勝率 {wr:.0f}% vs 非該当 {orate:.0f}%'
                f'（<b class="{cls}">{diff:+.0f}pt</b>・{wn:,}回中）</span></div>')
        body.append('<div class="tiernote">「勝ち」＝買いシグナルの後40営業日以内に利確ライン到達。'
                    '差がプラスの要因は採点で重視する価値があり、差が無い/マイナスの要因は配点を見直す根拠になります。'
                    '毎晩の実行で更新される、採点ルール自身の成績表です。</div></div>')
    body.append('<div class="card"><h2>参考: 資金無制限で全シグナルを拾った場合の内訳</h2>')
    body.append(f'<div class="fact"><span>買いに入った回数</span><span class="v num">{n_all:,}回</span></div>')
    body.append(f'<div class="fact"><span>利確ライン（+{CONFIG["TP_PCT"]:.0f}%）で売れた回数</span><span class="v num">{n_closed:,}回（{win_rate:.0f}%）</span></div>')
    body.append(f'<div class="fact"><span>確定した利益の合計</span><span class="v num plus">{yen2(total_realized)}</span></div>')
    body.append(f'<div class="fact"><span>利確までの平均日数</span><span class="v num">約{avg_held:.0f}営業日</span></div>')
    body.append(f'<div class="fact"><span>まだ売れていない持ち越し</span><span class="v num">{len(open_pos):,}件（うち含み損 {len(open_losers):,}件）</span></div>')
    cls = "plus" if open_total >= 0 else "minus"
    body.append(f'<div class="fact"><span>持ち越し分の含み損益 合計</span><span class="v num {cls}">{yen2(open_total)}</span></div>')
    body.append("</div>")

    if monthly:
        body.append('<div class="card"><h2>月別・利確が取れた回数</h2>')
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
                f"ルール: ◎（20日高値から{CONFIG['CHEAP_PCT']:.0f}%安）になったら翌日の始値で100株買い → "
                f"買値+{CONFIG['TP_PCT']:.0f}%の指値で売る")
    footnote = (f"この検証は「いまの対象銘柄」の過去1年をなぞった簡易計算です。手数料・税金は含みません。"
                f"買値は翌営業日の始値、売りは買値+{CONFIG['TP_PCT']:.0f}%に到達した日に成立と仮定（損切りなし）。"
                "持ち株管理で設定した個人の利確・損切り%とは独立に、共通基準で計算しています。")
    extra_css = """
  .capset{font-size:12px; color:var(--ink2); padding:2px 0 10px;}
  .capin{width:70px; font-size:14px; font-weight:700; padding:5px 8px;
    border:1.5px solid #d9d2bf; border-radius:8px; background:#fff; text-align:right;}
  .tier{padding:8px 10px; border-radius:10px; margin-bottom:6px; background:#fff;}
  .tier.me{outline:2px solid var(--cheap); background:var(--cheap-bg);}
  .tier.me .tname::after{content:" ← いまのあなた"; color:var(--cheap); font-size:10.5px;}
  .tname{font-size:13px; font-weight:800;}
  .tfacts{font-size:11px; color:var(--ink2); margin-top:3px; line-height:1.6;}
  .tiernote{font-size:11px; color:#8a5a17; line-height:1.7; padding-top:8px;}
  .best{color:#c62f2f; font-size:10.5px; margin-left:8px;}
  .barwrap{position:relative; background:#f0ead9; border-radius:6px; height:18px;
    margin:4px 0 2px; overflow:hidden;}
  .barp{height:100%; background:#7fae86; border-radius:6px;}
  .barm{height:100%; background:#d98c8c; border-radius:6px;}
  .barv{position:absolute; right:8px; top:1.5px; font-size:11px; font-weight:800;}
"""
    script = """<script>
const CAP_KEY = 'kabuobaa_capital';
const capIn = document.getElementById('cap');
function applyTier(){
  const man = parseFloat(capIn.value) || 0;
  const cap = man * 10000;
  localStorage.setItem(CAP_KEY, capIn.value || '');
  const tiers = Array.from(document.querySelectorAll('.tier[data-budget]'));
  tiers.forEach(t => t.classList.remove('me'));
  if (cap > 0){
    const fit = tiers.filter(t => t.dataset.budget !== 'inf' && Number(t.dataset.budget) <= cap).pop()
             || tiers[0];
    if (fit) fit.classList.add('me');
  }
}
if (capIn){
  capIn.value = localStorage.getItem(CAP_KEY) || '';
  capIn.addEventListener('input', applyTier);
  applyTier();
}
(function(){
  const sl = parseFloat(localStorage.getItem('kabuobaa_sl')) || 8;
  const rows = Array.from(document.querySelectorAll('.tier[data-sl]'));
  if (!rows.length) return;
  let best = null, bestDiff = 1e9;
  rows.forEach(t => {
    if (t.dataset.sl === 'none') return;
    const diff = Math.abs(Number(t.dataset.sl) - sl);
    if (diff < bestDiff){ bestDiff = diff; best = t; }
  });
  if (best) best.classList.add('me');
})();
</script>"""
    return (SUBPAGE_TEMPLATE
            .replace("__NAVCSS__", NAV_CSS)
            .replace("__NAVJS__", NAV_JS)
            .replace("__NAV__", nav_html("backtest"))
            .replace("__TITLE__", "手法の検証レポート")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", "\n".join(body))
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", script))


STATUS_DEF = {
    "picked": ("候補", "#fdeeee", "#c62f2f", "根拠スコア上位の厳選候補（帳簿に表示）"),
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
    chips.append("</div>")
    chips.append('<input id="q" class="search" type="search" placeholder="銘柄名・コードで検索">')

    # 業種カテゴリでまとめる（メイン帳簿と同じ分類）
    chip_class = {"プライム": "prime", "スタンダード": "std", "グロース": "growth"}
    groups = {}
    for r in all_results:
        g = SECTOR_GROUPS.get(r.get("sector", ""), DEFAULT_GROUP)
        groups.setdefault(g, []).append(r)
    ordered_groups = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)

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
        return (
            f'<details class="udet" data-s="{r["status"]}" data-code="{r["code"]}" '
            f'data-t="{html.escape(r["name"].lower())} {r["code"]}">'
            f'<summary class="urow">'
            f'<span class="st" style="background:{bg}; color:{fg}">{label}</span>'
            f'<span class="un"><b>{html.escape(r["name"])}</b> '
            f'<span class="chip {mchip}">{html.escape(r.get("market", "") or "−")}</span> '
            f'<span class="num uc">{r["code"]}</span>{reason_html}</span>'
            f'<span class="up num">{close}<small>{drop}</small></span></summary>'
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
"""
    script = """<script>
const rows = Array.from(document.querySelectorAll('details.udet'));
let filter = 'all';
function apply(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  for (const r of rows){
    const okF = (filter === 'all' || r.dataset.s === filter);
    const okQ = (!q || r.dataset.t.includes(q));
    r.classList.toggle('hidden', !(okF && okQ));
  }
  for (const g of document.querySelectorAll('details.gsec')){
    const visible = g.querySelectorAll('details.udet:not(.hidden)').length;
    g.classList.toggle('hidden', visible === 0);
  }
}
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
</script>"""

    weekdays = "月火水木金土日"
    subtitle = (f"{dt.month}/{dt.day}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d} 判定 ・ "
                f"全{len(all_results):,}銘柄の扱いと理由の台帳 ・ "
                f"候補ライン（{CONFIG['SHORTLIST_N']}位のスコア）: {stats.get('cutoff_score', 0):.0f}点")
    body = ('<div class="card"><h2>判定の凡例</h2>' + legend + "</div>"
            + "".join(chips)
            + '<div class="list">' + "\n".join(rows) + "</div>")
    footnote = ("「対象外」は上場間もない・株価100円未満・売買代金が少ない、のいずれか。"
                "「除外」は終わった株（1年高値から大幅下落・長期下落トレンド）に加え、"
                "直近の急落（落ちるナイフ）・荒すぎる値動き・1年安値圏更新中・下げ止まり未確認を含みます。"
                "各行に個別の理由を表示。判定は毎回の実行で更新されます。")
    return (SUBPAGE_TEMPLATE
            .replace("__NAVCSS__", NAV_CSS)
            .replace("__NAVJS__", NAV_JS)
            .replace("__NAV__", nav_html("universe"))
            .replace("__TITLE__", "全銘柄の判定一覧")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", body)
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", script))


def render_holdings(dt):
    """持ち株の管理ページ（データは全て閲覧端末のlocalStorageに保存）"""
    weekdays = "月火水木金土日"
    subtitle = (f"買った銘柄を登録すると、毎時の記帳価格と突き合わせて"
                f"「売り判断かどうか」を自動表示します ・ データはこの端末にだけ保存")

    body = """
<div class="card">
  <h2>売りルール（あなたの決めごと）</h2>
  <div class="fact"><span>利確: 買値から何%上がったら売るか</span>
    <span class="v"><input id="tp" class="rin num" type="number" inputmode="decimal" step="0.5" placeholder="5"> %</span></div>
  <div class="fact"><span>損切り: 買値から何%下がったら売るか</span>
    <span class="v"><input id="sl" class="rin num" type="number" inputmode="decimal" step="0.5" placeholder="8"> %</span></div>
  <div class="rulenote">未入力なら灰色の推奨値（利確+5%・損切り−8%）で計算します。
  登録した持ち株ごとに「◯円になったら売る」の具体的な値段と、そのときの損益額に換算して表示します。</div>
</div>

<div class="card">
  <h2>持ち株を登録</h2>
  <div class="addrow">
    <input id="acode" class="rin num" type="text" placeholder="コード 7203" maxlength="6">
    <input id="abuy" class="rin num" type="number" inputmode="decimal" placeholder="買値 3,050">
    <input id="ashares" class="rin num" type="number" inputmode="numeric" placeholder="株数 100">
    <button id="aadd" class="abtn">追加</button>
  </div>
  <div id="aerr" class="aerr"></div>
</div>

<div id="hlist"></div>
<div id="hupdated" class="note"></div>
"""

    extra_css = """
  .rin{width:90px; font-size:14px; font-weight:700; padding:6px 8px;
    border:1.5px solid #d9d2bf; border-radius:8px; background:#fff; text-align:right;}
  #acode{text-align:left;}
  .rulenote{font-size:11px; color:#8a5a17; line-height:1.7; padding-top:8px;}
  .addrow{display:flex; gap:6px; flex-wrap:wrap; align-items:center;}
  .addrow .rin{flex:1; min-width:90px;}
  .abtn{font-size:13px; font-weight:800; color:#fff; background:#1c1c1e; border:none;
    border-radius:9px; padding:9px 18px;}
  .aerr{color:var(--cheap); font-size:11.5px; font-weight:700; padding-top:6px; min-height:14px;}
  .hcard{background:#fff; border-radius:14px; padding:12px 14px; margin-bottom:10px;
    box-shadow:0 1px 3px rgba(0,0,0,.06);}
  .hcard.selltp{outline:2.5px solid var(--cheap); background:var(--cheap-bg);}
  .hcard.sellsl{outline:2.5px solid #b06a00; background:var(--mild-bg);}
  .htop{display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;}
  .hname{font-size:14px; font-weight:800;}
  .hname small{font-weight:600; color:var(--ink2); font-size:11px;}
  .hdel{font-size:11px; color:var(--ink3); background:none; border:1px solid var(--line);
    border-radius:7px; padding:3px 8px;}
  .hstatus{font-size:13px; font-weight:800; border-radius:9px; padding:7px 10px;
    text-align:center; margin:8px 0 4px;}
  .hstatus.tp{color:#fff; background:var(--cheap);}
  .hstatus.sl{color:#fff; background:#b06a00;}
  .hstatus.hold{color:var(--ink2); background:#f0f0f4;}
"""

    script = """<script>
const TP_DEF = 5, SL_DEF = 8;
const TP_KEY='kabuobaa_tp_pct', SL_KEY='kabuobaa_sl', H_KEY='kabuobaa_holdings';
const tpIn=document.getElementById('tp'), slIn=document.getElementById('sl');
let prices=null, gen='';

function loadH(){ try{return JSON.parse(localStorage.getItem(H_KEY)||'[]');}catch(e){return [];} }
function saveH(h){ localStorage.setItem(H_KEY, JSON.stringify(h)); }
function yen(v){ return Math.round(v).toLocaleString(); }

function render(){
  const tp = parseFloat(tpIn.value) || TP_DEF;
  const sl = parseFloat(slIn.value) || SL_DEF;
  localStorage.setItem(TP_KEY, tpIn.value||''); localStorage.setItem(SL_KEY, slIn.value||'');
  const list = document.getElementById('hlist');
  const holds = loadH();
  if (!holds.length){
    list.innerHTML = '<div class="card"><h2>持ち株一覧</h2><div class="note">まだ登録がありません。上の欄から追加してください。</div></div>';
    return;
  }
  let out = '';
  holds.forEach((h, idx) => {
    const p = prices ? prices[h.code] : null;
    const tpLine = h.buy * (1 + tp / 100);
    const slLine = h.buy * (1 - sl / 100);
    let inner = '';
    if (!p){
      inner = '<div class="hstatus hold">' + (prices ? '価格データなし（コードを確認）' : '価格データ読み込み中…') + '</div>';
    } else {
      const cur = p.c;
      const pnl = (cur - h.buy) * h.shares;
      const pnlTxt = (pnl >= 0 ? '+' : '−') + yen(Math.abs(pnl)) + '円';
      let st, cls;
      if (cur >= tpLine){ st = '売り判断 ｜ 利確ライン到達（' + pnlTxt + '）'; cls = 'tp'; }
      else if (cur <= slLine){ st = '売り判断 ｜ 損切りライン到達（' + pnlTxt + '）'; cls = 'sl'; }
      else { st = '持続 ｜ いま ' + pnlTxt + ' ・ 利確まであと' + yen((tpLine - cur) * h.shares) + '円'; cls = 'hold'; }
      inner =
        '<div class="hstatus ' + cls + '">' + st + '</div>' +
        '<div class="fact num"><span>いまの値段（' + (p.d ? p.d.slice(5).replace('-','/') : '') + '記帳）</span><span class="v">' + yen(cur) + '円</span></div>' +
        '<div class="fact num"><span>買値 × 株数</span><span class="v">' + yen(h.buy) + '円 × ' + h.shares + '株</span></div>' +
        '<div class="fact num"><span>利確ライン（+' + tp + '%＝+' + yen((tpLine - h.buy) * h.shares) + '円）</span><span class="v plus">' + yen(tpLine) + '円になったら売る</span></div>' +
        '<div class="fact num"><span>損切りライン（−' + sl + '%＝−' + yen((h.buy - slLine) * h.shares) + '円）</span><span class="v minus">' + yen(slLine) + '円になったら売る</span></div>';
    }
    const nm = (prices && prices[h.code]) ? prices[h.code].n : '銘柄 ' + h.code;
    out += '<div class="hcard ' + (inner.includes('hstatus tp') ? 'selltp' : inner.includes('hstatus sl"') ? 'sellsl' : '') + '">' +
      '<div class="htop"><div class="hname">' + nm + ' <small class="num">' + h.code + '</small></div>' +
      '<button class="hdel" data-i="' + idx + '">削除</button></div>' + inner + '</div>';
  });
  list.innerHTML = out;
  list.querySelectorAll('.hdel').forEach(b => b.addEventListener('click', () => {
    const holds2 = loadH(); holds2.splice(Number(b.dataset.i), 1); saveH(holds2); render();
  }));
}

document.getElementById('aadd').addEventListener('click', () => {
  const code = document.getElementById('acode').value.trim().toUpperCase();
  const buy = parseFloat(document.getElementById('abuy').value);
  const shares = parseInt(document.getElementById('ashares').value) || 100;
  const err = document.getElementById('aerr');
  if (!code || !(buy > 0)){ err.textContent = 'コードと買値を入力してください'; return; }
  err.textContent = '';
  const holds = loadH(); holds.push({code: code, buy: buy, shares: shares}); saveH(holds);
  document.getElementById('acode').value=''; document.getElementById('abuy').value='';
  render();
});
tpIn.value = localStorage.getItem(TP_KEY) || ''; slIn.value = localStorage.getItem(SL_KEY) || '';
tpIn.addEventListener('input', render); slIn.addEventListener('input', render);
fetch('prices.json').then(r => r.json()).then(j => {
  prices = j.prices; gen = j.generated_at;
  const u = document.getElementById('hupdated');
  if (u) u.textContent = '価格の記帳: ' + gen.replace('T', ' ').slice(0, 16);
  render();
}).catch(() => { prices = {}; render(); });
render();
</script>"""

    footnote = ("売りルールと持ち株データはこの端末のブラウザにだけ保存され、外部には送信されません。"
                "価格は取引時間中は毎時、夜に確定値で記帳されたものです。"
                "実際の売り注文は証券会社アプリで行ってください。")
    return (SUBPAGE_TEMPLATE
            .replace("__NAVCSS__", NAV_CSS)
            .replace("__NAVJS__", NAV_JS)
            .replace("__NAV__", nav_html("holdings"))
            .replace("__TITLE__", "持ち株の管理")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", body)
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", script))


def render_guide(dt):
    """使い方ページ（設定値から自動生成するので仕様変更に追随する）"""
    c = CONFIG
    body = f"""
<div class="card"><h2>これは何？</h2>
<div class="gtext">おじいさま・おばあさまの株手法——毎日の四本値をノートに記録し、いつもより安くなったら買い、
決めた利益で機械的に売る——をWeb化したものです。<b>下準備（記録・選定・計算）はシステムが毎日自動で行い、
買う・売るの判断と注文は人間が行います。</b>このサイトに注文機能はありません。</div></div>

<div class="card"><h2>毎日の使い方（1〜2分）</h2>
<div class="gstep"><b>1. 夜、帳簿を開く</b> ホーム画面のアイコンから開き、合言葉を入れる（記憶させた端末は自動で開きます）</div>
<div class="gstep"><b>2. 厳選{c["TOP_N"]}銘柄を眺める</b> 気になる銘柄をタップすると、選ばれた根拠（スコア内訳）・
最新日の四本値・10日分のノート・Yahoo!ファイナンスへのリンクが開きます</div>
<div class="gstep"><b>3. 買うと決めたら</b> 証券会社アプリで注文し、「持ち株」画面にコード・買値・株数を登録</div>
<div class="gstep"><b>4. 売り時はシステムが監視</b> 持ち株画面が毎時の価格と突き合わせ、利確ライン・損切りライン到達を
色付きで知らせます。赤やオレンジのカードが出たら売り注文を検討</div></div>

<div class="card"><h2>画面の説明</h2>
<div class="gstep"><b>帳簿</b> 全上場銘柄から選ばれた厳選{c["TOP_N"]}銘柄。持ち金を設定すると「100株買える銘柄」だけから選ばれます。
★を付けた銘柄は常に最上部に固定。銘柄名の下の茶色い1行は、数字から機械生成した事実コメントです</div>
<div class="gstep"><b>持ち株</b> 保有銘柄の登録と売り判断。利確（推奨+{c["TP_PCT"]:.0f}%）・損切り（推奨−8%）の%を設定すると、
銘柄ごとに「◯円になったら売る」に換算されます</div>
<div class="gstep"><b>全銘柄</b> 約4,000銘柄すべての判定台帳。候補・圏外にはスコア、除外・対象外には理由が付き、
「なぜあの銘柄が載っていないか」が調べられます</div>
<div class="gstep"><b>検証</b> 過去1年、この手法を機械的に続けていたらの成績。持ち金別（30万〜無制限）と
損切り%別（なし〜−12%）の比較で、自分の設定の妥当性を数字で確かめられます</div></div>

<div class="card"><h2>選定の仕組み（要約）</h2>
<div class="gtext">全銘柄から、流動性不足・低位株・上場間もない銘柄を対象外にし、
「終わった株」（1年高値から{int(c["DEAD_DRAWDOWN"]*100)}%以上下落など）と「危ない下げ方」
（1日{c["KNIFE_DROP_1D"]:.0f}%超の急落・荒い値動き・下げ止まり未確認）を除外。
残りを5観点（いまの安さ・下げの質・トレンドの地合い・過去1年の利確実績・売買のしやすさ)で採点し、
上位{c["SHORTLIST_N"]}銘柄を候補に、そこから{c["TOP_N"]}銘柄を表示します。
詳細は帳簿の「選定基準」カードと、各銘柄の根拠表示をご覧ください。</div></div>

<div class="card"><h2>設定とデータの保存場所</h2>
<div class="gtext">持ち金・売りルール・★お気に入り・持ち株・合言葉の記憶は、<b>すべて閲覧している端末の中にだけ</b>保存されます。
公開されているのは銘柄と株価という公開情報のみ。iPhoneとMacで設定は共有されないため、端末ごとに入力してください。</div></div>

<div class="card"><h2>指標の読み方（最低限これだけ）</h2>
<div class="gstep"><b>PER（株価収益率）</b> 株価が「1年分の利益の何年分か」。目安は10倍未満=割安圏・10〜20倍=標準・20倍超=割高圏（成長期待が高いほど高くなる）。
<b>注意:</b> 業績悪化で利益が減ると見かけのPERは上がり、逆に一時的な特別利益で下がることもある。業種によって水準が大きく違うので、同業と比べるのが基本</div>
<div class="gstep"><b>PBR（株価純資産倍率）</b> 株価が「会社の純資産の何倍か」。1倍未満は理論上「会社を解散した方が高い」水準で割安のサインだが、
<b>低いまま放置される「割安の罠」</b>も多い（稼ぐ力がない・資産の質が悪い等）。1倍割れ+業績健全なら注目、が正しい使い方</div>
<div class="gstep"><b>ROE（自己資本利益率）</b> 会社が株主のお金でどれだけ効率よく利益を稼いだか。10%以上=優良、
5%前後=標準、3%未満=収益力が弱い。日本株の平均は8〜9%程度。低PBRでもROEが低い会社は「割安の罠」になりやすい</div>
<div class="gstep"><b>配当利回り（実績）</b> 株価に対する年間配当の割合。3〜4%は高配当の部類。株価下落で見かけの利回りが上がっている場合は減配リスクに注意</div>
<div class="gstep"><b>RSI(14)</b> 直近14日の値動きの過熱感。30以下=売られすぎ（反発しやすい）、70以上=買われすぎ。下落トレンド中は30以下が続くこともある</div>
<div class="gstep"><b>MACD</b> 短期と中期の勢いの差を見る指標。マイナス圏からの「買い転換（ゴールデンクロス）」は
下げの勢いが尽きたサインとして最も広く使われる。転換直後が加点対象</div>
<div class="gstep"><b>ボリンジャーバンド</b> 過去20日の値動きの標準偏差（σ）で「統計的に普通の範囲」を測る。
−2σ以下は統計上約2%しか起きない売られすぎ水準で、反発しやすい</div>
<div class="gstep"><b>移動平均乖離率</b> 25日平均線から何%離れているか。−8%を超える下方乖離は逆張りの定番圏。
ただし−20%を超える乖離は「何か起きている」異常値で、むしろ警戒</div>
<div class="gstep"><b>ゴールデンクロス</b> 50日平均線が200日平均線の上にある状態。長期の上昇形で、「上昇トレンド中の押し目」を拾うこの手法と相性が良い</div>
<div class="gstep"><b>長期支持帯とタッチ回数</b> 過去に何度も反発した価格帯。定石は「〜3回目の試しまでは支持されやすく、4回目以降は割れやすい」。
帯を明確に割ったら支持帯は無効（このシステムは自動で除外・警告します）</div>
<div class="gstep"><b>地合いバナー</b> 帳簿上部の表示。日経平均が200日線の下にある間は市場全体が下落基調で、
個別銘柄の押し目買いの成功率も下がる。慎重モードの合図です</div></div>

<div class="card"><h2>操作のコツ</h2>
<div class="gstep"><b>スワイプで画面切り替え</b> 画面を左右にスワイプすると、帳簿⇄持ち株⇄全銘柄⇄検証⇄使い方 を行き来できます（上部のタブでも移動可）</div>
<div class="gstep"><b>財務の健全性は自動判定</b> PER・PBR・ROE・配当・時価総額・赤字の疑いは、システムが固定基準で
自動的にスコアへ反映します（設定不要）。判定の内訳は各銘柄の「選ばれた根拠」に点数付きで表示されます</div>
<div class="gstep"><b>銘柄コードのコピー</b> 一覧のコード（例: 2489 ⧉）をタップすると即コピーされます</div></div>

<div class="card"><h2>SBI証券アプリ連携の初期設定（1回だけ・30秒）</h2>
<div class="gstep"><b>1.</b> iPhoneの「ショートカット」アプリ（紫のアイコン・標準搭載）を開く</div>
<div class="gstep"><b>2.</b> 右上の「＋」→「アクションを追加」→検索欄に「Appを開く」と入力して選択</div>
<div class="gstep"><b>3.</b> 薄い字の「App」をタップ →「SBI証券 株」を選択</div>
<div class="gstep"><b>4.</b> 上部の名前を「<b>SBIへ</b>」に変更（この名前が合言葉になります。一字一句この通りに）→ 完了</div>
<div class="gstep">以後、帳簿の銘柄内の「SBI証券アプリで見る」を押すと、銘柄コードが自動でコピーされた状態で
SBIアプリが起動します。SBIアプリの銘柄検索に<b>長押し→ペースト</b>すれば表示完了。
※SBIアプリは外部から銘柄画面へ直接飛ぶ入口を公開していないため、これが最短の動線です</div></div>

<div class="card"><h2>更新タイミングとよくある質問</h2>
<div class="gstep"><b>いつ更新される？</b> 平日9:40〜14:40の毎時（取引時間中の途中経過）、15:45（大引け後）、
20:30（夜の確定記帳・銘柄入れ替えの基準）。実行に10〜20分かかるため、表示は最大1時間ほど前の値です</div>
<div class="gstep"><b>Yahooと数字が違う</b> このサイトは毎時の「写真」です。リアルタイムの板や気配は
各銘柄のYahoo!ファイナンスリンクで確認してください</div>
<div class="gstep"><b>昨日いた銘柄が消えた</b> 全銘柄画面でその銘柄を検索すると、今日の判定と理由が出ます</div>
<div class="gstep"><b>データの出どころ</b> 銘柄一覧はJPX公式、株価はYahoo Finance、
各銘柄の「会社からの発表」は東証の適時開示（TDnet）です。
このサイトは判断材料の表示のみで、投資判断はご自身の責任で行ってください</div></div>
"""
    extra_css = """
  .gtext{font-size:12.5px; line-height:1.9; color:var(--ink);}
  .gstep{font-size:12.5px; line-height:1.8; color:var(--ink); padding:7px 0;
    border-bottom:1px dashed #f0ead9;}
  .gstep:last-child{border-bottom:none;}
  .gstep b, .gtext b{color:#4a3f28;}
"""
    weekdays = "月火水木金土日"
    subtitle = f"このサイトの役割分担と毎日の流れ ・ {dt.month}/{dt.day}（{weekdays[dt.weekday()]}）時点の仕様"
    footnote = "仕様を変えるとこのページも自動で追随します。困ったことがあればこのページを最初に見てください。"
    return (SUBPAGE_TEMPLATE
            .replace("__NAVCSS__", NAV_CSS)
            .replace("__NAVJS__", NAV_JS)
            .replace("__NAV__", nav_html("guide"))
            .replace("__TITLE__", "使い方")
            .replace("__SUBTITLE__", subtitle)
            .replace("__BODY__", body)
            .replace("__FOOTNOTE__", footnote)
            .replace("__EXTRA_CSS__", extra_css)
            .replace("__SCRIPT__", ""))


STATUS_LABEL = {"picked": "厳選候補", "ok": "候補", "bench": "圏外",
                "dead": "除外", "skip": "対象外", "fail": "取得失敗"}


def render_stock_detail(e):
    """1銘柄の詳細HTML断片（全銘柄一覧のタップ展開用）"""
    parts = []
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

    fu = e.get("fund")
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
    svg = spark_svg(lg.get("spark"), lg)
    if svg:
        z = lg.get("zone")
        zline = (f'赤い帯=長期支持帯 {z["zone_low"]:,.0f}〜{z["zone_top"]:,.0f}円（▲=反発地点 ・ ●=いま）'
                 if z else "●=いま（3年週足）")
        parts.append(f'<div class="nhead">3年の値動きと支持帯</div><div class="spark">{svg}</div>'
                     f'<div class="discnote">{zline}</div>')

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

    yahoo_url = f'https://finance.yahoo.co.jp/quote/{e["code"]}{e.get("suffix", ".T")}'
    parts.append(f'<a class="ylink" href="{yahoo_url}" target="_blank" rel="noopener">'
                 f'Yahoo!ファイナンスで詳細を見る →</a>')
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
        picked, stats, all_results, sim_records, portfolio, slstats, extras = make_demo_data()
    else:
        picked, stats, all_results, sim_records, portfolio, slstats, extras = run_screening()

    data = build_output(picked, stats)
    data["market"] = extras.get("market")

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
        render_backtest(sim_records, dt_now, portfolio, slstats,
                        extras.get("factor_stats")), encoding="utf-8")
    (DOCS / "holdings.html").write_text(render_holdings(dt_now), encoding="utf-8")
    (DOCS / "guide.html").write_text(render_guide(dt_now), encoding="utf-8")
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

    # 持ち株管理用: 全銘柄の最新価格表（銘柄名・終値・日付のみの公開情報）
    prices = {}
    for r in all_results:
        if r.get("close") is not None:
            prices[r["code"]] = {"n": r["name"], "c": r["close"],
                                 "d": data["stocks"][0]["date"] if data["stocks"] else ""}
    for s in data["stocks"]:
        prices[s["code"]] = {"n": s["name"], "c": s["close"], "d": s["date"]}
    (DOCS / "prices.json").write_text(
        json.dumps({"generated_at": data["generated_at"], "prices": prices},
                   ensure_ascii=False), encoding="utf-8")

    print(f"完了: {len(data['stocks'])}銘柄を選定 "
          f"(除外 {stats.get('dead_excluded', 0)}銘柄) → docs/index.html"
          f" + universe.html + backtest.html")


if __name__ == "__main__":
    main()
