from datetime import datetime
from zoneinfo import ZoneInfo
from parser import analyze_post, parse_post
T = datetime(2026, 10, 30, 10, 0, tzinfo=ZoneInfo("America/New_York"))
CASES = [
    ("QQQ 694P 0DTE @1.14", ("QQQ", "P", 694, "0DTE", 1.14)),
    ("$QQQ 694P 1.14 LIGHT SIZE LOTTO", ("QQQ", "P", 694, "0DTE?", 1.14)),
    ("$SPY 758 puts 0dte 1.09", ("SPY", "P", 758, "0DTE", 1.09)),
    ("$SPY 758p 1.09 LOTTO Friday", ("SPY", "P", 758, "0DTE?", 1.09)),
    ("SPX 7570C", ("SPX", "C", 7570, "0DTE?", None)),
    ("AMZN 230P @.80 exp Monday", ("AMZN", "P", 230, "Mon 11/02", 0.80)),
    ("$AMZN 230p .80$ added small exp Monday", ("AMZN", "P", 230, "Mon 11/02", 0.80)),
    ("$JNJ 277.5p", ("JNJ", "P", 277.5, "0DTE?", None)),
    ("$HOOD 76P @.17", ("HOOD", "P", 76, "0DTE?", 0.17)),
    ("$HOOD 76P ODTE @.17", ("HOOD", "P", 76, "0DTE", 0.17)),
    ("$SPX 0dte C7720 @.75", ("SPX", "C", 7720, "0DTE", 0.75)),
    ("$768 put spy 0dte @.89", ("SPY", "P", 768, "0DTE", 0.89)),
    ("$SPY 11/5 770c @ 1.80 NEW AVG IN TO SWING", ("SPY", "C", 770, "11/05", 1.80)),
    ("$UPS 30 OCT 26 $110 CALL $0.07 GAP FILL", ("UPS", "C", 110, "10/30", 0.07)),
    ("Today's free #0DTE — 9:41 AM ET  $QQQ CALL 695 · entry mid $3.98", ("QQQ", "C", 695, "0DTE", 3.98)),
    ("Adding more $INTC 0dte $68 C .30", ("INTC", "C", 68, "0DTE", 0.30)),
    ("$HOOD November 6th 86C @ 2.10", ("HOOD", "C", 86, "11/06", 2.10)),
    ("ADDING HEAVY\n\n$TBBK\n20 NOV 26 $50\n$1.15", ("TBBK", "C", 50, "11/20", 1.15)),
    ("SMALL ACCOUNT CHALLENGE\n$TBBK\n20 NOV 26 $50\n$1.10", ("TBBK", "C", 50, "11/20", 1.10)),
    ("$SPY  09 NOV 26 $716 PUT  $1.15  high risk", ("SPY", "P", 716, "11/09", 1.15)),
    # Prefer IBIT next to the strike; ignore later $BTC commentary; ".16 entry" past filler words
    ("Trade Alert IBIT $34C 11/24 Day Trade incase $BTC wants to wakeup .16 entry  @TraderGrader",
     ("IBIT", "C", 34, "11/24", 0.16)),
    # TP update: ignore stock close / TP levels; take "from the 12.70 entry"
    ("TP1 HIT: $TSM 11/27 360P   5m close at 347.66, through TP1 349.26. Option at 16.25, +28.0% from the 12.70 entry",
     ("TSM", "P", 360, "11/27", 12.70)),
]
NEG = ["$TSLA 395C 10/30 | $1.18 → $4.35 | +269%", "EXIT ALERT $SPX $7785C 10/30/26 @ $0.20", "GM! Happy Friday $SPY $QQQ",
       "1,700 stocks are down 2% or more today", "$AIOK update; $31.09 NHOD",
       "Weekly Recap: $SPY 763P | 1,050% $SPY 770C | 600%",
       "in the past WEEK I gave YOU ALL over 970% in GAINS $AMD $570 CALL FOR 691%",
       "HIGHLIGHT TRADES JUST FROM THIS WEEK $LMT $635 30 OCT 26 CALL with ENTRY AND EXIT",
       "$XSP 760p 0.35  3.40 871%+",
       "$3.9K PROFIT IN 5 MINUTES TRADING SPX 0DTE! Once SPX held above my 7655 call trigger, I entered 7705C . I locked in",
       "#lottofriday Ideas so Far Today:\n71% Win ratio today\n\nMU 780c 1351%\nNBIS 118c 446%\nMETA 645c 334%\n\nFailed:\nSNDK\nMETA"]
FLAGGED = [
    ("$RKLB looks like an insane long-term opportunity. The estimates shown put 2026 revenue around $940M.", "no-strike"),
    ("$HOOD Trade Idea: November 6th 86C. Trigger: 85.28. This means we have a setup if price can defend and the dip gets bought. Below it is hands off for long.", "conditional"),
    ("$LLY Trade Idea: November 6th 1455C / 1400P. Bullish trigger 1449. Bearish trigger 1408. two plays, long/short.", "conditional"),
    ("A+ $SPY SETUP: Over 758, 760c any exp will work. Below 750, 750p any exp will work.", "conditional"),
    ("$SPX November 6th 7560P. Exactly where we closed. Above it, longs to 7600 are scalpable and below can fade.", "conditional"),
    ("$MU November 6th 850P. Ready for upside as well. If it fails to the downside, 858 is the line.", "conditional"),
    ("$MU Trade Idea: November 6th 850P. The weekly chart is consolidating.", "potential"),
    ("$TWLO Trade Idea: November 6th 210C. Trigger: 206.91 Target: 215.79 Stop: 202. If you see a first move down tomorrow, wait one more day.", "potential"),
    ("$TSLA Trade Idea: Nov 6 410C. Trigger: 400. Targets: 410, 414. Stop: 395. Calls can work above 400 this week.", "potential"),
    ("$QQQ Trade Idea: Nov 5 700C. Trigger: 693. Target: 700. Stop: 690. If QQQ can hold 685 and reclaim 700 a bottom should form.", "potential"),
]
ok = 0
for txt, exp in CASES:
    r = parse_post(txt, T)
    got = (r[0].ticker, r[0].opt_type, r[0].strike, r[0].expiry_label, r[0].entry) if r else None
    good = got == exp
    ok += good
    print("OK " if good else "BAD", txt, "->", got)
for txt in NEG:
    live, notes = analyze_post(txt, T)
    good = not live and not notes
    ok += good
    print("OK " if good else "BAD", "(drop)", txt, live and live[0].key, [(n.reason, n.detail) for n in notes])
for txt, reason in FLAGGED:
    live, notes = analyze_post(txt, T)
    good = (not live) and any(n.reason == reason for n in notes)
    ok += good
    print("OK " if good else "BAD", "(flag)", reason, txt, [(n.reason, n.detail) for n in notes])
# "4 put prints" must not become strike 4
flow_txt = "Someone just added another $12M+ of $MU puts  4 put prints in 39 minutes, led by a $6.9M March 2027 1130 put"
flow_live, flow_notes = analyze_post(flow_txt, T)
flow_ok = (not any(c.strike == 4 for c in flow_live)) and any("print" in (n.detail or "") for n in flow_notes)
ok += flow_ok
print("OK " if flow_ok else "BAD", "(no strike-4 from put prints)", [(c.strike, c.expiry_label) for c in flow_live], [(n.reason, n.detail) for n in flow_notes])
print(f"{ok}/{len(CASES)+len(NEG)+len(FLAGGED)+1} passed")
