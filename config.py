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
TIPBOT_VERSION = "v5.36 (2026-06-07: SGM ONE-MESSAGE CONSOLIDATION (Wilson). A saiyan SGM spilling across 2 accounts emitted TWO 'BET PLACED' Telegram messages (per-account notify_bet_placed at the 4 _place_sgm_v4 success sites); now it emits ONE consolidated notify_tip_placed_summary at the success tail (rolls up every account, stake-weighted avg odds, per-account timing, unfilled tag) — like the AFL fan-out + the MLB orchestrator already do. Mechanism: _place_sgm_v4 (non-orchestrated) sets tip._sgm_consolidate=True; notify_bet_placed no-ops on that flag (suppressing the per-account message AND its per-leg ledger write); the tail summary writes the per-leg bets_placed.csv rows (notifier.py:613 hook) so the LEDGER is preserved + sends one message. SEQUENTIAL spillover so concurrent_bookies=False (bookie span = SUM). MLB orchestrated path UNTOUCHED (not flagged; it owns its own summary). Legacy _place_sgm (6062) UNTOUCHED (different path). test_sgm_consolidate_suppresses_per_placement; suite green. NEXT (queued, full build map in memory): full per-step timing breakdown (parse/resolve/place-wall/other, reconciling, all tips) + SGM concurrent placement w/ [400,300,200] per-account liability cap (total ~same; money-path). PRIOR v5.35: concurrent-timing display fix + roster rename (Wilson). (1) CONCURRENT-TIMING DISPLAY — the BET PLACED summary showed '(bookies <SUM>s, other 0.0s)' for the AFL/ETR fan-outs because it SUMMED the per-account elapsed times, but the fan-out places CONCURRENTLY (ThreadPoolExecutor) so the bookie-phase wall-clock is the SLOWEST account (MAX), not the sum — summing parallel work overstated 'bookies' and drove 'other' negative -> clamped to 0.0. notify_tip_placed_summary gained a concurrent_bookies flag: MAX for the fan-out callers (_place_afl_fanout, _place_etr_nba_fanout pass concurrent_bookies=True), SUM unchanged for the sequential paths (singles/SGM/MLB) + racing (notify_tiptitans_placed untouched). So 'other' now reconciles to real parse/resolve/overhead instead of 0.0. (2) ROSTER RENAME — roster_afl.json 'Brad Hill' -> 'Bradley Hill' (Sportsbet's formal name), so the Eddie tip exact-matches the catalog directly (the v5.33 _afl_canonical_catalog_player resolver still covers the general short<->full case, e.g. Matt/Matthew). suite green. NEXT (queued): SGM one-message consolidation + full per-step timing breakdown (parse/resolve/place-wall/other, all tips) + SGM concurrent placement w/ [400,300,200] liability cap. PRIOR v5.34: THREE follow-ups on v5.33 (Wilson). (1) MLB ACCENT-FOLD LOOKUP — exact_match_player now retries accent-insensitively for MLB (roster._fold_accents strips diacritics), so an ASCII Shook tip 'Jose Alvarado'/'Yandy Diaz' with NO team matches the accented MLB Stats API roster key 'José Alvarado'/'Yandy Díaz' and infers the team instead of missing -> manual. Still EXACT on the folded form (no fuzzy/partial -> no wrong-player drift); an accent-only cross-team collision (≈impossible) is guarded by requiring a single team; MLB-only (NBA/AFL exact lookups unchanged). (2) MBL VIOLATION SUPPRESS-WHEN-FILLED — the CRITICAL 'MBL VIOLATION (sports)' alert was firing on a normal code-538 ladder-down that FULLY FILLED (saiyan SGM $600/$600 at 09:03 + 09:05). New _should_alert_mbl_violation gates BOTH the SGM (_emit_sgm_aux_alerts, on remaining_stake + _orchestrated) and singles (_place_singles_v4, on unfilled) alert sites: fire ONLY on a GENUINE shortfall (unfilled > $1 deadband) AND not orchestrated (the MLB per-account model leaves expected leftover); a fully-filled / orchestrated bet logs an INFO 'benign ladder-down' instead of a CRITICAL. Mirrors the racing CB treating 'stake too high' as benign; PRESERVES the $600 cap upside (no cap reduction — Wilson's two options were suppress-when-filled vs lower-cap; chose suppress to keep the bigger stakes the ladder lands where MBL allows). (3) WEEKLY MLB ROSTER REFRESH — new scheduled task TipBot-MLBRosterUpdate (Mon 04:00) runs update_mlb_roster.ps1 (python roster.py --update-mlb + commit-if-changed; post-commit hook auto-pushes) so roster_mlb.json stays current with trades/call-ups; loads on the next TipBot-Main restart, fork syncs on next deploy. register_mlb_roster_task.ps1 (idempotent) registers it. Tests test_mlb_accent_fold_lookup + test_mbl_violation_suppressed_when_filled; suite green. 5-agent adversarial verification. PRIOR v5.33: TWO player-name robustness fixes (Wilson). (1) MLB NO-TEAM ROSTER FALLBACK — when Shook OMITS the team (e.g. 'Matt Olson 2+ HRRBI', 2026-06-07 15:53 routed to manual 'no team'), resolve_event now infers the team from a NEW roster_mlb.json (2028 entries = 1191 players + unambiguous surname aliases, built from the MLB Stats API statsapi.mlb.com; team names are ESPN-compatible e.g. 'Atlanta Braves' so resolve_mlb_event substring-matches them) via the PLAYER name (exact match first since Shook sends full names, else a GUARDED fuzzy requiring a shared >=3-char token so it can never drift to a different player), then resolves the fixture. Fires ONLY when no team is announced — a STATED team is never overridden. roster.py: MLB_ROSTER_FILE + _mlb_roster + _load_rosters + the 4 sport-branch sites + update_mlb_roster_from_api (run: python roster.py --update-mlb). (2) AFL CATALOG NAME-MATCH — _catalog_lookup matched the player by EXACT lowercased string, but the AFL roster canonicalises 'Bradley Hill'->'Brad Hill' (score 0.945) while Sportsbet lists the FULL name 'Bradley Hill', so 'Brad Hill over 19.5' (a line that WAS carried — a live catalog probe confirms a full 0.5-increment over ladder per player, e.g. Merrett 18.5..39.5; game bounced 15:15, tip 15:08 = 7min pre-bounce so props were live) routed to manual (15:08 Brad Hill BET FAILED). New _afl_canonical_catalog_player resolves the tip player to the catalog's EXACT spelling, SURNAME-anchored + unambiguous (a short/full first-name form Brad<->Bradley / Matt<->Matthew is ONLY the collision tiebreak, >=3-char prefix); never guesses a different player. Called in _match_afl_player_prop before the line lookups, so it covers AFL singles AND SGM legs. Tests test_afl_catalog_name_match + test_mlb_no_team_roster_fallback (incl. the exact failure cases below); suite green. The 5-agent adversarial verification FOUND 2 wrong-bet regressions in the first cut — (a) the MLB no-team path used an UNFILTERED fuzzy that drifted a bare/partial name ('Soto') to a same-surname player on the WRONG team; (b) _afl_canonical_catalog_player resolved a lone same-surname player with NO first-name check (Archer Reid->Harley Reid) + a generic >=3-char prefix treated Jack/Jackson, Sam/Samuel as the same. BOTH HARDENED before ship: MLB inference is now EXACT full-name match only (NO fuzzy, >=2-token gate); AFL requires first-name compatibility for EVERY candidate via a curated short<->full nickname allowlist (_AFL_FIRST_NAME_GROUPS — Brad<->Bradley IN, Jack/Jackson OUT, no generic prefix). roster_mlb.json surname aliases are accent-normalised so 'Diaz'/'Díaz' count as one (ambiguous -> no alias). Re-verified 3/3 closed, money_path_ok. PRIOR v5.32: EDDIE 2-HOUR GAME LOOK-AHEAD — the Eddie bare-surname resolver now scans AFL games up to 2 HOURS ahead (EDDIE_GAME_LOOKAHEAD_SEC=7200, was the resolver default 2700=45min), passed explicitly to afl_games_in_play from _resolve_eddie_surname_to_player. Root cause: the 2026-06-07 14:24 Eddie batch (Mills/Wilson/McInerney/Owens) ALL went to manual with 'no AFL game in the start window' because the games bounced >45min after the post. The roster team-scoping + surname-anchor + same-surname odds-tie-break + manual-on-any-ambiguity logic are UNCHANGED — only the look-ahead widened; in-progress games (the 3h behind window) are unchanged. A wider window can surface more same-surname candidates but the existing collision safety (odds tie-break, else manual; never guess a $400 bet) still applies. resolver.py docstring notes the Eddie override. test_eddie_surname_2hr_lookahead (asserts ahead_sec=7200 + a game 90min out now resolves vs the old 45min miss); existing test_eddie_surname_matcher unchanged (its fake_games mock already accepts ahead_sec). 5-agent adversarial verification. PRIOR v5.31: ETR NBA TIPSTER — blind concurrent fan-out. New Telegram group ETR (-4974425383, bot 7738582293) posts leetspeak-OBFUSCATED NBA player props ('Lu2e Ko2net (SAS) ABOVE 1.5 Points' => Luke Kornet over 1.5). New _place_etr_nba_fanout: BLIND (no price-check — leg resolved by the pure _resolve_leg_for_hyperbot transform + POSTed; NBA props place with no proposition_id; target_odds=None => fills at any price, odds IGNORED), FIXED stake ladder [100,90,80,70] ($400 unit /4 = $100 each, then 90/80/70 on a stake reject) across the 4 sportsbet accounts (ETR_NBA_SESSION_IDS 65465/53522/65463/68723), concurrent ThreadPoolExecutor, per-account ladder-down, unfilled->Manual Bets — all inherited from the AFL fan-out. Dispatch: ETR multi/SGM -> manual (SINGLES ONLY); the singles gate (ETR_NBA_CONCURRENT_FANOUT + tipster etr_nba + sport nba) calls the blind fan-out; etr_nba force-bookie sportsbet + ignore-suggested-bookie + TIPSTERS_MAX_ODDS_MULT 0.0 (odds ceiling off; parser also emits odds=0). Groq SYSTEM_PROMPT de-obfuscates leetspeak -> real NBA name (Kev's map + context digit-for-letter), ABOVE/BELOW -> over/under, stat phrase -> stat (incl PRA/PR/PA compounds + 'Reounds'->rebounds), odds=0 forced; Groq-only + roster fuzzy backstop. LIVE-validated: Lu2e Ko2net->Luke Kornet points 1.5 over, Dyl@n H@rp3r->Dylan Harper PRA 21.5 under, both odds=0, both roster-matched to player_points/player_pra. Launched LIVE at $400 (ETR_NBA_TEST_MODE=false, Wilson's call). HARDENING from the 5-agent adversarial pass: (1) single-leg guard (a misparsed multi-leg -> manual, no silent leg-drop); (2) market WHITELIST (an unmapped stat leaves market='player_prop' sentinel -> routes to manual instead of a doomed blind POST). ACCEPTED residual: wrong-player (odds guards off + no price-check, roster team-scope is the only backstop) — Wilson's tradeoff, first placements watched. Re-delivery double-bet covered by the existing text-path dedup. test_etr_nba_fanout; suite green 517/0. 6-agent research + 5-agent adversarial verification. PRIOR v5.30: AFL OVER-LADDERS — direction-aware over/under liability caps (AFL_OVERS_HANDOFF Tasks A-D). A live price_check probe of today's 2 AFL games on Sportsbet confirmed the catalog: the ONLY *_threshold market is goalscorer_threshold_afl; every other stat's OVER lives in its BASE player_* O/U market (dir=over). So over ladders are applied at SIZING (the liability cap), NEVER by routing to a non-existent market (the v5.27 regression this avoids — verified liability_market reaches only resolve_stake_steps, never the bet payload). TASK A: _place_afl_fanout flips liability_market to the *_threshold cap key for an OVER (disposals over -> [300,250,200,150]) while under/ambiguous stays the base O/U cap [124,99,74,50]; OVER detected EXPLICITLY (sel=='over' / endswith ' over' / tip._is_threshold, AND 'under' not in sel) so ambiguous->under (smaller, safer); gated by all() eligible sessions carrying the cap so a missing yaml key never leaves an over UNCAPPED; placement market unchanged. TASK B: 8 more stat over-ladder cap keys on all 4 sportsbet accounts (65465/53522/65463/68723) + KNOWN_AFL_MARKETS — goals goalscorer_threshold_afl [300,250,200] (a DIRECT key; the _threshold_afl normaliser would else strip it to a non-existent sibling -> uncapped), fantasy [249,199,149], marks/tackles/kicks/handballs/clearances/hitouts [125,100]; for 8/9 stats these are INTERNAL sizing keys (the over places on the base player_* market), only goals' key is a real market; text 'fantasy' stat aliased to the fantasy ladder. TASK C: Eddie same-team surname COLLISION (Harley+Archer Reid) now odds-tie-breaks via the live catalog — price each candidate's prop for the tipped stat+line+side, resolve to the one within ~20% of the tip odds, ONLY when EXACTLY ONE is in range AND every candidate was priced (missing-from-catalog -> manual; never guess a $400 bet); legacy 2-arg callers still route collisions to manual. TASK D: verified the AFL fan-out does NOT integer-floor/strand cents (no change). 5-agent design + 5-agent adversarial verification; suite green 502/0. PRIOR v5.29: racing spillover no longer places sub-$1 'finishing' bets + allows DECIMAL stakes (Wilson: 'allow the last bet of the spillover to place with decimals rather than a whole extra bet to finish off the last bit'). New racing_placer._round_spillover_stake: a spillover bet >= $1 rounds to CENTS (was integer-floored), so a SINGLE bet absorbs the fractional remainder (e.g. $373.68) instead of flooring to $373 and stranding the $0.68 onto a separate tiny bet on the next bookie (tonight's MYSTA $0.68 pattern); and a sub-$1 stake that CANNOT reach the $1 min is SKIPPED rather than placed — no more pointless tiny bets that also fail tab's $1 fixed-odds minimum (the JORDANO $0.73 / Felix $0.05 pattern); a sub-$1 TEST stake with room still rounds UP to $1. The <$1 remainder is left unfilled (negligible). test_round_spillover_stake. (The AFL fan-out decimal-remainder is a separate path — floors at AFL_FANOUT_MIN_STAKE so it doesn't go sub-$1; tracked in AFL_OVERS_HANDOFF.md task D.) PRIOR v5.28: REVERT v5.27's disposals-overs-threshold-only routing — it was a live regression. A catalog probe (sportsbet 53522, 2 upcoming games) confirmed Sportsbet carries NO separate disposals/marks/etc threshold market (ONLY goalscorer_threshold_afl for goals): a disposals OVER ('23+'/over) lives in the BASE player_disposals market (selection = bare player NAME, direction=over; an UNDER is 'Player Under' in the SAME market). v5.27 had routed disposals overs to player_disposals_threshold ONLY, and since that market does not exist, every disposals over was going to MANUAL (live 21:16; no AFL games in the window so it never actually bit). _match_afl_player_prop now restores the base-O/U-over fallback. NET: disposals overs place again on player_disposals (over side) at the player_disposals cap [124,99,74,50] (shared with unders for now). The desired OVER-specific ladder [300,250,200,150] + the other-stat over ladders + collision odds-check + decimal-remainder fix are a HANDOFF to the next session — see AFL_OVERS_HANDOFF.md (the over/under cap SPLIT needs a direction-aware liability_market in _place_afl_fanout because over+under share the placement market). KEPT from v5.27 (pre-staged for the handoff): the player_disposals_threshold yaml cap key, the _threshold_afl cap-lookup normalisation, the KNOWN_AFL_MARKETS entry. PRIOR v5.27: AFL disposals UNDER vs OVER now use SEPARATE liability ladders + overs are THRESHOLD-ONLY (Wilson). sessions.yaml (4 sportsbet accts 65465/53522/65463/68723): player_disposals = [124,99,74,50] (UNDER / base O/U market; 125->124 + 100->99 from v5.26's rung) and a NEW explicit player_disposals_threshold = [300,250,200,150] (OVER / 'X+' market — Sportsbet allows far higher liability on the threshold market than the O/U line). (1) The explicit _threshold key OVERRIDES the old sibling-fallback (which made threshold inherit player_disposals). (2) session_priority.lookup_liability_cap normalises a '_threshold_afl' suffix -> '_threshold' so BOTH the HyperBot market name (player_disposals_threshold_afl) AND the internal one (player_disposals_threshold) resolve to the explicit cap — previously the _afl form fell through to None (uncapped), a real risk now thresholds ladder to $300. (3) _match_afl_player_prop: DISPOSALS overs bet the THRESHOLD market ONLY — the base player_disposals over-line fallback is REMOVED; a missing threshold prop snaps to the nearest threshold +/-1.0, else None -> manual (other AFL stats unchanged). KNOWN_AFL_MARKETS gained player_disposals_threshold. test_afl_disposals_split_caps (caps for all 3 market forms + over/under routing + no-base-over-fallback). PRIOR v5.26: AFL player_disposals fan-out ladder gained a $125-LIABILITY TOP rung across all 4 sportsbet accounts (65465/53522/65463/68723) in sessions.yaml: [100,74,50] -> [125,100,74,50] (Wilson). Each account now tries $125 liability FIRST (~$147 stake @1.85, ~$184 @1.68), then ladders down to the existing 100/74/50 ($117/$87/$58) on a stake-too-high reject. $125 liability sits in the historically-rejected $125-185 stake band, so on most accounts/games it'll stake-too-high and ladder down to $117 (= prior behaviour) — it only lands the bigger stake where the live MBL allows. Pure upside, no downside (rejects ladder down; benign fast 538 rejects, don't trip anything). Covers player_disposals O/U + threshold (X+) via the sibling-fallback key. yaml-only money-path change; version bumped for the audit trail + a verifiable restart. PRIOR v5.25: Eddie AFL bare-surname player props now resolve to the AFL game ABOUT TO START. Eddie posts last-name-only props at game time ('Daniel 25+ disposals'); a bare surname previously mis-resolved (get_player_team fuzzy-matched 'Daniel'->Daniel Turner, a FIRST-name hit) or went to manual. NEW _resolve_eddie_surname_to_player (main.py): finds the AFL game(s) about-to-start/in-progress near the POST time via resolver.afl_games_in_play (Squiggle q=games `unixtime` window — no timezone reasoning), then surname-anchors the token to a UNIQUE player on those teams via roster.afl_surname_candidates scoped by resolver.team_key (bridges Squiggle short names e.g. 'Adelaide' -> roster 'Adelaide Crows'). Daniel->Caleb Daniel (NM playing); Bailey->Zac Bailey (Brisbane playing). Game-scoping collapses league-wide ambiguity (Daniel/Bailey are common FIRST names) to ~unique within the two teams; FIRST-name matches are NEVER used; a collision / two in-play teams sharing the surname / no game in the window / not-in-roster all -> MANUAL (never guesses a $400 bet; no fuzzy fall-back for bare tokens). The eddie_afl vision prompt now EMITS a surname-only player (was told to never output just 'Daniel') so the matcher can resolve it. NO Claude API / web search needed (Squiggle + roster_afl.json cover it). test_eddie_surname_matcher (11 assertions). ALSO LIVE since the 19:01 .env restart: IMAGE_TIPS_TEST_MODE=false + EDDIE_UNIT_SIZE=400 — Eddie AFL flipped from the $1/u test stake to FULL $400/u (Wilson); only affects Eddie (the sole afl image parser); Zak/Trial stay on the separate IMAGE_RACING_TEST_MODE. PRIOR v5.24: racing circuit-breaker now cools a flaky bookie SESSION after ONE session-level failure (RACING_CB_FAIL_STREAK 3->1, in both the default and .env), and on a PLACEMENT failure as well as a price-check (previously only price-checks fed the breaker). A single tab/bet365 timeout or 'betslip enquiry failed'/'Failed to get odds' now cools that session for 300s (auto-retries + resets on the first good price-check) instead of dragging ~17-30s off every subsequent tip while Soup fixes the daemon. Placement failures now call _cb_record in BOTH the PLACE FAIL and PLACE EXCEPTION branches; _CB_FAILURE_MARKERS broadened (betslip / enquiry failed / failed to get odds / bet placement failed) while FAST stake/odds rejects (code 538 'stake too high') stay BENIGN so a normal MBL ladder-down never trips the breaker. Wilson's call after a controlled test proved tab/bet365's placement path is intermittently down server-side (not our payload — same runner_match + target_odds as the bot's successful 16:26 placements). test_racing_circuit_breaker pins N=3 for the consecutive-streak/reset/stale-window cases + asserts the v5.24 single-fail trip and the new placement markers. PRIOR v5.23: forensic-batch fixes from a 7-agent placement audit. (1) Saiyan HANDICAP bets (SGM OR single) -> MANUAL (Wilson) via _tip_has_handicap_leg + a place_tip guard. The Fremantle +0.5 SGM leg had broken with 'over not found' because the SGM same-player carry-over leaked a player into the stat-less handicap leg, so it masqueraded as a player prop and evaded the handicap->manual guard; the carry-over now ONLY fires for a leg that has a STAT. (2) saiyan_afl added to TIPSTERS_IGNORE_SUGGESTED_BOOKIE so a tip Saiyan quoted at a non-Sportsbet bookie (O'Driscoll @ Bet365) still places on Sportsbet via the fan-out instead of going to manual. (3) _promote_misparsed_sgms now requires 2+ DISTINCT STATS to merge singles into an SGM — a same-player SAME-stat ladder (AusBets 'Fox over 14.5 P / over 15.5 P') stays as independent singles (was wrongly merged into a 2-leg SGM that placed nothing). (4) IMAGE_PROMPT_AFL tightened to require the player's FULL name + team (Eddie vision dropped 'Caleb' from 'Caleb Daniel' -> bare surname mis-resolved -> manual). ALSO LIVE since the 14:25 .env restart: ZAK_UNIT_SIZE/TRIAL_SNIPER_UNIT_SIZE 10->400 (Zak/Trial were under-staking 40x). PRIOR v5.22: HARDENS the v5.21 Zak/Trial text path after a 5-agent adversarial review (no money emergency found; these close the real gaps). (1) RACING-PATH DEDUP — the racing path (image AND text) had NO dedup, so a reposted/re-delivered tip DOUBLE-PLACED real money; added a fingerprint skip (tipster,track,race#,runner,saddle,market,date) within DUPE_WINDOW_SECS in _route_image_racing_tips, registered BEFORE placing (covers image + text). (2) parse_racing_text now RAISES on a HARD failure (Groq down / bad JSON) so _process_text_racing_tip routes the tip to MANUAL instead of silently DROPPING it (was a lost-tip regression — the 'never lose a tip' fallback was dead code); a valid-but-empty parse still returns [] -> chatter -> drop. (3) RESULTS GUARD — _text_looks_like_result routes a results/recap post ('R7 Lingani WON at 4.50') to MANUAL before parsing, so a settled race cannot auto-place. Tests: test_racing_dedup + results/parse-fail assertions. PRIOR v5.21: (1) Zak/Trial TEXT-TIP PLACEMENT — actionable TEXT posts on the racing image channels (e.g. 'Adding Lingani for tomorrow') are now PARSED (new groq_parser.parse_racing_text, same racing schema the vision path emits) and routed through the SAME racing pipeline as images, so a genuine text tip PLACES at the live unit size. The relative-date helper (_img_parse_racing_date: tomorrow/6-6/weekday), runner-match, price floor/ceiling, 3u image cap, and runner-only/no-race# -> manual (Guard 2) ALL apply unchanged. Chatter (parser returns no runner) is DROPPED with no manual ping; a parse ERROR falls back to a manual alert so a real tip is never lost. AFL image channel (Eddie) keeps the old manual-alert behaviour. (2) bets_placed.csv LEDGER — new bet_ledger.py writes ONE row per LANDED bet (19 cols, CSV for Excel Power Query) at every placement-success across ALL paths (sports via notify_bet_placed + notify_tip_placed_summary, racing via notify_tiptitans_placed); bet_id dedup, fully guarded so a logging failure can NEVER break a placement; closes the audit coverage gap (racing + MLB previously wrote no machine-readable placement record). PRIOR v5.20: AFL fan-out UNFILLED now = the FULL gap vs the intended UNIT (intended - placed - maybe-landed), routed to Manual Bets. PRIOR (v5.13) a ladder-DOWN was 'expected, not unfilled' so the remainder was silently dropped (Tim English placed $340 of a $600 unit -> shown as 'placed $340 of $340', $260 never flagged). Wilson wants the WHOLE remainder placed by hand: BOTH the ladder-down shortfall AND the part the 4 SB accounts' liability brackets can't hold. Ambiguous (maybe-landed) stake stays COMMITTED (excluded from unfilled, not re-prompted). Applies to ALL AFL fan-out (Saiyan + Eddie). $1 deadband. PRIOR v5.19: Telegram STARTUP PING shows the version NUMBER only (TIPBOT_VERSION.split(' (')[0]), not the full changelog — the growing TIPBOT_VERSION string overflowed Telegram's 4096-char limit (HTTP 400 'message is too long' on the v5.18 startup notify). The file-log banner still records the full TIPBOT_VERSION. Cosmetic; no money-path change. PRIOR v5.18: MLB HRRBI leftover->manual GUARD against stale/replayed tips. A sportsbot fork instance (running the v5.16 code synced today) emitted FOUR identical Freddie Freeman 'BET UNFILLED' alerts — Event UNRESOLVED, empty Raw, '@ 0' odds, 'no error captured', $348/$400 — for an overnight Shook tip re-delivered on a flaky telethon reconnect (OUTSIDE the 10-min dedup window). This tipbot (v5.15) did NOT send them. FIX: _place_mlb_hrrbi now only fires the leftover->manual alert (the v5.16 Happ fix) when the tip is GENUINELY live — require a resolved tip.event OR a non-empty tip.raw_message as proof-of-liveness; a degraded replay (both empty) logs a warning and is suppressed instead of spamming Manual Bets. Root cause (re-delivery dedup window) noted as a follow-up. PRIOR v5.17: Eddie AFL HALF/QUARTER period guard — Eddie tips a 'Hawthorn -5.5 2nd Half Line' but the vision dropped '2nd Half' and it was treated as a FULL-GAME -5.5 line (caught only by the odds floor -> manual today, but would mis-place if the price matched). FIX: the eddie_afl vision prompt now captures a `period` field (1st/2nd half, quarters), and _build_afl_tip_from_image routes ANY non-full-game period to manual (the bot's AFL catalog is full-game only) — applies to lines, totals AND player props. PRIOR v5.16: forensic fixes — (a) MLB player-match Tucker bug: _resolve_mlb_player returned None silently (sub-0.82 fuzzy or ambiguity-tie) so 'Kyle Tucker' routed to manual though carried; added a SURNAME ANCHOR (unique catalog surname + compatible first name -> match the variant, never drift to a different player) + logged the previously-silent return-None branches. (b) MLB HRRBI leftover now ALSO fires a Manual Bets alert (notify_tip_unfilled_with_placements), not just the inline 'Unfilled $X' tag (Happ's $52 went unsent). (c) AFL_SGM_SESSION_PRIORITY=65465,53522,68723 — added Wilson 53522 + Daniel 68723 as SGM spillover partners (both afl.sgm:600, SGM-capable) so AFL SGM overflow rolls over instead of all-to-manual. PRIOR v5.15: (1) MLB HRRBI SGM tries a $100 STAKE rung FIRST, then the existing [87,85,80] ladder (MLB-scoped prepend in _place_sgm_v4; no pre-place SGM odds so 100 is a stake step, not a converted liability). (2) Zak/Trial racing-image plays flipped to FULL unit sizing ($400/u): fixed a bug where the production stake used the channel's default_units (1.0) instead of unit_size (so it never actually staked at the configured size); added a SEPARATE IMAGE_RACING_TEST_MODE gate (default false=live) so Zak/Trial go full-size WITHOUT un-testing Eddie AFL (still $1/u via IMAGE_TIPS_TEST_MODE). The 10% odds floor, runner-match, 1.5x wrong-horse ceiling, route-remainder/whole-to-manual, and 3u per-runner cap already existed (shared with Tip Titans). Builds on v5.14.) === ORIGINAL v5.14: AFL fan-out re-enables the WRONG-SELECTION ceiling. After the v5.13 scrutiny flagged that the odds ceiling was also a wrong-selection guard (a catalog-valid-but-wrong pick — same-surname / ±1.0 line / wrong-O/U snap — was placing across ALL accounts), Wilson chose 'ceiling only'. _resolve_single_for_placement's apply_odds_guards flag is split into apply_ceiling + apply_floor; the fan-out now resolves with apply_ceiling=True, apply_floor=False — so a live price > 1.25x tipped routes the whole tip to manual (off the resolve-time catalog odds, no extra call), while a shorter-than-tipped live price still places. All other paths (NBA/MLB/handicap/total/SGM/racing via presolved=None) keep BOTH guards (defaults). Residual (accepted): longer-fill liability overshoot + h2h-no-odds uncapped sizing still rely on bookie MBL. Builds on v5.13.)"

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
