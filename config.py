"""
TipBot configuration.
Loads secrets from .env and defines constants for tipster channels,
team mappings, stat mappings, and staking rules.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Version ──────────────────────────────────────────────────────────
# Bump on every meaningful change. Logged + Telegram'd at startup so it's
# always clear which build is running. The code fingerprint computed in
# main.py (_code_fingerprint) complements this — it catches partial/stale
# deploys even when this string wasn't bumped.
TIPBOT_VERSION = "v5.79 (2026-06-20: REROUTE OVERS-ONLY REVERT (Wilson). The v5.78 'all AFL disposals fan-out, over+under' redistribute reroute is reverted to OVERS ONLY (still any tipster — Saiyan or Eddie). Rationale: the disposals UNDER per-account cap is [124,99,74,50], FAR below the OVER/threshold cap [300,250,200,150] -- so the accounts that already placed an UNDER have almost no headroom to absorb a reroute (rebetting the leftover just re-rejects). Overs have the headroom; only overs reroute. An UNDER that doesn't fill first time is left as-is -> manual ('if it's passed the first reroute it is what it is'). `_is_afl_disposals_fanout` -> `_is_afl_disposals_over` (over-detection gate restored: selection over / endswith over / _is_threshold, never under). Money-safety unchanged. +tests (gate flipped to overs-only both tipsters). Requires main.py restart. --- v5.78 (2026-06-20: follow-on to v5.77 (Wilson). (1) Daniel 68723 AFL non-disposals caps lowered 400->300 + goals 1000->600 to MIRROR Wilson/Adam/Ryan -> ALL 4 SB sports accounts (+ Alex 65463) now FULLY equal-cap on AFL (completes the v5.77 parity; Daniel was the last 400/1000 outlier). (2) REDISTRIBUTE-TO-SUCCESSFUL reroute WIDENED from 'Eddie disposals OVERS only' to ALL AFL DISPOSALS fan-out -- both tipsters (Saiyan + Eddie) and BOTH directions (over + under): whatever the first fan-out round leaves UNFILLED (an account failed on low balance / suspended / proxy, or laddered down) gets ONE reroute onto the accounts that PLACED (even-split of the remainder, 100/90/80/70% ladder, stop on reject); if the reroute can't fill it either it's manual ('if it's passed the first reroute it is what it is'). Money-safe unchanged: capped at each account's 1/n share (no over-stake), reuses _fanout_place_account (ambiguous->stop, no double-stake), ONE pass. _is_eddie_disposals_over -> _is_afl_disposals_fanout; _afl_overs_redistribute_topup -> _afl_redistribute_topup; EDDIE_OVERS_REDISTRIBUTE -> AFL_DISPOSALS_REDISTRIBUTE. (3) 11pm DAILY REVIEW OVERHAULED (DAILY_REVIEW_PROMPT.md) to autonomously catch the AVOIDABLE-miss classes that slipped past prior reviews on 06-19 -- now 5 opus subagents at XHIGH effort with an explicit AVOIDABLE-MISS CHECKLIST: (A) surname->manual that was a vision TYPO of a roster player or a resolvable same-surname collision; (B) lopsided/stale fan-out split + failed-account stake that should reroute; (C) unfilled with an OPAQUE reason or placeable sessions skipped by do-not-bet config; (D) PER-ACCOUNT repeated-failure patterns (same error >=2x = systemic: code-540 suspended / 508 low-balance / TAB intercept / 403 proxy) -> config/ops fix; (E) misleading HB error labels. The review now SECOND-GUESSES every non-fill (BY-DESIGN vs AVOIDABLE vs GENUINE-FAULT) instead of accepting 'placed-or-alerted'. Ops/tooling (prompt) -- not a live-bot code change; (1)+(2) require main.py restart. +tests updated (gate widened to Saiyan+both-directions; base fan-out test pins redistribute OFF). --- v5.77 (2026-06-20: MULTI-FIX from the Wilson 06-19 review (reviewed by 5 independent opus subagents -- no over/double-stake, no wrong-player). (1) CAPS: Adam 65465 AFL non-disposals caps lowered 400->300 + goals 1000->600 to MIRROR Wilson 53522 -> the 4 SB sports accounts are equal-cap (disposals ladders were already identical). Daniel 68723 still 400/1000 -- FLAGGED to Wilson, not auto-changed. (2) MLB HRRBI SGM 4-way -> 3-WAY (Adam 65465/Wilson 53522/Daniel 68723 = $400/3 ~$133 ea); Ryan 102506 DROPPED from the SGM split (Sportsbet 'outcome is suspended code=540' on EVERY Ryan HRRBI SGM, 06-18 + 06-19); unfilled now spills to Alex 65463 THEN Ryan 102506 as 2+ HRRBI SINGLES (both SGM-disabled; Ryan given a player_stats ladder to mirror Alex). (3) NBA SGM drops Ryan -> Adam/Wilson/Daniel (matches AFL SGM). (4) Eddie bare-surname FUZZY fallback (difflib SequenceMatcher >=0.85, SCOPED to the in-play game roster, resolve only if a SINGLE player matches else manual) when the EXACT match misses a VISION TYPO -- 06-19 'D'Ambrossio' -> Massimo D'Ambrosio (scores 0.947 so 0.95 would MISS it; <4-char tokens skip). (5) Eddie surname COLLISION odds tie-break now resolves to the in-range candidate CLOSEST to the tipped odds (was: require EVERY candidate priced AND exactly-one-in-range -> bailed to manual when a same-surname sibling had no market; 06-19 Noah Anderson o28.5 disposals priced while defender Cody Anderson had none); equidistant top-2 -> manual; the 20% odds tol still gates eligibility. (6) Eddie BIG-bet (>2.5u) AFL fan-out split is now EVEN (intended/n + 10% decay ladder) -- retired the '$150 floor on the limited accounts + dump the remainder on the single highest-cap account' shape (06-19 Uwland concentrated $600 on Adam), obsolete now caps are equal. (7) Eddie AFL DISPOSALS-OVER redistribute-to-successful TOP-UP: when the fan-out leaves stake unfilled (an account failed -- e.g. Alex low balance -- or laddered down) AND others placed, re-split the WHOLE unfilled remainder EVENLY across the accounts that PLACED and top each up (100/90/80/70% ladder, stop on reject). Capped at each account's 1/n share (CANNOT exceed the unit) + reuses _fanout_place_account (ambiguous->stop, treated as committed = NO double-stake) + ONE pass. Disposals-OVER singles ONLY (unders/SGMs excluded -- harder liability). Gated EDDIE_OVERS_REDISTRIBUTE (kill-switch). (8) Racing UNFILLED alert now surfaces the in-range sessions that were SKIPPED (do-not-bet) + a below-floor count, instead of a blank '(no failure details)' (06-19 Minor Catastrophe: 13/18 below floor + 4 placeable accounts do-not-bet at Gloucester Park -> $1009 manual, unexplained). (9) Eddie image-channel TEXT gate: REMOVED the soft betting-VOCABULARY keywords (odds/tip/tips/back/lay/selection(s)/runner(s)/to win/the place/add) that pinged manual on CHATTER ('Just running the odds for the last bets', 'goal scorer pick ~8.30 AM', 'would've picked X last night'); TIME-guarded the decimal-odds pattern (8.30 am != odds); ADDED X+ ('25+ disposals') + over/under-LINE ('o30'/'over 30'/'under 23.5') selection patterns so a real text tip still pings -- instructions (scratch/cancel/late mail) + bet types (multi/sgm/...) kept. NOTE on (B): the HB 'sgm_bet_failed_unknown (code=508)' on a SINGLE = HyperBot's literal mislabel of an insufficient-balance reject (raise with HB). Requires main.py restart. +test_v577_disposals_overs_and_fuzzy, updated 5 existing tests. --- v5.76 (2026-06-19: AUDIT.JSONL TEST-POLLUTION LEAK CLOSED (Wilson 06-18 daily-review Issue 3). The v5.43 fix redirected ONLY _audit_tip() to a temp file under TIPBOT_TESTING, but the placement-path `tip_outcome` writers (7 `_log_jsonl(AUDIT_LOG, ...)` sites incl. the MLB/AFL SGM + fan-out outcomes) still hard-coded the production logs/audit.jsonl -> the unit suite leaked test rows (session_id 'x'/'C', bet_id null, empty event) into the real audit on EVERY run (~30 in the 06-18 ~01:29-01:51 deploy window; ~129 cumulatively; made the 06-18 audit mis-sum $9505/$2000-ambiguous vs the real $3285/$0). Fix: one call-time resolver `_audit_log_path()` (temp under TIPBOT_TESTING, else AUDIT_LOG) that EVERY audit writer now routes through -- _audit_tip refactored onto it too. Production path UNCHANGED (only diverts under TIPBOT_TESTING). Verified by an opus subagent: all 9 audit writers redirected, no other module writes audit.jsonl, the new test FAILS on a simulated revert. +3 assertions in test_audit_tip_test_isolation (resolver returns temp + a raw tip_outcome placement-write doesn't grow production). NOTE: the ~129 historical pollution rows already in logs/audit.jsonl are NOT auto-stripped (left for a manual filtered clean, like the v5.41 bet-ledger cutover). No production behaviour change (test-only redirect); main.py restart loads it. --- v5.75 (2026-06-18: EDDIE IMAGE WINNING-MARGIN -> PLACEABLE LINE (Wilson 06-17 review). Eddie's multi-bet AFL image was FULLY extracted (Groq got all 5 bets), but the 'Adelaide 40+' winning-margin leg was MISPARSED as 'Adelaide over 0.5 GOALS @ 4.50' (player_prop) -> goalscorer_threshold line=0.5 not carried -> manual; and even a correctly-typed `margin` market force-routed to manual (no placement path). Fix: (1) IMAGE_PROMPT_AFL now disambiguates a TEAM directly followed by 'N+' at longer odds ($3-$15) as a WINNING-MARGIN bet (market_type=margin, line=N, side=over) and NEVER a goals/player prop -- only a PLAYER name carries a goals/disposals prop. (2) _build_afl_tip_from_image CONVERTS margin -> a placeable -(N-0.5) LINE bet (Adelaide 40+ -> Adelaide -39.5; Wilson: '40+ winning = -39.5 alt line/handicap') so the bet is ATTEMPTED via the handicap matcher (±0.5 catalog match; a bookie miss still -> manual, never a blind/wrong line). The OTHER 'missed' bet (GWS u184.5) was NOT a bug: totals already snap within ±1.0 (_match_total_in_catalog) + a ±0.5 alt-line fallback, but u184.5 was 2.0 off SB's only line (u182.5) and u182.5 is a WORSE under -> correctly routed to manual with a BET UNFILLED alert (Eddie's own note: take u183.5 at Lads). Both misses DID fire manual alerts -- nothing was silently dropped. +3 margin regression assertions (test_image_afl_team_markets flipped from 'margin still manual'). Requires main.py restart. --- v5.74 (2026-06-17: MLB HRRBI 4-WAY EVEN SPLIT + %-LADDER (Wilson). The Shook 1+/2+ HRRBI multi now EVEN-SPLITS the $400 into 4 x $100 placed CONCURRENTLY across FOUR SGM accounts -- added Ryan 102506 (who can do multis + is now SB-limited) alongside Adam 65465 / Wilson 53522 / Daniel 68723 (was 3). Each account ladders DOWN 100/90/85/80% of its $100 stake on a bookie reject (MLB_HRRBI_LADDER_PCT; replaces the mlb.sgm liability bracket ladder for MLB only -- gated `if sport==mlb` in _place_sgm_fanout, AFL SGM unchanged). Any remainder UNFILLED after the 4-way SGM still SPILLS to Alex 65463 as a 2+ HRRBI SINGLE (kept as the spillover backstop). Config: MLB_SGM_SESSION_PRIORITY += 102506; MLB_HRRBI_SINGLE_SESSIONS stays 65463; new MLB_HRRBI_LADDER_PCT=1.0,0.9,0.85,0.8. Verified: 4x$100 when accepted, 4x$90 when $100 rejected ($40 -> Alex). Requires main.py restart. +1 test; updated the mlb.sgm priority test to 4 accounts. --- v5.73 (2026-06-17: RYAN SB SPORTS CAPS -> MIRROR WILSON 53522 (Wilson). Ryan Suwandy sportsbet 102506 is now bookie-LIMITED too, so its sports liability caps (nba/afl/mlb) were changed from the original 4.5x un-limited values (added v5.63/v5.65) to MIRROR Wilson 53522's standard limited Sportsbet caps -- keep 102506 in sync with 53522. EFFECT: the AFL singles fan-out capacity-weighted split (v5.65/66) now treats Ryan as an EQUAL-cap account -> EVEN split across the 5 sportsbet accounts instead of Ryan carrying the 4.5:1 share. sessions.yaml only; requires main.py restart to load. The v5.65/66 fan-out tests were rewritten to verify the weighting MATH with synthetic caps (so they no longer depend on a live account's caps) + assert the new live parity. --- v5.72 (2026-06-16: MLB ROSTER TEAM-OVERRIDE (Wilson; Matt Olson 06-16). The MLB resolve branch only inferred a team from the roster when NO team was stated ('if not team'), so a Groq-HALLUCINATED team (context bleed: a teamless 'Matt Olson 2+ HRRBI' inherited 'MIL' from the prior Brice Turang/MIL play + a standalone 'MIL' header in the 4-msg context) was trusted -> resolve_mlb_event('MIL') resolved the WRONG game (Milwaukee Brewers v Cleveland Guardians; Olson is an Atlanta Brave) -> Olson absent from that catalog -> NOT matched -> manual, $400 unfilled. Fix: the roster is now AUTHORITATIVE -- resolve_event runs roster.exact_match_player(player,'mlb') even when a team IS stated and OVERRIDES it on an exact full-name disagreement (mirrors the NBA team-override), turning 'MIL' -> 'Atlanta Braves'. EXACT full-name only (no fuzzy/bare-surname, the v5.33 hardening kept) + roster_mlb.json refreshed daily from the MLB Stats API (fresher than Groq), so it can only move a bet from the wrong game to the roster-correct game, never a wrong player; a typo/no-match falls back to the stated team (status quo). Plus an MLB-scoped Groq SYSTEM_PROMPT guard: do NOT copy a team from a different play/header in RECENT CONTEXT onto a later teamless player. +2 regression tests (and updated the v5.33 'stated team never overridden' test to the v5.72 override). --- v5.71 (2026-06-16: NOTIFIER READ-TIMEOUT DEDUP (Wilson). The v5.56 one-retry in notifier._send retried on ANY exception incl. requests.ReadTimeout -- but a READ timeout means the POST was TRANSMITTED and Telegram very likely already posted the message (only the HTTP response didn't return in time), so the retry re-POSTs the identical message and DELIVERS A SECOND COPY while the bot logs NOTIFY LOST. Caused the 06-16 ~19:53 Turang BET PLACED + Olson MANUAL double-sends during a Telegram slowness blip (placement itself was fine -- 4 distinct bet_ids, no double-bet). Fix: _send now (1) does NOT retry a ReadTimeout (logs 'DELIVERY-UNCERTAIN (read timeout)' instead of NOTIFY LOST -- the bot genuinely can't know if it landed); connect-level failures (ConnectTimeout/ConnectionError/DNS/refused = provably not sent) still get the one retry; (2) uses a (connect=5s, read=20s) timeout tuple so a slow-but-successful Telegram response is received rather than aborted at the old flat 10s. ReadTimeout imported at module level so the except resolves under a mocked requests in tests. +1 regression test. --- v5.70 (2026-06-15: BUG-HUNT ROUND 2 (9 fixes, 8 were REGRESSIONS from v5.69; report BUGHUNT_2026-06-15.md 'Round 2'). A second 12-agent pass (6 reviewing the v5.69 diff + 6 tail-hunters, adversarial-verified) found 18 confirmed, 13 regressions. Fixed: (R2#1, the worst) m11/m12 image conflict-key now NORMALISES period -- adding raw vision `period` to the key let over+under of the SAME full-game market escape the guard (one row 'full', the other null) and auto-place BOTH sides; period collapses to a full-game bucket + stat normalised. (R2#2/#6) M8 NBA disambiguation: a CONFIDENT full-name tip now resolves the top playable instead of false-manual when a similar same-surname sibling within the floor also plays; a BARE surname prefers the actual SURNAME match over a first-name collision ('Grant'->Jerami Grant not 'Grant Williams'). (R2#8) m16 now CLEARS a stale _is_threshold on an 'under' leg (was only skipping NEW promotion -> a model-contradictory {under, is_threshold:true} leg still placed as the over side). (R2#3) m18 0-tip ping suppression gated on a CONCRETE selection (_image_text_selection_pattern/urgent) not the broad actionable keywords ('tips'/'odds'/'selections' appear in real recaps -> re-spam). (R2#5/#7) m9 regex-tipster parse-fail alert now requires a unit/@-odds token AND not-no-bet-framing (was alerting on no-bet/recap chatter). (R2#17) i4 no-bet-framing regex tightened -- the {0,3}-word filler matched 'no bets ON X today, but <real bet>' and suppressed the real bet. (R2#10/#11) M3 bookie-alias added to the v4 SINGLES reconcile too (was only fan-out + SGM). (R2#13-15) m4 racing reconcile-placed now calls _pace_record_placed. (R2#4) bet_record_feed carries forward a settled Result that ages past the 14d window instead of reverting to blank. +autostart deprecation note. TAB 100003 skip-list deferred (needs 403 confirmation). +7 regression tests. --- v5.69 (2026-06-15: FULL-REPO BUG-HUNT (31 fixes; 10-agent end-to-end audit + adversarial verify, report BUGHUNT_2026-06-15.md). MAJORS: (M6) place_tip alert_only short-circuit made UNCONDITIONAL -- it was `alert_only and not is_sgm`, so a no-unit/no-bet AusBets/Kev msg promoted to an NBA SGM auto-placed past the v5.52 money-gate; (M3) reconcile now passes the BOOKIE-aliased event (_bookie_event) into decide_ambiguous in _reconcile_fanout_ambiguous + _place_sgm_v4 -- tip.event (Squiggle 'Greater Western Sydney') never matched pending_bets ('GWS Giants') so a landed AFL bet read as not-found -> manual re-bet (double); (M1) reconcile-CONFIRMED-placed now converts to a real SUCCESS (ledger/summary row) in the SEQUENTIAL v4 singles + _place_sgm_v4 paths (the v5.55 fan-out fix was never back-ported -> debited-but-never-ledgered + false 'MAY have placed' critical); (M2) SGM boost->no-boost retry now SKIPS when the boost attempt was slow/ambiguous (maybe-landed) -- closed an Erasmus double-stake hole (latent, boost off) in _place_sgm_v4 + legacy _place_sgm; (M8) NBA teamless disambiguation rebuilt -- generates candidates from the ORIGINAL bare surname (not the resolve_player_name-collapsed name, which biased scores) and routes 2+ same-surname players-both-playing-within-floor to MANUAL instead of an arbitrary first-pick; (M7) kev/ausbets regex parsers RESET _last_player/_last_stat per message (cross-message carry-forward leaked a previous game's player onto a follow-up tip); (M5) racing bookies missing from mbl_caps.yaml no longer place UNCAPPED -- added neds/hotbet/palmerbet/betright + _resolve_liability_cap fail-safe to a conservative country MBL; (M4) racing dedup _release_bet uses the SNAPSHOTTED claim fp so place_racing_tip's undated date-mutation can't leak a claim + suppress a valid re-send. MINORS: m4 racing reconcile-placed -> result['placed'] (ledger); m5 SGM fan-out splits over PLACEABLE accounts only (no permanent under-fill); m6 AFL fan-out OVER->threshold market flip moved BEFORE the weighted split; m7 h2h price-change retry flags a below-floor fill to Maintenance; m8 auto-alt restores leg.line on success; m9 regex-tipster parse-fail now alerts; m10 Shook 30s cooldown is content-aware (distinct prop within 30s no longer dropped); m11/m12 AFL image conflict key includes stat + resolves totals on event; m13 AFL SGM line match is exact-only (no silent 30+->29+ snap); m14 _find_prop_id direction match is exact (no 'Grover'-contains-'over' leak); m16 integer 'under' SGM leg no longer flipped to an over threshold; m17 v4 alt-chain sizes on the ALT leg's market/no-odds; m18 0-tip image summary-suppression skipped when the caption is actionable; m2 tip_parser Claude path got the v5.23 stat-guard + units_explicit; m1 replay_image_parse tuple unpack. INFO: i2 MLB HRRBI writes ONE consolidated audit row (incl Alex); i3 get_transactions retries the idempotent read; i4 no-bet-framing regex relaxed + applied to all UNITS_REQUIRED_TIPSTERS; i5 bet_ledger dedup seeds from CSV (survives restart); i1 _readme stamp. +13 regression tests. --- v5.68 (2026-06-15: FAN-OUT ONE-RETRY ON TRANSIENT PRE-PLACEMENT REJECT (Wilson). A proxy 403 / auth / network reject in the AFL singles + SGM fan-out means the bet was NEVER submitted to the bookie (gated by _is_definitely_pre_placement -- the narrow provably-not-placed set: forbidden/403/401/400/bad request/stale_command), so it now RETRIES the SAME rung ONCE (AFL_FANOUT_RETRY_DELAY_SEC=2s) before abandoning -- ZERO double-stake risk. Evidence: Alex 65463's Bailey $150 on 06-14 was a one-off proxy 403 that placed fine 12 min later (Alex went 7/9 on the day, placing 4 MORE bets after the 403) -> a retry would have filled it instead of dropping to Manual. Stake-rejects still ladder DOWN; AMBIGUOUS/maybe-landed still STOPS and is NEVER retried (Erasmus safety). Gated AFL_FANOUT_PREPLACEMENT_RETRY (default true). --- v5.67 (2026-06-15: EDDIE IMAGE PARSE FIXES (Wilson 06-14 day-review). 1) VISION MULTI-BET: the AFL image prompt (IMAGE_PROMPT_AFL) said 'Most images contain exactly ONE bet' -> Groq UNDER-extracted Eddie's multi-bet images (Ashcroft o26.5 3u + 30+ 0.5u -> ONE merged tip with the 26.5 line but 0.5u units, 30+ dropped; Greene 2+/3+/4+ -> 1 tip). Prompt rewritten: extract EVERY selection as its OWN tip, and the SAME player on multiple lines/thresholds -> a SEPARATE tip per line, units/odds read from THAT line only, NEVER merge. Validate live via replay_image_parse.py against the real images. 2) EDDIE SURNAME TIER-2 FALLBACK: _resolve_eddie_surname_to_player now tries the 2h-window scope (Tier 1) THEN falls back to ALL of TODAY'S fixtures' rosters regardless of time (new resolver.afl_games_on_date) -- Greene tipped 10:13 for a game >2h away went to manual; now resolves if the surname is unique across today's fixtures (ambiguity still -> manual, odds tie-break preserved). NOTE: Bailey/Alex 65463 failure on 06-14 was a PROXY 403 (HyperBot-side, not tipbot) -- the v5.66 Eddie big-bet $150/$600 decay fired correctly. --- v5.66 (2026-06-14: AFL FAN-OUT SPLIT SHAPES (Wilson). Refines v5.65. NORMAL (Saiyan any size; Eddie <=2.5u): the capacity-weighted split is CLAMPED so the high-cap acct (Ryan 102506) carries at most AFL_FANOUT_RATIO_CAP=4x the smallest -> 4:1:1:1:1 = Saiyan @600 $75 ea + $300 Ryan; Eddie @2.5u=1000 $125 ea + $500 Ryan (scales with unit). EDDIE BIG BETS (units > EDDIE_FANOUT_BIG_UNITS=2.5): each of the 4 LIMITED accts starts at EDDIE_BIG_LIMITED_STAKE=$150 (~their late-game disposals liability), Ryan takes the REMAINDER, EVERY acct ladders DOWN 10%/step (EDDIE_FANOUT_DECAY=0.9) until it places, unfilled -> Manual. New pure _afl_fanout_targets() (per-acct targets + ladder mode: yaml-brackets or decay), unit-tested; FAIL-SAFE to even split if any cap non-numeric. Budget still capped at unit. Saiyan has no big-bet escalation (always 4:1). --- v5.65 (2026-06-14: AFL SINGLES FAN-OUT CAPACITY-WEIGHTED SPLIT (Wilson). _place_afl_fanout (Saiyan + Eddie AFL singles) now splits the unit PROPORTIONAL to each account's liability cap (top bracket) instead of even 1/n -> Ryan 102506 (4.5x caps) carries 4.5:1:1:1:1 vs the 4 limited sportsbet accounts. A $1000 (2.5u) tip now fills FULLY (~$118 ea on the 4 limited which fits their ~$138 disposals cap; ~$529 on Ryan under his ~$620) vs even-split's ~$752 + $248->Manual. Still budget-capped at the unit + ladders DOWN on reject. New _afl_fanout_weights() (pure, unit-tested); FAIL-SAFE to even split if any acct lacks a clean numeric cap. Gated AFL_FANOUT_WEIGHTED (default true). SGM fan-out UNCHANGED (even-split across the 3 equal-cap accts = same result); Eddie unit stays $400. --- v5.64 (2026-06-14: SAIYAN SGM UNIT 600->750 (Wilson). Singles/disposals stay at SAIYAN_UNIT_SIZE=600 (hard unit); SGMs now use SAIYAN_SGM_UNIT_SIZE=750 -- the concurrent SGM fan-out EVEN-SPLITS the unit across the 3 SGM accounts (Adam 65465/Wilson 53522/Daniel 68723) so 750 -> $250 stake each (was $200 = 600/3). New _apply_saiyan_sgm_unit() mirrors _apply_mlb_flat_stake (overrides tip.unit_size in place, units preserved; runs after the image test-stake override). Ryan 102506 REMOVED from AFL_SGM (kept in AFL/NBA singles only -- 'dont add Ryan to SGM'). NOTE: the per-account afl.sgm liability ladder [400,300,200] still caps a high-combined-odds SGM below $250 (binds above ~2.6 combined). Eddie unchanged -- already does the concurrent ladder fan-out (even-split = units respected, ladders down on reject, never overstakes). --- v5.63 (2026-06-14: NEW ACCOUNTS WIRED + low-success diagnosis (Wilson). 1) Ryan Suwandy sportsbet (102506) ADDED to sessions.yaml SPORTS-ONLY, caps = 4.5x the standard sportsbet sports caps (65465 template; fresh un-limited acct), appended at the BACK of AFL/NBA singles + AFL/NBA SGM priority lists (NOT MLB SGM -- that's the bespoke Shook HRRBI 3-acct play); NO racing block + NOT in RACING_SESSION_PRIORITY. 2) Alex Liu neds (100006/ALPunts) appended to the racing tail (was set up + active but not placeable). 3) Diagnosed the 3-day low-success accounts: 78280 TAB + 100003 TAB = HyperBot 'Intercept handling failed'/SidecarRequestError (TAB sidecar, already -> HyperBot ops); 73359 bet365 = 'Bet placement failed' (flaky bet365, same family as the pulled 53523/73357); 100004 ladbrokes = NOT a fault, odds-guard correctly rejecting price-below-target (7.5 vs 8.55 etc), healthy; 100005 betr = 'Odds changed 2103' betslip re-accept gap (betr support unverified) + one transient insufficient-funds. 4/5 are HyperBot bookie-integration issues, not tipbot/limits/funding. .env priority-list edits need main.py restart. --- v5.62 (2026-06-14: SPORTSBET HARNESS CAP TUNE (53522 + 71275) -- follow-on to the v5.61 limits analysis. sessions.yaml only. 53522 (Wilson): harness PLACE ceiling ~$500 + WIN ~$750 -> lowered Albion Park/Marburg place 1000->500 and the Saturday overrides (win 1500->750, place 1000->500; the bookie does NOT allow more on Sat for this limited account, it caused 61997 05-30 + the 06-13 place rejections). 71275 (Jocelyn, more limited): harness PLACE ceiling ~$160-200 + WIN ~$400-500 -> all place 400->200, Gloucester Park win 1000->500. Thoroughbreds untouched on both (never limited). Caps are the bot's OWN risk cap; tuning to the bookie's real ceiling stops rejection churn + (with v5.61) MBL pings, not placed volume. Requires main.py restart to load. --- v5.61 (NOTIFICATION-NOISE REDUCTION, Wilson day-review). Goal: fewer alerts. 1) SESSION-HEALTH SILENT BACKUP: main.py's in-process _session_watchdog stamps logs/main_watchdog_heartbeat.txt every cycle (+at startup); the scheduled check_session_health.py now DEFERS entirely while that heartbeat is fresh (<800s) and only takes over when stale (main.py dead / watchdog stalled) -- kills the double-alerting (a real drop used to page BOTH monitors ~15min apart). 2) FOREIGN ACCOUNTS NEVER ALERT: check_session_health filters the shared HyperBot session list to OWNED ids (sessions.yaml) + prunes foreign from its state, so other PCs' accounts dropping/recovering stop pinging (was hitting Maintenance at 14:40/23:40/00:20). Fail-safe: yaml glitch -> track all. 3) MASS-DROP/RECOVERY BATCHING: check_session_health sends AT MOST one Critical + one Info per poll (was one Telegram PER session -- the 00:10 mass-drop fired 15 Criticals + 15 recoveries). 4) XWATCHER CRASH TRIMMED: x_watcher._alert strips Playwright's multi-KB 'Browser logs:'/traceback tail + caps to 280 chars (full detail still in x_watcher.log). 5) RACING BOOKIE STAKE-CAP (MBL) routing mirrors sports v5.52: Critical ONLY when nothing placed; when the ladder still landed a smaller bet it's suppressed (LADDER ACTIVITY on Maintenance already covers it) or routed to Maintenance -- stops the double-ping on tips 62179/62194. 6) Watchdog +60s recheck only speaks up when something RECOVERED (all-still-down recheck was noise; 15-min Critical covers it). LIMITS ANALYSIS (Wilson): sportsbet 53522 place@11 limited to ~$50 (cap allowed $100); 71275 place@9.5 limited to ~$19 (cap allowed $47) -- recommend lowering those racing place caps.)"

# ── Telegram ─────────────────────────────────────────────────────────
_raw_api_id = os.getenv("TELEGRAM_API_ID", "0")
try:
    TELEGRAM_API_ID = int(_raw_api_id)
except ValueError:
    raise ValueError(f"TELEGRAM_API_ID must be an integer, got: {_raw_api_id!r}") from None
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")


def _env_float(name: str, default: str) -> float:
    """float(os.getenv(...)) with a clear startup error on a bad value.

    C7 (2026-05-31): a non-numeric LINE_TOLERANCE / MAX_UNITS / *_UNIT_SIZE
    env var previously crashed startup with a bare ValueError deep in module
    import. Mirror the TELEGRAM_API_ID guard above: fail fast with a message
    that names the offending variable so the misconfig is obvious."""
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got: {raw!r}") from None


def _env_int(name: str, default: str) -> int:
    """int(os.getenv(...)) with a clear startup error on a bad value. Same
    intent as _env_float. Used by the X-watcher (poll interval, bot id)."""
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got: {raw!r}") from None


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var (1/true/yes => True)."""
    raw = os.getenv(name, "")
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes")

# ── Sports-only fork switch (sportsbot) ──────────────────────────────
# Set true ONLY in the sportsbot fork's .env. When true, the channel filter at
# the BOTTOM of this file trims TIPSTER_CHANNELS down to the two sports tipsters
# the fork supports (Saiyan AFL + Shook NBA/MLB) plus an optional user test
# channel, and the fork simply doesn't ship the racing / X / image / other-NBA
# modules (main.py tolerates their absence). Defaults FALSE, so this is a pure
# no-op in tipbot -- nothing about the live bot changes. See memory
# `sportsbot-fork` + REBUILD_SPORTSBOT.md.
SPORTSBOT_MODE = _env_bool("SPORTSBOT_MODE", False)

# ── Notifications ────────────────────────────────────────────────────
NOTIFY_BOT_TOKEN = os.getenv("NOTIFY_BOT_TOKEN", "")
NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID", "")
NOTIFY_SUCCESS_CHAT_ID = os.getenv("NOTIFY_SUCCESS_CHAT_ID", "")

# ── HyperBot ─────────────────────────────────────────────────────────
HYPERBOT_API_KEY = os.getenv("HYPERBOT_API_KEY", "")
HYPERBOT_BASE_URL = os.getenv(
    "HYPERBOT_BASE_URL",
    "https://api.hyperbot.imperialwealth.com",
)

# ── Groq ─────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ── Tip parser provider (LLM backend that parses tipster messages) ───
# Which LLM backend `tip_parser` uses: "groq" (default, LIVE) or "claude"
# (Anthropic Sonnet — SCAFFOLDED but INERT). The Claude path activates ONLY
# when ALL of these hold: TIP_PARSER_PROVIDER=claude, ANTHROPIC_API_KEY set,
# `anthropic` installed, and main.py's parser import pointed at tip_parser
# (one line). Until then this is a pure no-op — Groq stays the parser. If
# "claude" is selected but unusable (no key / SDK), tip_parser FAILS SAFE back
# to Groq rather than dropping tips. Full plan: REBUILD-note CLAUDE_PARSER.md.
TIP_PARSER_PROVIDER = os.getenv("TIP_PARSER_PROVIDER", "groq").strip().lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Sonnet 4.6 (Wilson's chosen model for the Claude parser path). Exact ID, no
# date suffix. Override via .env only if migrating models.
CLAUDE_PARSER_MODEL = os.getenv("CLAUDE_PARSER_MODEL", "claude-sonnet-4-6")

# ── Line Tolerance ───────────────────────────────────────────────────
LINE_TOLERANCE = _env_float("LINE_TOLERANCE", "1.0")

# ── Max Units ────────────────────────────────────────────────────────
MAX_UNITS = _env_float("MAX_UNITS", "3.0")

# ── MLB flat stake ───────────────────────────────────────────────────
# MLB is bet at a FLAT dollar stake, ignoring Shook's recommended unit
# (Wilson 2026-06-01: "flat 400 stakes no matter what the recommended unit is
# for shook mlb"). The SAME knob is the validation gate: it's set to $1 for the
# gated live test, then raised to the production value (e.g. 400) once the
# pipeline is validated. The clamp lives in the PLACING process
# (main._apply_mlb_flat_stake, called from _process_tip, loaded on restart) —
# NOT just here — so an .env edit alone can't change the live stake without a
# restart (the $600 lesson, 2026-06-01: a "$1-capped" test placed a real $600
# bet because main.py wasn't restarted). Set 0 to disable the flat override
# (MLB would then size like any Shook tip).
MLB_FLAT_STAKE = _env_float("MLB_FLAT_STAKE", "1")

# v5.74: MLB HRRBI per-account STAKE ladder — fractions of the even-split target
# ($400 / 4 accounts = $100 -> $100/$90/$85/$80). The MLB HRRBI SGM fan-out uses
# this fixed-% ladder (NOT the mlb.sgm liability brackets): each of the 4 SGM
# accounts (Adam/Wilson/Daniel/Ryan) places the full even-split first, then
# ladders DOWN on a bookie stake-reject; any unfilled remainder spills to Alex as
# a 2+ single. Restart to load.
MLB_HRRBI_LADDER_PCT = [
    float(x) for x in os.getenv("MLB_HRRBI_LADDER_PCT", "1.0,0.9,0.85,0.8").split(",")
    if x.strip()
] or [1.0, 0.9, 0.85, 0.8]

# ── Max-odds CEILING (wrong-selection sanity guard, ALL sports tipsters) ──
# If the live (catalog/price-check) odds for the resolved selection exceed the
# tipped odds × this multiplier, the bet is NOT auto-placed — it routes to
# manual. A too-good-to-be-true price almost always means a WRONG selection or
# line. Applies to every sports tipster (Wilson 2026-05-31); pairs with the
# global 0.9× floor → auto-place band [tipped×0.9, tipped×1.25]. Per-tipster
# overrides exist (e.g. the X/EasyMoney tipster's X_MAX_ODDS_MULT). Set <=1 to
# disable. NOTE: racing has its own separate ceiling (racing_placer
# ODDS_DRIFT_CEILING = 1.5×); this one is for sports placement.
MAX_ODDS_MULT = _env_float("MAX_ODDS_MULT", "1.25")

# ── Tipster Channel Configs ──────────────────────────────────────────
# Unit sizes loaded from .env: SAIYAN_UNIT_SIZE, KEV_UNIT_SIZE, AUSBETS_UNIT_SIZE, SHOOK_UNIT_SIZE, TEST_UNIT_SIZE
SAIYAN_UNIT_SIZE = _env_float("SAIYAN_UNIT_SIZE", "150")
# Saiyan SGMs stake a LARGER unit than his singles/disposals (Wilson 2026-06-14).
# Singles/disposals keep SAIYAN_UNIT_SIZE (600 live); SGMs use this. The SGM
# fan-out EVEN-SPLITS the unit across the 3 SGM accounts (Adam 65465 / Wilson
# 53522 / Daniel 68723), so 750 -> $250 stake each. (The per-account afl.sgm
# liability ladder [400,300,200] still caps a high-combined-odds SGM lower.)
SAIYAN_SGM_UNIT_SIZE = _env_float("SAIYAN_SGM_UNIT_SIZE", "750")
KEV_UNIT_SIZE = _env_float("KEV_UNIT_SIZE", "100")
AUSBETS_UNIT_SIZE = _env_float("AUSBETS_UNIT_SIZE", "100")
SHOOK_UNIT_SIZE = _env_float("SHOOK_UNIT_SIZE", "300")
TEST_UNIT_SIZE = _env_float("TEST_UNIT_SIZE", "1")
ETR_UNIT_SIZE = _env_float("ETR_UNIT_SIZE", "400")  # ETR NBA (text, obfuscated names)

# ── Image-tip channels (vision-parsed via Groq Scout) ───────────────
# Three Telegram CHANNELS post tips as images: Eddie's Bets AFL (sports),
# Zak Trussell SA Racing + The Trial Sniper (racing). Only the channel
# admin/tipster can post in a broadcast channel, so these channels carry NO
# bot_id — the handler's sender filter is skipped and every post is treated
# as the tipster's. The image is downloaded (Telethon download_media works
# even on no-forward/no-save channels for a subscribed account) and run
# through groq_parser.parse_tip_image (Llama-4 Scout vision).
#
# TEST GATE: while IMAGE_TIPS_TEST_MODE is true (default), every image tip
# is staked at IMAGE_TIPS_TEST_UNIT_SIZE per recommended unit (default $1/u,
# so a 2.5u tip = $2.50, a 1u tip = $1). Enforced in the PLACING process
# (main._apply_image_test_stake for sports; the racing image orchestrator
# for racing) — NOT just here — so an .env edit alone can't change the live
# stake without a restart (the $600 lesson, 2026-06-01). Flip
# IMAGE_TIPS_TEST_MODE=false and set real *_UNIT_SIZE values for production.
IMAGE_TIPS_TEST_MODE = _env_bool("IMAGE_TIPS_TEST_MODE", True)
IMAGE_TIPS_TEST_UNIT_SIZE = _env_float("IMAGE_TIPS_TEST_UNIT_SIZE", "1")

# Racing image tips (Zak / Trial) have their OWN test gate, SEPARATE from the
# global IMAGE_TIPS_TEST_MODE (which still governs Eddie AFL at $1/u). Default
# False = Zak/Trial place at their FULL unit size (ZAK/TRIAL_UNIT_SIZE) with the
# shared racing guards (10% odds floor, runner-match, 1.5x wrong-horse ceiling,
# route-remainder/whole-to-manual, 3u cap). Set IMAGE_RACING_TEST_MODE=true to
# drop Zak/Trial back to the $1/u test stake WITHOUT affecting Eddie. The racing
# image path is Zak/Trial-only (Eddie AFL uses a different route). v5.15
# (Wilson 2026-06-05: "flip Zak/Trial to full unit sizing at 400 now").
IMAGE_RACING_TEST_MODE = _env_bool("IMAGE_RACING_TEST_MODE", False)

# Production per-channel unit sizes (Eddie used ONLY when IMAGE_TIPS_TEST_MODE is
# false; Zak/Trial used when IMAGE_RACING_TEST_MODE is false — now the default).
EDDIE_UNIT_SIZE = _env_float("EDDIE_UNIT_SIZE", "10")
ZAK_UNIT_SIZE = _env_float("ZAK_UNIT_SIZE", "400")
TRIAL_SNIPER_UNIT_SIZE = _env_float("TRIAL_SNIPER_UNIT_SIZE", "400")

# Hard max units per racing-image play (Zak / Trial). DEDICATED cap, independent
# of the global MAX_UNITS — so raising MAX_UNITS for other tipsters never
# un-caps these. Their unit size will be $400 in production, so 3u = $1,200
# intended (then liability-capped to the $1000 win / $500 place thoroughbred
# cap per account); $1/u in test = $3. 2026-06-03.
IMAGE_RACING_MAX_UNITS = _env_float("IMAGE_RACING_MAX_UNITS", "3.0")

# Parser keys whose channels deliver tips as IMAGES (vision path). Maps the
# parser key -> sport so the handler routes racing tips to the racing
# pipeline (tiptitans_processor.process_image_racing_tip) and AFL tips to
# the sports pipeline (place_tip). Also gates _apply_image_test_stake.
IMAGE_TIP_PARSERS = {
    "eddie_afl": "afl",
    "zak_racing": "racing",
    "trial_sniper": "racing",
}

TIPSTER_CHANNELS = {
    -1003201095340: {
        "name": "Saiyan AFL",
        "parser": "saiyan_afl",
        "bot_id": 7869219767,
        "default_units": 1.0,
        "unit_size": SAIYAN_UNIT_SIZE,
        "sport": "afl",
    },
    -4925672658: {
        "name": "AusBets NBA",
        "parser": "ausbets_nba",
        "bot_id": 7964646743,
        "default_units": 1.0,
        "unit_size": AUSBETS_UNIT_SIZE,
        "sport": "nba",
    },
    -4634143283: {
        "name": "Kev NBA",
        "parser": "kev_nba",
        "bot_id": 8085856336,
        "default_units": 1.0,
        "unit_size": KEV_UNIT_SIZE,
        "sport": "nba",
    },
    -5130133556: {
        "name": "TipBot Test",
        "parser": "test",
        "bot_id": 1821631216,
        "default_units": 1.0,
        "unit_size": TEST_UNIT_SIZE,
        "sport": "nba",
    },
    # 2026-05-16: Shook's group was promoted from basic group to supergroup,
    # which flipped the chat_id from -4825377525 to -1003761736978. Telethon
    # silently stopped seeing messages because it was subscribed to the dead
    # ID. Bot_id is unchanged. Future regression to watch for: any tipster
    # group hitting supergroup conversion (Telegram auto-promotes on member
    # growth) will silently break the listener with no log signal. The
    # diagnostic log in main.py handler is the early warning for next time.
    -1003761736978: {
        "name": "Shook Plays",
        "parser": "shook",
        "bot_id": 7687523872,
        "default_units": 1.0,
        "unit_size": SHOOK_UNIT_SIZE,
        "sport": "auto",
        "buffer_messages": True,
    },
    # ETR NBA (2026-06-07): a Telegram GROUP (has a bot sender) posting
    # leetspeak-OBFUSCATED NBA player props (e.g. "Lu2e Ko2net (SAS) ABOVE 1.5
    # Points" => Luke Kornet over 1.5). parser="etr_nba" is Groq-only (no regex
    # fallback) — the Groq SYSTEM_PROMPT de-obfuscates + the NBA roster fuzzy-match
    # backstops it. Placement: _place_etr_nba_fanout (blind, no price-check, fixed
    # [100,90,80,70] ladder across the 4 sportsbet accounts). NBA singles only; a
    # multi/SGM routes to manual. Force-bookie sportsbet. (NFL deferred.)
    -4974425383: {
        "name": "ETR NBA",
        "parser": "etr_nba",
        "bot_id": 7738582293,
        "default_units": 1.0,
        "unit_size": ETR_UNIT_SIZE,
        "sport": "nba",
    },
    # ── Image-tip CHANNELS (vision-parsed). NO bot_id by design: a broadcast
    #    channel's posts come from the channel (not a user sender_id), and
    #    only the tipster can post, so the sender filter is intentionally
    #    skipped (see IMAGE_TIPS_TEST_MODE above). image_tips=True routes the
    #    post's media through parse_tip_image. The tipbot's Telethon account
    #    MUST be subscribed to each channel to receive posts. ──
    -1003719024597: {
        "name": "Eddie's Bets AFL",
        "parser": "eddie_afl",
        "default_units": 1.0,
        "unit_size": EDDIE_UNIT_SIZE,
        "sport": "afl",
        "image_tips": True,
    },
    -1003155675019: {
        "name": "Zak Trussell SA Racing",
        "parser": "zak_racing",
        "default_units": 1.0,
        "unit_size": ZAK_UNIT_SIZE,
        "sport": "racing",
        "image_tips": True,
    },
    -1002980787986: {
        "name": "The Trial Sniper",
        "parser": "trial_sniper",
        "default_units": 1.0,
        "unit_size": TRIAL_SNIPER_UNIT_SIZE,
        "sport": "racing",
        "image_tips": True,
    },
}

# ── AFL Mappings ─────────────────────────────────────────────────────
AFL_TEAMS = {
    "ADE": "Adelaide", "ADEL": "Adelaide",
    "BRI": "Brisbane Lions", "BR": "Brisbane Lions", "BL": "Brisbane Lions", "BRIS": "Brisbane Lions",
    "CAR": "Carlton", "CARL": "Carlton",
    "COL": "Collingwood", "COLL": "Collingwood",
    "ESS": "Essendon", "ESSE": "Essendon",
    "FRE": "Fremantle", "FREM": "Fremantle", "FREO": "Fremantle",
    "GEE": "Geelong", "GEEL": "Geelong", "CATS": "Geelong",
    "GC": "Gold Coast", "GCS": "Gold Coast", "GCFC": "Gold Coast", "SUNS": "Gold Coast",
    "GWS": "Greater Western Sydney", "GIANTS": "Greater Western Sydney",
    "HAW": "Hawthorn", "HAWI": "Hawthorn", "HAWTH": "Hawthorn",
    "MEL": "Melbourne", "MELB": "Melbourne", "DEES": "Melbourne",
    # NWM is intentionally NOT mapped here — it collides with the AFL player
    # nickname "NWM" = Nasiah Wanganeen-Milera (St Kilda). NM is the
    # canonical North Melbourne code; Saiyan only uses NM in his messages.
    "NM": "North Melbourne", "NMFC": "North Melbourne", "KANGAS": "North Melbourne",
    "PA": "Port Adelaide", "PAFC": "Port Adelaide", "PORT": "Port Adelaide",
    "RIC": "Richmond", "RICH": "Richmond", "TIGERS": "Richmond",
    "STK": "St Kilda", "STKI": "St Kilda", "STKFC": "St Kilda", "SAINTS": "St Kilda",
    "SYD": "Sydney", "SWANS": "Sydney",
    "WCE": "West Coast", "WEAG": "West Coast", "WEST": "West Coast", "EAGLES": "West Coast",
    "WBD": "Western Bulldogs", "WB": "Western Bulldogs", "BULLDOGS": "Western Bulldogs", "DOGS": "Western Bulldogs",
}

AFL_TEAM_NAMES = {}
for abbr, full in AFL_TEAMS.items():
    AFL_TEAM_NAMES.setdefault(full, []).append(abbr)

# ── Bookmaker-specific AFL team aliases ───────────────────────────
# Squiggle's team names don't always match how bookmakers index their
# event listings. HyperBot's resolver tries case/order variants but
# doesn't alias team names. Translate at the placement boundary only.
# Add new entries when HyperBot returns "Could not find event" for an
# event that's actually live on the bookmaker (just under a different
# name).
BOOKIE_AFL_ALIASES = {
    "sportsbet": {
        "Greater Western Sydney": "GWS Giants",
    },
    # bet365 currently broken for AFL sports anyway
    # "bet365": {},
    # "tab": {},
    # "ladbrokes": {},
}

AFL_STAT_MAP = {
    "disposals": "disposals", "disposal": "disposals",
    "goals": "goals", "marks": "marks", "tackles": "tackles",
    "kicks": "kicks", "handballs": "handballs",
    "clearances": "clearances", "hitouts": "hitouts",
    "fantasy points": "fantasy_points",
    "fantasy_points": "fantasy_points", "fp": "fantasy_points", "fantasy": "fantasy_points",
}

# ── NBA / NBL Stat Mappings ──────────────────────────────────────────
NBA_STAT_MAP = {
    "p": "points", "pts": "points", "points": "points",
    "r": "rebounds", "reb": "rebounds", "rbd": "rebounds", "rebounds": "rebounds",
    "a": "assists", "ast": "assists", "assists": "assists",
    "pr": "points_rebounds", "rp": "points_rebounds",
    "pa": "points_assists", "ap": "points_assists",
    "ra": "assists_rebounds", "ar": "assists_rebounds",
    "pra": "points_rebounds_assists",
    "threes": "threes", "3s": "threes",
    "blocks": "blocks", "blk": "blocks",
    "steals": "steals", "stl": "steals",
}

# ── MLB Stat Mappings (2026-06-01) ───────────────────────────────────
# Maps tip phrasings to the `stat` values Sportsbet uses INSIDE the single
# 'player_stats' MLB market (confirmed live 2026-06-01: rbis, total_bases,
# runs, hits, home_runs, strikeouts, singles, doubles, triples, stolen_bases,
# h_r_rbi). Used by _match_mlb_player_prop.
MLB_STAT_MAP = {
    "hits": "hits", "hit": "hits", "h": "hits",
    "total bases": "total_bases", "total_bases": "total_bases", "tb": "total_bases", "bases": "total_bases",
    "rbi": "rbis", "rbis": "rbis", "runs batted in": "rbis",
    "runs": "runs", "run": "runs", "runs scored": "runs",
    "home run": "home_runs", "home runs": "home_runs", "hr": "home_runs",
    "homer": "home_runs", "homers": "home_runs", "home_runs": "home_runs",
    "strikeout": "strikeouts", "strikeouts": "strikeouts", "k": "strikeouts", "ks": "strikeouts", "so": "strikeouts",
    "single": "singles", "singles": "singles",
    "double": "doubles", "doubles": "doubles", "2b": "doubles",
    "triple": "triples", "triples": "triples", "3b": "triples",
    "stolen base": "stolen_bases", "stolen bases": "stolen_bases", "stolen_bases": "stolen_bases",
    "sb": "stolen_bases", "steal": "stolen_bases", "steals": "stolen_bases",
    "h+r+rbi": "h_r_rbi", "hits runs rbis": "h_r_rbi", "hrr": "h_r_rbi", "h_r_rbi": "h_r_rbi",
    # Defensive: older Groq prompt wording emitted "hits_runs_rbis"; the live
    # catalog stat is `h_r_rbi`, so alias it so a stale parse still matches.
    "hits_runs_rbis": "h_r_rbi", "hrrbi": "h_r_rbi",
}

# ── Kev Deobfuscation ────────────────────────────────────────────────
KEV_CHAR_MAP = {
    "!": "i", "@": "a", "0": "o", "3": "e", "1": "l", "$": "s", "5": "s",
}

# ── v4.0 — Sessions YAML + per-sport priority ───────────────────────
# Path to sessions.yaml (per-session metadata: bookmaker, liability caps,
# boost eligibility). Wilson maintains this file manually; restart tipbot
# to reload changes.
SESSIONS_YAML_PATH = os.getenv("SESSIONS_YAML_PATH", "sessions.yaml")

# Per-sport priority lists. Comma-separated session IDs in priority order.
# Sessions not in the relevant list are excluded from auto-placement for
# that (sport, kind) combo and routed to manual instead.
#
# These are intentionally exposed both as raw env strings (for legacy
# callers) and parsed lists via session_priority.load_priority_from_env().
NBA_SESSION_PRIORITY = os.getenv("NBA_SESSION_PRIORITY", "")
NBA_SGM_SESSION_PRIORITY = os.getenv("NBA_SGM_SESSION_PRIORITY", "")
AFL_SESSION_PRIORITY = os.getenv("AFL_SESSION_PRIORITY", "")
AFL_SGM_SESSION_PRIORITY = os.getenv("AFL_SGM_SESSION_PRIORITY", "")
RACING_SESSION_PRIORITY = os.getenv("RACING_SESSION_PRIORITY", "")

# v4.0 placement rollback flag. When true, all placement code uses the
# v3.10 path (legacy SESSION_PRIORITY env var, no liability caps, no
# multi-bookmaker price comparison). Session 1 startup hooks (yaml load,
# priority module init) are also skipped — the flag is a true full rollback.
# Default false: ship v4.0 logic active.
USE_LEGACY_PLACEMENT = os.getenv("USE_LEGACY_PLACEMENT", "false").strip().lower() in ("1", "true", "yes")

# ── AFL concurrent fan-out placement (v5.11/v5.12, 2026-06-05) ──────
# Saiyan + Eddie AFL singles place on ALL eligible Sportsbet sessions
# CONCURRENTLY (fired in parallel via a thread pool) instead of the sequential
# one-account-at-a-time spillover of _place_singles_v4. The intended unit stake
# is split EVENLY across the eligible accounts; each account then walks its OWN
# liability ladder (top bracket from sessions.yaml first, dropping a bracket on
# a stake-too-high / MBL reject — v5.12). The exact catalog line is resolved
# ONCE per bookie (the line resolver is kept — a catalog miss still routes to
# manual) and that resolved payload is fanned out; there is NO per-account price
# check. v5.14: the WRONG-SELECTION ceiling (live > 1.25x tipped -> manual) is
# KEPT (it runs off the resolve-time catalog odds already in hand — no extra
# call); the price-FLOOR is dropped (a shorter-than-tipped live price still
# places). Per rung initial_post_max_attempts=1,
# and the ladder STOPS on an ambiguous/maybe-landed rung (no double-stake).
# Default ON (Wilson 2026-06-05: "build it live now"). Set
# AFL_CONCURRENT_FANOUT=false to revert AFL to the sequential _place_singles_v4
# path. Only affects sport == "afl" singles; NBA/MLB/racing/SGM untouched.
# Restart tipbot to apply.
AFL_CONCURRENT_FANOUT = os.getenv("AFL_CONCURRENT_FANOUT", "true").strip().lower() in ("1", "true", "yes")

# Minimum per-account stake floor for the fan-out. When the even split
# (intended / num_accounts) rounds below this, the account places this floor
# instead so the bet still reaches the bookie. Primarily matters for Eddie in
# IMAGE_TIPS_TEST_MODE ($1/unit), where a $1 tip split across 4 accounts would
# otherwise be ~$0.25/account (below the bookie minimum). For live Saiyan
# ($600/unit) the floor never binds. Set to 0 to disable (sub-min splits then
# just fail at the bookie). Liability caps still bound the stake from above.
AFL_FANOUT_MIN_STAKE = float(os.getenv("AFL_FANOUT_MIN_STAKE", "1.0"))

# CAPACITY-WEIGHTED fan-out split (Wilson 2026-06-14, v5.65). Default ON. When
# true, _place_afl_fanout splits the unit across accounts PROPORTIONAL to each
# account's liability cap for the market (instead of an even 1/n split), so a
# high-cap account (Ryan 102506 @ 4.5x) carries proportionally more and the
# limited accounts get a share that fits their cap -> fills more of the unit
# without overflowing to Manual. Ratio for the uniform 4.5x = 4.5:1:1:1:1.
# Still budget-capped at the unit + ladders DOWN on reject. Falls back to the
# even split when any account lacks a clean numeric cap. Set false to revert.
AFL_FANOUT_WEIGHTED = _env_bool("AFL_FANOUT_WEIGHTED", True)

# v5.66 (Wilson 2026-06-14): AFL singles fan-out split shape.
# NORMAL (Saiyan any size; Eddie <= EDDIE_FANOUT_BIG_UNITS): capacity-weighted,
# but the high-cap account (Ryan 102506 @ 4.5x) is CLAMPED to RATIO_CAP x the
# smallest account's weight -> a 4:1:1:1:1 split: $75 ea + $300 Ryan (Saiyan
# @600), $125 ea + $500 Ryan (Eddie @2.5u=1000).
AFL_FANOUT_RATIO_CAP = _env_float("AFL_FANOUT_RATIO_CAP", "4.0")

# v5.68 (Wilson 2026-06-15): ONE-retry on a TRANSIENT pre-placement reject in the
# AFL/SGM fan-out. A proxy 403 / auth refusal / network error means the bet was
# NEVER submitted to the bookie (gated by _is_definitely_pre_placement — the
# narrow provably-not-placed set), so retrying the SAME rung once is zero
# double-stake risk. Alex 65463's Bailey $150 on 06-14 was a one-off proxy 403
# that placed fine 12 min later -> a retry would have filled it instead of
# dropping to Manual. Stake-rejects still ladder DOWN; ambiguous still stops.
AFL_FANOUT_PREPLACEMENT_RETRY = _env_bool("AFL_FANOUT_PREPLACEMENT_RETRY", True)
AFL_FANOUT_RETRY_DELAY_SEC = _env_float("AFL_FANOUT_RETRY_DELAY_SEC", "2.0")
# EDDIE BIG BETS (> EDDIE_FANOUT_BIG_UNITS units): different shape — each LIMITED
# account starts at EDDIE_BIG_LIMITED_STAKE (= ~their late-game player_disposals
# liability, since Eddie tips close to start), the high-cap account (Ryan) mops up
# the REMAINDER, and EVERY account ladders DOWN by (1 - EDDIE_FANOUT_DECAY) i.e.
# 10% per step until it places; whatever can't fill -> Manual. (Wilson: a
# 150/150/150/150/rest placement, laddered 10% each step.)
EDDIE_FANOUT_BIG_UNITS = _env_float("EDDIE_FANOUT_BIG_UNITS", "2.5")
EDDIE_BIG_LIMITED_STAKE = _env_float("EDDIE_BIG_LIMITED_STAKE", "150")  # v5.77: unused (big bets even-split now caps equal)
EDDIE_FANOUT_DECAY = _env_float("EDDIE_FANOUT_DECAY", "0.9")  # 10% step-down
# v5.78 (Wilson 2026-06-20): AFL DISPOSALS redistribute-to-successful top-up. When
# the fan-out leaves stake unfilled (an account failed e.g. on low balance, or
# laddered down) AND other accounts placed, re-split the unfilled remainder EVENLY
# over the accounts that worked and top each up (100/90/80/70% ladder, stop on
# reject) — ONE reroute round, then it's manual. AFL disposals OVERS only (any
# tipster — Saiyan or Eddie); SGMs are a different path. v5.79: UNDERS excluded —
# their per-account cap [124,99,74,50] is far below the OVER cap [300,250,200,150],
# so placed accounts have no headroom for a reroute (it just re-rejects). (v5.77 was
# Eddie-overs-only; v5.78 briefly added unders; v5.79 reverted to overs.) Kill-switch.
AFL_DISPOSALS_REDISTRIBUTE = _env_bool("AFL_DISPOSALS_REDISTRIBUTE", True)

# ── Concurrent SGM fan-out (v5.38, 2026-06-07) ──────────────────────
# Unified concurrent placement for BOTH Saiyan AFL SGMs and Shook MLB HRRBI
# SGMs (mirrors the AFL singles fan-out). The intended unit is EVEN-SPLIT across
# the SGM-capable accounts; each account is capped by its yaml `sgm` LIABILITY
# ladder (afl.sgm [400,300,200] / mlb.sgm [130,100,87]) sized off the ESTIMATED
# combined SGM odds (= PRODUCT of the per-leg catalog odds, since an SGM has no
# pre-placement price), then placed CONCURRENTLY (ThreadPoolExecutor), each
# laddering DOWN a bracket on a stake-too-high reject in its own thread. Because
# the even-split's liability lands below the top bracket, the cap normally does
# NOT bind — it only ladders down on a reject or caps a long-odds SGM. The
# product OVERESTIMATES a positively-correlated SGM's true odds, so the derived
# stake UNDERSIZES (safe — under the cap). If a leg lacks catalog odds, the
# whole tip FALLS BACK to the sequential _place_sgm_v4 (the proven path). MLB:
# the orchestrator (_place_mlb_hrrbi) runs the fan-out across the 3 SGM accounts
# (orchestrated -> no own summary) then Alex as the single-account backstop for
# the remainder. Default ON (Wilson 2026-06-07). Set SGM_CONCURRENT_FANOUT=false
# to revert to the sequential _place_sgm_v4 (the [400,300,200]/[130,100,87]
# ladders then act as STAKE steps). NBA SGMs always stay sequential. Restart to
# apply.
SGM_CONCURRENT_FANOUT = os.getenv("SGM_CONCURRENT_FANOUT", "true").strip().lower() in ("1", "true", "yes")

# ── ETR NBA concurrent fan-out (2026-06-07) ─────────────────────────
# ETR posts obfuscated NBA player props. Placement mirrors the AFL fan-out
# (concurrent ThreadPoolExecutor across the sportsbet accounts, per-account
# ladder-down on a stake reject, leftover -> Manual Bets) but with TWO key
# differences: (1) a FIXED stake ladder [100,90,80,70] (not the liability-bracket
# ladder — $400 unit / 4 accounts = $100 each, then 90/80/70 on a stake reject),
# and (2) BLIND placement — NO price-check (the leg is resolved by the pure
# _resolve_leg_for_hyperbot transform and POSTed straight away; "fast as
# possible", odds ignored). Set ETR_NBA_CONCURRENT_FANOUT=false to route ETR
# back through _place_singles_v4. Restart to apply.
ETR_NBA_CONCURRENT_FANOUT = os.getenv("ETR_NBA_CONCURRENT_FANOUT", "true").strip().lower() in ("1", "true", "yes")
# The 4 sportsbet accounts ETR fans out across. Kept SEPARATE from
# NBA_SESSION_PRIORITY so a future NBA-priority edit (e.g. dropping a limited
# account) doesn't silently change ETR's fan-out.
ETR_NBA_SESSION_IDS = [s.strip() for s in os.getenv(
    "ETR_NBA_SESSION_IDS", "65465,53522,65463,68723").split(",") if s.strip()]
# Fixed descending stake ladder per account ($100 top = $400 unit / 4; then
# 90/80/70 on a stake-too-high reject, stopping at $70). Each rung is also capped
# by the running budget so the 4 accounts never exceed the unit.
ETR_NBA_FIXED_LADDER = [100.0, 90.0, 80.0, 70.0]
# Test gate (mirrors MLB / image tips): when true, ETR stakes ETR_NBA_UNIT_SIZE_TEST
# per unit (default $1) so the first live message fires ONE blind $1 placement
# end-to-end (parse -> deobfuscate -> roster -> event -> POST) before risking the
# $400/4 fan-out. Default FALSE: Wilson chose to launch LIVE at $400 (2026-06-07).
# Enforced inside _place_etr_nba_fanout (not just .env) so an env edit alone can't
# flip the stake without a restart (the $600 lesson).
ETR_NBA_TEST_MODE = _env_bool("ETR_NBA_TEST_MODE", False)
ETR_NBA_UNIT_SIZE_TEST = _env_float("ETR_NBA_UNIT_SIZE_TEST", "1")

# ── Bet Record feed (v5.44, 2026-06-08 — BUILT BUT GATED OFF) ───────
# Transform logs/bets_placed.csv -> a row-per-placement CSV in Wilson's
# "2025-26 Bet Record" workbook conventions (bet_record_feed.py), which bettrack
# merges with his manual sheet. The bot only ever writes the CSV (never the
# .xlsx), so it can't lock/corrupt the open workbook. Default OFF — inert until
# Wilson flips it on. AUTORESULT pulls Win/Loss/Void + Net Change from
# /v3/transactions (status->result, pnl->net change). PATH defaults next to the
# workbook in OneDrive so Power Query / bettrack have a stable location.
BET_RECORD_FEED_ENABLED = _env_bool("BET_RECORD_FEED_ENABLED", False)
BET_RECORD_FEED_AUTORESULT = _env_bool("BET_RECORD_FEED_AUTORESULT", False)
BET_RECORD_FEED_PATH = os.getenv(
    "BET_RECORD_FEED_PATH",
    r"C:\Users\Wilson\OneDrive\Sportsbetting\bet_record_feed.csv")
# CLEAN-WIPE / cutover date (YYYY-MM-DD). When set, the feed emits ONLY bets on or
# after this date. Use it at switchover: Wilson manually enters tipbot bets in Bet
# Record up to the cutover, then sets this to the cutover date + flips
# MERGE_TIPBOT_FEED on, so the feed covers post-cutover only and there are no dupes
# with the rows he already typed. Empty (default) = emit all history.
BET_RECORD_FEED_SINCE = os.getenv("BET_RECORD_FEED_SINCE", "").strip()
# Sessions whose bookie denies /v3/transactions (403) — skip them in auto-result to
# avoid error spam + wasted polls. Their bets stay blank (manual-result). Confirmed
# 2026-06-08: TAB 71265/78280 + Sophie PointsBet 76341 all 403. (Soup-side; revisit
# if TAB/PointsBet transactions are ever enabled.)
BET_RECORD_FEED_SKIP_SESSIONS = [s.strip() for s in os.getenv(
    "BET_RECORD_FEED_SKIP_SESSIONS", "71265,78280,76341").split(",") if s.strip()]

# ── Ambiguous-outcome reconciliation (v4.5, 2026-05-31) ─────────────
# When a placement gets a slow rejection (Erasmus class), query
# /api/pending_bets to CHECK whether the bet actually landed instead of
# guessing from latency. Two tiers, both default OFF:
#   RECONCILE_AMBIGUOUS — master switch. When on, a confirmed-landed bet is
#     recorded with its real bet_id + actual stake (Tier 1, safe regardless of
#     feed lag — finding it = it's there). A confirmed-NOT-found or an API
#     failure still falls back to today's conservative debit-as-placed + alert.
#   RECONCILE_SPILL — Tier 2. Only when BOTH flags on: a confirmed-not-found
#     slow rejection is treated as a genuine reject and the stake is
#     laddered/spilled to recover it. DANGER if the pending_bets feed lags
#     beyond the poll window (a landed-but-not-yet-shown bet would be re-bet) —
#     enable ONLY after validating feed latency in the logs. Applies to the
#     SLOW-REJECTION class only; text-pattern (Pointsbet "intercepted") never
#     spills (it lands after MBL/trader review).
RECONCILE_AMBIGUOUS = os.getenv("RECONCILE_AMBIGUOUS", "false").strip().lower() in ("1", "true", "yes")
RECONCILE_SPILL = os.getenv("RECONCILE_SPILL", "false").strip().lower() in ("1", "true", "yes")
# v5.50: RACING-ONLY Tier-2 spill. On a CONFIRMED-not-placed racing leg (reconcile
# polled /api/pending_bets and the bet is provably absent), RE-PLACE the amount on
# the NEXT eligible bookie instead of routing it to manual/unfilled — while still
# firing the critical alert. CARVE-OUT (the documented double-bet trap): NEVER spill
# a slow-landing / trader-review bookie (Pointsbet), where a land slower than the
# reconcile poll window would read "not found" and double the bet. decide_ambiguous
# already only returns 'spill' on a POSITIVE not-placed confirmation (landed=False
# from a successful poll) — never on uncertainty (reconcile_failed -> conservative)
# or a landed bet ('placed'). Requires RECONCILE_AMBIGUOUS on (the master poll switch).
RACING_RECONCILE_SPILL = os.getenv("RACING_RECONCILE_SPILL", "true").strip().lower() in ("1", "true", "yes")
RACING_NO_SPILL_BOOKIES = set(
    b.strip().lower() for b in os.getenv("RACING_NO_SPILL_BOOKIES", "pointsbet").split(",") if b.strip()
)

# ── Handicap-SGM safety (2026-05-31) ─────────────────────────────────
# Route any SGM that contains a handicap (line / first_half_line) leg to MANUAL
# instead of attempting placement. Handicap legs inside SGMs (pick_own_line
# resolution) have been unreliable; until that's hardened, alert rather than
# risk a mis-placed leg. Set false to re-enable auto-placement of handicap SGMs.
AUTO_MANUAL_HANDICAP_SGM = os.getenv("AUTO_MANUAL_HANDICAP_SGM", "true").strip().lower() in ("1", "true", "yes")

# ── X (Twitter) watcher (2026-05-31) ─────────────────────────────────
# Watches an X account, filters for one capper, and FORWARDS matching posts
# into a Telegram group that this bot already auto-places from. Runs as a
# SEPARATE process (x_watcher.py); it never places bets itself. See
# X_WATCHER_HANDOFF.md. All values are env-driven and the channel below is
# only registered when X_FORWARD_CHANNEL_ID is set, so this is INERT by
# default — nothing changes until you deliberately configure it.
X_WATCH_ACCOUNT = os.getenv("X_WATCH_ACCOUNT", "AFLCapperLeague")
X_FILTER_CAPPER = os.getenv("X_FILTER_CAPPER", "EasyMoneyAFL")
X_POLL_SEC = _env_int("X_POLL_SEC", "60")              # be gentle: >=60s avoids lockout
# Ingestion method: "twikit" (cookie API, fragile vs X anti-bot) or "playwright"
# (real headless browser intercepting the UserTweets JSON — robust against the
# transaction-id wall twikit hits). See X_WATCHER_HANDOFF.md.
X_FETCH_METHOD = os.getenv("X_FETCH_METHOD", "twikit").strip().lower()
X_BROWSER_PROFILE_DIR = os.getenv("X_BROWSER_PROFILE_DIR", "x_browser_profile")
X_BROWSER_HEADLESS = _env_bool("X_BROWSER_HEADLESS", True)
# Anti-detection: X flags the AUTOMATED LOGIN hardest. Best practice is to log in
# as a HUMAN in a normal browser, copy the session cookies (DevTools -> Application
# -> Cookies -> x.com -> auth_token + ct0), and set them here. The watcher then
# only READS with that human session — no automated login, no login challenge.
X_AUTH_TOKEN = os.getenv("X_AUTH_TOKEN", "").strip()
X_CT0 = os.getenv("X_CT0", "").strip()
# Use a real installed browser ("chrome" / "msedge") instead of bundled Chromium
# (less detectable). Empty = bundled chromium.
X_BROWSER_CHANNEL = os.getenv("X_BROWSER_CHANNEL", "").strip()
X_COOKIES_PATH = os.getenv("X_COOKIES_PATH", "x_cookies.json")
X_STATE_PATH = os.getenv("X_STATE_PATH", "x_watcher_state.json")
X_FORWARD_BOT_TOKEN = os.getenv("X_FORWARD_BOT_TOKEN", "")   # Telegram bot that posts the tips
X_FORWARD_CHANNEL_ID = os.getenv("X_FORWARD_CHANNEL_ID", "") # the Telegram group id to post into
X_FORWARD_BOT_ID = _env_int("X_FORWARD_BOT_ID", "0")        # the forward bot's user id (0 = skip sender check)
X_TIPSTER = os.getenv("X_TIPSTER", "easymoney_afl")        # Groq-only tipster name (no regex parser)
# Force these tips onto ONE bookie (HARD: if that bookie has no active session,
# the tip routes to manual — it is NEVER placed on a different bookie). Empty =
# price-shop across all AFL bookies like Saiyan. EasyMoneyAFL = sportsbet per Wilson.
X_FORCE_BOOKIE = os.getenv("X_FORCE_BOOKIE", "").strip().lower()
# Max-odds sanity CEILING for the X tipster (EasyMoneyAFL). If Sportsbet's live
# odds for the resolved selection exceed tipped x this, refuse to auto-place
# (a too-good-to-be-true price usually means a WRONG selection/line) and route
# to manual instead. Pairs with the global 0.9x floor → auto-place band
# [tipped x0.9, tipped x1.25]. Set 0 (or <=1) to disable.
X_MAX_ODDS_MULT = _env_float("X_MAX_ODDS_MULT", "1.25")
# Filter knobs — by default forward only top-level original posts.
X_DROP_REPLIES = _env_bool("X_DROP_REPLIES", True)
X_DROP_RETWEETS = _env_bool("X_DROP_RETWEETS", True)
X_DROP_QUOTES = _env_bool("X_DROP_QUOTES", True)
# AFLCapperLeague posts an automated, labeled format ("Bet: ...", "Odds: ...").
# When True, the watcher extracts the Bet + Odds into a clean "afl <bet> @ <odds>"
# line (prefix triggers the deterministic sport override; strips the capper/chrome
# noise). Falls back to forwarding the RAW tweet if no "Bet:" label is present.
X_EXTRACT_TIP = _env_bool("X_EXTRACT_TIP", True)
# Unit size for EasyMoneyAFL tips. Defaults to the $1 TEST size on purpose —
# fail-safe so an un-set go-live UNDER-stakes ($1) rather than OVER-stakes.
# SET EASYMONEY_UNIT_SIZE explicitly (e.g. to SAIYAN_UNIT_SIZE) before real-size
# placement. (Open question for Wilson — see X_WATCHER_HANDOFF.md.)
EASYMONEY_UNIT_SIZE = _env_float("EASYMONEY_UNIT_SIZE", str(TEST_UNIT_SIZE))

# Gated registration: only when a forward channel is configured. Until then
# the X watcher is fully inert and tipbot's channel set is unchanged. The
# tipster name X_TIPSTER has NO regex parser, so it's a "Groq-only tipster"
# (generic AFL parse, alerts on parse failure) — never misrouted to Saiyan's
# emoji preprocessing. The forward GROUP must have the forward bot posting and
# Wilson's Telethon account as a member (see X_WATCHER_HANDOFF.md).
if X_FORWARD_CHANNEL_ID:
    try:
        TIPSTER_CHANNELS[int(X_FORWARD_CHANNEL_ID)] = {
            "name": "EasyMoneyAFL (via X)",
            "parser": X_TIPSTER,
            "bot_id": X_FORWARD_BOT_ID or None,
            "default_units": 1.0,
            "unit_size": EASYMONEY_UNIT_SIZE,
            "sport": "afl",
        }
    except ValueError:
        # Malformed X_FORWARD_CHANNEL_ID -> do NOT register (fail safe; the
        # watcher simply won't have a place to forward to / auto-place from).
        pass

# ── Sports-only fork (sportsbot) channel filter ─────────────────────
# Applied LAST so it overrides every registration above. When SPORTSBOT_MODE is
# true (sportsbot fork only), keep just the two sports tipsters this fork runs --
# Saiyan (AFL) and Shook (NBA + MLB, sport=auto, incl. the HRRBI->2-leg-SGM
# rule) -- and drop everything else (other NBA tipsters, the internal test
# channel, and the racing / image / X channels). NO-OP in tipbot, where
# SPORTSBOT_MODE is false, so the live channel set is untouched.
if SPORTSBOT_MODE:
    _SB_KEEP_PARSERS = {"saiyan_afl", "shook"}
    TIPSTER_CHANNELS = {
        cid: cfg for cid, cfg in TIPSTER_CHANNELS.items()
        if cfg.get("parser") in _SB_KEEP_PARSERS
    }
    # Optional: the fork user's OWN test channel/group. Post any tip there and
    # it places at the $1 TEST_UNIT_SIZE gate (sport=auto -> Groq decides
    # AFL/NBA/MLB). bot_id None = no sender check, so the user's own posts count
    # as tips. Inert until SPORTSBOT_TEST_CHANNEL_ID is set in .env.
    _sb_test_id = os.getenv("SPORTSBOT_TEST_CHANNEL_ID", "").strip()
    if _sb_test_id:
        try:
            TIPSTER_CHANNELS[int(_sb_test_id)] = {
                "name": "My Test Channel",
                "parser": "test",
                "bot_id": None,
                "default_units": 1.0,
                "unit_size": TEST_UNIT_SIZE,
                "sport": "auto",
            }
        except ValueError:
            pass
