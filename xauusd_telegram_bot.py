"""
XAU/USD Analysis Console — Telegram Bot
========================================

A Telegram bot version of the manual XAU/USD confluence console. It walks the
user through the same inputs as the original HTML tool (price, multi-timeframe
votes, macro, news, market structure, risk sizing) using simple tappable
buttons wherever possible, then returns the same kind of structured read-out:
status, technical score, confluence tier, suggested stop/targets, position
size, and a JSON block you can copy.

It is still just a calculator — nothing is fetched live. All the same
disclaimers apply: not financial advice, no execution, garbage-in/garbage-out.

SETUP
-----
1. pip install python-telegram-bot==21.*
2. Get a bot token from @BotFather on Telegram.
3. Set it as an environment variable and run:
       export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"
       python xauusd_telegram_bot.py

USAGE (inside Telegram)
------------------------
/start    - begin a new guided analysis (tap through the questions)
/example  - instantly run the built-in example dataset (great for a first try)
/cancel   - abort the current analysis
Every question has a "Skip" button — skipped fields just show up as missing
data in the final read-out, exactly like leaving a field blank in the web
console.
"""

import html
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger("xauusd-bot")

COLLECT = 1

TF_ORDER = ["M1", "M5", "M15", "H1", "H4"]
TF_WEIGHT = {"M1": 5, "M5": 10, "M15": 20, "H1": 30, "H4": 35}
DEFAULT_USED_TF = {"H1", "H4", "M15"}
STALE_THRESHOLD_MIN = {"SCALP": 1, "INTRADAY": 3, "SWING": 15}


# --------------------------------------------------------------------------
# Field definitions (mirrors the input fields of the HTML console)
# --------------------------------------------------------------------------

@dataclass
class FieldSpec:
    key: str
    prompt: str
    ftype: str  # 'num' | 'text' | 'choice' | 'bool' | 'ts' | 'tf_select'
    choices: Optional[list] = None  # list of (label, value)
    default: Any = None
    note: str = ""

    def keyboard(self):
        rows = []
        if self.ftype == "choice":
            rows = [[InlineKeyboardButton(label, callback_data=f"c:{val}")] for label, val in self.choices]
        elif self.ftype == "bool":
            rows = [[
                InlineKeyboardButton("✅ Yes", callback_data="c:1"),
                InlineKeyboardButton("❌ No", callback_data="c:0"),
            ]]
        elif self.ftype == "ts":
            rows = [[InlineKeyboardButton("🕒 Use now (UTC)", callback_data="c:__now__")]]
        if self.ftype != "tf_select":
            rows.append([InlineKeyboardButton("⏭ Skip", callback_data="skip")])
        return InlineKeyboardMarkup(rows) if rows else None


def static_fields_part1():
    return [
        FieldSpec(
            "classification",
            "📊 <b>Step 1 — Trade classification</b>\nWhich timeframe are you trading?",
            "choice",
            choices=[("Scalp (M1/M5)", "SCALP"), ("Intraday (M5/M15)", "INTRADAY"), ("Swing (H1/H4)", "SWING")],
            default="SWING",
        ),
        FieldSpec("symbol", "💱 Market symbol? (e.g. XAU/USD)", "text", default="XAU/USD"),
        FieldSpec("currentPrice", "💰 Current price?", "num"),
        FieldSpec(
            "priceTimestamp",
            "🕒 Timestamp of this price (used for staleness check + kill-zone timing).\n"
            "Tap “Use now” or type UTC time as <code>YYYY-MM-DD HH:MM</code>.",
            "ts",
        ),
        FieldSpec("atr1m", "📈 ATR(1 minute), in USD — used for hold-time estimate.", "num"),
        FieldSpec("atr1h", "📈 ATR(1 hour), in USD — used for stop-loss distance.", "num"),
    ]


def tf_select_field():
    return FieldSpec(
        "tf_used",
        "🧮 <b>Step 2 — Timeframes</b>\nTap to toggle which timeframes you have indicator votes for, "
        "then press Done. (H1 + H4 + M15 are pre-selected — that's the default swing set.)",
        "tf_select",
    )


def tf_fields(tf_key: str):
    fs = [
        FieldSpec(
            f"trend-{tf_key}",
            f"📐 <b>{tf_key} — Trend vote</b> (sum of your trend indicators, range −8…+8, 0 = neutral)",
            "num",
            default=0,
        ),
        FieldSpec(
            f"mom-{tf_key}",
            f"⚡ <b>{tf_key} — Momentum vote</b> (sum of your momentum indicators, range −20…+20, 0 = neutral)",
            "num",
            default=0,
        ),
        FieldSpec(
            f"vol-{tf_key}",
            f"🌊 <b>{tf_key} — Volatility vote</b>",
            "choice",
            choices=[("-2", "-2"), ("-1", "-1"), ("0", "0"), ("1", "1"), ("2", "2")],
            default="0",
        ),
    ]
    if tf_key == "H1":
        fs.append(FieldSpec("adx-H1", "📏 ADX(14) on H1 — used to read the regime (ranging/trending).", "num"))
    return fs


def static_fields_part2():
    return [
        FieldSpec("dxyPrice", "💵 <b>Step 3 — Macro</b>\nDXY price?", "num"),
        FieldSpec("dxyChangePct", "💵 DXY 1‑day change (%)?", "num"),
        FieldSpec("y10", "🏦 US10Y yield (%)?", "num"),
        FieldSpec("y10ChangeBps", "🏦 US10Y 1‑day change (bps)?", "num"),
        FieldSpec("vix", "😬 VIX level?", "num"),
        FieldSpec("eventName", "📰 <b>Step 4 — News</b>\nNext high-impact event name? (e.g. Core CPI)", "text"),
        FieldSpec(
            "eventImpact", "📰 Impact folder?", "choice",
            choices=[("None scheduled", "None"), ("Orange folder", "Orange"), ("Red folder", "Red")],
            default="None",
        ),
        FieldSpec(
            "eventType", "📰 Event type?", "choice",
            choices=[("Standard release", "Standard"), ("NFP", "NFP"), ("FOMC", "FOMC")],
            default="Standard",
        ),
        FieldSpec("minutesToEvent", "⏱ Minutes to event? (negative = minutes since release)", "num"),
        FieldSpec("eventAffectsUsdGold", "📰 Does it directly affect USD/gold?", "bool", default=True),
        FieldSpec("speakerRisk", "🎙 Fed speaker scheduled? (flag only)", "bool", default=False),
        FieldSpec(
            "structureDirection",
            "🏗 <b>Step 5 — Market structure (ICT/SMC)</b>\nStructure direction?",
            "choice",
            choices=[("Not read yet", "None"), ("Bullish", "Bullish"), ("Bearish", "Bearish")],
            default="None",
        ),
        FieldSpec("invalidationLevel", "🏗 Invalidation level (price)?", "num"),
        FieldSpec("accountEquity", "💼 <b>Step 6 — Risk & sizing</b>\nAccount equity (USD)?", "num"),
        FieldSpec("riskPct", "💼 Risk per trade (%)?", "num", default=1),
        FieldSpec("stopLossOverride", "💼 Manual stop-loss price? (blank = auto-suggest)", "num"),
        FieldSpec("pipSize", "💼 Pip size in USD (gold default 0.01)?", "num", default=0.01),
        FieldSpec("pipValuePerLot", "💼 Pip value per lot ($/pip from your broker)?", "num", default=1),
        FieldSpec("kFactor", "💼 k-factor (hold-time calibration)?", "num", default=0.3),
        FieldSpec("tp1R", "💼 TP1 R-multiple?", "num", default=1.5),
        FieldSpec("tp2R", "💼 TP2 R-multiple?", "num", default=3),
    ]


EXAMPLE_DATA = {
    "classification": "SWING", "symbol": "XAU/USD", "currentPrice": 2378.45,
    "priceTimestamp": None,  # filled with "now" at runtime
    "atr1m": 0.85, "atr1h": 8.40,
    "tf_used": {"M5", "M15", "H1", "H4"},
    "trend-M5": 2, "mom-M5": 6, "vol-M5": 1,
    "trend-M15": 3, "mom-M15": 9, "vol-M15": 1,
    "trend-H1": 4, "mom-H1": 12, "vol-H1": 1, "adx-H1": 27,
    "trend-H4": 3, "mom-H4": 10, "vol-H4": 0,
    "dxyPrice": 101.85, "dxyChangePct": -0.32, "y10": 4.12, "y10ChangeBps": -3.5, "vix": 14.2,
    "eventName": "Core CPI (US)", "eventImpact": "Red", "eventType": "Standard",
    "minutesToEvent": 180, "eventAffectsUsdGold": True, "speakerRisk": False,
    "structureDirection": "Bullish", "invalidationLevel": 2361.20,
    "accountEquity": 10000, "riskPct": 1, "stopLossOverride": None,
    "pipSize": 0.01, "pipValuePerLot": 1, "kFactor": 0.3, "tp1R": 1.5, "tp2R": 3,
}


# --------------------------------------------------------------------------
# Core calculation — direct port of the HTML console's calc() function
# --------------------------------------------------------------------------

def clamp(n, lo, hi):
    return max(lo, min(hi, n))


def sign(n):
    return 1 if n > 0 else (-1 if n < 0 else 0)


def tf_score(trend, mom, vol):
    raw = ((trend / 8) * 0.40 + (mom / 20) * 0.45 + (vol / 2) * 0.15) * 100
    return clamp(raw, -100, 100)


def band(score):
    if score is None:
        return "N/A"
    if score <= -60:
        return "Strong Sell"
    if score <= -20:
        return "Sell"
    if score < 20:
        return "Neutral"
    if score < 60:
        return "Buy"
    return "Strong Buy"


def fmt(n, d=2):
    if n is None:
        return "—"
    return f"{n:.{d}f}"


def compute_signal(d: dict) -> dict:
    classification = d.get("classification") or "SWING"
    symbol = d.get("symbol") or "XAU/USD"
    current_price = d.get("currentPrice")
    price_ts = d.get("priceTimestamp")  # datetime (UTC) or None

    age_minutes = stale = kill_zone_active = utc_hour = None
    stale = False
    kill_zone_active = False
    if price_ts is not None:
        age_minutes = (datetime.now(timezone.utc) - price_ts).total_seconds() / 60
        threshold = STALE_THRESHOLD_MIN[classification]
        stale = age_minutes > threshold
        utc_hour = price_ts.hour
        kill_zone_active = (7 <= utc_hour < 10) or (12 <= utc_hour < 15)

    used = d.get("tf_used") or set()
    scores = {}
    for tf in TF_ORDER:
        if tf in used:
            trend = d.get(f"trend-{tf}") or 0
            mom = d.get(f"mom-{tf}") or 0
            vol = d.get(f"vol-{tf}") or 0
            scores[tf] = tf_score(trend, mom, vol)
        else:
            scores[tf] = None

    sum_w = sum_ws = 0
    for tf in TF_ORDER:
        if scores[tf] is not None:
            sum_w += TF_WEIGHT[tf]
            sum_ws += TF_WEIGHT[tf] * scores[tf]
    final_score = (sum_ws / sum_w) if sum_w > 0 else None
    score_band = band(final_score)

    adx_h1 = d.get("adx-H1")
    if adx_h1 is not None:
        regime = "RANGING" if adx_h1 < 20 else ("TRENDING" if adx_h1 >= 25 else "TRANSITIONAL")
    else:
        regime = "N/A"

    alignment_checked = True
    alignment_ok = False
    if classification in ("SWING", "INTRADAY"):
        if scores["H4"] is not None and scores["H1"] is not None:
            alignment_ok = (
                sign(scores["H4"]) == sign(scores["H1"])
                and sign(scores["H4"]) != 0
                and abs(scores["H4"]) >= 20
                and abs(scores["H1"]) >= 20
            )
    else:
        if scores["H1"] is not None:
            lead = scores["M15"] if scores["M15"] is not None else scores["M5"]
            if lead is None or sign(lead) == 0 or sign(scores["H1"]) == 0:
                alignment_ok = True
            else:
                alignment_ok = not (sign(lead) != sign(scores["H1"]) and abs(lead) >= 20 and abs(scores["H1"]) >= 20)

    dxy_change_pct = d.get("dxyChangePct")
    y10_change_bps = d.get("y10ChangeBps")
    dxy_trend = "N/A" if dxy_change_pct is None else ("Up" if dxy_change_pct > 0.15 else ("Down" if dxy_change_pct < -0.15 else "Flat"))
    yield_trend = "N/A" if y10_change_bps is None else ("Rising" if y10_change_bps > 2 else ("Falling" if y10_change_bps < -2 else "Flat"))
    tech_direction = "N/A" if final_score is None else ("Bullish" if final_score > 0 else ("Bearish" if final_score < 0 else "Neutral"))
    conflict_flag = tech_direction not in ("N/A", "Neutral") and (
        (tech_direction == "Bullish" and (dxy_trend == "Up" or yield_trend == "Rising"))
        or (tech_direction == "Bearish" and (dxy_trend == "Down" or yield_trend == "Falling"))
    )

    event_impact = d.get("eventImpact") or "None"
    affects_usd_gold = bool(d.get("eventAffectsUsdGold"))
    minutes_to_event = d.get("minutesToEvent")
    event_type = d.get("eventType") or "Standard"
    stab_window = 12 if event_type in ("NFP", "FOMC") else 3
    pre_blackout = event_impact != "None" and affects_usd_gold and minutes_to_event is not None and 0 <= minutes_to_event <= 10
    post_window = event_impact != "None" and affects_usd_gold and minutes_to_event is not None and minutes_to_event < 0 and abs(minutes_to_event) <= stab_window
    blackout_active = pre_blackout or post_window
    speaker_risk = bool(d.get("speakerRisk"))
    news_state = "BLACKOUT" if blackout_active else ("ELEVATED_RISK" if speaker_risk else "CLEAR")

    structure_direction = d.get("structureDirection") or "None"
    structure_agrees = structure_direction != "None" and final_score is not None and (
        (structure_direction == "Bullish" and final_score > 0) or (structure_direction == "Bearish" and final_score < 0)
    )
    invalidation_level = d.get("invalidationLevel")

    tp1_r = d.get("tp1R")
    tp1_r = tp1_r if tp1_r is not None else 1.5
    c1 = structure_agrees
    c2 = alignment_checked and alignment_ok
    c3 = not conflict_flag
    c4 = news_state == "CLEAR"
    c5 = kill_zone_active
    c6 = tp1_r >= 1.5
    c7 = invalidation_level is not None
    criteria = [
        ("Technical direction agrees with market structure", c1),
        ("H1/H4 multi-timeframe alignment holds", c2),
        ("Macro layer (DXY/yields) does not conflict", c3),
        ("News status is clear", c4),
        ("Setup is inside an active kill-zone window", c5),
        ("Risk:reward at TP1 ≥ 1:1.5", c6),
        ("A clear invalidation level is defined", c7),
    ]
    criteria_met = sum(1 for _, ok in criteria if ok)
    tier = "HIGH" if criteria_met >= 6 else ("MODERATE" if criteria_met >= 4 else "LOW")
    confluence_pct = round(criteria_met / 7 * 100)

    missing = []
    if current_price is None:
        missing.append("current_price")
    if price_ts is None:
        missing.append("data_timestamp")
    if "H1" not in used:
        missing.append('H1 timeframe indicator votes (mark "used")')
    if d.get("atr1h") is None:
        missing.append("atr_1h")
    if d.get("dxyPrice") is None:
        missing.append("dxy_price")
    if d.get("y10") is None:
        missing.append("us10y_yield")
    if d.get("vix") is None:
        missing.append("vix_level")
    if d.get("accountEquity") is None:
        missing.append("account_equity")
    if d.get("riskPct") is None:
        missing.append("risk_pct_per_trade")
    if stale:
        missing.append(f"data_timestamp is stale ({age_minutes:.1f}min old, max {STALE_THRESHOLD_MIN[classification]}min for {classification})")
    if sum_w == 0:
        missing.append('at least one timeframe marked "used" with votes entered')

    status, action, reason = None, "NONE", ""
    if missing:
        status = "INSUFFICIENT_DATA"
        reason = "Missing or stale: " + "; ".join(missing) + "."
    elif blackout_active:
        status = "NEWS_BLACKOUT"
        reason = "Signal generation frozen — inside the pre/post-news window for " + (d.get("eventName") or "the scheduled release") + "."
    elif final_score is None or abs(final_score) < 20:
        status = "NO_SIGNAL"
        reason = "Blended technical score is inside the Neutral band (−19…+19)."
    elif not alignment_ok:
        status = "NO_SIGNAL"
        reason = ("Lower-timeframe setup contradicts the H1 score's sign." if classification == "SCALP"
                   else "H4 and H1 scores disagree or don't both clear ±20 — the multi-timeframe gate blocks the signal.")
    elif tier == "LOW":
        status = "NO_SIGNAL"
        reason = f"Only {criteria_met}/7 confluence criteria met (LOW tier)."
    else:
        status = "SIGNAL"
        action = "BUY" if final_score > 0 else "SELL"
        reason = f"{criteria_met}/7 confluence criteria met ({tier}). {score_band} band, {regime.lower()} regime."

    direction = 1 if action == "BUY" else (-1 if action == "SELL" else 0)

    atr1h = d.get("atr1h")
    structural_buffer = 1.5 * atr1h if atr1h is not None else None
    ob_distance = abs(current_price - invalidation_level) if (current_price is not None and invalidation_level is not None) else None
    stop_dist = None
    if structural_buffer is not None or ob_distance is not None:
        stop_dist = max(structural_buffer or 0, ob_distance or 0)
    stop_override = d.get("stopLossOverride")
    stop_loss = None
    if stop_override is not None:
        stop_loss = stop_override
    elif direction != 0 and current_price is not None and stop_dist is not None:
        stop_loss = current_price - direction * stop_dist

    stop_distance_usd = abs(current_price - stop_loss) if (current_price is not None and stop_loss is not None) else None
    pip_size = d.get("pipSize") or 0.01
    stop_distance_pips = (stop_distance_usd / pip_size) if stop_distance_usd is not None else None
    pip_value_per_lot = d.get("pipValuePerLot")
    account_equity = d.get("accountEquity")
    risk_pct = d.get("riskPct")
    lots = None
    if account_equity and risk_pct and stop_distance_pips and pip_value_per_lot:
        lots = (account_equity * (risk_pct / 100)) / (stop_distance_pips * pip_value_per_lot)

    tp2_r = d.get("tp2R")
    tp2_r = tp2_r if tp2_r is not None else 3
    tp1 = tp2 = None
    if direction != 0 and current_price is not None and stop_distance_usd is not None:
        tp1 = current_price + direction * stop_distance_usd * tp1_r
        tp2 = current_price + direction * stop_distance_usd * tp2_r

    atr1m = d.get("atr1m")
    k_factor = d.get("kFactor")
    hold_time = None
    if atr1m and k_factor and tp1 is not None and current_price is not None:
        hold_time = abs(tp1 - current_price) / (atr1m * k_factor)

    bull_pct = 50 if final_score is None else round(clamp((final_score + 100) / 2, 0, 100))
    bear_pct = 100 - bull_pct

    return {
        "symbol": symbol, "classification": classification, "status": status, "action": action,
        "reason": reason, "final_score": final_score, "score_band": score_band,
        "criteria": criteria, "criteria_met": criteria_met, "tier": tier, "confluence_pct": confluence_pct,
        "bull_pct": bull_pct, "bear_pct": bear_pct, "regime": regime,
        "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2, "tp1_r": tp1_r, "tp2_r": tp2_r,
        "lots": lots, "hold_time": hold_time, "current_price": current_price,
        "missing": missing, "scores": scores, "dxy_trend": dxy_trend, "yield_trend": yield_trend,
        "conflict_flag": conflict_flag, "news_state": news_state, "structure_direction": structure_direction,
        "invalidation_level": invalidation_level, "price_ts": price_ts, "tech_direction": tech_direction,
    }


def build_json_out(d: dict, r: dict) -> dict:
    price_ts = r["price_ts"]
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "asset": r["symbol"],
        "status": r["status"],
        "missing_fields": r["missing"],
        "market_state": {
            "current_price": r["current_price"],
            "technical_score": None if r["final_score"] is None else round(r["final_score"], 1),
            "regime": r["regime"],
            "active_trend": r["tech_direction"],
        },
        "macro_context": {"dxy_trend": r["dxy_trend"], "yield_trend": r["yield_trend"], "conflict_flag": r["conflict_flag"]},
        "news_status": {"state": r["news_state"], "next_high_impact_event": d.get("eventName") or None},
        "structure_analysis": {"pattern": r["structure_direction"], "invalidation_level": r["invalidation_level"]},
        "confluence": {"criteria_met": r["criteria_met"], "criteria_total": 7, "tier": r["tier"]},
        "signal": {
            "action": r["action"] if r["status"] == "SIGNAL" else "NONE",
            "entry_price": r["current_price"] if r["status"] == "SIGNAL" else None,
            "stop_loss": r["stop_loss"] if r["status"] == "SIGNAL" else None,
            "take_profit_1": r["tp1"] if r["status"] == "SIGNAL" else None,
            "take_profit_2": r["tp2"] if r["status"] == "SIGNAL" else None,
            "risk_reward": f"1:{r['tp1_r']} (TP1) / 1:{r['tp2_r']} (TP2)" if r["status"] == "SIGNAL" else "",
            "estimated_hold_time_minutes": round(r["hold_time"]) if (r["status"] == "SIGNAL" and r["hold_time"] is not None) else None,
            "suggested_position_size_lots": round(r["lots"], 2) if (r["status"] == "SIGNAL" and r["lots"] is not None) else None,
            "reasoning": r["reason"],
        },
        "disclaimer": "Informational only. Not financial advice, not a guarantee of outcome.",
    }


STATUS_EMOJI = {"SIGNAL": "🟢", "NO_SIGNAL": "⚪️", "NEWS_BLACKOUT": "🟠", "INSUFFICIENT_DATA": "🟡"}


def format_result_message(d: dict, r: dict) -> str:
    emoji = STATUS_EMOJI.get(r["status"], "⚪️")
    action_line = f"<b>{r['action']}</b>" if r["status"] == "SIGNAL" else "—"
    lines = [
        f"{emoji} <b>{html.escape(r['symbol'])}</b> — {r['status'].replace('_', ' ')}",
        f"Action: {action_line}",
        "",
        f"Technical score: <b>{fmt(r['final_score'], 1)}</b> ({r['score_band']})",
        f"Buy/Sell split: {r['bull_pct']}% / {r['bear_pct']}%",
        f"Confluence: <b>{r['criteria_met']}/7 · {r['tier']}</b> ({r['confluence_pct']}%)",
        f"Regime: {r['regime']}",
        "",
        f"<i>{html.escape(r['reason'])}</i>",
        "",
        "<b>Confluence checklist</b>",
    ]
    for label, ok in r["criteria"]:
        lines.append(f"{'✅' if ok else '▫️'} {html.escape(label)}")

    lines.append("")
    lines.append("<b>Risk / sizing</b>")
    lines.append(f"Stop-loss: {fmt(r['stop_loss'])}")
    lines.append(f"TP1 / TP2: {fmt(r['tp1'])} / {fmt(r['tp2'])}")
    lines.append(f"Position size: {fmt(r['lots'], 2)} lots")
    lines.append(f"Est. hold time: {fmt(r['hold_time'], 0)} min")

    if r["missing"]:
        lines.append("")
        lines.append("<b>⚠️ Missing / stale inputs</b>")
        for m in r["missing"]:
            lines.append(f"• {html.escape(m)}")

    lines.append("")
    lines.append("<i>Informational read-out only — not financial advice. Confirm independently.</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Conversation plumbing
# --------------------------------------------------------------------------

def build_queue():
    return list(static_fields_part1()) + [tf_select_field()]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["answers"] = {}
    context.user_data["queue"] = build_queue()
    context.user_data["idx"] = 0
    context.user_data["tf_selected"] = set(DEFAULT_USED_TF)
    await update.message.reply_text(
        "🥇 <b>XAU/USD Analysis Console</b>\n"
        "I'll ask a series of quick questions (same as the web console) and then give you a "
        "structured technical/macro read-out. Every question has a Skip button.\n\n"
        "Type /cancel anytime to stop, or /example to see a filled-in demo instead.",
        parse_mode="HTML",
    )
    await ask_current(update, context)
    return COLLECT


async def ask_current(update: Update, context: ContextTypes.DEFAULT_TYPE):
    queue = context.user_data["queue"]
    idx = context.user_data["idx"]
    if idx >= len(queue):
        return await finalize(update, context)

    spec = queue[idx]
    chat = update.effective_chat

    if spec.ftype == "tf_select":
        await send_tf_select(chat, context)
        return

    text = spec.prompt
    if spec.default is not None:
        text += f"\n<i>(Skip → default {spec.default})</i>"
    await chat.send_message(text, parse_mode="HTML", reply_markup=spec.keyboard())


TF_LABELS = {"M1": "M1", "M5": "M5", "M15": "M15", "H1": "H1", "H4": "H4"}


def tf_select_keyboard(selected: set):
    row = []
    for tf in TF_ORDER:
        mark = "✅ " if tf in selected else "▫️ "
        row.append(InlineKeyboardButton(mark + TF_LABELS[tf], callback_data=f"tf:{tf}"))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("➡️ Done", callback_data="tf:done")]])


async def send_tf_select(chat, context):
    spec = context.user_data["queue"][context.user_data["idx"]]
    await chat.send_message(spec.prompt, parse_mode="HTML", reply_markup=tf_select_keyboard(context.user_data["tf_selected"]))


async def advance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["idx"] += 1
    return await ask_current(update, context)


def store_answer(context, key, value):
    context.user_data["answers"][key] = value


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    queue = context.user_data.get("queue")
    if queue is None:
        await q.edit_message_text("Session expired — send /start to begin again.")
        return ConversationHandler.END

    idx = context.user_data["idx"]
    spec = queue[idx]
    data = q.data

    if spec.ftype == "tf_select":
        if data == "tf:done":
            selected = context.user_data["tf_selected"]
            answers = context.user_data["answers"]
            answers["tf_used"] = set(selected)
            new_fields = []
            for tf in TF_ORDER:
                if tf in selected:
                    new_fields.extend(tf_fields(tf))
            queue[idx + 1:idx + 1] = new_fields + static_fields_part2()
            await q.edit_message_text(f"Timeframes selected: {', '.join(sorted(selected, key=TF_ORDER.index)) or 'none'}")
            return await advance(update, context)
        else:
            tf = data.split(":", 1)[1]
            sel = context.user_data["tf_selected"]
            if tf in sel:
                sel.discard(tf)
            else:
                sel.add(tf)
            await q.edit_message_reply_markup(reply_markup=tf_select_keyboard(sel))
            return COLLECT

    if data == "skip":
        store_answer(context, spec.key, spec.default)
        await q.edit_message_text(f"{strip_html(spec.prompt.splitlines()[0])} → skipped")
        return await advance(update, context)

    if data.startswith("c:"):
        raw = data[2:]
        if spec.ftype == "bool":
            value = raw == "1"
        elif spec.ftype == "ts" and raw == "__now__":
            value = datetime.now(timezone.utc)
        elif spec.ftype == "choice":
            # try to keep numeric choices numeric (volatility votes)
            value = raw
        else:
            value = raw
        store_answer(context, spec.key, value)
        await q.edit_message_text(f"{strip_html(spec.prompt.splitlines()[0])} → {raw if spec.ftype != 'ts' else 'now'}")
        return await advance(update, context)

    return COLLECT


def strip_html(s: str) -> str:
    import re
    return re.sub("<[^<]+?>", "", s)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    queue = context.user_data.get("queue")
    if queue is None:
        await update.message.reply_text("Send /start to begin an analysis.")
        return ConversationHandler.END
    idx = context.user_data["idx"]
    if idx >= len(queue):
        return await finalize(update, context)
    spec = queue[idx]
    raw = update.message.text.strip()

    if spec.ftype == "num":
        try:
            value = float(raw)
        except ValueError:
            await update.message.reply_text("That doesn't look like a number — try again, or tap Skip above.")
            return COLLECT
        store_answer(context, spec.key, value)
        return await advance(update, context)

    if spec.ftype == "text":
        store_answer(context, spec.key, raw)
        return await advance(update, context)

    if spec.ftype == "ts":
        for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
            try:
                value = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
                store_answer(context, spec.key, value)
                return await advance(update, context)
            except ValueError:
                continue
        await update.message.reply_text("Please use format YYYY-MM-DD HH:MM (UTC), or tap “Use now”.")
        return COLLECT

    await update.message.reply_text("Please use the buttons above for this question.")
    return COLLECT


def normalize_answers(answers: dict) -> dict:
    d = dict(answers)
    for key in list(d.keys()):
        if key.startswith("vol-") and d[key] is not None:
            d[key] = float(d[key])
    if "eventAffectsUsdGold" not in d:
        d["eventAffectsUsdGold"] = True
    if "eventImpact" not in d:
        d["eventImpact"] = "None"
    if "eventType" not in d:
        d["eventType"] = "Standard"
    if "structureDirection" not in d:
        d["structureDirection"] = "None"
    return d


async def finalize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = normalize_answers(context.user_data.get("answers", {}))
    r = compute_signal(d)
    msg = format_result_message(d, r)
    out = build_json_out(d, r)
    import json as _json
    json_text = _json.dumps(out, indent=2, default=str)

    chat = update.effective_chat
    await chat.send_message(msg, parse_mode="HTML")
    if len(json_text) < 3500:
        await chat.send_message(f"<b>Structured JSON output</b>\n<pre>{html.escape(json_text)}</pre>", parse_mode="HTML")
    else:
        await chat.send_message("<b>Structured JSON output</b> (truncated):\n<pre>" + html.escape(json_text[:3400]) + "…</pre>", parse_mode="HTML")

    context.user_data.clear()
    return ConversationHandler.END


async def example(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = dict(EXAMPLE_DATA)
    d["priceTimestamp"] = datetime.now(timezone.utc)
    r = compute_signal(d)
    await update.message.reply_text("📋 Running the built-in example dataset…")
    msg = format_result_message(d, r)
    out = build_json_out(d, r)
    import json as _json
    json_text = _json.dumps(out, indent=2, default=str)
    await update.message.reply_text(msg, parse_mode="HTML")
    await update.message.reply_text(f"<b>Structured JSON output</b>\n<pre>{html.escape(json_text)}</pre>", parse_mode="HTML")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Send /start to begin again.")
    return ConversationHandler.END


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN environment variable first.")

    app = ApplicationBuilder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            COLLECT: [
                CallbackQueryHandler(on_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("example", example))

    log.info("Bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
