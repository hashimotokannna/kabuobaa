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
    "SHORTLIST_N": 40,       # 候補として用意する数（持ち金で外れる分の予備を含む）
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
        if ah is not None and ah <= 10:
            score += 10
            reasons.append(f"利確まで平均 {ah:.0f}営業日と回転が速い（+10点）")
    else:
        reasons.append("過去1年はこの買い方の成立実績なし（加点なし）")
    if t.get("open_loss"):
        score -= 15
        reasons.append("ただし直近の仮想買いが塩漬け中（−15点）")

    # 5. 売り買いのしやすさ
    tv = s.get("turnover", 0)
    if tv >= 1_000_000_000:
        score += 12
        reasons.append(f"1日平均 {tv/100_000_000:.0f}億円の売買があり注文が通りやすい（+12点）")
    elif tv >= 100_000_000:
        score += 6
        reasons.append(f"1日平均 {tv/100_000_000:.1f}億円の売買（+6点）")

    return score, reasons


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
        elif i >= pos["buy_i"] and highs[i] >= pos["buy"] * (1 + CONFIG["TP_PCT"] / 100):
            # 買値+TP_PCT% の指値で売れた
            trades.append({
                "buy_date": days[pos["buy_i"]]["date"],
                "sell_date": days[i]["date"],
                "held": max(1, i - pos["buy_i"] + 1),
                "pnl": pos["buy"] * CONFIG["TP_PCT"] / 100 * 100,
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
            trades = simulate_grandma(days)
            candidates.append({**stock, **m, "days": days[-10:],
                               "sim": sim_summary(trades)})
            all_results.append({**base, "status": "ok", "reason": ""})
            if trades:
                sim_records.append({"code": stock["code"],
                                    "name": stock["name"], "trades": trades})
            sim_universe.append({
                "code": stock["code"], "name": stock["name"],
                "dates": [d["date"] for d in days],
                "opens": [d["open"] for d in days],
                "highs": [d["high"] for d in days],
                "closes": [d["close"] for d in days],
            })

    for c in candidates:
        c["score"], c["reasons"] = score_stock(c)
    candidates.sort(key=lambda s: s["score"], reverse=True)
    picked = candidates[:CONFIG["SHORTLIST_N"]]
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
    print("資金別シミュレーションを計算中...")
    portfolio = simulate_portfolio(sim_universe)
    return picked, stats, all_results, sim_records, portfolio


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
        s["score"], s["reasons"] = score_stock(s)
    picked.sort(key=lambda s: s["score"], reverse=True)
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
    return picked, stats, all_results, sim_records, portfolio


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


def yen(v):
    return f"{v:,.0f}"



# 持ち金設定（帳簿ページ用・端末内保存）
CAP_JS = '''<script>
const CAP_KEY = 'kabuobaa_capital';
const TOPN = __TOPN__;
const capIn = document.getElementById('cap');
const allChk = document.getElementById('showall');
function applyCap(){
  const man = parseFloat(capIn.value) || 0;
  const cap = man * 10000;
  localStorage.setItem(CAP_KEY, capIn.value || '');
  const rows = Array.from(document.querySelectorAll('details.drow'));
  let shown = 0;
  rows.forEach(r => {
    const cost = parseFloat(r.dataset.cost);
    const afford = cap <= 0 || cost <= cap;
    r.classList.toggle('over', !afford);
    let visible;
    if (allChk.checked){
      visible = true;
    } else {
      visible = afford && shown < TOPN;
      if (visible) shown++;
    }
    r.classList.toggle('caphidden', !visible);
  });
  let n = 0;
  rows.forEach(r => {
    if (!r.classList.contains('caphidden')){
      n++;
      const rk = r.querySelector('.rk');
      if (rk) rk.textContent = n;
    }
  });
  const cnt = document.getElementById('showncnt');
  if (cnt && !allChk.checked) cnt.textContent = String(shown);
  if (cnt && allChk.checked) cnt.textContent = String(n);
}
if (capIn){
  capIn.value = localStorage.getItem(CAP_KEY) || '';
  capIn.addEventListener('input', applyCap);
  allChk.addEventListener('change', applyCap);
  applyCap();
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
        rows_html.append(f"""
      <details class="drow" data-cost="{s["cost"]}">
      <summary class="row">
        <div class="rk num">{s["rank"]}</div>
        <div class="nm">
          <div class="n1">{html.escape(s["name"])} <span class="chip {chip}">{html.escape(s["market"])}</span>{new_mark}</div>
          <div class="n2 num">{s["code"]} ・ {html.escape(s["group"])} ・ 100株 {s["cost"] / 10000:,.1f}万円 ・ {s["score"]:.0f}点<span class="nofund">資金不足</span></div>
        </div>
        <div class="px">
          <div class="p1 num"><small>{price_label}</small> {yen(s["close"])}<small>円</small></div>
          <div class="p2 num drop">高値から −{s["drop_pct"]:.1f}%</div>
        </div>
        <div>{badge}</div>
        <div class="chev">›</div>
      </summary>
      <div class="notebox">
        <div class="nhead">選ばれた根拠（スコア {s["score"]:.0f}点）</div>
        {reasons_html}
        {latest_block(s)}
        <div class="fact"><span>100株の必要資金</span><span class="num">{s["cost"] / 10000:,.1f}万円</span></div>
        <div class="fact"><span>普段の値段（20日平均）</span><span class="num">{yen(s["usual"])}円</span></div>
        <div class="fact"><span>直近の高値（20日）</span><span class="num">{yen(s["high20"])}円</span></div>
        <div class="fact"><span>高値からの下げ</span><span class="num drop">−{yen(s["drop_yen"])}円（−{s["drop_pct"]:.1f}%）</span></div>
        {range1y}
        <div class="nhead">ノート（新しい順）</div>
        {day_rows(s)}
        <a class="ylink" href="{yahoo_url}" target="_blank" rel="noopener">Yahoo!ファイナンスでこの銘柄の詳細を見る →</a>
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
</style>
</head>
<body>
<header>
  <div class="t">今夜の厳選<span id="showncnt">{cfg["TOP_N"]}</span>銘柄</div>
  <div class="s">{date_str} {dt.hour:02d}:{dt.minute:02d} 記帳{"（取引時間中・当日分は途中経過）" if is_intraday else ""} ・ 根拠スコア順 ・ タップで根拠とノート ・ 判断はご自身で</div>
</header>
<div class="pnav">
  <a href="holdings.html">持ち株の管理 ›</a>
  <a href="universe.html">全銘柄の判定 ›</a>
  <a href="backtest.html">手法の検証 ›</a>
</div>
<details class="crit">
  <summary>この厳選{cfg["TOP_N"]}銘柄の選定基準（タップで開閉）<span class="chev">›</span></summary>
  <div class="critbody">
    <div class="step"><b>1. 対象</b> 東証プライム・スタンダード・グロースの全銘柄（{universe:,}銘柄）</div>
    <div class="step"><b>2. 土俵に上げない</b> 上場から日足{cfg["MIN_RECORDS"]}日未満 ／ 株価{cfg["MIN_PRICE"]}円未満 ／ 直近{cfg["RECENT_DAYS"]}日の平均売買代金{int(cfg["MIN_TURNOVER"]/10000):,}万円未満（売りたい時に売れない銘柄を避ける）</div>
    <div class="step"><b>3. 危ない下げ方を除外</b> ①1年高値から{int(cfg["DEAD_DRAWDOWN"]*100)}%以上下落・長期の下落トレンド継続（終わった株） ②直近10日に1日{cfg["KNIFE_DROP_1D"]:.0f}%超の急落（決算ミス等の材料落ち=落ちるナイフ） ③日々の値動きが±{cfg["MAX_VOL20"]:.1f}%超の荒い銘柄 ④1年安値圏を更新中 ⑤下げ止まり未確認（前日から安値切り下げ中）——本日計{excluded:,}銘柄を除外</div>
    <div class="step"><b>4. 根拠スコアで採点</b> 残った銘柄を「いまの安さ」「下げの質（じわ下げか急落か・値動きの穏やかさ）」「トレンドの地合い（200日線の上の押し目か・1年レンジ内の位置）」「過去1年でこの買い方が利確+{cfg["TP_PCT"]:.0f}%を取れた実績」「売買のしやすさ」の5観点で採点し、上位{cfg["SHORTLIST_N"]}銘柄を候補に</div>
    <div class="step"><b>5. 厳選{cfg["TOP_N"]}銘柄</b> 候補のうち、持ち金設定があれば「100株買える銘柄」だけを対象に、スコア上位{cfg["TOP_N"]}銘柄を表示。各銘柄の点数の内訳はタップで確認できます</div>
    <div class="step"><b>6. 目安ラベル</b> ◎=高値から{cfg["CHEAP_PCT"]:.0f}%以上安い ／ ○={cfg["MILD_PCT"]:.0f}%以上安い ／ 「普段の値段」={cfg["RECENT_DAYS"]}日の終値平均</div>
    <div class="step" style="color:#8a5a17;">基準は毎回の実行時点の設定で、この文章も自動で追随します。個々の銘柄の判定理由は「全銘柄の判定一覧」で確認できます。</div>
  </div>
</details>
<div class="capcard">
  <div class="caprow">持ち金 <input id="cap" class="capin num" type="number" inputmode="numeric"
    placeholder="50"> 万円
    <label class="caponly"><input id="showall" type="checkbox"> 候補{cfg["SHORTLIST_N"]}銘柄すべて表示</label></div>
  <div class="capnote">この端末にだけ保存されます。設定すると「100株買える銘柄」の中からスコア上位{cfg["TOP_N"]}銘柄が選ばれます。
  検証レポートの「持ち金別シミュレーション」とも連動します。</div>
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
""".replace("__CAPJS__", CAP_JS.replace("__TOPN__", str(cfg["TOP_N"])))


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


def render_backtest(sim_records, dt, portfolio=None):
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
"""
    script = """<script>
const CAP_KEY = 'kabuobaa_capital';
const capIn = document.getElementById('cap');
function applyTier(){
  const man = parseFloat(capIn.value) || 0;
  const cap = man * 10000;
  localStorage.setItem(CAP_KEY, capIn.value || '');
  const tiers = Array.from(document.querySelectorAll('.tier'));
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
</script>"""
    return (SUBPAGE_TEMPLATE
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

    chips = ['<div class="chips"><button class="chip on" data-f="all">すべて '
             f'{len(all_results):,}</button>']
    for key, (label, bg, fg, _desc) in STATUS_DEF.items():
        if counts.get(key):
            chips.append(f'<button class="chip" data-f="{key}" style="background:{bg}; color:{fg}">'
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
        reason = html.escape(r.get("reason") or "")
        reason_html = f'<span class="why">{reason}</span>' if reason else ""
        return (
            f'<div class="urow" data-s="{r["status"]}" data-t="{html.escape(r["name"].lower())} {r["code"]}">'
            f'<span class="st" style="background:{bg}; color:{fg}">{label}</span>'
            f'<span class="un"><b>{html.escape(r["name"])}</b> '
            f'<span class="chip {mchip}">{html.escape(r.get("market", "") or "−")}</span> '
            f'<span class="num uc">{r["code"]}</span>{reason_html}</span>'
            f'<span class="up num">{close}<small>{drop}</small></span></div>')

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
const rows = Array.from(document.querySelectorAll('.urow'));
let filter = 'all';
function apply(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  for (const r of rows){
    const okF = (filter === 'all' || r.dataset.s === filter);
    const okQ = (!q || r.dataset.t.includes(q));
    r.classList.toggle('hidden', !(okF && okQ));
  }
  for (const g of document.querySelectorAll('details.gsec')){
    const visible = g.querySelectorAll('.urow:not(.hidden)').length;
    g.classList.toggle('hidden', visible === 0);
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
                "「除外」は終わった株（1年高値から大幅下落・長期下落トレンド）に加え、"
                "直近の急落（落ちるナイフ）・荒すぎる値動き・1年安値圏更新中・下げ止まり未確認を含みます。"
                "各行に個別の理由を表示。判定は毎回の実行で更新されます。")
    return (SUBPAGE_TEMPLATE
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
            .replace("__TITLE__", "持ち株の管理")
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
        picked, stats, all_results, sim_records, portfolio = make_demo_data()
    else:
        picked, stats, all_results, sim_records, portfolio = run_screening()

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
        render_backtest(sim_records, dt_now, portfolio), encoding="utf-8")
    (DOCS / "holdings.html").write_text(render_holdings(dt_now), encoding="utf-8")

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
