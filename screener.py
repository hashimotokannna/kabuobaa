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
import html
import json
import math
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
    "THROTTLE_SEC": 0.35,    # 1銘柄ごとの待ち時間（Yahoo様への礼儀）
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
        "high1y": high1y, "drawdown_1y": drawdown_1y,
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
# 4. スクリーニング本体
# ------------------------------------------------------------
def run_screening():
    import requests

    print("上場銘柄一覧を取得中...")
    universe = fetch_universe()
    print(f"  対象: {len(universe)}銘柄")

    session = requests.Session()
    candidates, dead_count, skip_count, fail_count = [], 0, 0, 0

    for i, stock in enumerate(universe, 1):
        if i % 200 == 0:
            print(f"  {i}/{len(universe)} 取得済み...")
        days = fetch_daily(session, stock["code"], stock["suffix"])
        time.sleep(CONFIG["THROTTLE_SEC"])
        if not days:
            fail_count += 1
            continue
        m = compute_metrics(days)
        status, _reason = classify(m)
        if status == "dead":
            dead_count += 1
            continue
        if status == "skip":
            skip_count += 1
            continue
        candidates.append({**stock, **m})

    candidates.sort(key=lambda s: s["drop_pct"], reverse=True)
    picked = candidates[:CONFIG["TOP_N"]]

    stats = {
        "universe": len(universe),
        "dead_excluded": dead_count,
        "skipped": skip_count,
        "failed": fail_count,
    }
    return picked, stats


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
        })
    picked.sort(key=lambda s: s["drop_pct"], reverse=True)
    stats = {"universe": 3912, "dead_excluded": 214, "skipped": 1480, "failed": 3}
    return picked, stats


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
            "level": level_of(s["drop_pct"]),
            "is_new": (prev_codes is not None and s["code"] not in prev_codes),
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

    chip_class = {"プライム": "prime", "スタンダード": "std", "グロース": "growth"}

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
            rows_html.append(f"""
      <div class="row">
        <div class="rk num">{i}</div>
        <div class="nm">
          <div class="n1">{html.escape(s["name"])} <span class="chip {chip}">{html.escape(s["market"])}</span>{new_mark}</div>
          <div class="n2 num">{s["code"]} ・ 普段 {yen(s["usual"])}円 ・ 総合{s["rank"]}位</div>
        </div>
        <div class="px">
          <div class="p1 num">{yen(s["close"])}<small>円</small></div>
          <div class="p2 num drop">高値から −{s["drop_pct"]:.1f}%</div>
        </div>
        <div>{badge}</div>
      </div>""")

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
</style>
</head>
<body>
<header>
  <div class="t">今夜の{len(stocks)}銘柄</div>
  <div class="s">{date_str}の終値で記帳済み ・ 業種別 ・ 判断はご自身で</div>
</header>
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
# main
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true",
                        help="ダミーデータでページ生成のみ確認")
    args = parser.parse_args()

    if args.demo:
        print("デモモード: ダミーデータでページを生成します")
        picked, stats = make_demo_data()
    else:
        picked, stats = run_screening()

    data = build_output(picked, stats)

    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    (DOCS / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (DOCS / "index.html").write_text(render_html(data), encoding="utf-8")

    print(f"完了: {len(data['stocks'])}銘柄を選定 "
          f"(除外 {stats.get('dead_excluded', 0)}銘柄) → docs/index.html")


if __name__ == "__main__":
    main()
