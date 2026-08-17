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
TIPBOT_VERSION = "v6.09 (2026-08-17: NAME WHAT A RESTART SKIPPED, AND MARK A PARTIAL FILL AS PARTIAL. 21-agent audit of the 08-17 racing slate, every finding adversarially verified. THE DAY WAS NEAR PERFECT: $2,150.00 tipped, $2,149.62 landed, $0.38 short (correct bookie rounding plus a correct sub-$1 guard); the spill ladder recovered every other dollar at risk, including $148.85 of bet365 auto-cap, $153.85 of schema_changed, $216.35 of moved odds and $318.72 of insufficient funds. CORRECTING MY OWN DIAGNOSIS OF THE SLOW TRIAL TIP: I said bet365 latency was the problem and that roughly 100 of the 112 seconds went to bookies that never filled it. WRONG ATTRIBUTION. Measured on Gay Tarantella ($400, 112.06s): OUR OWN post-failure pending_bets reconcile ran TWICE for 66.33s = 59.2%, versus bet365 gating the shop 10.28s = 9.2%, bet365:73361 attempt 11.00s, bet365:116964 attempt 8.86s, betright attempt 9.04s, and everything else including all three Sportsbet fills just 6.55s. The largest single cost was our own safety check, not a bookie. Two further timeline errors of mine: +64.2s was the bet365 reconcile COMPLETING, not the betright failure, and sportsbet:118458 at +106.4s was when its attempt STARTED, not when it landed. bet365 quote bought NOTHING on that tip: betright quoted the identical 7.5 at +0.882s, ten seconds earlier. Sportsbet took the whole $400 in 5.78s once reached. ALSO: per-tip latency savings CANNOT be summed, since racing tips run concurrently and the log interleaves - a naive sum of the weekly bet365 saving reads 912.2s against a true merged 317.4s, inflated ~2.9x. (1) A COLD RESTART MADE ITS SKIPPED TIPS PERMANENTLY UNRECOVERABLE: the 13:55 restart skipped four Tip Titans tips and the alert carried IDs only. A skip happens BEFORE any processing so nothing else on disk records the tip, and the audit could bound those four only as somewhere between $0 and roughly $2,800, the largest unquantified number of the day - while the feed rows were in scope at the alert the whole time. Now rendered by describe_missed_tips (titan, event, selection, units, odds per id), at WARNING in the log as well as the Telegram so detail survives a missed alert; an id with no feed row still reports, because the id alone beats nothing. (2) AN AUTO-CAPPED ACCEPT WAS INDISTINGUISHABLE FROM A FULL FILL: Sportsbet/Neds/Ladbrokes/bet365 all silently accept a token amount and return success, so every fill-rate number derived from this log was wrong - bet365 reads 7 of 32 (21.9%) but 4 were token accepts, a genuine 3 of 32 (9.4%), and betright reads 14 of 15 (93.3%) against a genuine 8 of 15 (53.3%). The success line now carries partial=true requested=$X filled=$Y via partial_fill_marker. Prerequisite for any ladder-ordering work, since ordering by a rate 2.3x too high for bet365 is worse than not ordering. Zero behaviour change; returns empty on non-numeric input because it runs AFTER the money is on. BOTH were extracted into named importable helpers specifically so the tests call PRODUCTION code: the first cut asserted on source text near the call site and a deliberate sabotage of both fixes still PASSED. Same antipattern this project has hit before, so the rule is explicit now - a test that greps source is not a test. ALSO FIXED A TIME-DEPENDENT TEST OF MY OWN: test_topup_retire_and_alert hardcoded queued_at 2026-08-15 and the top-up queue has a 36h hard expiry, so three tests passed the day they were written and failed two days later against UNCHANGED production code. Now relative to datetime.now(). REPORTED NOT FIXED: the reconcile costs 826.3s/week over 25 events, all 25 NOT FOUND, median 33.1s; the only promising bucket is auth_rejected (n=11, 364.6s) but nothing establishes it is a PRE-SUBMISSION refusal and skipping a reconcile on a rejection that actually landed is a DOUBLE STAKE - needs HyperBot to confirm where outcome_code=auth_rejected is raised, and must NEVER include the stake-too-high strings (a reproducible 2026-05-03 incident shows a 538 back in 33.3s that HAD landed, $525 exposure on a $400 tip). bet365 is BIMODAL: its no-answer responses median 0.79s but its real quotes median 13.27s with a 4.12s MINIMUM, so it is slow precisely when useful; it held best price in 16 of 39 shops but converted almost none (32 attempts, 7 landed, 3 genuine, $921 of $7,043 requested) for a realized weekly benefit of $32.50 gross / ~$14 EV. Ops option first: re-auth both sessions, since 13 of 25 failures are auth_rejected first seen 08-08 and the burst has stopped. insufficient funds sits in _CB_BENIGN_MARKERS so an unfunded account NEVER trips the breaker and is re-attempted every tip (24 rejections on neds:100006 in 7 days); do NOT just move it to the failure list, since replaying all 51 rejections shows a stake-blind 300s cooldown would have blocked 4 genuine fills worth $388.89 - the correct design is a per-session stake CEILING with a TTL. 766 green, 9 new tests, each verified by sabotaging the fix and watching it fail.) --- v6.08z (2026-08-16: AUDIT OF THE 08-15 SLATE - TEST POLLUTION, A SILENT GIVE-UP, AND A WASTED RETRY BUDGET. 25-agent audit, every finding adversarially verified. THE DAY: $19,800.00 tipped, $12,634.71 HARD-LANDED, $7,165.29 short, of which ~$2,373 is NOT a bug (the SB to-win cap doing its job + one correct exact-line refusal), leaving ~$4,792 of real defect and ops failure. CORRECTIONS TO MY OWN EARLIER REPORTING: (a) landed $13,020 was placed PLUS ambiguous, and ambiguous is NOT landed (placed $12,634.71, $402.08 maybe-on under Guard B); (b) the $2,000 of team/total/SGM that could not be mapped to a fixture was WRONG - all three resolved correctly and $879.17 LANDED; the gap is a REPORTING defect, since the structured legs array is gated on legs[0].market==player_prop so team handicaps/totals/threshold SGMs write only raw+stake (240 SGM records, only 72 carry legs); (c) SB fixture matching is NOT per-call non-deterministic - 6 probes in 7s returned the same board and 6 more on 08-14 were deterministic per string; it is stable for MINUTES and drifts over TENS of minutes, so an outer retry loop recovers $0.00, and my memory has been corrected. (1) THE TEST SUITE WAS WRITING FAKE BETS INTO THE PRODUCTION AFL LOG, a regression from yesterdays own test files, which drive the real _execute_bet/_place_afl_fanout with REAL fixture names. _afl_log_event had no TIPBOT_TESTING guard, so every run appended PLACED bookie=sportsbet sid=53522 bet_id=B1 odds=1.85 stake=$100.00 to apiafl.log, indistinguishable from a genuine placement (MEASURED 10 per run of ONE file, and the pre-commit gate runs the WHOLE suite on every commit). That file is the AFL incident trail and the entire 08-15 investigation was run off these logs. Now REDIRECTED to a temp path, not silenced. THIRD occurrence after the live Saiyan queue (v6.08q) and the live Telegrams, so the test asserts the production file DOES NOT GROW rather than checking a flag. (2) SAIYAN TOP-UP STAKE GAVE UP SILENTLY AND BURNED ITS BUDGET ON GUARANTEED REFUSALS: at 13:47:49 FOUR selections were dropped in ONE tick totalling $1,099.80 (largest $559.04) with SIX HOURS TWENTY-THREE MINUTES still to run before the 20:10 bounce, a fifth at 13:19, and every drop was a bare log.warning so none of it could be hand-placed. A not-carried failure now RETIRES at attempt 1 (SB has moved the line off the board and the exact-line rule refuses identically forever - MEASURED, Bergman spent all 8 attempts that way with zero POSTs and zero 538s after his two-way line moved 20.5 to 19.5), while a 538 KEEPS retrying because capacity exists but is currently full and the cap RISES toward bounce (a mixed fan-out result containing any 538 also keeps retrying, pinned by a test). A give-up now alerts once per tick, batched, with the dollar total. Deliberately does NOT touch SAIYAN_TOPUP_MAX_ATTEMPTS: v6.08q shipped that cap on purpose and 40 attempts on a moved line produce 40 correct refusals. (3) A 538 CARRYING NO USABLE MAX WAS INVISIBLE: across all 20 POSTs of the Karl Amon fan-out at 13:57 (5 accounts x 4 rungs, every one 538) the max-stake rebet never fired, because _sb_max_stake_target returns None SILENTLY when SB sends no max_stake or one below $1.00, and the existing SKIPPED warning sits inside the is-not-None branch so its absence proved nothing. Whether ANY fill was available on Amon at 13:57 is STILL UNKNOWN. Now logged either way. The mechanism itself is healthy, firing 61 times on the day down to SB max $1.10. REPORTED NOT FIXED: $2,300.00 (the largest single item) - four selections never POSTed because AFL event verify could not resolve any name form (McKercher, Caleb Daniel, Luke Parker, Macrae); the real fix is to resolve by SB EVENT ID (16251728 Richmond / 16251729 North Melbourne / 16251730 Brisbane already recorded) instead of by name, and explicitly NOT by accepting comp_id==17131, since every mens Round 23 fixture shares that id and it would let a Richmond tip price and bet the North Melbourne board. $990.00 OPS: sessions 53522 and 68723 ran out of money from 11:59 (80 HTTP 508 lines) and 65465 was DEAD at startup, turning the 3-way SGM split into a 2-way one and zeroing a $750 tip. $806.24 conditional: the top-up ladder floor (Horne-Francis had a single rung [49.44] against a list-cap floor of 50.0, while Berrys $22.92 filled on the same tick, proving capacity existed below it). $252.78: a reconcile-CONFIRMED-not-placed 538 leg is never re-attempted even though reconcile has positively proven nothing landed. 757 green, 9 new tests.) --- v6.08y (2026-08-15: THE ZERO-FILL ALERT ASKED THE WRONG QUESTION AND STARTED PAGING HOURLY. Same-day correction to v6.08x, which made a DisposalsModel tip page when nothing landed - right for Xerri/Zorko (whole $500 gone, silent) but gated on total_placed>0, i.e. 'did THIS tranche fill', which is too blunt. Kysaiah Pickett had $425.74 GENUINELY on the book with the model re-offering the last $74.26 hourly exactly as designed; each re-offer placed $0.00 against SB's cap, so from 14:14 every hourly retry fired a fresh BET FAILED (retry6 at 15:02 was the first Wilson saw) and it would have paged until bounce - the Bergman noise pattern one release later. The right question is whether the SELECTION is alive, not whether this tranche filled: the gate now also reads the exposure ledger via tip._disposals_sel_id for PRIOR fills (the current run's commit happens after the fan-out returns, so it sees earlier tranches only) and nets off ambiguous, because maybe-landed dollars are exactly the ones that might not be there - which keeps the XERRI shape alerting, since committed $100 that is entirely ambiguous is not a live selection. FAILS OPEN: unreadable ledger, missing sel id or any exception leaves the money-losing case alerting. OPERATOR ACTION DONE: $200 of frozen exposure RELEASED after Wilson confirmed at SB that neither bet is on the account (Xerri $100 Alex Liu 65463, Zorko $100 Denzel 118458); both back to committed 0.0. FOUR MORE still frozen and needing the same eyeball: Archie Roberts $100 (reads FULL at $500/$500 so it can NEVER be re-offered), Bergman $73.60, Laird $49.42, Amon $7.60. MEASURED NOT FIXED: 13 ambiguous placements today, ALL the 15s place_timeout on /v3/place_bet, and NOT evenly spread - Denzel 118458 has 6, Alex Liu 65463 and Ryan Suwandy 102506 have 3 each, Wilson 53522 has 1, and 4 of the 5 STILL ABSENT findings are on Denzel. Per-account latency problem, separate from the fixture bug, and each one freezes money via Guard B. 748 green, 3 new tests.) --- v6.08x (2026-08-15: THE VERIFIED FIXTURE WAS PROVED AND THEN THROWN AWAY + A TOTAL MISS WAS FILED AS A SHORTFALL. Three DisposalsModel selections lost their whole $500 today ($1,500 never on) and the operator heard about one; found by tracing all 15 per-account placements. (1) CRITICAL: _price_check_afl_verified establishes which name form reaches the MEN'S board and logs 'using it', but that answer was bound to a local, never read again, and _execute_bet rebuilt the payload from tip.event. MEASURED 5ms apart: 10:01:52,798 'North Melbourne v Geelong' matched the WRONG fixture; 'Geelong v North Melbourne' resolves to the right one (comp_id=17131) - using it / 10:01:52,803 payload event='North Melbourne v Geelong'. HB re-matched the UNVERIFIED string: all 5 Xerri POSTs hit the AFLW board (comp 46287, rejected against a list of AFLW players) and all 5 Zorko POSTs hit comp 44839, which carries only head_to_head+line. 4 of each 5 were clean rejections; the 5th timed out, was held by Guard B as debit-as-placed, and was found ABSENT by the deferred verifier 466s later. The guard was doing real work and its conclusion was discarded at the one moment it decided money. The verified name now returns as event_verified and _execute_bet prefers it, still via _bookie_event (GWS alias intact) and falling back to the exact previous expression when nothing was verified. Macrae failed DIFFERENTLY and correctly: no name form resolved, no POST was made, and that one DID alert. (2) HIGH: the disposals branch skipped the unfilled alert because 'the model re-offers the shortfall' - sound for a PARTIAL fill and unchanged, but false at placed==$0.00: no live selection, nothing for the ledger to cap, and MEASURED, no re-offer ever came (the only later mention of either name is the 10:10 deferred verify). The suppressed call renders as BET FAILED when nothing placed, so the one message that would have said 'this tip is dead' was the one swallowed, taking the errors.jsonl tip_unfilled row with it. Now gated on total_placed>0. CORRECTING MY OWN OVERSTATEMENT: get_pending_bets returned {bets:[],count:0} on all 5 SB accounts and I reported that as the BOOKIE confirming nothing landed. It does not - the same endpoint returns zero for a landed-and-confirmed bet, and get_transactions shows no trace of Pickett's known-landed $8 from the same morning, so neither can distinguish 'never placed' from 'placed and settled'. The 4 clean rejections per player ARE definitive; the two $100 ambiguous slices are NOT. LEFT UNFIXED ON PURPOSE: $200 frozen exposure (Xerri and Zorko each committed 100.0 / ambiguous 100.0 of a $500 target, so each reads $100 fuller than the book). _disposals_release_ambiguous would clear it but is an OPERATOR tool by design - releasing on absence of evidence re-arms the double stake Guard B prevents, and no endpoint can prove absence, so a human must eyeball the two accounts. 745 green, 7 new tests, each verified by reverting the fix and watching it fail.) --- v6.08w (2026-08-15: A FAILED TOP-UP RE-ASK PAGED THE OPERATOR EVERY TIME. Saiyan tipped Miles Bergman UNDER 20.5 disposals for $600; $375.20 filled against SB's to-win cap and the $224.80 shortfall was queued for top-up. SB then moved Bergman's two-way line 20.5 -> 19.5 (measured live 13:22: Over 19.5 @1.85 / Under 19.5 @1.88), which for an UNDER is strictly WORSE, so the exact-line rule refused every re-ask. THAT REFUSAL IS CORRECT AND IS UNCHANGED. The bug is that each refusal fell through the ordinary 'line not carried -> route to manual' path, which ALERTS: 7 attempts produced 7 identical manual-bet Telegrams for one tip already reported. A top-up is a BONUS attempt on stake that already alerted once, so a failed one is a no-op and must be silent; only the SUCCESSFUL top-up still notifies, because that one carries new information (money moved). Suppressed CENTRALLY in notifier (_place_afl_fanout alerts from several sites and a per-site patch would leak next time one is added), gated on the _saiyan_topup marker _saiyan_topup_tick sets on its synthetic tip, so an ORIGINAL failure pages exactly as loudly as before - pinned by a test that fails if the guard widens to catch it. REVIEW CAUGHT THAT THE FIRST CUT SUPPRESSED FAR TOO MUCH and was right: _place_afl_fanout funnels FIVE different failures through ONE notify_bet_failed call, so the marker alone also silenced 'No active HyperBot sessions' (6922), 'No auto-placement configured' (6931) and 'No priority sessions configured' (6946) - a session/auth/proxy outage can BEGIN between the original placement and a retry tick, which is new information, not a duplicate, and silencing it would have recreated the exact complaint this change answers. The guard now needs BOTH the marker AND a reason on a two-entry allowlist (not carried / no placeable accounts) and FAILS SAFE: an unrecognised reason, or none at all, still pages. 6 parametrised tests pin each must-still-page reason. ALSO RECORDED FROM LIVE PROBES WHILE DIAGNOSING: the v6.08v AFL/AFLW collision fired for MONEY today - all three North Melbourne v Geelong tips ($600 each, $1,800) were refused at 11:36 because BOTH name forms returned comp_id=46287 (AFLW), 10 markets, 2h off the real bounce; the guard did its job but two candidates were not enough to find the men's board. NO EVIDENCE THE MEN'S AFL ID CHANGES: every men's Round 23 fixture probed returned 17131 with sequential event ids (16251728 Richmond / 16251729 North Melbourne / 16251730 Brisbane); what varies is which competition a NAME collides with, not the id. AND A THIRD COMPETITION ANSWERS TO AN AFL NAME: 'Richmond v St Kilda' returns comp_id=44839 event 16297210 bounce 14:05 with only Head to Head + Line, while the REAL fixture is event 16251728 comp_id=17131 bounce 12:35 - the START-TIME check caught it and fell through to 'St Kilda v Richmond'. That is the case FOR start-time verification over a competition-id blocklist: 44839 is on no blocklist and was rejected anyway, on evidence. 729 green + 14 new.) --- v6.08v (2026-08-14: PROVE THE MATCHED FIXTURE, DO NOT JUST NAME IT. Sportsbet carries an AFLW fixture under the SAME team names as the men's AFL one and HyperBot's matching between them is NAME-BASED and NON-DETERMINISTIC: the single string 'Fremantle v Adelaide Crows' on session 118458 returned the MEN'S board at 12:12 (109 markets, Rory Laird present), the AFLW board at 12:37 (6 markets, no Laird) and the men's again at 12:58. Same request, three answers. It cost a DisposalsModel tip (Laird U22.5, $500) and three Saiyan tips ($1,800) in one afternoon, each logged as the misleading 'line not carried in catalog'. IT FAILED SAFE ONLY BY LUCK - no AFLW player is named Laird; a shared surname would have priced and possibly PLACED against the wrong competition. BOTH ALIAS ATTEMPTS REVERTED, and the second made it WORSE: 'Adelaide'->'Adelaide Crows' (v6.08s) is what pushes the match ONTO the AFLW board, and 'Fremantle'->'Fremantle Dockers' (v6.08u) worked for seventeen minutes. An alias that works half the time is worse than none because it reads like a fix. Only the stable GWS alias remains. The real guard is _price_check_afl_verified: ask, then CHECK start_time_iso against Squiggle's real bounce, and retry other name forms (including the REVERSED pairing, which is what actually reached the men's game) until one is genuinely the right fixture; if none is, return None and route to MANUAL rather than price a different competition. Verified live: detects the 42h drift and competition_external_id 46287, retries, lands on 17131 at 20:10. It only ever tightens on POSITIVE evidence - no Squiggle bounce, or no start_time_iso on the envelope, means it cannot verify and returns the first response exactly as before, so a Squiggle outage never blocks every AFL bet. The common case still costs ONE call. KEPT from v6.08s as a genuine latent bug: _bookie_event aliased by substring .replace(), so ANY 'Adelaide' alias would have rewritten every PORT ADELAIDE fixture to 'Port Adelaide Crows'. It now aliases each whole side of 'A v B'. RECOVERED BY HAND meanwhile: Saiyan's Luke Ryan U19.5 re-placed IN FULL ($600 at 1.79 across four accounts) after confirming the line had not moved; Jordan Clark (U20.5, board moved to 19.5) and Hayden Young (U19.5, board moved to 18.5) correctly SKIPPED since rebetting a moved line is a different bet; Rory Laird reached ~$302 of $500. 8 new tests. 717 green.) --- v6.08t (2026-08-14: THE TEST SUITE WAS PAGING WILSON ON EVERY DEPLOY. Wilson: 'why so many saiyan top up msgs for dangerfield?' - they came from the TEST SUITE. test_saiyan_topup stubs the fan-out to return a SUCCESSFUL top-up, so _saiyan_topup_tick fired a genuine 'Saiyan top-up: $X more on Patrick Dangerfield' alert on EVERY full-suite run, and the suite runs on every deploy through the pre-commit gate. 'Patrick Dangerfield' is the FIXTURE name in that test file, not a real tip. notifier._send had NO TIPBOT_TESTING guard at all, so ANY test reaching ANY notify path sent a real message. Fixed at the single transport choke point rather than per test, where the next one would forget: under TIPBOT_TESTING the message is logged and the function returns True so tests asserting on the return value keep their meaning. Verified by running the whole suite and grepping for 'Telegram send ok': ZERO. SAME CLASS as the 08-10 incident where the suite wrote 28 fake items into the LIVE Saiyan top-up queue and production tried to place them. Both were tests reaching production side effects; both are now blocked STRUCTURALLY. test_notifier_send_retry is the one legitimate exception (it exercises the transport's own retry logic) so it opts out explicitly and restores the flag, with requests.post stubbed throughout. A new test proves no notify helper can bypass the guard AND that the guard does NOT fire in production - if it ever did, every alert would vanish silently, far worse than the spam. 4 new tests. 709 green.) --- v6.08s (2026-08-14: SPORTSBET'S AFLW FIXTURE SHADOWED THE AFL ONE, SENDING TIPS TO MANUAL. Wilson: 'why did new tip for laird today go to manual? sportsbet has it as a proper market' - he was right, the market was there the whole time. A DisposalsModel tip on Rory Laird UNDER 22.5 @ 1.85 refused with 'line=22.5 not carried in catalog'. SB carries an AFLW fixture under the SAME plain team names and that is what we priced. MEASURED: 'Fremantle v Adelaide' = 6 markets / 98 disposals / ALL OVER / zero unders / no Laird (the AFLW board); 'Fremantle v Adelaide Crows' = 109 markets / 831 disposals / 16 unders / Laird PRESENT, with the tipped line sitting there as UNDER 22.5 @ 1.89. IT FAILED SAFE ONLY BECAUSE NO AFLW PLAYER IS NAMED LAIRD - a shared surname would have priced, and possibly placed, against the WRONG COMPETITION. Round 23 is the first slate where the two seasons overlap. Fixed by aliasing Adelaide -> Adelaide Crows for sportsbet, AND THE MECHANISM HAD TO CHANGE FIRST: _bookie_event did a plain substring .replace(), safe only because the one pre-existing alias (Greater Western Sydney) is not contained in another club name. An 'Adelaide' alias would have rewritten every PORT ADELAIDE fixture to 'Port Adelaide Crows' and broken them all. It now splits on ' v ' and aliases each side as a WHOLE name; the no-' v ' fallback is whole-string only (an earlier draft guarded by comparing against other alias KEYS, which does not help since 'Port Adelaide' is a club name not an alias key - its own test caught it). SCOPED TO ADELAIDE DELIBERATELY: blanket nicknames are ACTIVELY HARMFUL - 'North Melbourne v Geelong' returns 109 markets as-is and collapses to 9 as 'North Melbourne Kangaroos v Geelong Cats'. A test pins the alias map to exactly two entries so no club is added without probing the live catalog both ways. 7 new tests. 705 green.) --- v6.08r (2026-08-13: A LOST POST WAS SPILLED AS CONFIRMED-NOT-PLACED + THE LEDGER COULD NOT NAME THE RUNNER. From a bug hunt over the 08-10..12 logs (the hunt hit a session limit; these are the completed sweeps, each verified by hand). (1) CRITICAL: 08-11 08:37:55 sportsbet returned '[placement_uncertain] bet POST answer lost (TLSClientExeption); Sportsbet may hold this coupon (elapsed 31.9s)'. No unresolved-cid marker matched, so a 30s pending_bets poll read absence as PROOF and $500.00 was SPILLED to the next bookie 33s later - if SB then registered the held coupon that is $500 of unintended double exposure, off-ledger. The bookie saying it MAY hold the coupon is the strongest maybe-landed signal there is and 30s cannot disprove it. Added placement_uncertain / answer lost / may hold this coupon to _CID_UNRESOLVED_MARKERS in BOTH racing_placer and main (documented as kept in sync, had drifted; a test now pins them equal). REVIEW CAUGHT THAT THE MARKERS ALONE WERE NOT ENOUGH and was right: the block is gated on _racing_slow OR resp['ambiguous'], and the bookie never sets ambiguous for this envelope, so the incident was only caught because it took 31.9s - a TLS drop answering in under 5s would still spill undebited. outcome_code=='placement_uncertain' is now an INDEPENDENT trigger so timing does not decide money safety. (2) HIGH: tip 62701 read 'RUBY RHAYNE BOW' and all four priced bookies bound saddle #5 to 'Rebecca Rhayne Bow'; $700 landed and the ledger stored only the TIPSTER's string, so the mismatch was invisible to every downstream review. runner_match + match_method now flow from the quote into bets_placed.csv (selection keeps the tipster wording, which the Bet Record feed depends on). REVIEW CAUGHT that only 2 of the THREE placed-append sites were updated - the missed one is the reconcile-confirmed-placed branch, where a saddle guess PLUS a slow/ambiguous cycle is the worst case for this exact bug class. A test now counts the sites structurally. (3) All 13 spill alerts rendered an empty '()' because _r_recon has no reason on that path, so the one dangerous spill looked identical to twelve safe ones; both messages now fall back to the bookie's own error text. (4) _CB_FAILURE_MARKERS had no auth_rejected / addbet refused / access token / not ready for betting, so a broken-auth session stayed in the shop and was retried every tip (bet365 18 attempts / 4 landed / 5 auth-rejected in 3 days, with ONE breaker trip in that period and not on either failing bookie). CORRECTNESS fix, not throughput: measured saving is only ~42.6s over 3 days because the shop runs CONCURRENTLY. ALSO CORRECTED MY OWN LATENCY ANALYSIS: the first table attributed gaps inside the CONCURRENT price shop, where cost is the SLOWEST responder not the sum, so 'bet365 = 22% of wall clock' was WRONG. Measured properly, tabtouch is the bottleneck (5.88s median, last to answer in 48% of shops) while bet365 is among the FASTEST at price-check (0.82s) and slow only at placement (~11% of total). Shopping costs ~8x placing; the real long tail is shop COUNT (median 1/tip but the SA track resolver fires 14-53). REPORTED NOT FIXED: no race-started guard for any non-tiptitans racing tipster (_jump_epoch has ONE writer, tiptitans_processor:704, and the guard fails OPEN when absent, so Zak/Trial/Leroy have none - HB's racing price response carries no start time, so a staleness bound would either block good bets or fail to protect); the SA track probe-order hint (key on track + RACE date, never the lookup date, and REORDER the probe rather than replace the name check); the thoroughbreds cap applied per bet vs documented per account; and highest-odds-first ladder ordering, which is a price-vs-speed policy call. Window was otherwise CLEAN: 84 landed bets, $15,513.16, cross-checked both directions with zero orphans, zero duplicate bet ids, zero double stakes. 6 new tests. 693 green.) --- v6.08q (2026-08-10: STOPPED THE SAIYAN TOP-UP SPAM. TWO BUGS, BOTH MINE, BOTH FROM v6.08p. (1) THE TEST SUITE WROTE INTO THE LIVE QUEUE: test_afl_sgm_fixes.py drives the REAL _place_afl_fanout with a saiyan_afl tip and does not patch SAIYAN_TOPUP_PATH, so the deploy's OWN pre-commit gate wrote 28 fake items ('Team A v Team B', player 'P') into logs/saiyan_topup_queue.json. Production then re-asked each of them and paged BET FAILED every attempt. Queueing now REFUSES while TIPBOT_TESTING is set, and a full suite run leaves the live queue empty (asserted by a test). (2) RETRIES DID NOT STOP AT GAME START (Wilson: 'need to stop the retries after the game starts'). resolver._fetch_afl_fixtures_by_year() returns only INCOMPLETE games, so a fixture DISAPPEARS from it the moment it starts - which is precisely when to stop - and the old code read an unresolvable bounce as 'keep going', re-asking until the 36h expiry. That is how one slate reached ATTEMPT 21. Now fails SAFE: if we cannot PROVE the game has not started, we drop it. Plus SAIYAN_TOPUP_MAX_ATTEMPTS=8 as a clock-independent backstop. Ops: queue cleared (backup logs/saiyan_topup_queue.json.polluted_bak), kill-switched and restarted within minutes of Wilson reporting it, then re-enabled once both fixes were tested. 3 new tests. 670 green.) --- v6.08p (2026-08-10: SAIYAN SHORTFALLS ARE NOW RE-ASKED AS SPORTSBET'S CAP RISES. Wilson: 'never let a bet be dead just because it has hit max stake, as later on it should be retried once limits are higher (but only retried if it is at the same line a tipster has tipped / 10% prices)'. MEASURED from HB's own logs: SB caps a player prop per account on TO-WIN not stake, and the cap RISES toward bounce, so max_stake = cap/(odds-1). EXACTLY $75.00 at 8-12h out across 20+ first-touch readings, all $74.96-$75.04 on odds 1.74-1.94; ~$100 at 2-4h. An unfilled remainder is EARLY, not dead. THIS ALSO EXPLAINS THE DISPOSALS SHORTFALL CHASED ALL WEEK: that feed fires every base tranche at 07:01 = 9.1-9.5h before a 16:35 bounce, squarely the LOWEST band, so 5 accts x ~$85 = ~$425 of a $500 target. Its hourly retries are what harvest the rising cap (Worpel filled on the SIXTH: $97, $97, $97+$14.80, $82.20, $82.20+$13.20, $69). Nothing was broken; we bet at the worst time. The code NEVER PREDICTS the cap, deliberately: Wilson noted betMaxStake is REMAINING capacity, already net of other tipsters AND of his own manual bets, so the tick re-asks and takes what is offered. SAIYAN ONLY. Guards: SAME line never a neighbour; odds >= the WORST price we ACTUALLY FILLED AT (not the tipped price, since a Saiyan tip moves the market within seconds of us betting); top-up <= intended-placed; Sportsbet only; nothing inside 10min of bounce; 36h expiry; queue persisted so a watchdog restart cannot lose it, failing OPEN since losing it costs a top-up not money safety. REVIEW CAUGHT FIVE DEFECTS, TWO MAKING IT DO THE OPPOSITE OF WHAT WAS ASKED, and my tests missed both by STUBBING the very function carrying the guarantee: (a) the price floor was 10-20% WORSE than the fill price, because suggested_odds runs through _afl_target_odds which discounts 10% (20% above $2) -- a fill at 2.17 produced a floor of 1.74, while the log line and comment both claimed otherwise; (b) exact_only was NOT forced, so the +/-1.0 snap was live and a top-up could land on a neighbouring line -- fixed in the fan-out AND at the _resolve_single_for_placement choke point, since a test calling the resolver directly proved the fan-out-only fix was bypassable (snapped 23.5 onto a carried 24.5); (c) AMBIGUOUS stake was never netted out, so maybe-landed money was queued for re-ask and a maybe-landed top-up re-asked again next tick, both double-stake vectors; (d) suggested_bookie was DEAD CODE here (saiyan_afl is in TIPSTERS_IGNORE_SUGGESTED_BOOKIE) so the Sportsbet lock the feature rests on was unenforced; (e) the queue lock did not span the read-modify-write, so a tip queued mid-tick was erased by the tick's stale snapshot. DECLINED one review suggestion: suppressing the existing hand-place alert for Saiyan as the disposals branch does. It would close the hand-place + auto-re-ask double-stake tidily, but it breaks the v5.90 contract that a genuine shortfall always reaches Wilson and would make a silently failed queue a silently lost bet. The alert stands; a second message says it is queued and not to hand-place. Information, not silence. 19 new tests incl. four driving the REAL resolver. 667 green.) --- v6.08o (2026-08-10: SPORTS AMBIGUOUS STAKE HAD NO DEFERRED RE-CHECK. The verified 3-day review found $515.70 of ambiguous stake debited-as-placed by Guard B across 6 events with NOTHING ever revisiting it ($342.10 of it with no ledger row at all). Racing has had a deferred cid re-check since 07-06; the AFL fan-out had none. _schedule_deferred_sports_cid_verify ports it, hooked into the _hard_deadline arm of _reconcile_fanout_ambiguous: it re-queries the book later and either confirms LANDED, reports INCONCLUSIVE, or pages a CRITICAL naming the amount still frozen in the DisposalsModel ledger. _disposals_release_ambiguous is added as an OPERATOR tool (a test asserts it has NO automatic caller anywhere in the tree). THE FIRST DRAFT HAD THE VERIFIER AUTO-RELEASE THAT LEDGER EXPOSURE AND REVIEW WAS RIGHT TO KILL IT: reconcile.verify_bet_landed returns landed=False for 'not found in the poll window' (its own log line says exactly that), which is absence of EVIDENCE not proof the bet is dead, and hyperbot_client documents the wall-clock on a cid timeout as ~5 MINUTES. Releasing would let the model re-offer the shortfall and a LATE LAND would then DOUBLE-STAKE, the one outcome Guard B debits-as-placed to prevent; racing has always been alert-only for the same reason. Two more review findings fixed: the delay was 150s, INSIDE the ~5min cid window where 'still absent' means little (now 420s); and the call passed r.stake, frequently falsy on an ambiguous result, so stake=0 would fail verify_bet_landed's stake gate against every real pending bet and page a bogus $0.00 CRITICAL on exactly the subclass this targets (now reuses _eff_step, which the caller already computes for that reason). ALSO CORRECTED TODAY, a claim I shipped in v6.08n: I said kind=retry re-offers the FULL tranche hourly and that Worpel was offered $500 six times. WRONG, withdrawn. The fills file shows the model asks for the SHORTFALL and re-derives it after every partial fill (Worpel: base $500 -> $403 placed, then $97, $97, $97, $82.20, $82.20, $69 -> target). It came from an audit finding the verification pass had ALREADY marked REFUTED, and I repeated it anyway. There are TWO independent caps: the model requests the shortfall, and the ledger clamps to target - committed - inflight. DELIBERATELY NOT SHIPPED: re-keying the racing bookie_blocklist to sessions (the $818.18 slow-538 case, $320 provable). That set feeds the eligibility filter at racing_placer.py:1810 and is written from five branches with different semantics, so re-keying changes spill behaviour in all of them; it needs its own focused change. 12 new tests. 648 green.) --- v6.08n (2026-08-10: EDDIE HEAD-TO-HEADS WENT TO MANUAL + ONE STALE QUOTE PINNED SIX UNDATED RACING TIPS TO THE WRONG DAY + A LOST DISPOSALS LEDGER READ AS A CLEAN FIRST RUN. From the verified 3-day review (08-06..08-09: 367 bets, $42,787.98 staked, 76.5% fill). NOTHING went wrong with money that was PLACED: zero double stakes, zero wrong player/team/side in 43 sports tips, zero fills below the price floor, no in-play, 321 racing runner matches ALL exact-name. Every fix below is intent that never reached a bookie. (1) EDDIE H2H (Wilson: 'carlton ML to win??'): 08-09 18:25 'Carlton head-to-head win @ 1.87, 2.5u' -> manual as 'unschematic/multi'. Root cause IMAGE_PROMPT_AFL, which told the vision model to return market_type='other' for a HEAD-TO-HEAD *and* for a MULTI in ONE sentence, so a single-leg match winner inherited the never-auto-place rule the MULTI case exists for. h2h is first-class everywhere else (normalised :12109, selection backfilled from team_full :12113, alias-normalised :12128) so nothing downstream changed. Prompt now separates them; image builder grows an h2h branch with THREE guards, because the failure mode is placing ONE LEG of a parlay at the COMBINED price: a single named team required; ANY free-text description refuses (the schema leaves it null for a schematic h2h, so its presence means the model was unsure -- refusing on PRESENCE not keywords was a REVIEW finding, since the keyword regex scanned 3 fields the AFL image schema never emits); and a half/quarter h2h -> manual (SB prices it separately). KNOWN RESIDUAL, pinned by a test not left implicit: a multi mis-tagged h2h with a null description and one named team is indistinguishable from a real match winner. (2) UNDATED RACING GATE, $2,875.00: 08-08 Morphettville, the forward-date lookahead fired only on ZERO quotes, so ONE stale BetRight quote in above_floor closed it; six Zak tips stayed dated TODAY and BetRight's date-STRICT place endpoint answered 'Failed to find event' on all six ($2,875 intended, $0.00 placed). BetRight's PRICE lookup is date-loose, its PLACE path date-strict. A shop backed by fewer than RACING_UNDATED_MIN_BOOKS (2) distinct bookies now also probes forward, but adopts a later day ONLY if STRICTLY better corroborated, bounding the risk of rolling a real-but-thin meeting onto the wrong card. The zero-quote path is UNTOUCHED (3 sibling tips resolved through it and filled in full). _next_meeting_date probes base+1 so today is never re-adopted. Kill-switch RACING_UNDATED_THIN_REPROBE. (3) A MISSING disposals ledger read as a clean first run. It is the ONLY thing stopping a serial double-stake: kind=retry re-offers the FULL tranche hourly, not the shortfall (Worpel was offered $500 six times in six hours and the ledger clamped every one), so a deleted/reset/re-pathed state file would re-admit every key at FULL size. Now fails CLOSED when the file is absent but the fills file already holds placement records; keyless hourly sweep records deliberately do not trip it or one sweep would brick the feed. REVERTED BEFORE SHIPPING: widening AFL_SGM_SESSION_PRIORITY to 5 accounts. Wilson AND the reviewer caught it independently -- those accounts are excluded because they are NOT SGM-ENABLED, which .env says two lines above the setting and SGM_BLACKLIST_SESSIONS enforces. The audit's $329.70 arithmetic was right and its conclusion wrong: it counted accounts that can never take the bet. .env is byte-identical to its backup. Two review findings on my own work fixed pre-ship: the h2h guard scanned phantom fields, and the racing test MIRRORED the adoption comparison instead of calling it (would have passed with > regressed to >=). Predicate extracted to undated_should_adopt, module-level and pure. 27 new tests. 641 green.) --- v6.08m (2026-08-06: THE ODDS FLOOR WE SEND UNDERCUT THE MODEL'S OWN THRESHOLD + THE SWEEP RECORD WAS INVISIBLE TO ITS READER. From an 8-agent audit of whether the model's OddsAPI view of Sportsbet agrees with the HyperBot catalog we place against. THE VIEWS AGREE EXACTLY: 16 of 16 player-fixtures, same line, both prices to the cent, across both Round 22 dumps; no market-mapping mismatch (OddsAPI player_disposals IS the per-player two-way MAIN LINE that HB exposes as '<Player> - Disposals'; the 'N+ Disposals' ladder under the same HB key is OVERS-ONLY and the model never requests it); only divergence is Thomas vs Tom Liberatore, already resolved by _AFL_FIRST_NAME_CANON. So NO bridge and NO alias map needed, and the model STRUCTURALLY CANNOT name a non-main line (OddsAPI returns one line per player; the model emits `point` verbatim). FIX 1: target_odds could undercut DISPOSALS_MODEL_MIN_ODDS. The model refuses below 1.70 at resolve time, but the floor SENT to HB was a flat 90% of posted, so posted 1.73 floored at 1.56 - below the model's own threshold and at/below the ~1.57 break-even the gate's own comment cites; a price fall between our price-check and HB's fill was acceptable at a price the strategy would have REJECTED. Now max(MIN_ODDS, 0.9 x posted), scoped to THIS tipster (others keep their tuned 10%/20% band - the 07-12 Himmelberg fix widened AFL >$2 to 20% precisely so a 2.25 tip accepts SB's live 1.87). Raising a floor can only cause a CLEAN REJECTION, never a worse bet: one-directional. FIX 2: the sweep record was SILENTLY DISCARDED by the only reader that matters - the model's load_fills accepts a record only `if rec.get('key') or rec.get('type')=='reconciliation'`, and ours said 'reconciliation_sweep', so ALL 42 RECORDS ON DISK WERE INVISIBLE and its summarise() still said 'no tipbot fills on disk yet'. That record was added v6.08f specifically to distinguish 'the sweep ran and the book is empty' from 'the sweep never ran', so it was dropped by the exact gap it exists to close. Now type='reconciliation' + kind='sweep'. REPORTED TO THE MODEL, highest priority there: A REFUSAL IS TERMINAL - it commits the tranche + sent-key on a successful Telegram SEND knowing nothing of what we did, so line-moved-DOWN (45 of 92 moves) gives plan()=None on the high-water rule AND plan_retry()=[] on line!=t.line -> NEVER re-offered, $500 lost permanently while its ledger records the position as BACKED; line-unchanged-but-we-placed-nothing is also permanent via `if not t.placements`; line-moved-UP re-offers at ADDON $250 not BASE $500. Only a same-line partial fill and the 23:00-07:00 blackout self-heal, and its own source documents this for the blackout case ONLY. Asymmetry: our outcome='none'/accounts=[] record PROVES $0 is on, but it gates on dollar placements, so provable-zero and silence are identical to it. AND THE DRIFT CLAIM I WITHDREW YESTERDAY WAS RIGHT AFTER ALL, on better evidence: per ~20-min tick 0.34% beyond 12h -> 7.14% IN THE FINAL HOUR, a 20x concentration in exactly the window the T-1h strategy bets in (hazard 0.013 vs 0.283/player-hour), 33% of quotes move at some point. The CLAIM was sound; the EVIDENCE I first cited (a settled Round 21 Richmond v West Coast bet compared against a Round 22 line) was not, and that distinction is the point. Also corrected: ~8 UNDERs/game is the FAR-HORIZON board - measured 13-14 inside 6h, 41 of 124 under quotes first appear inside 12h. Two audit claims corrected on review: the feed IS enabled (live, test mode) not dormant, and the resolve-time min-odds gate does exist. AND MY FIRST TESTS FOR FIX 1 WERE WORTHLESS, caught in review: they computed max(_afl_target_odds(...), floor) in the test body - the production logic copied into the assertion - so they never called the changed code; deleting the guard from main.py left all 7 PASSING. Replaced with tests that drive the real _resolve_single_for_placement against a stubbed catalog in the live /v3/price shape and assert the target_odds handed to HB; deleting the guard now fails 3. Second time in two days with the same lesson: a test written from the same mental model as the fix confirms the model, not the code. 611 green.) --- v6.08l (2026-08-05: TEST MODE DRIP-FED A SELECTION PAST ITS CEILING, found by finally running the DRY RUN Wilson asked for: real BET|v2 messages through the live pipeline with every place_* method severed, notifier captured, ledger redirected. TWO defects, SAME SHAPE (the exposure cap computed then discarded), two layers. (1) _disposals_claim took the ceiling from the model's $500 target while _apply_disposals_model_stake forces every placement to the flat $7 TEST_STAKE -> the $500 ceiling was never binding at $7 a time, so it could not stop a drip: the model retries a shortfall HOURLY, tipbot placed another $7 each time, ~71 retries/selection, ~$672 of REAL money across ~8 selections from 12h out, in the mode whose whole purpose is ONE small bet. Observed: replaying a filled offer took selection_committed $7 -> $14. Ceiling now clamped to the test stake, so the first test bet FILLS the selection and later retries take the designed benign 'nothing left to place' path (log-only, no alert). (2) Raised IN REVIEW of fix 1: the TEST MODE branch of _apply_disposals_model_stake assigned the flat stake UNCONDITIONALLY, using `posted` only in its log message, while the production branch beside it has always taken min(posted, cap). `posted` IS the claim's `allowed`, so a PARTIAL fill was re-inflated on retry -- routine here, since the stake splits across ~5 SB accounts and one clean-rejected leg leaves a shortfall: $5.60 filled + a $1.40 remainder forced back to $7 = $12.60 against a $7 ceiling. Now min(flat, posted). DRY RUN ALSO VERIFIED, all correct: exact_only HELD on an unlisted line (Jack Ross UNDER 21.5, only 24.5 listed) -> manual, NO blind POST, NO snap to 24.5; event resolution, roster match 1.0, player_disposals, selection 'Jack Ross Under', direction=under, 5-account even split $7->$1.40, target_odds 1.73 = 90% of 1.92; proposition_id sent as None (HB matches by NAME, so the model's pid is a ledger key component only). MATERIAL CONSTRAINT SURFACED, undocumented on both sides: Sportsbet lists only ~8 UNDER selections per game vs ~800 OVERs, always the near-even MAIN line, one line per player, and that line DRIFTS (Jayden Short UNDER 20.5 now, though the book holds real money at 22.5) -- so with exact_only deliberately forced, an UNDER not currently listed at the named line CANNOT auto-place. 4 new tests incl. the TEST_MODE true->false flip (a persisted $7 ceiling would have silently suppressed the whole $500 tranche); the re-inflation test verified to FAIL without the fix; the state fixture now pins TEST_MODE=False since .env ships it true and it was silently capping every production ledger test at $7. 606 green.) --- v6.08k (2026-08-05: A DROPPED WATCH MESSAGE LEFT NO TRACE IN THE LOG. Verifying the live feed end to end (Wilson noted the model had just posted a 'to watch' msg). Everything WAS working — and that was the problem: there was no way to tell from the log. The chatter/WATCH drop was log.debug while the root logger runs at INFO, so a WATCH arriving and being correctly ignored produced NO LOG LINE AT ALL; the only record was the liveness stamp, which keeps just the LAST message. So 'WATCH arrived and was correctly dropped', 'nothing arrived', and 'the parser broke and is silently dropping real bets' were INDISTINGUISHABLE from the log — the exact silence-vs-evidence gap that already justified the reconciliation sweep record and the NOOP heartbeat. 'Drops silently' must mean no ALERT, not no RECORD. Now one INFO line per message naming the kind and that nothing was placed, distinguishing NOOP (liveness owns the seq accounting) from a WATCH list from ordinary chatter, ~1-2 lines/hour. 3 tests. [PIPELINE VERIFIED LIVE] kill-switch on, channel registered (id in .env only), sender lock active, machine_feed flag bypassing the LLM path, regex parser loaded, Sportsbet lock, price floor 1.70 + 10% worse-gate, reconcile mode alert, hourly sweep all status=ok. Feed CONFIRMED ARRIVING: last message 2026-08-05T22:01:53, NOOP seq 2 at 21:01. Handling re-verified against all 9 real fixture cases: the 4 BET messages parse to exactly ONE placeable row each (incl. the retry and the bet half of the bet+watch pair); the 5 with no BET| line — watch-only, the too-early qualifier, the unmappable club, the heartbeat — all return None and place nothing. [A MEASUREMENT ERROR OF MINE, caught by re-checking] I first reported '8 of 15 disposals-capable accounts up, $0.88/account in test mode' and flagged it as below the $1 bookie minimum; that counted every account in ANY priority list. Against AFL_SESSION_PRIORITY, which is what the fan-out actually uses, it is 5 OF 7 UP -> $1.40/account, which is fine. All seven carry the identical (124,99,74,50) cap so the even-split reasoning is unchanged.) | PRIOR v6.08j (2026-08-04: THE MODEL DROPPED ITS TRIGGER 3.5 -> 2.6 and my STALENESS TEST is how we found out — regenerating their wire-fixtures.md tripped test_vendored_fixtures_match_the_generated_source, exactly what it exists for. The diff is a STRATEGY change not a format change: 'under trigger' is now an edge of 2.0-2.6 (was 2.5-3.5), 'too early' means >=2.6 (was >=3.5), and they note 'even a 2.0 gap is +8.01% pooled' and that the band is 'held back by ACCOUNT CAPACITY rather than by a missing edge'. EVERY BET| MACHINE LINE IS BYTE-IDENTICAL (verified by diffing only the machine lines) so the parser is unaffected and no tipbot code changed — but two operational consequences: a lower trigger means MATERIALLY MORE qualifying bets, and their own note says the binding constraint is now OUR ACCOUNT CAPACITY, which is 5 of 7 accounts up at ~$100 each, so partial fills become the NORM not the exception. THEY ALSO ADDED A DELIBERATE ROUNDING TRAP, now pinned: one watch row sits at edge 2.57, 0.03 BELOW the trigger, with the note 'a parser that rounds the printed edge to one decimal will read it as 2.6 and place it' — tipbot is immune STRUCTURALLY not numerically (it never reads the printed edge, only the BET| prefix, and that row has no BET| line), and a new test asserts exactly that so the immunity stays structural. [A FLAW IN MY OWN STALENESS TEST, found by it firing TWICE] the first fire was CORRECT (the trigger change); the second was pure NOISE — the generator stamps the current time into the header and into the NOOP ts=, so a byte-exact comparison fires on EVERY regeneration even when nothing moved. A staleness check that cries wolf gets IGNORED, the exact failure mode this feed has been designed against throughout. It now compares SUBSTANCE, normalising only those two timestamps, and a second test proves the normalisation is NARROW: a timestamp-only change is tolerated, a changed line= is still caught. [RACING PRIORITY TRIMMED 18 -> 11] betright 99996, betr 100005, the 100004 ORPHAN (in the list with NO sessions.yaml entry — the mistake that file's own header names), plus 73361, 116964, 71275, 78280 — all accounts Wilson turned off, all classified PLACEABLE purely by appearing in a priority list, which routed their drops to CRITICAL on the MONEY chat. Now inert -> INFO, and they no longer waste a racing spill slot (same treatment as neds:100006 in v6.06). 65465 + 111463 deliberately LEFT paging because they are being verified. [ASKED THE MODEL TO VALIDATE evt/pid BEFORE EMITTING] resulting the 3 Saturday positions found Wines recorded as CD_I296200 which DOES NOT EXIST in the 2026 AFL data (real CD_I294318), on the one row that also had a placeholder evt — hand-entered. WORSE: their own generated fixtures use CD_I1027367 for Luke Nankervis and that id belongs to ZANE PEUCKER. A wrong-player id is the HARDER case — two players then share one identity or one splits across two, and every cap counting distinct pid counts wrong SILENTLY — so the ask includes a SAME-PLAYER RESOLUTION check not just a regex, plus a test pointed at the fixtures which would currently FAIL ON TWO OF FIVE ROWS. A wrong pid cannot cause a wrong BET (tipbot resolves the player by player+team, never pid) but it silently disables the exposure cap, key dedup and the per-fixture cap. [CORRECTIONS I HAD TO MAKE IN THAT ASK] I first wrote a bad id 'is not another player's id, it simply does not exist' — the Nankervis case disproves that and the doc now says so; I also called the source 'your Champion Data' when it is the AFL API (api.afl.com.au) fetched via fitzRoy, which their own schema.py:19 calls 'Champion-Data-derived'; and CD_I... is NOT a HyperBot id — HB never sees it, tipbot identifies players to the bookmaker BY NAME. Round 21 completeness was CONFIRMED before resulting anything (9 games, 414 player rows, all 46 players per game with plausible full-game totals). [THE 3 POSITIONS RESULTED] Bergman 22 vs under 20.5 LOSS, Wines 25 vs under 24.5 LOSS (by HALF a disposal), Hall-Kahan 18 vs under 22.5 WIN +$405 = -$595 on $1,500, ROI -39.7%; three bets so it says nothing about the edge, and Hall-Kahan's fill was hand-patched rather than observed so treat it as unverified. 594 tests green.) | PRIOR v6.08i (2026-08-04: RETRACTION — v6.08h's Tipper labels were WRONG and the workbook was readable all along. Wilson asked 'did it use to be Zak Trussell in my Bet Record excel??' and it DID. Read 2025-26 Bet Record.xlsx -> sheet 'Bet Record' -> Tipper column, which I should have done BEFORE touching a label: 'Zak Trussell' = 82 rows, 'Zak' = ZERO (appears nowhere); 'Myself' = 924 rows, 'Wilson' = ZERO. So v6.08h broke TWO labels. Both restored: zak/zak_racing -> 'Zak Trussell', self_bet -> 'Myself'. Confirmed still correct against the sheet: Eddie 164, Cownie 3673, TrialSniper 58, Saiyan 1333, Shook 724, EasyMoneyAFL 16, Kev 1871, Aus 1229, Dello 914, ETR 5. disposals_model has NO precedent in the sheet so its label is genuinely a new CHOICE not a match, now marked as such. [HOW THIS HAPPENED — the mechanism matters more than the fix] Wilson wrote 'Eddie, Cownie, Zak, TrialSniper' in conversation; I treated that PROSE as the specification, changed the code, and when the pre-existing assertion brf.tipper('ZAK')=='Zak Trussell' failed I EDITED THE ASSERTION TO MATCH MY CHANGE and reported it as 'the test encoded the old label'. It did not — it encoded a VERIFIED FACT, and the file that could have settled it was one directory from the CSV I had been reading all session. A PASSING TEST THAT CONTRADICTS A CHANGE IS EVIDENCE ABOUT THE CHANGE, NOT A CHORE. [GUARD so it cannot recur from memory] test_every_label_exists_in_the_actual_workbook opens the REAL workbook and asserts every TIPPER_MAP label actually appears in its Tipper column (disposals_model exempt as genuinely new); verified to FAIL on both v6.08h mistakes rather than passing vacuously. [A SECOND DEFECT THIS SESSION SURFACED — also mine, and only visible because it was AFTER MIDNIGHT] v6.08f made _partition_crashed_alerts read the WALL CLOCK (a drop inside the 23:00-07:00 blackout reports INFO not CRITICAL), which made a PRE-EXISTING test (test_inert_crash_downgrade) PASS BY DAY AND FAIL OVERNIGHT — so the pre-commit gate would have blocked any night-time commit. It shipped unnoticed purely because that deploy ran at 15:16. Fixed by making `now` an EXPLICIT parameter defaulting to datetime.now() so production is unchanged while the severity decision is injectable; both tests now pin the clock and my own blackout tests moved off a main.datetime mock onto the real parameter, plus a new test asserting the parameter exists and that the same input gives the same answer whatever time the suite runs. LESSON: a decision that varies by time of day must be INJECTABLE, not mocked around — otherwise the gate is only trustworthy during office hours. [UNAFFECTED, still correct from v6.08h] the Sport work — the sheet's own racing values CONFIRM the convention (QLD Harness 2236, WA Harness 1448, NSW Harness 571, VIC Thoroughbreds 90, SA Thoroughbreds 76, WA Thoroughbreds 76, TAS Thoroughbreds 52, TAS Harness 42), so state-prefixing every racing row and fixing the 206 stateless rows were right. Feed regenerated over all 3,567 rows.) | PRIOR v6.08h (2026-08-03: BET RECORD FEED labels now match the MANUAL sheet + every racing row gets its state. Wilson: 'Eddie, Cownie, Zak, TrialSniper should have been the naming conventions based off the bet record I had manually, as I need to copy paste these cells over into the manual sheet ... and then sport it should be able to populate WA Harness or QLD Harness or SA Thoroughbreds and VIC Thoroughbreds'. WORTH STATING FIRST: this already existed and was already RUNNING — bet_record_feed.py generates C:\\Users\\Wilson\\OneDrive\\Sportsbetting\\bet_record_feed.csv in the Bet Record's own column layout (Date|Tipper|Sport|Team|Bet Name|User|Bookie|Stated Odds|Actual Odds|Stake (U)|Stake ($)|Result|Net Change) with BET_RECORD_FEED_ENABLED=true and autoresult on; bets_placed.csv is the RAW machine ledger, this is the copy-paste-ready output. Two things in it were wrong. [1] 'Zak Trussell' should be 'Zak' — these cells go straight into the sheet so a label that is merely close means hand-editing every row. [2] 206 OF 1,700 RACING ROWS (12%) HAD NO STATE, rendering a bare 'Harness'/'Thoroughbreds' where the sheet wants 'QLD Harness'. TWO distinct causes and only one was missing data: (a) 5 were SURFACE/CIRCUIT VARIANTS of tracks ALREADY MAPPED — Sandown Hillside, Sandown Lakeside, Pakenham Synthetic, Ballarat Synthetic, Randwick Kensington (36 rows) — because the lookup was an EXACT match so a mapped 'sandown' could not resolve 'sandown hillside'; fixed properly with _state_for_track(): exact match, then the LONGEST mapped track the name begins with on a WORD BOUNDARY (enumerating every surface variant would go stale the next time a club adds one; word-boundary so a short name cannot swallow a longer one; longest-first so 'morphettville parks' keeps its own entry). (b) 14 GENUINELY MISSING venues taken from the live ledger rather than guessed: Marburg (QLD), Kellerberrin+Narrogin (WA), Naracoorte+Mount Gambier+Bordertown (SA), Seymour+Sale+Echuca+Wangaratta (VIC), Wyong+Goulburn+Scone+Tamworth (NSW). RESULT over all 3,567 rows: Tipper = Eddie/Cownie/Zak/TrialSniper/Saiyan/Shook/EasyMoneyAFL/Kev/Aus/Wilson; Sport = QLD Harness 707, SA Thoroughbreds 435, WA Harness 374, VIC Thoroughbreds 89, NSW Thoroughbreds 84, QLD Thoroughbreds 11, and 0 ROWS LACK A STATE (was 206); no blank cells in Date/Tipper/Sport/Team/Bet Name/User/Bookie/Stake($). 44 tests incl. one that FAILS if any tipster in the live ledger would render a raw id in the sheet. [A GUESS CHECKING DISPROVED] I expected v6.08g's `side` backfill to also fix the Bet Name column, which embeds OVER/UNDER. It did NOT and did not need to — the feed derives direction from `selection` independently, so 0 disposals Bet Names lacked a direction either before or after; the side backfill's value is for analysing the RAW ledger, not this feed.) | PRIOR v6.08g (2026-08-03: bets_placed.csv MADE UNIFORM + AUDITED. Three defects in the money ledger, all found by reading the LIVE FILE rather than the code. [1] `tipster` carried TWO CONVENTIONS so a join silently half-worked: racing wrote the uppercase titan code from parsed['titan'] (OC/ZAK/TRIAL) while sports wrote the lowercase internal id from tip.tipster (eddie_afl/saiyan_afl/shook) — joining that column against config worked for SPORTS and SILENTLY FAILED for RACING, the shape of mistake that halves a P/L report without erroring. Every row now carries the lowercase internal id (tiptitans/zak_racing/trial_sniper/leroy/eddie_afl/...) and the titan/display code moved to a new `titan` column so WHICH titan a Tip Titans bet came from is not lost; both writers go through the same helper so the column cannot diverge again. [2] THE HEADER HAD SILENTLY DESYNCED, STRANDING REAL DATA: _write_row only writes a header when the file is FRESH, so appending `note` to COLUMNS in v5.92 left every row written afterwards carrying 20 fields under a 19-COLUMN HEADER — 1,640 rows at 19 fields, 1,927 at 20, and 10 ROWS WHOSE REAL `note` TEXT NO CONSUMER COULD READ BY NAME (the 403-proxy re-bet annotations). It never crashed because every reader uses csv.DictReader, which files a surplus value under the None key. Root cause fixed with a one-time idempotent _migrate_ledger_columns() that runs on seed, so the NEXT appended column repairs itself instead of desyncing; pandas.read_csv now reads it cleanly at (3567, 21). [3] 531 PLAYER-PROP ROWS HAD A BLANK `side` — a third of the player-prop history could not be told over from under (v6.07 fixed the WRITER but never backfilled, so all June/July, none in August). Backfilled, and it is a DERIVABLE value not a guess: an under's selection ends ' Under' while an over's is the BARE PLAYER NAME, because an AFL over places on the base O/U market with a bare-name selection. Verified against the 781 rows that DID carry a side — ends_under->under 689/689 and bare-name->over 92/92, ZERO COUNTEREXAMPLES — and the same convention appears in live /api/pending_bets text. A recorded side always wins over the inference, and non-player-prop rows are left alone (a racing win row must not acquire a direction). [MIGRATION SAFETY] it rewrites every row of a money ledger, so: atomic tmp+replace, a .bak_precolmigrate copy, and it REFUSES and leaves the file untouched if it finds a column it does not recognise rather than dropping data. Verified BEFORE running live: 3,567 rows in and out, 0 VALUE MISMATCHES across every pre-existing column. [AUDIT of the migrated file: CLEAN] 0 duplicate bet_ids, 0 blank bet_ids, 0 stakes<=0, 0 odds<=1.0, 0 rows where potential_return/profit disagree with stake x odds, 0 blank required fields, 0 blank sides, every tipster lowercase. [A REGRESSION THIS WOULD HAVE CAUSED, CAUGHT BEFORE SHIPPING] bet_record_feed.py maps discipline and the Tipper label off `tipster` and its keys were oc/zak/trial — with OC rows now reading `tiptitans`, EVERY OC RACING ROW WOULD HAVE SILENTLY LOST ITS 'Harness' WORD and come out as a bare state. Added tiptitans+leroy to both maps; 0 racing rows now lose a discipline. Also closed two PRE-EXISTING gaps found while checking: self_bet (11 rows of history) was never mapped and rendered a raw id in the Tipper column, and dello_afl/disposals_model are mapped ahead of their first landed bet. 33 new tests, 546 green.) | PRIOR v6.08f (2026-08-03: THE SCHEDULED NIGHTLY SHUTDOWN NO LONGER PAGES THE MONEY CHAT + a zero-selection sweep now leaves evidence it ran. [1] Wilson scheduled every bookie session OFF 23:00-07:00 (2026-08-01) to conserve data; the session monitors predate that and cannot tell a scheduled shutdown from a crash, so they paged CRITICAL for it — 5 CRITICALS on the night of 08-02, on chat -5276223732, the one carrying the money pages incl. the startup-reconcile 'MAY have LANDED, VERIFY'. Being trained to ignore THAT chat is worse than the noise, and it is v6.07 #30 again (a tipster burying the alerts that mattered under ~1200/day). Inside the window a confirmed drop is now INFO: still detected, still logged, still names every session, and the message states when it will escalate. A session genuinely dead overnight pages on the FIRST CYCLE AFTER the window, so the worst case is a delay to 07:00 and no AFL match starts before then. DAYTIME DROPS UNTOUCHED — 08-03 11:20 (7 sessions) and 12:46 (1) were REAL and still paged. ONE seam covers both paths because _startup_dead_session_check does not alert directly: it feeds _pending_drops and the watchdog emits through _partition_crashed_alerts. check_session_health.py is DELIBERATELY left paging — it only speaks when main.py's watchdog is NOT running, and main.py runs through the blackout, so it stays silent anyway; if it ever DOES fire overnight then Main is dead too, a genuine emergency. Kill-switch SESSION_BLACKOUT_ALERTS_QUIET=false (or equal hours). 25 tests. [2] A ZERO-SELECTION RECONCILIATION SWEEP NOW LEAVES EVIDENCE IT RAN: per-selection records are written one PER SELECTION, so an empty sweep wrote NOTHING and the model could not tell 'the sweep ran and the book is empty' from 'the sweep never ran' — the same silence-vs-evidence gap this whole exchange has been closing (it is why they send a NOOP heartbeat), and it was the LIVE case since the first nine sweeps after enabling all found 0 selections. Every sweep now appends one type='reconciliation_sweep' record with status/selections/stake_total, so selections=0 + status=ok is POSITIVE evidence of an empty book while a MISSING record means the sweep did not run. Told to the model in DISPOSALS_MODEL_ANSWER_2026-08-03e.md. Fixing it broke an existing test that assumed the fills file held exactly one line; it now selects the record it means. 513 tests green. [3] OPERATIONAL, recorded because the contract's §3 numbers read as current and are NOT: only 5 OF THE 7 disposals accounts are up (65465 Adam Tran + 111463 Aditya down since at least 08-02, a session-login job not a code one), so a $500 base splits 5 ways at $100 EACH, not 7 at $71; and a real 538 today on 53522 and 118458 returned max=$100.00 (harness racing so not directly comparable to an AFL prop, but it puts the per-account share at a limit we have now seen quoted).) | PRIOR v6.08e (2026-08-03: [REVIEWED — 3 lenses + a refutation pass per finding. I1 (money path untouched), I5 (relabel inert for genuine Titans) and I6 (sportsbot fork safety) all PROVEN CLEAN BY EXECUTION: parse() byte-identical, parse_noop sharing no state with it, config importing with no .env, _racing_source inert for OC/unknown/empty/None. ONE finding survived and is FIXED: a flushed seq gap could re-alert EVERY POLL FOREVER when the state file is unwritable — the flush was recorded only by clearing pending_seq_gap ON DISK and _disposals_save_liveness swallows write errors, so a full disk / read-only logs dir / AV holding the file left the gap in the reloaded state and re-emitted the identical alert every 900s (~96/day, in the detector whose entire design note is about not burying the money pages). The two SIBLING latches (silence, recovery) had already been mirrored into _DISPOSALS_LIVENESS_MEM for exactly this reason; the gap flush was left out. Now mirrored the same way, plus `flushed_gap` added to the test fixture's between-test reset (the fixture's own comment warns an unreset mirror silently suppresses the next test's alert). The pre-existing storm test could NOT have caught it — it stamps a single NOOP so pending_seq_gap is never populated — so a new test queues a real gap first, and it was VERIFIED TO FAIL WITHOUT THE FIX: 5 criticals over 5 polls disk-only vs 1 with the mirror.] [RETRACTION — an unattended run's 'correction' was itself wrong] the 05:00 auto-resume run reported that tipbot's per-fixture player cap comment ('Enforced model-side; this is tipbot's backstop') was wrong and that 'there is no upstream cap: the emitter's selection is an unbounded filter, tipbot's cap of 4 is the ONLY enforcement', and edited config.py + the refusal message to say so. THE UPSTREAM CAP EXISTS. The emitter's CANDIDATE FILTER (send_tips_telegram.py:694) is indeed unbounded, but it only builds a candidate list; the emitter then iterates it and calls ledger.plan() per row (:743-745), and plan() enforces MAX_PLAYERS_PER_EVENT=4 (ledger.py:251) by returning None so no tranche and no message is produced. READING THE FILTER AND STOPPING ONE FUNCTION SHORT OF THE GATE produced the claim. Both comments restored, with the call chain recorded. tipbot keeps its own cap regardless (trusting an external system for a money bound is how you find out it regressed) and the two agree in behaviour as well as number since both gate NEW players only. Same class as the open_shortfalls() drift the counterparty caught: a claim about someone else's code asserted without following it to the end. [ZAK/TRIAL BET-LOG LABEL, Wilson] notify_tiptitans_placed/_unfilled/_manual_alert are shared by Tip Titans AND the standalone racing tipsters (Zak Trussell + The Trial Sniper via their own channels' image/text auto-place path, Leroy via Betfair BSP), but all three headers were HARDCODED 'TIP TITANS' — so every Zak and Trial bet appeared in the Bet Log as a Tip Titans placement, with a 'Titan:' field to match. Now derived by _racing_source() from the `titan` code (ZAK->ZAK RACING, TRIAL->TRIAL SNIPER, LEROY->LATE MAIL LEROY, field label -> 'Tipster:'), and an UNRECOGNISED code deliberately KEEPS 'TIP TITANS' — Tip Titans is a service with several titans, so an unknown code is far likelier to be a real titan than a new standalone tipster; failing that direction means this can NEVER relabel a genuine Titans alert (OC and every future titan code are byte-identical). 13 tests incl. one pinning all three alert bodies as derived rather than hardcoded so the literal cannot creep back. Telegram Bet Log ONLY: bets_placed.csv already recorded ZAK/TRIAL/OC distinctly from parsed['titan'], so the ledger was never wrong; the CSV's two naming conventions (racing uppercase codes vs sports lowercase ids) remain a separate deferred cleanup. [DISPOSALS ENABLED, Wilson 2026-08-03] DISPOSALS_MODEL_ENABLED=true + TG_CHANNEL + SENDER_ID added to .env, TEST_MODE left TRUE ($7 total = ~$1/account) for the first live slate. Wilson's call: the feed's next games are Thursday so enabling now is immediately harmless — it moves the reconciliation verification from 'before enabling' to 'before Thursday'. Verified at restart: the channel RESOLVES and registers ('Monitoring: DisposalsModel'), all 13 channels still register, no telethon entity error, and the FIRST REAL reconciliation sweep ran status=ok (0 selections, $0.00 — the round is over and yesterday's disposals bets have settled). NOTE the export writes one record PER SELECTION, so a 0-selection sweep writes NOTHING: the model still cannot distinguish 'sweep ran, found nothing' from 'sweep never ran', which is the same silence-vs-evidence gap we have been closing all thread — a sweep-heartbeat record is the follow-up.) | ALSO IN v6.08e (2026-08-03: NOOP LIVENESS DETECTOR, and the reason it cannot be sensitive yet. The model posts NOOP|v2|ts=<iso>|seq=<n> on a tick that produced no bets, so an hourly feed's silence should become evidence. Built, 57 tests. [STATE CORRECTION] earlier bumps said 'unit-tested only, it cannot observe anything until DISPOSALS_MODEL_ENABLED is true, which it still is NOT' -- .env line 292 now reads DISPOSALS_MODEL_ENABLED=true (TEST_MODE=true, ~$1/acct, dated 2026-08-03 by Wilson) and TipBot-Main restarted 10:09:12 into this exact working tree (fingerprint c53bc7dcf8, log shows 'NOOP liveness detector started, poll 900s, silence threshold 24.0h'). The feed is LIVE and the detector is running; the fixes below are NOT live because main.py/config.py are read only at startup and this run was told not to restart. Read by a SEPARATE parse_noop() so parse() still returns None for anything without a BET| prefix -- that one rule is what makes a heartbeat structurally unplaceable rather than merely labelled. Stamped in main.py's machine_feed dispatch where a real message is already in hand, NEVER on a timer (the v6.07 X-watcher heartbeat was stamped before the work and stayed fresh through 170 blind poll cycles, 2026-07-30 17:17 to 07-31 07:14). State PERSISTED so a restart mid-outage does not read as 'just started'. [THE FINDING, and it changed the design] the heartbeat does NOT fire when its own docstring says it does: FOUR no-output paths reach no heartbeat (three early exits in tip_schedule.py -- outside the Thu 07:00-Sun 21:00 block, no upcoming match, next bounce >24h -- plus 'if found.empty' in send_tips_telegram.py:630), and a watch-only tick skips it too. NOT theoretical: their tip_schedule.log shows Sun 2026-08-02 17:00/18:00/19:00/20:00 all in-window, all silent (next bounce 98.5h away), right after an afternoon of watch messages -- a 3h threshold would have paged four times that evening, and a round whose last game is Saturday leaves a game-free in-window Sunday. [THE ~22h WAS WRONG, IT IS 30.5h] 22 measured a game-free Sunday as a CALENDAR DAY; silence starts at the feed's LAST MESSAGE (roughly the last game's bounce), not at Sunday midnight. Computed over the model's own cached 2026 fixture (218 matches), the worst legitimate in-window silence is the GRAND FINAL: Sat 2026-09-26 14:30, the round's only game, no Sunday game after it = 30.5h. Next largest all season is 12.7h. So 24 FALSE-PAGED on Grand Final Sunday and on any finals weekend whose last game is Saturday, with an alert asserting the quiet period was NOT expected -- the cry-wolf failure this detector exists to avoid. So DISPOSALS_MODEL_SILENCE_HOURS ships at 36, an admission rather than a setting (no value below the 86h window length is provably safe while the model stays silent on those four paths, and with the burst rule a feed dying in the back half of a window is not caught until the next one), and DROPS TO 3 the moment the model heartbeats on all four paths (one .env line). [ADVERSARIAL REVIEW: 6 lenses, 25 findings verified, 21 refuted, 4 SURVIVED, all 4 fixed] (1) the 24h false-page above; (2) the one-alert-per-outage throttle lived ONLY in the state file and _disposals_save_liveness swallows write errors, so a locked/full disk turned it into ~64 identical pages a day (and froze last_seen, making them false too), plus 'feed is BACK' on every message -- now mirrored in memory; (3) _NOOP_RE demanded EXACTLY four fields, so the moment the model added the |reason= field TIPBOT ITSELF ASKED FOR, every heartbeat would parse as None and seq-gap detection would die SILENTLY (the unreadable message still stamps last_seen, so the feed looks healthy) -- extra key=value fields now parse into Noop.extra and an unparseable NOOP| line alerts once per SHAPE; (4) setting the window knobs to 'disabled/always active' anchored window_start to the top of the current hour, and the burst rule is last_seen < window_start, so it made the detector permanently DEAF instead of maximally sensitive. The seq-gap check IS sound today and does the sensitive work: next_seq() has one call site on the no-bets path, so consecutive heartbeats are always +1. Also raised with them: _quiet_tick fires on 'no live prices', so an exhausted odds key emits a PERFECTLY HEALTHY heartbeat while zero bets are possible -- their side of the same liveness-is-not-progress bug. Anti-noise: MAINTENANCE chat not the money-critical chat (v6.07 #30 = a tipster burying money pages under ~1200 criticals/day), one outage = ONE alert latched on the triggering stamp and persisted, a 03:00 gap DEFERRED not dropped, recovery reported once, and the state file FAILS OPEN on corruption (it holds no money state; a corrupt liveness file that paged would be the opposite of the point). Two of tipbot's own comments corrected by checking: the per-fixture player cap is NOT 'enforced model-side' (the emitter's selection is an unbounded filter, so tipbot's cap of 4 is the ONLY enforcement), and the contract's staking note is computed at 1.80 when the measured centre is 1.87. Added the one parser refusal branch with no test, ALLOWED_MARKETS, now that the model side has named player_disposals_over in writing.) || v6.08d (2026-08-03: TESTED AGAINST THE MODEL'S GENERATED WIRE FIXTURES — and they found a WHOLE-FEED REFUSAL BUG. The model side committed docs/wire-fixtures.md: nine cases produced by scripts/emit_wire_fixtures.py from the REAL build_message/build_leans/noop_line, so they ARE the format rather than examples of it. Vendored byte-for-byte as wire-fixtures-vendored.md and pinned by test_disposals_wire_fixtures.py (20 tests) + a STALENESS test that fails if the vendored copy diverges from the source repo (a regenerated fixture set must surface as a FAILURE, not as a stale test quietly passing). [THE BUG, and it is the important part] the machine line is emitted wrapped in Telegram HTML: '<code>BET|v2|...</code>'. With parse_mode=HTML those become entities and telethon's raw_text (what main.py reads) is PLAIN, so the tags never arrive — but if parse_mode is ever omitted, changed or rejected they arrive as LITERAL TEXT, and the trailing '</code>' lands inside the LAST field, `key`, which fails validation. EVERY SINGLE BET WOULD BE REFUSED: a whole-feed outage caused by a formatting flag, and one that would look like the FEED being broken rather than a tipbot bug. Proven by running the parser against the fixture as written (\"key '...~base</code>' is not a plausible idempotency key\"). The parser now strips simple HTML tags from the candidate line and every BET fixture is asserted to parse IDENTICALLY IN BOTH FORMS — cheap immunity to a class we would otherwise have found live. [CASE 4, the one they flagged as dangerous, PASSES] a 'too early' watch row whose gap (4.49) ALREADY CLEARS the 3.5 trigger reads exactly like a qualifier but sits beyond the 12h window and carries NO BET| line (real case: Tim Kelly 2026-08-02, a 4.49-gap qualifier at 25.5 for SIX consecutive hours, every one outside the window). tipbot's only rule is the BET| prefix so it drops, and the test now pins that for every watch/NOOP fixture; their assertion 'no WATCH or NOOP message contains BET| anywhere' is re-checked INDEPENDENTLY here because tipbot's entire safety model rests on it. Also re-checked against the fixtures: 18 fields per line, stake==target on every one (design (a): the model asserts the DESIRED TOTAL, tipbot places the difference), no field value containing a pipe, the retry carrying a distinct retry1 key, and case 2's two bets arriving as TWO separate messages with distinct keys. [NOTE BACK TO THE MODEL] the fixture doc is titled 'exactly what tipbot receives' but shows the WIRE form sent to the Telegram API, not what telethon delivers — they differ by exactly those HTML tags, and the difference IS the bug. 413 tests green.) | PRIOR v6.08d (2026-08-03: TESTED AGAINST THE MODEL'S GENERATED WIRE FIXTURES — and they found a WHOLE-FEED REFUSAL BUG. [RESTORED to this chain 2026-08-03: the v6.08e edit chained straight to v6.08c and dropped this entry from TIPBOT_VERSION; it survived in CHANGELOG.md and in commit 42af02d, but the startup banner is the institutional memory so it is re-inserted here.] The model committed docs/wire-fixtures.md — nine cases GENERATED by scripts/emit_wire_fixtures.py from the real build_message/build_leans/noop_line, so they ARE the format rather than examples of it. Vendored byte-for-byte as wire-fixtures-vendored.md, pinned by test_disposals_wire_fixtures.py (20 tests) + a STALENESS test that fails if the vendored copy diverges from source. THE BUG: the machine line is emitted wrapped in Telegram HTML ('<code>BET|v2|...</code>'). With parse_mode=HTML those become entities and telethon's raw_text is PLAIN so the tags never arrive — but if parse_mode is ever omitted, changed or rejected they arrive as LITERAL TEXT and the trailing '</code>' lands inside the LAST field, `key`, which fails validation. EVERY SINGLE BET WOULD BE REFUSED: a whole-feed outage caused by a formatting flag, presenting as a per-message CRITICAL saying the SCHEMA was broken — so the natural reading would have been 'the feed is emitting garbage' rather than 'tipbot is mis-slicing it'. Proven by running the parser against the fixture as written. The parser now strips simple HTML tags and every BET fixture must parse IDENTICALLY IN BOTH FORMS. CASE 4, the one they flagged as dangerous, PASSES: a 'too early' watch row whose gap (4.49) ALREADY CLEARS the 3.5 trigger reads exactly like a qualifier but sits beyond the 12h window and carries NO BET| line (real case: Tim Kelly 2026-08-02, a 4.49-gap qualifier at 25.5 for SIX consecutive hours, every one outside the window); tipbot's only rule is the BET| prefix so it drops, and their assertion 'no WATCH or NOOP message contains BET| anywhere' is re-checked INDEPENDENTLY here because tipbot's entire safety model rests on it. Also re-verified: 18 fields, stake==target on every line, no field value containing a pipe, the retry carrying a distinct retry1 key, case 2's two bets arriving as TWO messages with distinct keys. NOTE BACK TO THE MODEL: the fixture doc is titled 'exactly what tipbot receives' but shows the WIRE form sent to the Telegram API, not what telethon delivers — they differ by exactly those HTML tags, and the difference IS the bug.) | PRIOR v6.08c (2026-08-03: RECONCILIATION EXPORT + Wilson's mode call. [MODE = ALERT, Wilson 2026-08-02] 'I'm fine with more than 500 on a single selection if tipped by 2+ tipsters, I am bounded by SB limits anyway.' So DISPOSALS_MODEL_RECONCILE_MODE now DEFAULTS to 'alert': outside money is RECORDED and ALERTED but does NOT reduce the stake. 'block' would have suppressed 6 OF 8 disposals unders on the 2026-08-02 board, because the players this model likes are the players Eddie/Saiyan like (two systems reading the same market, not a coincidence) — if the disposals edge is real and independent, blocking gives most of it away. NO global per-selection ceiling was added: SB's own limits are the bound, which is handover §9's original stance with the collision now VISIBLE instead of invisible. Note Eddie/Saiyan placing at THEIR OWN line is a DIFFERENT selection and correctly does not pool with the model's — the exposure join matches on exact (player, line, side), which is also how SB actually limits. [EXPORT BUILT] the hourly loop (_disposals_reconciliation_loop, started in main()) sweeps the book and appends one type='reconciliation' SNAPSHOT record per disposals selection to the fills file for the MODEL to read. Records carry NO `key` (a bet placed outside tipbot never had a message) and are joined on (player, line, side, event) — the model's loader originally filtered on `key` and would have discarded every one SILENTLY, which is why the shape is spelled out in the record itself via semantics='snapshot'. SNAPSHOT NOT DELTA: a later record REPLACES the earlier one; summing successive sweeps would inflate exposure every poll and progressively suppress real bets. Gated on RECONCILE_ENABLED and DELIBERATELY NOT on DISPOSALS_MODEL_ENABLED — the tipster ships dormant but the model has live tranches and hand-placed money to reconcile NOW, so the export must flow while placement is still off. Skips the 23:00-07:00 blackout (proven live: at 00:00 the guard correctly returns True and the sweep is skipped; the 23:04:41 'Session DROPPED batch: 13' is the schedule working). [EXPORT NAMES] each record carries BOTH player_book (Sportsbet's verbatim, case-mangled spelling, authoritative) AND player (the AFL canonical, or NULL when the join was not confident). _disposals_book_to_canonical is DETERMINISTIC — surname candidates from the roster, unique-after-first-initial or None — and never roster.fuzzy_match_player (four wrong-player regressions in v6.07): an explicit null is safer to hand the model than a plausible name, since a plausible name is exactly what a fuzzy matcher produces on the day it is wrong. [REFACTOR A TEST CAUGHT] the claim-time check and the hourly export each duplicated the session fetch+filter and only ONE used the fail-closed get_sessions_or_none; collapsed into a single _disposals_sweep_sessions(). Also fixed: both new functions referenced a bare `reconcile` but main.py imports it LOCALLY inside functions, never at module level. Scope is DERIVED (owned + has an afl.player_disposals cap), never hardcoded ids. [NEW ASK SENT TO THE MODEL] a retry must re-assert CURRENT qualification, not merely that a shortfall exists: open_shortfalls() returns any tranche with shortfall>0 and does NOT re-check the edge, so a tranche opened at 22.5 when the gap was 3.5 could be retried hours later at a gap of 2.0 — Wilson: 'it should only be retried if the line is at a point where it still has an edge'. [CORRECTION 2026-08-03, MY ERROR] I attributed that failure to open_shortfalls() as though it were the LIVE retry path. IT HAD ZERO PRODUCTION CALLERS — the model's live path iterates tips, which is already the qualifying set, so the failure AS I DESCRIBED IT could not have happened. Worse than not checking: I HAD checked (an earlier probe in the same session found no callers for either record() or open_shortfalls(), and I wrote that down) and then described open_shortfalls() as feeding an hourly retry anyway — the claim DRIFTED from what I verified to something stronger, across my own notes. WHAT SURVIVES: the record()-has-no-caller finding (verified, and load-bearing for the double-stake conclusion) and the ASK itself. The model implemented it BETTER than I framed it: plan_retry now takes the current gap+trigger and refuses when the gap no longer clears, asserted WHERE THE DECISION IS MADE rather than inherited from the caller's iteration order ('it holds given where the loop sits' is not good enough for money); they also DELETED open_shortfalls() as dead code and 'the unsafe twin, with no frozen check, no qualification check and no fresh key', keeping a test that asserts it stays deleted — the same shape as v6.07's finding that fuzzy_match_all was the un-guarded twin of fuzzy_match_player. ONE DEPARTURE FROM MY FRAMING, AND THEIRS IS RIGHT: I suggested CLOSING a tranche when it stops qualifying; they made it STATELESS (if the projection moves back and it qualifies again at the same line, the shortfall is fillable again) because a sticky flag is state kept for a case the gap test already answers — my own Q4 argument against a 'retry it if the line comes back' path, turned around correctly. This does NOT conflict with tipbot's sticky manual/ambiguous refusals: those are sticky because money MAY BE ON that we cannot confirm, a different concern from whether a bet still qualifies. 393 tests green.) | PRIOR v6.08b (2026-08-02: BOOK RECONCILIATION — the DisposalsModel exposure cap can now see OTHER PEOPLE'S money. Closes the v6.08a finding; reply to the model side in DISPOSALS_MODEL_ANSWER_2026-08-02b.md. [THE GAP] the v6.08 cap is keyed on THIS tipster's record of ITS OWN placements, so a BASE bet on a selection already backed by another tipster or by hand was unprotected by anything on either side. The model side made the sharper point: their gate A only ever guarded RETRIES, so for a first bet DISPOSALS_MODEL_ENABLED=false was the ONLY thing standing between us and it. I had also given them WRONG ADVICE — I told them to drop gate A once stake=target landed, on an idempotency argument my own analysis had already invalidated: the cap subtracts what it KNOWS ABOUT, not what is on the book, so re-asserting the total every tick was a standing instruction to top up ABOVE whatever was already there. Retracted. [BUILT] reconcile.collect_prop_exposure + parse_player_prop_bet (in reconcile.py so it syncs to the fork): sweep /api/pending_bets across every SB session carrying an afl.player_disposals cap (DERIVED from sessions.yaml, not hardcoded — the '100004 mistake' class), total money on per (player,line,side), count it against the selection ceiling. Swept at CLAIM TIME not on a background poll, so the number is fresh exactly when it matters and there is no staleness to reason about. [FIRST LIVE SWEEP: $4,049.60 of unsettled disposals props across 5 accounts], incl. $574.50 on ONE selection, none of it visible to the ledger (correcting v6.08a: the accurate Jayden Short figure is $574.50, not the ~$690 estimated from a partial sample). [THE PARSE TRAP, NOW PROVEN IN LIVE DATA] Sportsbet labels a prop '<Player> <N>+ <Stat>' and puts the side in the PARENTHETICAL: 'Jayden Short 22.5+ Disposals (jayden Short Under)' is an UNDER despite the '22.5+'. JACK ROSS HOLDS BOTH AN OVER 23.5 ($625) AND AN UNDER 24.5 ($600) RIGHT NOW, so reading the side off the label would have produced $1,225 of PHANTOM over-exposure and $0 under-exposure on a selection carrying $600. Parsed in ONE place with the four real strings as parametrised tests. [FAILS CLOSED] an API error, a missing account, or an ambiguous player join all return status='unknown', and unknown REFUSES the bet in block mode — a failed sweep reading as $0 re-arms exactly the double-stake this prevents. [THE PLAYER JOIN AVOIDS roster.fuzzy_match_player ON PURPOSE] it shipped FOUR wrong-player regressions in v6.07 and this is the worst possible path for it (a wrong canonical name either INVENTS exposure, blocking a good bet, or MISSES it, permitting a double one); instead a three-valued join — exact casefold, else surname+initial (so 'Ollie Wines'=='Oliver Wines', the case the handover flagged), else UNKNOWN on a shared surname ('Nick Daicos' vs 'Josh Daicos'), else no. unknown is NOT no. [A POLICY DECISION LEFT TO WILSON, NOT MADE FOR HIM] DISPOSALS_MODEL_RECONCILE_MODE: 'block' (default, live) counts outside money against the ceiling so a $500 target on a selection with $574.50 on it places $0; 'alert' records+alerts without reducing the stake, preserving handover §9's stance while making collisions VISIBLE instead of invisible. block is the right default but on today's board it suppresses 6 OF 8 disposals unders, because the players this model likes are the players Eddie and Saiyan like — two systems reading the same market. Whether that suppression rate is wanted is a money call, flagged OPEN. [ALSO AGREED] snapshot semantics confirmed (each sweep is the full current set; a later observation REPLACES the earlier, never sums — summing would inflate exposure every poll); DISPOSALS_MODEL_ENABLED=false is recorded as LOAD-BEARING SAFETY not a rollout convenience, and does not flip until reconciliation is verified against Jayden Short under 22.5 by READING THE REFUSAL REASON (not the absence of a bet, which is also what a silently broken sweep produces). [NOT YET BUILT, NOT CLAIMED AS BUILT] exporting type='reconciliation' records into the fills file for the model to read — the sweep feeds tipbot's own cap, the export is outstanding, so the model correctly KEEPS its gate A until records actually arrive. 387 tests green.) | PRIOR v6.08a (2026-08-02: DisposalsModel follow-ups from the model side's double-stake question (docs/tipbot-double-stake-question.md), answered in DISPOSALS_MODEL_ANSWER_2026-08-02.md. [1] EVERY PROCESSED MESSAGE NOW WRITES EXACTLY ONE FILL RECORD: two early returns (unparseable `start`, and `start` already past) wrote NONE, and the model's reader treats absence-of-feedback as 'assume it is on' — so a silent gap would have FROZEN that tranche forever. Both now write outcome='refused', and a test asserts every early return in _place_one_disposals_row is preceded by a write so it cannot silently regress. [2] PROBED /api/pending_bets LIVE AND FOUND A HOLE NEITHER DOC HAD WRITTEN DOWN: 29 pending records across 5 SB accounts, almost all DISPOSALS UNDERS, at liability-capped fan-out shares ($114.90/$120/$85.20/$86.20/$101.40) repeating across 5-7 accounts — i.e. ANOTHER TIPSTER'S (Eddie/Saiyan) disposals unders placed this morning. The v6.08 exposure cap is keyed on THIS tipster's own record of its own placements, so it is STRUCTURALLY BLIND to every bet from any other source (another tipster, or Wilson by hand — the model reports all 4 of its season's bets were hand-placed). A disposals-model tip on Jayden Short under 22.5 would see $0 committed and add a full $500 on top of ~$690 already on. The original handover §9 ('cross-tipster collisions need no check, the accounts will not have enough remaining liability for both') has the SAME defect as its retry reasoning: it assumes the first bet exhausts capacity, and at ~$100-120 a share against a ~$155/account ceiling it does not. CONCLUSION: pending_bets reconciliation is not a nice-to-have, it is the only thing that makes the exposure cap mean anything — now the TOP follow-up, above everything else. Endpoint is already wired (hyperbot_client.get_pending_bets + reconcile.py). Record = {dt,id,bet(text),odds,event,sport,stake,result,bet_type,account_id}; the `bet` text carries player+line+side but THE SIDE IS IN THE PARENTHETICAL, NOT THE LABEL ('Jayden Short 22.5+ Disposals (jayden Short Under)' is an UNDER despite the '22.5+' prefix, and 'Tom Mccarthy 23.5+ Disposals (tom Mccarthy)' with no 'Under' is an OVER) — anything reading the side off the N+ prefix gets EVERY bet backwards, so tipbot owns that parse, not the model. ALSO FOUND: only 5 of the 7 contract accounts currently have a live SB session (65465 Adam Tran and 111463 Aditya absent), so a $500 base is ~$100/acct against a ~$155 ceiling, much less headroom than the 7-account maths implied. [3] TWO CONTRACT CORRECTIONS FROM THE MODEL SIDE, BOTH ACCEPTED. (a) MY ERROR: §2 claimed 'Port Adelaide Power' FAILS to resolve because the matcher 'only substring-matches target-in-fixture' — I quoted a resolver.py comment describing behaviour that had since been REPLACED, as if it were live. The real matcher (resolver.py:193-196) does exact-or-nickname-suffix-strip equality and ' power' IS in _AFL_NICKNAME_SUFFIXES, so it resolves fine. The instruction to send the canonical 18 SURVIVES for a better reason now written into §2: nickname handling is a PER-CLUB ACCIDENT not a rule (' power'/' crows' are in the suffix list, ' giants' is NOT — 'Greater Western Sydney Giants' works only via an explicit whole-string AFL_TEAMS key added 2026-06-25 for an unrelated parser), so the canonical names need no nickname logic and cannot be broken by a change to either list. (b) §7 item 4 now records 'never edits, CONFIRMED' (sendMessage only, no message_id captured) rather than 'assumed'. [4] ANSWERED Q3 = (a) tipbot authoritative, AND IT IS ALREADY BUILT: the claim path computes headroom = min(target, MAX_SELECTION_STAKE) - committed - inflight and allowed = min(stake, headroom), so if the model emits stake=target every time tipbot places EXACTLY the shortfall and the model's record() wiring stops being load-bearing for money. Asked the model to emit stake=target, keep `target` for a stake==target schema assert, and DROP its gate A ('no fill record -> no retry') since under (a) a retry is an idempotent re-assertion of a desired total, so gate A only blocks good bets. Q4: strand a moved line — not a policy choice, the bet simply stopped qualifying (gap = line - projection, so a LOWER line is a SMALLER gap); the line moving UP is already handled by the high-water addon rule. Q5: the model HOLDS overnight rather than announcing — my §5 'it self-heals, the feed re-offers hourly' was true of tipbot in isolation and FALSE end-to-end, because the model's plan() commits a tranche on successful SEND and then returns None for that player+line forever, so an announced-but-unplaceable overnight tip was $500 silently lost; the quiet-window suppression stays as a BACKSTOP not the mechanism. Q6: yes to a NOOP|v2 heartbeat (my parser already drops any message without a BET| prefix, so it costs nothing) but the silence DETECTOR is NOT built yet — 'tipbot is not alerting on silence' must not be read as 'tipbot is reading me'. NO cross-tipster lock, reservation protocol or shared ledger between the repos: reconciliation against the BOOK observes what is actually on, rather than what each side believes it did.) | PRIOR v6.08 (2026-08-02: NEW TIPSTER DisposalsModel — an AFL player-disposals UNDER MACHINE FEED, ships DORMANT (DISPOSALS_MODEL_ENABLED=false). The model posts ONE bet per Telegram message as a fixed 18-field `BET|v2|...` line and owns nothing downstream; tipbot owns everything from the message onward. Contract: DISPOSALS_MODEL_CONTRACT_v2.md. [PARSE] pure regex, NO LLM and NO LLM fallback — for this strategy the LINE IS THE TRIGGER so nothing may re-interpret it; a new `machine_feed` channel flag branches in the telethon handler and RETURNS so the message never reaches the text/LLM path (REGEX_FIRST_TIPSTERS would NOT have sufficed — it falls through to the LLM when its trust gate fails). 'no BET| line' (chatter -> silent drop) and 'bad BET| line' (-> CRITICAL alert) are deliberately DIFFERENT outcomes; field order is POSITION-checked so a transposed emitter can't have a line read as a price. [6 HANDOVER CORRECTIONS, each verified in code first] (1) exact_only=True was NOT reachable for a new tipster — keyword-only, default False, and the AFL fan-out computes exact_only=_no_tipster_odds which is FALSE for any priced tip, so the ±1.0 nearest-line snap was LIVE (a silent 24.5->23.5 places a bet the model never selected, at full stake, reported as clean success); now FORCED ON for this tipster. (2) the BET| line's market=player_disposals is a schema ASSERTION not a leg value — the leg must carry the literal 'player_prop' and _resolve_leg_for_hyperbot derives player_disposals from the stat; copying it through would have routed 100% of tips to manual. (3) 'Ryan takes an equal share, not the 4.5x RATIO_CAP weighting' — AFL_FANOUT_RATIO_CAP is 4.0 and Ryan was equalised in v5.77, so all 7 accounts share an identical player_disposals ladder [124,99,74,50] and the weighting ALREADY produced an even split: an ACCIDENT OF YAML PARITY that one routine cap edit would silently re-skew with no test failing, so the even split is now taken DELIBERATELY. (4) kind=retry AS SPECIFIED WOULD HAVE DOUBLE-STAKED EVERY BET — the handover asserted Sportsbet liability is cumulative so a re-offer fills only leftover capacity, but even granting that, the ceiling is floor(124/0.80)=$155 of stake per account against a $500/7=$71.43 share (2.2x headroom), so a re-offer FILLS AGAIN and the selection lands at ~2x intended, every placement reporting success; worse, the model's own ledger.py has record() (the only thing that sets Tranche.filled) with NO CALLER, so shortfall always equals target and open_shortfalls() feeds an HOURLY retry = every bet re-offered at full stake every tick. (5) the handover's named template _place_dello_single writes NO ledger row and fires NO success alert (both are notifier side-effects, notifier.py:412/:838) so cloning it would have landed real $500 bets INVISIBLY — this path delegates to _place_afl_fanout instead and inherits the audited ladder, the 538 max-stake rebet, ambiguous/Guard-B handling, the Telegram summary and the bets_placed.csv row. (6) the $1 rollout via inline GLOBAL_MAX_STAKE=1 does NOT work for a fan-out (clamps per POST AFTER the split -> $7 placed + a false '$493 place by hand' alert) and the .env variant IS the 2026-07-18 incident; use DISPOSALS_MODEL_TEST_MODE ($7 across 7 accounts = $1 each), clamped at PLACEMENT via _apply_disposals_model_stake (the $600 lesson). [EXPOSURE LEDGER] logs/disposals_model_state.json, atomic tmp+replace, keyed on the SELECTION (evt~pid~line~side) NOT the message key because the point is to cap what accumulates ACROSS keys: caps cumulative placed at the message's `target` (and an independent MAX_SELECTION_STAKE) so a retry tops up only the SHORTFALL; permanent restart-surviving key dedup (a repeat key = upstream bug -> dropped + CRITICAL, never placed); a selection with an AMBIGUOUS or MANUAL outcome refuses ALL further offers (a maybe-landed bet counts as ON); cid_unresolved counts as exposure alongside is_ambiguous; a CORRUPT ledger FAILS CLOSED rather than resetting every cap. [SELF-REVIEW CAUGHT A BLOCKER] the claim recorded the key but did NOT RESERVE the dollars — place_tip runs in an executor, so a second message for the same selection could claim while the first was in flight and BOTH saw full headroom (verified by probing the real functions: two claims each returned $500 against a $500 target). Claims now reserve into an `inflight` field, released in _disposals_commit under try/finally; a crash mid-placement records the WHOLE reservation as AMBIGUOUS so nothing re-offers against a maybe-landed bet. [OTHER GATES] start-time refusal — which matters because resolve_afl_event deliberately accepts a game up to SIX HOURS past bounce (resolver.py:263-265), making this the only thing stopping a stale message placing IN-PLAY; `start` is parsed to an AWARE UTC datetime because main.py is otherwise naive-local and an aware/naive compare would TypeError inside the crash guard and turn the WHOLE feed into manual alerts (resolver.py:220-226 documents this class biting before). Absolute price floor DISPOSALS_MODEL_MIN_ODDS=1.70 (the model won't TIP below it so tipbot won't PLACE below it; break-even ~1.57) + a 10% worse-than-posted gate on top of the existing 1.25x MAX_ODDS_MULT ceiling — either routes the WHOLE tip to manual. Sportsbet-locked via TIPSTERS_FORCE_BOOKIE, a STRATEGY constraint here not just availability (the +18.35% was measured on Sportsbet's lines). A 4-distinct-players-per-fixture backstop (CORRELATION not turnover: every bet is an under, so N unders in one match behave like one bet at N x the size). bot_id is REQUIRED to register — the sender filter is the ONLY thing stopping another group member's BET| line placing real money and main.py's filter is a NO-OP when bot_id is absent, so a missing sender id refuses to register and logs loudly. [OVERNIGHT BLACKOUT] all bookie sessions are now scheduled OFF 23:00-07:00 to conserve data but this feed posts hourly THROUGH the night: inside the window tipbot logs a breadcrumb and stops BEFORE claiming a key (a claim would be burned and the alert would be ~8 false 'place by hand' pages a night); nothing is lost since the feed re-offers hourly and no AFL game starts before 07:00. [REVIEW] 5-lens adversarial pass + one independent refutation agent per finding -> 29 candidates, 21 REFUTED, 8 SURVIVED (2 BLOCKER + 1 HIGH), ALL FIXED. (1) BLOCKER ambiguous booked as $0: a SLOW REJECTION (elapsed>=5s, e.g. a 538 at 33s — the documented Erasmus case which HAD landed) is maybe-landed but carries NO FLAG (is_ambiguous is only set by the reconcile path and RECONCILE_AMBIGUOUS defaults false), so classifying on flags alone recorded that money as ZERO exposure and the next re-offer placed over a LIVE bet; now classifies with the shared _is_ambiguous_result predicate + mirrors the fan-out's _at_risk_stake fallback chain (r.stake is often None there) + fails conservative by booking the whole remainder if it still measures $0. (2) BLOCKER the fills file wrote selection_committed=0.0 on the refused/blackout/crash paths — the ONE field the contract tells the model to compute its shortfall from — so a FULL selection would be re-offered forever; now reads the true cumulative via a read-only _disposals_committed(). (3) HIGH selection identity was the emitter's RAW strings and every cap keys off it, so 'cd_i1001' vs 'CD_I1001' or '24.50' vs '24.5' would mint a NEW selection with a fresh full allowance; now normalised (casefold+strip+float canonicalisation) — and fixing it exposed that the per-fixture cap compared a RAW evt prefix against normalised ids so the cap had SILENTLY STOPPED FIRING, caught by its own test. (4) THE GUARDS WERE SCOPED TO THE PATH, NOT THE TIPSTER: AFL_CONCURRENT_FANOUT defaults true and is NOT pinned in .env and config.py documents 'set it false to revert', so ONE flag flip would route this feed to _place_singles_v4, which calls _execute_bet with presolved=None and re-resolves with BARE DEFAULTS (exact_only=False) — silently snapping 24.5->23.5 at FULL STAKE — after which _compute_alt_line_candidates deliberately walks an under UP by 1.0; closed THREE ways (place_tip refuses the unguarded path, exact_only is forced ON inside _resolve_single_for_placement so the invariant is INTRINSIC not per-call-site, and the alt-line walk returns [] for this tipster). (5) HIGH START_SLACK_SEC was unclamped and additive on the PERMISSIVE side so an innocent 3600 ('an hour of grace') would land a bet an hour past bounce with the resolver still serving the live game; clamped to 300s at import with a warning. ALSO: ledger write failures fail CLOSED; the per-fixture cap counts in-flight; a naive `start` is REFUSED not assumed-UTC (assuming UTC is the PERMISSIVE direction in AU, worth 10-11h of in-play exposure); the crash path books the post-clamp attempt not the pre-clamp reservation; placement uses the dedicated v6.03 pool; a benign 'nothing left to place' no longer pages CRITICAL every tick (v6.07 #30's ~1200/day lesson). KNOWN RESIDUAL, deliberately not fixed: an in-flight reservation LEAKS if os._exit (the freeze watchdog) kills the process between claim and commit, permanently freezing that selection — FAIL-SAFE (blocks rather than double-places), and expiring reservations would reintroduce the very double-stake it prevents, the same call v6.03 made for _inflight_fps; clear it by hand in logs/disposals_model_state.json if a selection stops topping up. [TESTS] test_disposals_model.py = 64 money-safety tests, 366 green; the pre-commit hook's pytest list is now a GLOB rather than a hand-written enumeration — the v6.07 comment already claimed 'every test_*.py' but the command still named six files, so this new file would have been silently UNGATED the moment it landed (finding #34 recurring for exactly the reason it happened the first time). Full suite green. [STILL OWED BY THE MODEL SIDE] emit the v2 line at all (send_tips_telegram.py does not yet), wire record() from logs/disposals_model_fills.jsonl so `filled` is real, emit stake=shortfall on a retry, and confirm the bot never EDITS a message (tipbot listens for NewMessage only, so an edit is invisible).) | PRIOR v6.07 (2026-07-31: MONEY-SAFETY SWEEP BUNDLE - 18 HIGH findings + the X-WATCHER 14h BLIND OUTAGE + the ROOT CAUSE of all three 'alive but silent' multi-hour outages. [ROOT CAUSE, the biggest win] 07-06 (~9.5h), 07-08 (~6h) and 07-29 (14.5h) were ONE bug: tipbot.bat ran `python main.py` with NO redirection and logging.basicConfig attached a StreamHandler, so every log line went to the launcher CONSOLE - and on Windows a console with QuickEdit (default ON) BLOCKS ALL WRITES while any text is selected. One stray click froze every thread that logs, indefinitely. The v6.02 freeze watchdog was NEVER broken: the liveness ticker does not log, so its stamp stayed fresh and it correctly stayed quiet. Fixed in 3 layers: the bat redirects to logs/stdout.log; the console handler is attached ONLY when sys.stdout.isatty() (TIPBOT_FORCE_CONSOLE_LOG=1 forces it back); and _loop_freeze_tick logs + alerts inside the bounded worker thread so a blocked logger can never prevent the os._exit restart. + a WORK-liveness watchdog (tiptitans LAST_POLL_TS) for tasks dying while the loop spins. [4 HIGHs THE SWEEP UNDER-RATED AS MEDIUM/LOW, all wrong-bet class] #22 WRONG HORSE: the TEXT racing parsers strip a runner-less row but the two IMAGE parsers do not, and router Guard 1 only skips a row missing ALL of saddle/runner/odds - so a cropped horse name reached racing_placer as runner=empty and BOUND A HORSE ANYWAY (saddle-only Pass 3, AND Pass 2's empty-substring test which is ALWAYS True in Python bound the only priced runner with NO SADDLE AT ALL) at up to $1500, reported as a clean success; fixed in BOTH layers. #16 BOTH SIDES OF ONE FIXTURE: a handicap's sides are the line's SIGN, not over/under, so every signed team_line row was dropped by _image_afl_conflicting_indices and a vision over-read of the SB line grid could auto-place West Coast -7.5 AND Port Adelaide +7.5 at $1000 each (a guaranteed overround loss on a bet never tipped); now derives the side from the SIGN + resolves both team labels to the fixture. #21 WRONG MARKET: the period guard scanned market/market_detail/title/selection, NONE of which the AFL image schema emits (market_detail has ZERO assignments repo-wide), so it was 100% DEAD - '2nd Half' in `description` placed a FULL-GAME handicap, and both price ~$1.90 so no odds gate saves it; now scans (period, description). #27 WRONG PLAYER: fuzzy_match_player broke score TIES by roster-FILE ORDER (the surname branches hard-code 0.85) - daicos->Josh not Nick, jones scoped [Adelaide,Essendon]->Chayce but REVERSED->Harrison so home/away ordering decided the bet, and Bailey J. Williams (an EXACT roster key, West Coast) resolved to Bailey Williams (Western Bulldogs); now scores ALL candidates, gates EACH, exact-key then surname preference, else REFUSES to guess. [X-WATCHER was BLIND 14h, 170 cycles] (a) _ensure() returned whenever _ctx was merely NON-NONE, so a dead chromium subprocess left a dead-but-non-None context and every goto() retried THE SAME CORPSE forever = one browser crash meant blind until a human restarted; now liveness-checks the page, resets on a dead-driver error, and rebuilds the client after a failure streak. (b) NOTHING COULD SEE IT: _touch_heartbeat() ran at the TOP of the poll loop, BEFORE the fetch and unconditionally, so it measured loop spin not work and stayed FRESH for 14h while xwatcher_watchdog.ps1 correctly never fired - the SAME liveness-is-not-progress bug as the 07-29 main.py outage, in a second process; now stamps ONLY after a successful poll (+ once at startup, since a MISSING heartbeat is treated as 'just started'), STALE_MIN 11->25, and the blind alert throttles on persisted wall-clock so watchdog restarts cannot reset the latch. (c) a stale-tip guard was built then set DEFAULT OFF (X_TWEET_AGE_GUARD, Wilson's call): the PLACEMENT path is the real backstop - a finished/in-play game carries no SB market (-> manual) and the 1.25x MAX_ODDS_MULT ceiling catches a moved line - so it mostly duplicated existing protection while costing manual work on good tips; the logic stays tested and a breadcrumb logs an old auto-placed tip. [OTHER HIGHs] all FOUR maybe-landed HB envelopes now carry cid_unresolved (the 4th, a POST timeout with no cid at all, was missed on the first pass) + both Guard B sites + racing coerces not_placed/spill -> conservative (my first racing fix only stopped the AUTO-spill, leaving a MANUAL re-place = hand double-stake); #7/#14 the pseudo-SGM demotion was DEAD in the shared builder (the stat lands in leg.market so an object-based check could never pass) -> a same-player alt-line pair placed as a $750/u SGM; #10 a Titans tip killed mid-placement was SILENT (it IS in seen_tip_ids so the restart alert could never surface it, and os._exit raises nothing) -> an inflight CLAIM, kept on CancelledError, alerted AMBIGUOUS at startup, NEVER retried; #11 a bare AFL surname went through the FUZZY get_player_team (Reid -> Liam Reidy -> Carlton, so a Carlton-Essendon event then matched Zach Reid) -> exact surname candidates, unique-or-manual, canonical full name; #9 the Titans poll-loop state_corrupt path would have RE-PLACED THE WHOLE FEED; #12/#13 roster surname + team-nickname gates; #19 benign rejects no longer trip the racing circuit-breaker (TAB 'Enquiry Failed - price too low' x50); #18 the breaker cooldown is now enforced at PLACEMENT too (proven 07-22: cooled 18:05:02, still PLACED on the same session 18:05:23); #24 WA Kellerberrin place=0; #26 racing fails CLOSED on an empty priority list. [AUDIT] 6-lens adversarial audit of the full 3,253-line diff, every finding independently verified: 30 verdicts, 22 REFUTED, 8 SURVIVED, 9 fixes applied. roster.py was the riskiest file and shipped FOUR wrong-player regressions found ONLY by old-vs-new differentials: generational suffixes made Jr/III the SURNAME (Jaren Jackson Jr->Quenton Jackson, on a LIVE $400/u blind NBA fan-out); the lone-token no-drift rule was added to fuzzy_match_player but NOT to its twin fuzzy_match_all, where the drifted name often sorted FIRST (Slawson->A.J. Lawson) and main.py rewrites the leg from it; surname drift ALONE bound a different SAME-CLUB player when the tipped player was absent from a stale roster (Toby Greene->Tom Green); and my own fix for that let a single-token CANDIDATE skip the check, which the ~1,300 bare-surname ALIAS entries reopened (Nikola Vucevic->Tristan Vukcevic). Also caught: the pseudo-SGM demotion keyed `market` before `stat` and the parsers stamp the generic market=player_prop on every leg, so a genuine mixed-stat SGM collapsed to a SINGLE at the COMBINED price; and the work-stall watchdog FALSE-FIRED os._exit(1) MID-BATCH (_process_batch is awaited inline so the stamp never refreshed; worst real batch 1283s vs an 1800s trip) -> now stamps per completed tip + 3600s. REFUTED and deliberately unchanged: the isatty logging (PROVED correct with no TTY), _load_rosters (ran it: all 4 caches + _mlb_collisions populate), the reconcile win/place gate (read /api/pending_bets across 37 accounts, 83 records: bet_type is the PROMO field, 'non_promo' on 83/83, and there is NO market key - so the gate is INERT and money-SAFE), the v5.94 top-up cap (already fixed; the residual is a documented NIT), #28 roster staleness (by design + web-search/manual backstop), #23 SGM raw_legs team key, #17 web-search (already bounded ~80s). [ALSO] lone unique NBA/MLB FIRST names resolve again (Chet x12 in the log, Deni, Rudy, Daniss, Jaylin), SPORT-GATED so AFL bare-surname behaviour is untouched, and purely additive (it only fires where the answer was already empty), verified over all 785 NBA + 1,613 MLB lone tokens = 0 wrong; roster writes are ATOMIC (a torn read cached an EMPTY roster for the whole process lifetime = every lookup missed, every tip silently manual, behind one log.warning); the ledger `side` now comes from the structured direction (521 of 1,095 disposals rows were blank) and is TOKEN-based so 'Overlord'/'Overton' is no longer an OVER bet; claude_usage_report priced claude-opus-5 at $0 SILENTLY = 17% of the true cost ($431 of $2,610) invisible; #25 a DST-correct _au_east_day replaces three hand-rolled +10h anchors and fixes the trackless Zak/Trial resolver asking about YESTERDAY's meetings for any 00:00-10:00 AEST post; #29 a session DEAD AT STARTUP was invisible to every monitor -> alert-only seeding of the existing watchdog; #30 a persistent Titans auth failure paged ~1200 CRITICALs/day on the same chat as the money-critical VERIFY alerts -> latched once per outage; #15 _log_jsonl can no longer take down a tip batch and a POST-placement crash KEEPS the dedup claim. 297 tests green. KNOWN/DEFERRED: lone NBA 'jaylen' resolves to Jaylen Brown though 3 players share it (PRE-EXISTING, proven by toggling the new branch off; left alone because fuzzy_match_player already absorbed 5 changes this bundle); two-word AFL surnames ('Ah Chee') route to manual; #17 executor offload; #25(C) jump-time drift.) | PRIOR v6.06 (2026-07-26: EDDIE TEXT-PLACE + ZAK SA-TRACK LIVE-PROBE + latency prune. [1 EDDIE TEXT-PLACE] Eddie posts some COMPLETE bets as TEXT follow-ups, not betslip images ('2.5u - Richmond +41.5 $1.91', 'Ross over 22.5 @ 1.87') -- they went to manual and never placed (07-25). Now an ACTIONABLE Eddie AFL TEXT post routes through _process_tip (Claude AFL parse -> place_tip fan-out, Sportsbet-locked, EXACT-OR-MANUAL): a clean single places, an unresolvable/matchup market -> manual, CHATTER -> 0 tips -> dropped (also silences the 'Adding one more' ping). eddie_afl added to UNITS_REQUIRED_TIPSTERS (no-stake text -> manual). Kill-switch EDDIE_TEXT_PLACE_ENABLED. [2 ZAK SA-TRACK LIVE-PROBE] Zak SA tips whose track web-search TIMED OUT (07-25: all 4) fell to a blind LLM fallback that guessed 'Gawler' (its prompt listed Gawler twice) when the real meeting was Morphettville -> whole card to manual. New racing_placer STAGE 1B: when a Zak tip prices nothing, probe today's real SA meetings (config.SA_THOROUGHBRED_TRACKS, metropolitan-first) against the LIVE catalog and bind the one that carries the runner. De-biased the fallback prompt + fixed the misleading 'CLAUDE WEB-SEARCH resolved' log. Kill-switch RACING_SA_TRACK_PROBE (default ON). [3 LATENCY] dropped chronically-unfunded neds:100006 from RACING_SESSION_PRIORITY (it fails 'insufficient funds' on every racing tip, wasting a spill slot); the reconcile-before-spill ~33s wait is intentional money-safety (LEFT INTACT); the real racing latency (tab:78280 sidecar 65s hang + bet365 WS) is HB-side/ops. REVIEW: 13-agent adversarial audit (3 lenses x verify) -> 2 CONFIRMED HIGH: (a) an Eddie TEXT half/quarter PERIOD handicap ('Hawthorn -5.5 2nd Half') would place FULL-GAME (wrong market -- the text parser drops the period and the period->manual backstop was saiyan-scoped); (b) Zak STAGE 1B could bind a GUESSED track on a Pass-2 SUBSTRING collision (wrong horse/track, worst on no-odds tips where the odds band is off). BOTH FIXED: (a) route any period-qualified Eddie text to MANUAL via _SAIYAN_PERIOD_HC_RE (extended to catch 'Qtr N'/'1st Q'/'HT'); (b) STAGE 1B binds ONLY on an EXACT runner_match, never a substring (STAGE 1 name-variants unchanged). 1-agent re-verify = SHIP (both closed, no new defect). 4 MEDIUM (Eddie margin-band/multi/image-vs-text-double) are odds-floor/ceiling + shared-dedup backstopped -- accepted residual (Wilson: odds gates catch the gross mispricings). Full suite green (199); the pre-existing test_v580 saddle test still fails (racing, unrelated). FOLLOW-UPS: period-AWARE Eddie TEXT parse so quarter/1st-half handicaps auto-PLACE via the v6.00 prop_ids instead of manual; cross-format fingerprint normalization; tighten the 13-track probe cost.) | PRIOR v6.05 (2026-07-21: EDDIE AFL IMAGE-TIP FIXES (3) + 11-agent money-safety audit. [1 CAPTION UNITS] Eddie posts a betslip PHOTO with the stake in the photo CAPTION ('3u'), not on the slip -> the vision read 'NO unit sizing' and the 2.5u fallback fired instead of his real size (07-18 12:43/20:06). Now _extract_units_from_caption reads the caption units and _build_afl_tip_from_image uses them as the sizing BASE -- but STILL through the no-units $1000 stake + $1000 liability caps (a caption is a HEURISTIC read, lower-confidence than on-slip, so it must NOT bypass the caps). Robust extraction: REJECTS results/brag captions (_CAPTION_NONBET_RE: won/banked/profit/heater/'this week'/'up N'...), requires EXACTLY ONE units token (findall != 1 -> ambiguous -> fallback), sane range (0,5]. SCOPED to a SINGLE-tip image (len==1 AND no_units_count==1, eddie_afl) so caption units can never be mis-attributed across a multi-slip image or applied N x. [2 AFL NICKNAMES] added the plural team nicknames missing from AFL_TEAMS -- HAWKS/CROWS/LIONS/BLUES/PIES/MAGPIES/BOMBERS/DOCKERS/DEMONS/KANGAROOS/ROOS/POWER -- fixing the 07-19 'Chol over 1.5 goals' EVENT NOT FOUND -> manual (team was 'Hawks'; only HAW/HAWI/HAWTH were mapped). [3 TRI-BET / EITHER-TEAM MARGIN BAND] IMAGE_PROMPT_AFL now recognises 'Either Team 1-24' / 'TriBet' as market_type=margin (team=null); the margin branch routes a NO-TEAM band to a CLEAN MANUAL alert (NEVER a -(N-0.5) handicap, never blind placement) instead of the old generic 'unschematic/multi' ping. AUTO-PLACEMENT DEFERRED: winning_margin_spread has NO placement infra -- needs a LIVE-CATALOG PROBE (weekend) to map the exact band selection/prop_id before it can auto-place. REVIEW: 11-agent adversarial audit (4 lenses x verify) -> 4 CONFIRMED findings, ALL in the caption-units feature (caps BYPASSED because injected units were trusted as real + naive re.search took the first Nu token) -> FIXED (caps now applied to caption units, robust extraction, single-tip scope) + 1-agent re-verify = SHIP (all 4 CLOSED, no new defect). Regression lens clean; HAWKS-vs-NBA-Hawks + tri-bet-routing REFUTED. Full suite green (216 passed). PRE-EXISTING (NOT this diff): test_v580_claude_fallback::test_saddle_only_fires_for_normal_tip fails (racing saddle, unrelated).) | PRIOR v6.04 (2026-07-18: WA HARNESS PLACE -> DO-NOT-BET on BetRight + Betr. WA place bets have NO MBL obligation, so BetRight REVIEWS them, partially accepts, then the v2 place_bet API reports the REQUESTED stake as PLACED -- an over-record (2026-07-18 Northam R2 Maas Attack: req $111.11, HB returned success stake=$111.11, but only $20.83 actually debited from balance; HB's own log even says 'Bet partially accepted' before misreporting). bets_placed.csv + every downstream P/L artifact inherits the inflated stake. Betr just REJECTS WA places outright ('maximum allowed of $0.00', ErrorNo 1010) = pure churn. Root cause of the attempt: Northam (WA) was NOT in the session harness track list -> fell to default:mbl -> country place cap $200 -> $200/(2.8-1)=$111.11. FIX (sessions.yaml): added all WA country harness venues (Northam, Narrogin, Central Wheatbelt, Wagin, Williams, Busselton, Collie, Albany) with place:0 (do-not-bet) + win kept at the country MBL, to BOTH betright (99996) and betr (100005) -- same WA-place pattern the file already had for Gloucester Park/Pinjarra/Bunbury. Verified: WA places -> do-not-bet on both accts; WA wins unaffected; non-WA harness places (Menangle/Melton/Globe Derby/Albion Park/Redcliffe/Marburg) still place at $200 (no regression). Cross-checked EVERY bookie's HB log for 2026-07-18: ONLY betright silently over-records; sportsbet/tabtouch/neds ACCEPT WA places in full; hotbet/ladbrokes/bet365 clean-rejected. HB bug submitted separately (partial-accept reported as full stake). Config-only; requires restart (sessions.yaml is startup-read). Sportsbet/TABtouch/Neds WA places LEFT ON (they honour them).) | PRIOR v6.03 (2026-07-17: FREEZE-HARDENING BUNDLE + SAFE-MODE governor. [#2 ROOT CAUSE] HARD wall-clock ceiling (V3_POLL_HARD_CEILING_SEC=420) on the v3 placement poll loop (_post_v3_async) -- the 6.5h freeze was a poll loop with NO upper bound on the server budget; a /place_bet past the ceiling now returns the EXISTING ambiguous envelope (debit + blocklist + STOP the ladder, never retry/spill), price paths a plain transient fail; _compute_poll_budget also clamps at source. [GUARD B] a 'hard poll deadline' ambiguous is NEVER downgraded to a clean not_placed (that -> UNFILLED -> manual re-place -> LATE land = DOUBLE STAKE) -- applied in _reconcile_fanout_ambiguous (AFL fan-out) AND _place_mlb_alex_single (MLB single, the review-caught gap). [#1] place_tip OFFLOADED off the asyncio loop via a dedicated ThreadPoolExecutor (PLACEMENT_OFFLOAD_ENABLED, default on) so a hung placement can't freeze telethon / the freeze ticker; dedup reworked to CLAIM-BEFORE-PLACE (the added await opened a check->register race) with a NEVER-time-purged _inflight_fps set so a >600s slow-HB placement can't have its claim evicted mid-flight (the review-caught double-stake window); image router _route_image_afl_tips is now async (claim moved AFTER jobs.append -> no leak on a build crash). [#3] startup /api/pending_bets orphan reconcile (reconcile.find_orphan_pending) -- ALERT-ONLY (never re-places), owned-scope, dedup-persisted -- SHIPPED GATED OFF (STARTUP_PENDING_RECONCILE_ENABLED). [CLAUDE-CODE PLACEMENT CAP] a place_*-choke-point governor (GLOBAL_MAX_STAKE clamps stake / GLOBAL_DAILY_PLACEMENT_CAP caps attempts/day; price checks + all other HB endpoints UNRESTRICTED; inert under TIPBOT_TESTING; both default 0 = OFF, fork-safe). It is a safety bound for CLAUDE-CODE-triggered TEST placements ONLY (so Claude can place small bets to verify markets without asking) — set INLINE per command, e.g. `GLOBAL_MAX_STAKE=1 GLOBAL_DAILY_PLACEMENT_CAP=10 python ...`. The LIVE tipster feed's .env keeps BOTH at 0 so the feed places FULL normal amounts, uncapped. NOTE: a 2026-07-18 misconfig put $1/10 in the live .env and briefly clamped/rejected the tipster feed 00:14-10:33 until reverted to 0/0. Review: 3-lens adversarial pass on the diff -> 2 CONFIRMED HIGH double-stake findings (MLB not_placed missing Guard B; claim-expiry window) BOTH FIXED + re-verified, +1 LOW image claim-leak fixed. Full suite green (+ governor/inflight/ceiling/GuardB tests). Re-verify pass = SHIP; +1 post-review residual closed (text-path dedup timestamp REFRESHED on a landed bet, mirroring the image PASS-3 refresh, so a >600s flight can't age the claim past DUPE_WINDOW_SECS -> a re-send just after completion is still deduped). Alongside: settings.local.json bet-placement confirmation guard removed (replaced by the $1/10-a-day governor).) | PRIOR v6.02 (2026-07-17: [LOOP-FREEZE WATCHDOG] a SECOND silent multi-hour outage -- the whole asyncio EVENT LOOP FROZE ~6.5h (02:52-09:32) on a hung synchronous HyperBot call, so EVERY in-loop watchdog (the telethon get_me prober AND the session-watchdog heartbeat stamper) froze WITH it and nothing auto-recovered; the external check_session_health correctly saw the heartbeat go stale but only alerts on DOWN sessions -- HB sessions were all UP the whole time -- so it stayed silent. FIX = an OUT-OF-LOOP OS-thread watchdog: a tiny asyncio ticker stamps an in-memory _loop_heartbeat_ts every 15s while the loop spins; a daemon thread (started in __main__ so it survives reconnects; a blocking network call releases the GIL so it keeps running while the loop is wedged) checks every 30s and, if the stamp is >MAX_SILENCE stale, fires a BOUNDED synchronous Telegram alert (worker-thread + 12s join so a DNS stall can't hang it) then os._exit(1) -> tipbot.bat's :start loop relaunches clean. ARMED at main() top, DISARMED (ts=0) during the reconnect backoff so it never false-fires during the fast in-process telethon reconnect. MAX_SILENCE=900s is a deliberate BACKSTOP (not a precise trip): place_tip runs SYNCHRONOUSLY on the loop so a hung bookie session legitimately blocks it ~one HB cid-timeout (~305s), and the watchdog MUST still fire during a freeze that happens DURING a placement, so 900s cleanly separates a bounded legit block from an INDEFINITE wedge -> never trips mid-bet. Turns a 6.5h dead freeze into ~15min. Kill-switch LOOP_FREEZE_WATCHDOG_ENABLED (default ON). 2-lens adversarial review (false-fire/money-safety + correctness/recovery) -> raised the threshold 300->900, bounded the alert, made the watchdog thread loop exception-proof, added a test driving the os._exit path + pinning the >=600s money-safety floor; full suite green. FOLLOW-UPS (deferred): offload sports/image placement via run_in_executor (the real cure -- racing/Leroy already do), startup /api/pending_bets reconciliation for os._exit-orphaned bets, hard total-deadline on HB calls. Config alongside: Zak + Trial racing-image units 400->500.) | PRIOR v6.01 (2026-07-17: [Dello SGM] same-game 2-player player-prop SGMs now AUTO-PLACE via the SGM fan-out (-> Daniel, $1 DELLO_TEST_MODE) instead of manual; _place_sgm_fanout scopes every leg to the event's teams so a CROSS-game leg fails to resolve -> manual (never a wrong cross-game bet); a non-player-prop / non-standard-stat leg -> manual. [E tri-bet] Eddie winning-margin RANGE BAND ('St Kilda 1-39') now routes to MANUAL and can NEVER become a -(N-0.5) handicap (the bug Wilson flagged); IMAGE_PROMPT_AFL refined to emit range bands as market_type=margin + description='<team lo-hi>' + line=null; open-ended 'N+' still converts to the placeable alt-line. [B-MEDIUM] extracted _afl_team_word_match (word-boundary + the 3 cross-club rejections) to module level; the PERIOD resolvers now share the full-game handicap's FUZZY team-match, so nickname tips (Sydney<->Sydney Swans, GWS<->GWS Giants) auto-place instead of falling to manual (the _match_handicap_in_catalog refactor is byte-identical: 1156/1156 pairs). Refined the Claude API parse prompts for Eddie (margin bands) + Dello (new same-game-SGM section). Adversarial review: Dello-SGM + team-match CLEARED; the band guard now reads the real `description` field the refined prompt emits (was scanning phantom fields). DEFERRED: full tri-bet auto-PLACEMENT (still needs a real Eddie tri-bet image to map the parse -> winning_margin_spread band).) | PRIOR v6.00 (2026-07-17: AFL PERIOD-MARKETS EXPANSION + no-odds Eddie fix. [B] QUARTER (1q-4q) team handicaps now AUTO-PLACE via the EXACT per-quarter proposition_id (quarter_line) — un-gated from v5.99's quarters-manual restriction; Wilson accepts the identical-line-across-quarters vision-digit risk (a placed-quarter confirmation is logged). [D] PERIOD TOTALS auto-place: 1h->first_half_total, 2h->second_half_total, 1q-4q->quarter_total (exact side+line+prop_id). [C] 2nd-half RECOGNISED (2h) but SB carries NO 2h LINE market -> 2h handicaps route to manual (exact-or-manual); 2h TOTALS place. All resolvers verified against a LIVE SB catalog probe (Syd v Ade, 2026-07-17); EXACT-or-manual invariant, never a full-game/wrong-period fallback. [A] NO-ODDS Eddie player prop: a LONE one now AUTO-PLACES as a single (was a SILENT drop -- the 07-16 Marshall/Nasiah miss) EXACT-line-only (no +/-1 snap) with a safety band -- live price must be >1.75 (else manual), total stake capped so to-win<=$1500, no live price -> manual; >=2 no-odds player legs in ONE image = SGM combo -> ONE manual alert (never silent). 5-agent adversarial review returned DON'T-SHIP on an A-HIGH no-odds blocker (wrong-line +/-1 snap + no price guard at production stakes) -> FIXED (exact-line + band) and a 1-agent re-verify returned CLEARED. B/C/D verified clean. Gated by AFL_PERIOD_MARKETS_ENABLED except the no-odds player-prop routing (unconditional for eddie_afl). Config alongside: denzel SB (118458) added to AFL singles+SGM+racing (Daniel-cap mirror); Daniel afl.sgm cap raised [400,300,200]->[1500,1000,600] for the Daniel-only SGM window (Adam/Wilson out ~1wk for declarations). DEFERRED: tri-bet/margin bands [E, needs a real Eddie image]; period-resolver fuzzy team-match [B-MEDIUM, money-safe under-fire].) | PRIOR v5.99 (2026-07-13: [#5] AFL PERIOD MARKETS -- 1st-HALF team handicaps auto-place on Sportsbet via quarter_time_line + the EXACT proposition_id (quarters route to MANUAL: identical line+odds across quarters = undetectable wrong-quarter); kill-switch AFL_PERIOD_MARKETS_ENABLED. [#7] SAIYAN-HC-SGM -- Saiyan FULL-GAME-line handicaps (single + SGM, e.g. Wilkie/Richards + STK+36.5) now PLACE at the Sportsbet max across all accounts (via the new SGM max-stake rebet) instead of routing to manual; the handicap leg resolves EXACT-or-manual (first_half_line / period / mangled / cross-club team all -> manual, NEVER a full-game fallback); kill-switch SAIYAN_HC_SGM_ENABLED. Plus SGM MAX-STAKE REBET on the SGM fan-out (mirrors the reviewed singles rebet). Each opus-reviewed (20-agent for #7, 10-agent for #5) with EVERY confirmed finding fixed; full suite green. Both kill-switches ENABLED via .env at this deploy.) | PRIOR v5.98 (2026-07-12: TWO features shipped together. [A] SB MAX-STAKE-REBET (v5.9x, Wilson; 20-agent+9-lens reviewed clean): the FIRST Sportsbet stake-too-high (538) reject now rebets ONCE at the bookie-stated allowable max (top-level max_stake field / 'max=$X' in the error) instead of laddering DOWN blindly -- sports (AFL fan-out + singles_v4) AND racing (_sb_racing_max_stake); FAST-reject-only, with an Erasmus double-stake guard; kill-switch SPORTSBET_MAX_STAKE_REBET; AFL price floor >$2.00 tightened to 20%. [B] NEW tipster Dello AFL (Telegram DELLO_TG_CHANNEL -4892033826, ingested via telethon like Saiyan -- NOT the Discord self-bot, which stays a read-only corpus tool). Auto-places AFL player-prop SINGLES ONLY on Sportsbet at a $1 TEST stake (DELLO_TEST_MODE default true, flat-clamped at PLACEMENT via _apply_dello_flat_stake -- the $600 lesson; DELLO_UNIT_SIZE=400 is the prod stake). _place_dello_single gates: live SB odds must be in [DELLO_BAND_LO 1.50, DELLO_BAND_HI 3.00] (>HI or <LO -> manual), EXACT line/selection (exact_only=True threaded through _resolve_single_for_placement -> _match_afl_player_prop, NO +/-1 nearest-line snap), live SB >= (1-DELLO_SB_WORSE_GATE 0.10)*Dello's quoted price else manual (rule 3), and NO tipster odds -> manual (rule 4). SGMs / cross-game multis (HyperBot has NO parlay primitive) / exotics / team markets / image betslips all -> MANUAL alert. dello_afl added to UNITS_REQUIRED_TIPSTERS (+_NO_BET_FRAMING_RE extended with his 'not tipping it'/'won't tip'/'just leans'/'worth a mention' vocab so a live in-band single inside an explicit non-tip is suppressed whole-message) and CLAUDE_TEXT_FALLBACK_TIPSTERS; TIPSTERS_FORCE_BOOKIE dello_afl=sportsbet. GATED on DELLO_ENABLED (kill-switch, DEFAULT OFF -> ships DORMANT; the channel is NOT monitored until flipped). 17 money-safety tests (test_dello.py) + a 3-agent adversarial review that found+fixed the snap / no-bet-framing / no-odds holes; AFL regression suite green. Design+corpus+adversarial record: dello_integration_plan.md. NOT DEPLOYED / NOT ENABLED -- Wilson confirms the channel is his tips feed (+ optional DELLO_SENDER_ID) and flips DELLO_ENABLED to go live at $1. Image single-betslip auto-place + his own bet channel = follow-ups.)"

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

# ── Claude per-call FALLBACK (v5.80) ─────────────────────────────────
# Independent of TIP_PARSER_PROVIDER (the global swap). This is the per-call
# RECOVERY LAYER: Groq runs first; Claude fires ONLY when Groq fully fails on a
# GENUINE bet (repair-failed / 0-tips-on-a-bet-looking msg / empty vision) — and
# NEVER on the v5.58/v5.59 no-bet / summary / chatter branches (that would
# re-open the AusBets "$400 on a no-bet message" hole). Every Claude-recovered
# tip re-enters the IDENTICAL placement/roster/floor/dedup gates. OFF by default.
CLAUDE_FALLBACK_ENABLED = os.getenv("CLAUDE_FALLBACK_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
# Opus 4.8 — fires only after Groq already failed (a few/day), so accuracy over
# cost. Exact ID, no date suffix. Parse is prompt-guided + JSON-repair (schema
# NOT enforced — a nested raw_legs/alt_line json_schema would risk a 400); a
# repair failure returns the empty sentinel -> manual. Opus reliably emits JSON,
# so the 06-20 Groq-gibberish failure mode does not recur on the Claude tier.
CLAUDE_FALLBACK_MODEL = os.getenv("CLAUDE_FALLBACK_MODEL", "claude-opus-4-8")
# v5.81: web-search resolvers (player/track) use SONNET — ~3x cheaper than Opus
# and ample for "current club / today's SA track" lookups. The web_search path is
# the costly one (search fees + large result contexts), so the cheaper model +
# the search/loop caps below keep each resolve well under a dollar. Parse fallback
# stays on Opus (CLAUDE_FALLBACK_MODEL) for extraction accuracy.
CLAUDE_WEBSEARCH_MODEL = os.getenv("CLAUDE_WEBSEARCH_MODEL", "claude-sonnet-4-6")
# v5.83: CLAUDE PRIMARY — when true, the parser SKIPS Groq entirely and parses
# every tip (text / vision / racing-text) with Claude up front. Rationale: the
# Groq llama-4-scout model is deprecated (shutdown 2026-07-17) + gibberish-prone,
# and waiting for a Groq failure before the Claude fallback added too much
# latency. Pure parser swap — the SAME downstream no-bet/units/roster/floor/
# placement gates apply. FORK-SAFE: defaults FALSE, and falls through to Groq if
# Claude is unavailable (no key/SDK), so the sportsbot fork (no key) stays on Groq.
CLAUDE_PRIMARY = os.getenv("CLAUDE_PRIMARY", "false").strip().lower() in ("1", "true", "yes", "on")
# Claude web-search resolvers (player->team/game, SA track, racing runner) when
# the roster/catalog can't resolve a player/track. Still gated by the bookie
# catalog + odds floor before any bet. Requires CLAUDE_FALLBACK_ENABLED too.
CLAUDE_WEBSEARCH_RESOLVE = os.getenv("CLAUDE_WEBSEARCH_RESOLVE", "false").strip().lower() in ("1", "true", "yes", "on")

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
# ($400 / 4 accounts = $100 -> $100/$90/$85/$80...). The MLB HRRBI SGM fan-out uses
# this fixed-% ladder (NOT the mlb.sgm liability brackets): each of the 4 SGM
# accounts (Adam/Wilson/Daniel/Ryan) places the full even-split first, then
# ladders DOWN on a bookie stake-reject; any unfilled remainder spills to Alex as
# a 2+ single. Restart to load.
# 2026-06-25: extended with 0.7/0.6 rungs. The 06-23 Matt Olson SGM had a bookie
# per-line cap of $105.40 -- JUST under the old 0.8 bottom rung ($106.66 off a
# $133.33 split) -- so EVERY rung was rejected and the whole SGM went $0->manual.
# The extra rungs let an account place BELOW a tight cap instead of failing fully.
# Purely additive: lower rungs only engage AFTER a higher rung is stake-rejected,
# so a normal fill (top rung accepted) is unchanged.
MLB_HRRBI_LADDER_PCT = [
    float(x) for x in os.getenv("MLB_HRRBI_LADDER_PCT", "1.0,0.9,0.85,0.8,0.7,0.6").split(",")
    if x.strip()
] or [1.0, 0.9, 0.85, 0.8, 0.7, 0.6]

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
# v5.9x (Wilson 2026-07-12): on the FIRST Sportsbet stake-too-high (538) reject,
# rebet ONCE at the bookie-returned max stake (HB v1.7.85 surfaces it as
# "max=$X" + a top-level max_stake) instead of laddering DOWN blindly. Applies to
# sports (AFL fan-out + singles_v4) AND racing. Bounded: the rebet is always
# <= the rejected stake, so it never over-stakes; one rebet only (no loop).
# Kill-switch — set false to revert to the old percentage ladder.
SPORTSBET_MAX_STAKE_REBET = _env_bool("SPORTSBET_MAX_STAKE_REBET", True)

# v6.07 (HB API doc 1.7.10x): send `max_odds` on /v3/place_bet — a BOOKIE-SIDE price
# ceiling that auto-rejects a fill above it ("guards against wrong-line matching").
# We already enforce the same ceiling client-side (_exceeds_odds_ceiling), but only
# against the catalog price seen BEFORE placing; max_odds also covers drift between
# the price-check and the fill. It can only ever PREVENT a bet. Kill-switch in case
# it ever rejects more than expected (then the client-side check still applies).
HB_SEND_MAX_ODDS = _env_bool("HB_SEND_MAX_ODDS", True)
# v6.07: send the documented `direction` (over/under) field alongside `selection`.
# Purely additive — HB already honours our side today (verified 763/763 in
# production), so this is alignment with the documented contract, not a fix.
HB_SEND_DIRECTION = _env_bool("HB_SEND_DIRECTION", True)

# v5.9x (Wilson 2026-07-12): Wilson's OWN bet channel (a Telegram group he owns).
# 6-field format: "sport / teams-or-event / single|sgm / selection / stake / odds".
# AFL/NBA/MLB route through the normal per-sport fan-out (split-stake across all
# accounts + the max-stake rebet); racing -> manual alert (an explicit-dollar
# racing self-bet doesn't fit the image-racing test/cap pipeline). HARD stake cap
# (accidental extra-0 guard). NO dedup (Wilson re-bets the same selection
# intentionally). Kill-switch SELF_BET_ENABLED (unmonitored when false). Channel
# id overridable via env for testing.
SELF_BET_ENABLED = _env_bool("SELF_BET_ENABLED", True)
try:
    SELF_BET_CHANNEL_ID = int(os.getenv("SELF_BET_CHANNEL_ID", "-5423601371") or "0")
except ValueError:
    SELF_BET_CHANNEL_ID = 0
SELF_BET_MAX_STAKE = _env_float("SELF_BET_MAX_STAKE", "1500")

# v5.9x (Wilson 2026-07-12): Eddie CAPTION FALLBACK kill-switch. When an AFL image
# vision-parses to 0 tips, optionally parse the Telegram caption through the AFL
# text pipeline (for tips whose bet lives in the caption, not the image). Ships
# DORMANT (default False): the 07-12 review found a recap/scratch caption could
# auto-place a live bet, so it stays off until the strict gate is watched live.
EDDIE_CAPTION_FALLBACK_ENABLED = _env_bool("EDDIE_CAPTION_FALLBACK_ENABLED", False)

# v6.06 (Wilson 2026-07-25): Eddie TEXT-TIP PLACEMENT kill-switch. Eddie posts some
# COMPLETE, placeable bets as TEXT follow-up messages, not betslip images (e.g.
# "2.5u - Richmond +41.5 $1.91 - 365", "Ross over 22.5 at $1.87"). Today those went
# to a manual alert and never placed. When on, an ACTIONABLE Eddie AFL TEXT post is
# parsed + placed through the SAME AFL text pipeline Saiyan uses (Claude AFL parse ->
# place_tip fan-out, Sportsbet-locked, EXACT-OR-MANUAL): a clean single places, a
# non-placeable selection (matchup/parlay/period) fails to resolve -> manual, and
# chatter parses to 0 tips -> dropped (no ping). Units REQUIRED (eddie_afl in
# UNITS_REQUIRED_TIPSTERS) so a no-stake text -> manual. Ships DORMANT (default False)
# until the money-safety review is done + watched live.
EDDIE_TEXT_PLACE_ENABLED = _env_bool("EDDIE_TEXT_PLACE_ENABLED", False)

# v5.9x (#5, Wilson 2026-07-12): AFL PERIOD markets kill-switch. When on, an AFL
# quarter (1q-4q) or 1st-half TEAM HANDICAP is placed on Sportsbet via the bookie's
# EXACT proposition_id (resolved live from price_check_sports: quarter_line /
# quarter_time_line). Ships DORMANT (default False): niche markets, exact-prop-id
# critical — validate with a pre-game probe before enabling. When off, period
# markets route to manual as before. Unmatched / non-SB / unsupported period ->
# manual (never a wrong prop_id, never a full-game fallback).
AFL_PERIOD_MARKETS_ENABLED = _env_bool("AFL_PERIOD_MARKETS_ENABLED", False)

# v5.9x (#7, Wilson 2026-07-12): stop auto-routing SAIYAN handicap bets to manual.
# With the max-stake rebet live, a Saiyan handicap SINGLE (AFL fan-out) or SGM
# (SGM fan-out, e.g. Wilkie/Richards + STK +36.5) now PLACES at the bookie max
# across all accounts, unplaced -> manual. The handicap leg resolves via the exact
# _match_handicap_in_catalog (line/pick_own_line) — a miss routes to manual, never
# a wrong/blind line. Ships DORMANT (default False): the handicap-in-SGM path was
# historically flagged unreliable, so it stays off until the 07-12 review + a live
# probe confirm it, then flip on. When off, Saiyan handicaps route to manual as
# before. Kill-switch.
SAIYAN_HC_SGM_ENABLED = _env_bool("SAIYAN_HC_SGM_ENABLED", False)

# ── Tipster Channel Configs ──────────────────────────────────────────
# Unit sizes loaded from .env: SAIYAN_UNIT_SIZE, KEV_UNIT_SIZE, AUSBETS_UNIT_SIZE, SHOOK_UNIT_SIZE, TEST_UNIT_SIZE
SAIYAN_UNIT_SIZE = _env_float("SAIYAN_UNIT_SIZE", "150")
# Saiyan SGMs stake a LARGER unit than his singles/disposals (Wilson 2026-06-14).
# Singles/disposals keep SAIYAN_UNIT_SIZE (600 live); SGMs use this. The SGM
# fan-out EVEN-SPLITS the unit across the 3 SGM accounts (Adam 65465 / Wilson
# 53522 / Daniel 68723), so 750 -> $250 stake each. (The per-account afl.sgm
# liability ladder [400,300,200] still caps a high-combined-odds SGM lower.)
# v5.89 (2026-06-26): the DEFAULT now FALLS BACK TO SAIYAN_UNIT_SIZE (not a
# hardcoded 750). The sportsbot FORK sets no unit overrides in its .env, so it was
# inheriting Wilson's full $750 SGM unit while its singles defaulted to $150 -- now
# its SGM unit matches its single unit ($150). Tipbot is UNCHANGED: its own .env
# sets SAIYAN_SGM_UNIT_SIZE=750 explicitly, which still wins. A fork only ever
# over-stakes an SGM if it explicitly sets SAIYAN_SGM_UNIT_SIZE in its OWN .env.
SAIYAN_SGM_UNIT_SIZE = _env_float("SAIYAN_SGM_UNIT_SIZE", str(SAIYAN_UNIT_SIZE))
KEV_UNIT_SIZE = _env_float("KEV_UNIT_SIZE", "100")
AUSBETS_UNIT_SIZE = _env_float("AUSBETS_UNIT_SIZE", "100")
SHOOK_UNIT_SIZE = _env_float("SHOOK_UNIT_SIZE", "300")
TEST_UNIT_SIZE = _env_float("TEST_UNIT_SIZE", "1")
ETR_UNIT_SIZE = _env_float("ETR_UNIT_SIZE", "400")  # ETR NBA (text, obfuscated names)

# ── Dello AFL (Telegram, DELLO_TG_CHANNEL) ──────────────────────────
# Ingested via telethon like Saiyan (NOT the Discord self-bot). Auto-places
# SINGLES ONLY on Sportsbet: HyperBot has no cross-game-multi primitive and the
# AFL SGM line bug (why Saiyan SGMs are manual) means SGMs/multis/exotics route
# to a MANUAL alert. Rules (dello_integration_plan.md): place only if the LIVE
# SB price is in [DELLO_BAND_LO, DELLO_BAND_HI]; >HI or <LO -> manual; if live SB
# is >DELLO_SB_WORSE_GATE below Dello's quoted price -> manual; anything unclear
# -> manual. $1 TEST stake is enforced at PLACEMENT time (the $600 lesson) while
# DELLO_TEST_MODE — flip it off + set DELLO_UNIT_SIZE for real-size placement.
# DELLO_ENABLED is the kill-switch: the channel is NOT monitored until it's true.
DELLO_UNIT_SIZE = _env_float("DELLO_UNIT_SIZE", "400")
DELLO_TEST_MODE = _env_bool("DELLO_TEST_MODE", True)
DELLO_TEST_UNIT_SIZE = _env_float("DELLO_TEST_UNIT_SIZE", "1")
DELLO_ENABLED = _env_bool("DELLO_ENABLED", False)
DELLO_TG_CHANNEL = os.getenv("DELLO_TG_CHANNEL", "").strip()
DELLO_BAND_LO = _env_float("DELLO_BAND_LO", "1.50")
DELLO_BAND_HI = _env_float("DELLO_BAND_HI", "3.00")
DELLO_SB_WORSE_GATE = _env_float("DELLO_SB_WORSE_GATE", "0.10")

# ── Scheduled nightly HyperBot shutdown (session-monitor quiet hours) ──
# v6.08f (Wilson 2026-08-01 scheduled it, 08-03 fixed the fallout). Every bookie session
# is turned OFF overnight to conserve data. The session monitors predate that and cannot
# tell a scheduled shutdown from a crash, so they paged CRITICAL for it -- 5 criticals on
# the night of 08-02, on the MONEY chat, the one carrying the "MAY have LANDED, VERIFY"
# pages. That trains you to ignore the one channel that must stay trustworthy, which is
# the v6.07 #30 failure again (a tipster burying real alerts under ~1200/day).
#
# Inside the window a confirmed drop is reported as INFO rather than CRITICAL. Still
# detected, still logged, still visible; only the paging stops. A session genuinely dead
# overnight escalates to CRITICAL on the first cycle AFTER the window, so the worst case
# is a delay to 07:00 -- and no AFL match starts before then. Daytime drops are untouched.
#
# Set the two hours EQUAL, or SESSION_BLACKOUT_ALERTS_QUIET=false, to page as before.
SESSION_BLACKOUT_ALERTS_QUIET = _env_bool("SESSION_BLACKOUT_ALERTS_QUIET", True)
SESSION_BLACKOUT_START_HOUR = _env_int("SESSION_BLACKOUT_START_HOUR", "23")
SESSION_BLACKOUT_END_HOUR = _env_int("SESSION_BLACKOUT_END_HOUR", "7")

# ── DisposalsModel (AFL player disposals UNDER, machine feed) ─────────
# v6.08 (2026-08-02). The AFL Player Disposals Model posts ONE bet per Telegram
# message as a fixed 18-field `BET|v2|...` machine line (contract:
# DISPOSALS_MODEL_CONTRACT_v2.md). Parsed by pure regex in parsers/disposals_model.py
# with NO LLM and NO fallback — for this strategy the LINE IS THE TRIGGER, so a
# malformed line is REFUSED, never guessed at.
#
# Always AFL / player_disposals / side=under / Sportsbet only. Stake arrives as
# DOLLARS in the message (not units) and is split EVENLY across the active Sportsbet
# accounts. The yaml `player_disposals` caps are LIABILITY ([124, 99, 74, 50] ->
# ~$155 of stake/account at 1.80), enforced unchanged at sizing.
#
# DISPOSALS_MODEL_ENABLED is the kill-switch: the channel is NOT monitored until it
# is true, so this ships DORMANT. Channel + sender id live in .env (never hardcoded)
# so a Telegram basic->supergroup promotion, which silently flips the chat id, is a
# one-line .env edit plus a restart rather than a code deploy.
DISPOSALS_MODEL_ENABLED = _env_bool("DISPOSALS_MODEL_ENABLED", False)
DISPOSALS_MODEL_TG_CHANNEL = os.getenv("DISPOSALS_MODEL_TG_CHANNEL", "").strip()
DISPOSALS_MODEL_SENDER_ID = os.getenv("DISPOSALS_MODEL_SENDER_ID", "").strip()
# Hard per-message stake ceiling — the accidental-extra-zero guard, enforced at
# PLACEMENT (see main._apply_disposals_model_stake), not just here. The $600 lesson:
# an .env edit alone must never be able to change the live stake without a restart.
DISPOSALS_MODEL_MAX_STAKE = _env_float("DISPOSALS_MODEL_MAX_STAKE", "600")
# Cumulative ceiling for ONE selection (evt+pid+line+side) across every key the feed
# sends for it. The model's `target` governs; this is the independent backstop so a
# runaway emitter cannot walk a selection up indefinitely.
DISPOSALS_MODEL_MAX_SELECTION_STAKE = _env_float(
    "DISPOSALS_MODEL_MAX_SELECTION_STAKE", "1000")
# Test mode: ignore the posted stake and place this flat amount instead. Clamped at
# placement, same as Dello/MLB/self-bet.
DISPOSALS_MODEL_TEST_MODE = _env_bool("DISPOSALS_MODEL_TEST_MODE", True)
DISPOSALS_MODEL_TEST_STAKE = _env_float("DISPOSALS_MODEL_TEST_STAKE", "7")
# Price gates (Wilson 2026-08-01). The model itself refuses to tip below 1.70, so
# tipbot refuses to PLACE below 1.70 — never a bet the model would not have tipped.
# Break-even is ~1.57 at the measured 63.8% hit rate, so 1.70 keeps real margin.
# WORSE_GATE mirrors DELLO_SB_WORSE_GATE: live SB more than this fraction below the
# posted price -> manual. The existing 1.25x MAX_ODDS_MULT ceiling still applies.
DISPOSALS_MODEL_MIN_ODDS = _env_float("DISPOSALS_MODEL_MIN_ODDS", "1.70")
DISPOSALS_MODEL_WORSE_GATE = _env_float("DISPOSALS_MODEL_WORSE_GATE", "0.10")
# Refuse a message whose `start` (UTC bounce) is already past, plus this much slack.
# Critical: resolve_afl_event deliberately accepts a game up to SIX HOURS into play
# (resolver.py:263-265), so without this gate a stale message places a real in-play
# bet. Slack absorbs small clock skew only.
# CLAMPED to a skew-sized band: this value is ADDITIVE ON THE PERMISSIVE SIDE, so an
# innocent-looking DISPOSALS_MODEL_START_SLACK_SEC=3600 ("an hour of grace") would let
# the feed place an HOUR past bounce, and resolve_afl_event still returns the live
# fixture out to bounce+6h, so the bet lands IN-PLAY. Nothing else guards that. A
# NEGATIVE value is safe (it refuses earlier), so only the positive side is tightened.
DISPOSALS_MODEL_START_SLACK_SEC = _env_float("DISPOSALS_MODEL_START_SLACK_SEC", "0")
if DISPOSALS_MODEL_START_SLACK_SEC > 300.0:
    import logging as _logging
    _logging.getLogger("tipbot.config").warning(
        "DISPOSALS_MODEL_START_SLACK_SEC=%s is too permissive (it would allow a bet "
        "that far PAST bounce, and the fixture resolver accepts a game up to 6h "
        "in-play) — clamping to 300s. Clock skew needs seconds, not minutes.",
        DISPOSALS_MODEL_START_SLACK_SEC,
    )
    DISPOSALS_MODEL_START_SLACK_SEC = 300.0
# Upstream invariant: at most this many DISTINCT players per fixture (correlation
# bound, not a turnover one — every bet is an under, so N unders in one match behave
# like one bet at N x the size).
#
# Enforced model-side TOO; this is tipbot's independent backstop.
#
# RETRACTED 2026-08-03: an unattended run "corrected" this comment to say the model has
# no per-fixture cap and tipbot's is "the ONLY enforcement". That was WRONG, and the
# mistake is worth recording because it is an easy one to repeat: the emitter's
# candidate filter (`tips = found[(found["gap"] >= trigger) & bettable]`,
# send_tips_telegram.py:694) genuinely is unbounded, but it is only a CANDIDATE list.
# The emitter then iterates it and calls `ledger.plan()` per row
# (send_tips_telegram.py:743-745), and plan() enforces `MAX_PLAYERS_PER_EVENT = 4`
# (ledger.py:251) — returning None so no tranche and no message is produced. Reading the
# filter and stopping one function short of the gate is what produced the wrong claim.
#
# So this really is a second line of defence. Keep it anyway: trusting an external system
# for a money bound is how you find out it regressed, and both caps gate NEW players only
# (the model's comment says so explicitly), so they agree in behaviour as well as number.
# Note it counts DISTINCT PLAYERS, so backing one player at several lines is deliberately
# free of this cap (see the per-line note on MAX_SELECTION_STAKE above).
DISPOSALS_MODEL_MAX_PLAYERS_PER_EVENT = _env_int(
    "DISPOSALS_MODEL_MAX_PLAYERS_PER_EVENT", "4")
# kind=retry re-offers a tranche. Default ON, but ONLY safe because the exposure
# ledger caps cumulative placed per selection at the message's `target`: Sportsbet
# does NOT exhaust capacity on the first pass (a $71 share against a ~$155/account
# ceiling leaves 2.2x headroom), so an uncapped retry would DOUBLE-stake, not top up.
DISPOSALS_MODEL_RETRY_ENABLED = _env_bool("DISPOSALS_MODEL_RETRY_ENABLED", True)
# Bookie sessions are scheduled OFF 23:00-07:00 to conserve data, but this feed posts
# hourly THROUGH the night. Inside the window a tip cannot place, so suppress the
# per-tip manual ping (~8 a night) and log a breadcrumb instead; the feed re-offers
# after 07:00 and no AFL game starts before then, so nothing is lost. Local hours.
DISPOSALS_MODEL_QUIET_START_HOUR = _env_int("DISPOSALS_MODEL_QUIET_START_HOUR", "23")
DISPOSALS_MODEL_QUIET_END_HOUR = _env_int("DISPOSALS_MODEL_QUIET_END_HOUR", "7")
# v6.08b BOOK RECONCILIATION. The exposure ledger below only knows THIS tipster's own
# placements, so it is structurally blind to the same selection being backed by another
# tipster's fan-out or by hand. A live sweep on 2026-08-02 found $4,049.60 of unsettled
# disposals props across 5 Sportsbet accounts (incl. $574.50 on a single selection),
# none of it visible to the ledger. /api/pending_bets observes what is ACTUALLY on.
#
# MODE decides what to DO with that number, and it is a real money-policy choice:
#   "block" - outside money counts against the selection's ceiling, so tipbot places
#             only target-minus-everything-already-on. Safest; also means tipbot places
#             NOTHING when another tipster already filled the position.
#   "alert" - outside money is recorded and alerted but does NOT reduce the stake.
#             Preserves the original handover's relaxed stance on cross-tipster
#             collisions while making them visible instead of invisible.
# A sweep FAILURE or an ambiguous player join is always treated as UNKNOWN, never as
# zero, and UNKNOWN refuses in "block" mode (a failed sweep reading as zero would
# re-arm the very double-stake this exists to prevent).
DISPOSALS_MODEL_RECONCILE_ENABLED = _env_bool("DISPOSALS_MODEL_RECONCILE_ENABLED", True)
# WILSON'S CALL 2026-08-02: "alert". `block` would have suppressed 6 of 8 disposals
# unders on that day's board, because the players this model likes are the players
# Eddie and Saiyan like -- two systems reading the same market, not a coincidence. If
# the disposals edge is real and independent, blocking gives most of it away. So outside
# money is RECORDED and ALERTED but does NOT reduce the stake, which is handover
# section 9's original stance ("let it try, the bookie's limit is the throttle") with the
# collision made visible instead of invisible. The accepted tradeoff is a genuinely
# larger correlated position on those players, bounded by Sportsbet's own limit.
DISPOSALS_MODEL_RECONCILE_MODE = os.getenv(
    "DISPOSALS_MODEL_RECONCILE_MODE", "alert").strip().lower()
if DISPOSALS_MODEL_RECONCILE_MODE not in ("block", "alert"):
    import logging as _logging
    _logging.getLogger("tipbot.config").warning(
        "DISPOSALS_MODEL_RECONCILE_MODE=%r is not 'block' or 'alert' — defaulting to "
        "'block' (fail safe).", DISPOSALS_MODEL_RECONCILE_MODE)
    DISPOSALS_MODEL_RECONCILE_MODE = "block"

# Persisted state: per-selection exposure + permanent key dedup, and the fills file
# the model reads back to learn what actually filled (contract §4).
DISPOSALS_MODEL_STATE_PATH = os.getenv(
    "DISPOSALS_MODEL_STATE_PATH", "logs/disposals_model_state.json").strip()
DISPOSALS_MODEL_FILLS_PATH = os.getenv(
    "DISPOSALS_MODEL_FILLS_PATH", "logs/disposals_model_fills.jsonl").strip()

# ── v6.08e NOOP LIVENESS DETECTOR ───────────────────────────────────────────────
# The model posts `NOOP|v2|ts=<iso>|seq=<n>` on a tick that produced no bets, so an
# hourly feed's silence is meant to be unambiguous. tipbot has had three multi-hour
# "alive but silent" outages, so a positive heartbeat is worth having.
#
# GATED ON DISPOSALS_MODEL_ENABLED, unlike the reconciliation loop which is
# deliberately gated the other way. Reconciliation reads the BOOK, which has live money
# on it whether or not this tipster is switched on. Liveness reads the CHANNEL, and the
# channel is only registered when DISPOSALS_MODEL_ENABLED is true (config ~1258). A
# detector left running while the feed is off would observe permanent silence and page
# forever about a feed nobody turned on.
DISPOSALS_MODEL_LIVENESS_ENABLED = _env_bool("DISPOSALS_MODEL_LIVENESS_ENABLED", True)
DISPOSALS_MODEL_LIVENESS_PATH = os.getenv(
    "DISPOSALS_MODEL_LIVENESS_PATH", "logs/disposals_model_liveness.json").strip()
# How long the feed may be silent, INSIDE its active window and AFTER it has already
# spoken in that window, before this alerts.
#
# 24h LOOKS ABSURD FOR AN HOURLY FEED AND IS DELIBERATE. The model's NOOP docstring says
# it fires "on every tick that produces no bets", but that is not what the code does:
# only 3 of its 7 no-output paths reach the heartbeat. tip_schedule.py takes three SILENT
# early exits before send_tips_telegram.py is even launched (outside the match block, no
# upcoming match, next bounce > LOOKAHEAD_HOURS=24h), send_tips_telegram.py has a fourth
# at `if found.empty` that returns without heartbeating, and ANY tick that sends a WATCH
# block skips the heartbeat too. So silence inside the window is NOT yet evidence.
#
# Measured, not assumed: their tip_schedule.log shows Sunday 2026-08-02 17:00, 18:00,
# 19:00 and 20:00 all inside the block, all silent ("next bounce ... 98.5h away"), after
# the feed had been talking all afternoon. A 3h threshold would have paged that evening.
#
# 36, NOT 24, AND THE 24 WAS WRONG. The earlier note here put the worst legitimate silence
# at "about 22h" by measuring a game-free Sunday as a calendar day. Silence does not start
# at Sunday midnight; it starts at the feed's LAST MESSAGE, which is roughly the last
# game's bounce. Computed properly over the model's own cached 2026 fixture (218 matches,
# aflapi.afl.com.au/afl/v2/matches), taking the feed to speak only inside the 12h before a
# bounce, the worst legitimate in-window silence is 30.5h: the GRAND FINAL, Sat 2026-09-26
# 14:30, the round's ONLY game, with no Sunday game after it. The next largest all season
# is 12.7h. So 24h false-pages on Grand Final Sunday, and on any semi/preliminary weekend
# whose last game is Saturday, with an alert whose own text asserts that the quiet period
# is NOT expected. That is precisely the cry-wolf failure this detector exists to avoid.
#
# 36h clears the measured 30.5h with margin. Be honest about what it does not buy: no
# threshold below the 86h window length is PROVABLY safe while the model stays silent on
# its four no-output paths, and combined with the burst rule a feed that dies in the back
# half of a window is not caught until the next window. This half of the detector catches
# a feed that has died OUTRIGHT; the SEQ-GAP check is the half that is sound today.
# DROP TO 3 (three missed ticks) as soon as the model emits its NOOP on all four silent
# paths -- that is a one-line .env change and it is raised with them.
DISPOSALS_MODEL_SILENCE_HOURS = _env_float("DISPOSALS_MODEL_SILENCE_HOURS", "36")
DISPOSALS_MODEL_LIVENESS_POLL_SEC = _env_int("DISPOSALS_MODEL_LIVENESS_POLL_SEC", "900")
# The feed's ACTIVE WINDOW, mirroring the model's own `in_match_block`
# (scripts/tip_schedule.py: BLOCK_START=(3,7), BLOCK_END=(6,21), Monday=0). Continuous
# from Thursday 07:00 to Sunday 21:00 INCLUDING the intervening nights — the model's
# scheduled task was verified 2026-08-03 to fire hourly 24/7 (Interval PT1H, Duration
# P1D), so the feed really does tick overnight. Outside the block the model exits before
# sending anything, so silence there is expected and must never alert.
DISPOSALS_MODEL_ACTIVE_START_DAY = _env_int("DISPOSALS_MODEL_ACTIVE_START_DAY", "3")
DISPOSALS_MODEL_ACTIVE_START_HOUR = _env_int("DISPOSALS_MODEL_ACTIVE_START_HOUR", "7")
DISPOSALS_MODEL_ACTIVE_END_DAY = _env_int("DISPOSALS_MODEL_ACTIVE_END_DAY", "6")
DISPOSALS_MODEL_ACTIVE_END_HOUR = _env_int("DISPOSALS_MODEL_ACTIVE_END_HOUR", "21")
# Alert when `seq` jumps by more than 1 between two heartbeats. Sound with no false
# positives: next_seq() is called ONLY on the no-bets path
# (scripts/send_tips_telegram.py:580), so a tick that produced BETS consumes no seq and
# consecutive NOOPs are always +1. A jump therefore means heartbeats were genuinely lost.
DISPOSALS_MODEL_SEQ_GAP_ALERT = _env_bool("DISPOSALS_MODEL_SEQ_GAP_ALERT", True)
# How far seq may step BACKWARDS and still be read as late/out-of-order delivery rather
# than as the model's seq file having been lost and restarted at 1. Both really happen:
# SEQ_FILE is relative to CWD and gitignored, so moving the repo resets it, while
# Telegram can deliver out of order. The two need opposite handling — a reset should
# re-baseline and drop the now-meaningless pending gap, whereas a late message must do
# NEITHER, or one reordered heartbeat both raises a false alert and silently discards a
# real gap report. A reset lands near 1, so a small step is the safe discriminator.
DISPOSALS_MODEL_SEQ_RESET_TOLERANCE = _env_int("DISPOSALS_MODEL_SEQ_RESET_TOLERANCE", "5")
# Where liveness alerts land. Default MAINTENANCE (notify_info), not the critical chat.
# The critical chat carries money pages — the startup-reconcile "MAY have LANDED, VERIFY"
# and the racing deferred-verify among them — and v6.07 finding #30 was a tipster burying
# those under ~1200 criticals a day. A feed heartbeat is diagnostics about a tipster that
# currently places nothing, so it belongs with the other ops traffic. Flip to true if the
# feed is ever load-bearing enough that a silent one should page.
DISPOSALS_MODEL_LIVENESS_CRITICAL = _env_bool("DISPOSALS_MODEL_LIVENESS_CRITICAL", False)

# Leroy (Late Mail Leroy) — TEXT racing tipster (saddle#+units, Thoroughbreds AU,
# same-day), placed via Betfair BSP (back) on LEROY_BETFAIR_SESSION. $LEROY_UNIT_SIZE
# per unit, capped at LEROY_MAX_UNITS PER BET (win + place count separately), full
# stake — NO splitting across accounts. LEROY_ENABLED is the kill-switch. v5.93.
LEROY_UNIT_SIZE = _env_float("LEROY_UNIT_SIZE", "400")
LEROY_MAX_UNITS = _env_float("LEROY_MAX_UNITS", "4")
LEROY_BETFAIR_SESSION = os.getenv("LEROY_BETFAIR_SESSION", "99997")
LEROY_ENABLED = _env_bool("LEROY_ENABLED", True)

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

# v5.91 (2026-06-28): the N tips in a SINGLE Saiyan/Eddie image place CONCURRENTLY
# (each tip already fans its OWN accounts out concurrently; this parallelises the
# slow per-tip resolve+place ACROSS the tips). 06-27 Crisp/De Goey/Trezise were 3
# tips processed SEQUENTIALLY -> 76s for the 3rd. Build + dedupe + audit stay
# sequential (race-free); only place_tip is parallelised. Bounded so tips x
# accounts threads don't explode. IMAGE_TIP_CONCURRENT=false reverts to sequential.
# SOFT LAUNCH (v5.91 10-opus review): default 2 (not 4) — keeps the per-proxy POST
# burst low while the shared proxy is still degraded (ops) and proves the hot path
# on a live multi-tip slate; raise once verified + the proxy is fixed.
IMAGE_TIP_CONCURRENT = _env_bool("IMAGE_TIP_CONCURRENT", True)
IMAGE_TIP_MAX_CONCURRENCY = int(_env_float("IMAGE_TIP_MAX_CONCURRENCY", "2"))

# Racing image tips (Zak / Trial) have their OWN test gate, SEPARATE from the
# global IMAGE_TIPS_TEST_MODE (which still governs Eddie AFL at $1/u). Default
# False = Zak/Trial place at their FULL unit size (ZAK/TRIAL_UNIT_SIZE) with the
# shared racing guards (10% odds floor, runner-match, 1.5x wrong-horse ceiling,
# route-remainder/whole-to-manual, 3u cap). Set IMAGE_RACING_TEST_MODE=true to
# drop Zak/Trial back to the $1/u test stake WITHOUT affecting Eddie. The racing
# image path is Zak/Trial-only (Eddie AFL uses a different route). v5.15
# (Wilson 2026-06-05: "flip Zak/Trial to full unit sizing at 400 now";
#  Wilson 2026-07-17: bumped Zak/Trial to $500/u).
IMAGE_RACING_TEST_MODE = _env_bool("IMAGE_RACING_TEST_MODE", False)

# Production per-channel unit sizes (Eddie used ONLY when IMAGE_TIPS_TEST_MODE is
# false; Zak/Trial used when IMAGE_RACING_TEST_MODE is false — now the default).
# Wilson 2026-07-18: Eddie reduced $400 -> $250/u.
EDDIE_UNIT_SIZE = _env_float("EDDIE_UNIT_SIZE", "250")
ZAK_UNIT_SIZE = _env_float("ZAK_UNIT_SIZE", "500")
TRIAL_SNIPER_UNIT_SIZE = _env_float("TRIAL_SNIPER_UNIT_SIZE", "500")

# Hard max units per racing-image play (Zak / Trial). DEDICATED cap, independent
# of the global MAX_UNITS — so raising MAX_UNITS for other tipsters never
# un-caps these. Their unit size will be $400 in production, so 3u = $1,200
# intended (then liability-capped to the $1000 win / $500 place thoroughbred
# cap per account); $1/u in test = $3. 2026-06-03.
IMAGE_RACING_MAX_UNITS = _env_float("IMAGE_RACING_MAX_UNITS", "3.0")

# FIX C (2026-07-05): Zak/Trial post several races in ONE image/text post; the
# router placed them SEQUENTIALLY (~48s for 5 races on 2026-07-05, and a ~228s
# worst-case tail). Place the batch CONCURRENTLY like Tip Titans (_process_batch)
# and the AFL image path (_route_image_afl_tips). The 5-10s SAME-ACCOUNT pacing is
# preserved downstream in racing_placer._pace_before_place (keyed per session_id),
# so tips on the SAME account still stagger; only tips on DIFFERENT accounts
# overlap. Bounded so tips x accounts threads don't explode; default 3 is a soft
# launch (< TT's 4) while the shared proxy is degraded. IMAGE_RACING_CONCURRENT=false
# reverts to the sequential loop (kill-switch).
IMAGE_RACING_CONCURRENT = _env_bool("IMAGE_RACING_CONCURRENT", True)
IMAGE_RACING_MAX_CONCURRENCY = int(_env_float("IMAGE_RACING_MAX_CONCURRENCY", "3"))

# FIX B (v5.95): deterministic REGEX-FIRST fast path. Structured-format tipsters
# whose battle-tested regex parser runs BEFORE the LLM; the LLM (Claude primary /
# Groq) is SKIPPED only when main._regex_first_trusted passes (all fields present +
# resolved club + known stat + sane line/odds + no dropped line + single-leg +
# NOT live/SGM + no explicit unit token). ANY doubt falls through to the LLM, so a
# regex (which extracts literal substrings or fails — it cannot hallucinate) never
# reintroduces the llama wrong-parse class. Ships saiyan_afl ONLY (its
# parse_saiyan_message is already the trusted Groq-fail fallback). Comma-separated
# env override; empty string disables the fast path entirely.
REGEX_FIRST_TIPSTERS = {
    t.strip().lower()
    for t in os.getenv("REGEX_FIRST_TIPSTERS", "saiyan_afl").split(",")
    if t.strip()
}

# ── Telethon connection-liveness watchdog (incident 2026-07-06) ─────
# main.py stayed alive but its telethon socket went HALF-OPEN ~01:15-10:53 (~9.5h):
# zero updates received, telethon's own ping/reconnect did NOT detect the dead
# socket, so every morning tip was missed until it finally reconnected at 10:53.
# This watchdog ACTIVELY probes the connection (get_me() round-trip under a hard
# timeout) every INTERVAL sec and, on FAIL_THRESHOLD consecutive genuinely-unhealthy
# probes, forces a full listener restart (fresh client, resumes LIVE — the dead-window
# tips are DROPPED not replayed, which is money-safe: no stale bet on an already-run
# race). Turns a ~9.5h deaf window into ~2-4 min. TELETHON_WATCHDOG_ENABLED is the
# kill-switch. A passive "update received within INTERVAL" stamp only VETOES a false
# positive (never triggers a reconnect) so a blocked event loop can't force a needless
# restart. Defaults: 120s probe, 15s timeout, 2 consecutive fails.
TELETHON_WATCHDOG_ENABLED = _env_bool("TELETHON_WATCHDOG_ENABLED", True)
TELETHON_WATCHDOG_INTERVAL_SEC = _env_int("TELETHON_WATCHDOG_INTERVAL_SEC", "120")
TELETHON_WATCHDOG_PROBE_TIMEOUT_SEC = _env_int("TELETHON_WATCHDOG_PROBE_TIMEOUT_SEC", "15")
TELETHON_WATCHDOG_FAIL_THRESHOLD = _env_int("TELETHON_WATCHDOG_FAIL_THRESHOLD", "2")

# ── Event-loop FREEZE watchdog (incident 2026-07-17) ───────────────────
# A DIFFERENT failure mode from the half-open socket above. On 2026-07-17 the
# whole asyncio event loop FROZE ~02:52-09:32 (~6.5h): a synchronous call (a
# place_tip / HyperBot HTTP round-trip that hung on the crash-looping HB backend)
# blocked the single loop thread, so EVERY in-loop watchdog froze WITH it — the
# telethon prober couldn't run its get_me() probe AND the session watchdog stopped
# stamping its heartbeat. An in-loop watchdog structurally CANNOT recover an in-loop
# freeze. This watchdog runs on a SEPARATE OS THREAD (outside asyncio): a tiny
# asyncio ticker stamps an in-memory timestamp every TICK_SEC while the loop spins;
# if the thread sees that stamp go older than MAX_SILENCE_SEC the loop is wedged ->
# it fires a SYNCHRONOUS Telegram alert (the async notifier is on the frozen loop
# and unusable) and os._exit(1)s, and tipbot.bat's :start loop restarts the process
# fresh. A blocking network call releases the GIL, so the thread keeps running while
# the loop is blocked. Turns a ~6.5h dead freeze into ~15 min.
# MAX_SILENCE = 900s (15 min) is a deliberate BACKSTOP, not a precise trip: place_tip
# runs SYNCHRONOUSLY on the loop (sports + image-AFL paths are NOT offloaded like
# racing/Leroy are), so a single hung bookie session can legitimately block the loop
# for ~one HB cid-timeout (~305s) — and this watchdog MUST still fire during a genuine
# freeze that happens DURING a placement (the 2026-07-17 incident was exactly that), so
# it cannot exempt placements. 900s cleanly separates a bounded legit placement block
# (~305-500s, blocklist-capped) from an INDEFINITE wedge, so it never trips mid-bet.
# The real cure (offload sports placement via run_in_executor) is a deferred follow-up;
# until then keep MAX_SILENCE >= ~600s. LOOP_FREEZE_WATCHDOG_ENABLED is the kill-switch.
LOOP_FREEZE_WATCHDOG_ENABLED = _env_bool("LOOP_FREEZE_WATCHDOG_ENABLED", True)
LOOP_FREEZE_TICK_SEC = _env_int("LOOP_FREEZE_TICK_SEC", "15")
LOOP_FREEZE_CHECK_SEC = _env_int("LOOP_FREEZE_CHECK_SEC", "30")
LOOP_FREEZE_MAX_SILENCE_SEC = _env_int("LOOP_FREEZE_MAX_SILENCE_SEC", "900")
# v6.07 (incident 2026-07-29, 14.5h silent outage): WORK-liveness threshold. The
# loop-freeze watchdog above only detects a WEDGED loop; on 07-29 the loop was
# spinning normally while all WORK had stopped (poller dead, no telethon updates, no
# tips ingested or placed 01:43 -> 16:19), so it correctly never fired. This second
# check restarts the process when the Tip Titans poller stops making progress.
# v6.07 AUDIT CORRECTION (2026-07-31): the original justification here was WRONG.
# It compared the threshold to the ~10s POLL INTERVAL and claimed ~180x headroom, but
# _process_batch is awaited INLINE in the poll loop, so during a batch the loop never
# reaches the top to re-stamp and the real quantity is the BATCH duration. Measured
# over 266 real batches in logs/tipbot.log: worst whole-batch span 1283s — only 1.4x
# under the old 1800s trip — and a single racing tip laddering across bookies can
# reach ~420s (V3_POLL_HARD_CEILING_SEC) x 4 attempts + the ~33s reconcile-before-
# spill waits, i.e. ~1780s ON ITS OWN, so 1800s had effectively NO margin. A false
# fire is expensive: os._exit(1) lands mid-batch and those tips are already marked
# seen and hold inflight claims, so they are (correctly) never retried — the batch
# silently becomes a set of "may have landed, VERIFY" non-bets.
# Two independent fixes: tiptitans_processor._note_progress() now also stamps on
# every COMPLETED tip (worst measured gap between completions: 585s), and this
# threshold doubles to 3600s. Recovery target moves from ~30min to <=60min, still a
# ~14x improvement on the 14.5h blackout, and the 900s loop-freeze check above
# independently covers a wedged loop. Set 0 to disable (loop-freeze unaffected).
WORK_STALL_MAX_SILENCE_SEC = _env_int("WORK_STALL_MAX_SILENCE_SEC", "3600")

# ── HyperBot placement poll HARD CEILING (incident 2026-07-17, v6.03) ──
# ROOT CAUSE of the 6.5h freeze: the v3 placement poll loop (_post_v3_async)
# polls /api/bet_status until the cid resolves OR a server-supplied budget
# (timeout_at - submitted_at) elapses -- and that budget had NO upper bound, so a
# runaway/huge server value made the loop poll for HOURS. Per-request timeouts
# did NOT help because it was the LOOP, not any single request, that hung. This
# is a HARD wall-clock ceiling on the WHOLE poll loop. A PLACEMENT that doesn't
# resolve within it is treated AMBIGUOUS (may have landed -> debit + blocklist +
# STOP the ladder, NEVER retry/spill), byte-identical to the existing cid-timeout
# path. min(budget+grace, ceiling) NEVER shortens a legit budget. 420s clears the
# ~300s server cid timeout with margin; do NOT set below ~330s (would clip a real
# slow settle). Price-check paths are idempotent so a ceiling hit there is a plain
# transient failure (no placement, no ambiguous, no debit).
V3_POLL_HARD_CEILING_SEC = _env_float("V3_POLL_HARD_CEILING_SEC", "420")

# ── Placement OFFLOAD off the event loop (incident 2026-07-17, v6.03) ──
# place_tip runs SYNCHRONOUSLY; called directly on the asyncio loop it BLOCKS the
# whole loop for the placement's duration, so a hung HB call freezes Telegram
# receive + the freeze-watchdog ticker + every other tip (the root shape of the
# 6.5h incident). When enabled the message handlers run place_tip on a DEDICATED
# bounded thread pool via run_in_executor (mirroring the racing/Leroy paths, which
# already do this), so a slow/hung placement no longer blocks the loop. HARD
# PREREQUISITE: the v3 poll HARD CEILING (V3_POLL_HARD_CEILING_SEC) MUST stay
# bounded — without it a hung placement would silently pin a worker with NO
# freeze-watchdog recovery (the watchdog only sees a stalled LOOP, not a stalled
# worker). Dedup stays on the loop thread (claim-before-place) so the added await
# can't double-place. Kill-switch: set false to revert to synchronous on-loop
# placement (the freeze watchdog still covers that path, bounded by the ceiling).
PLACEMENT_OFFLOAD_ENABLED = _env_bool("PLACEMENT_OFFLOAD_ENABLED", True)
PLACEMENT_EXECUTOR_WORKERS = _env_int("PLACEMENT_EXECUTOR_WORKERS", "8")

# ── Startup pending-bets reconciliation (incident 2026-07-17, v6.03) ───
# On process restart (freeze-watchdog os._exit, crash, or reconnect), a bet that
# was IN FLIGHT when the process died is never reconciled -> a real staked bet is
# invisible to the ledger + Wilson. When enabled, a startup sweep queries
# /api/pending_bets for OWNED sessions and ALERTS (never re-places — an in-flight
# bet MAY have landed; re-placing = double stake) about pending bets within the
# lookback window that aren't in the ledger. ALERT-ONLY by construction. Ships
# GATED OFF (NEEDS_DESIGN_CHOICE: lookback/channel/false-orphan tolerance) -> the
# ledger<->pending_bets join is fuzzy (place-response UUID vs pending int id), so
# it can surface manual/pre-ledger bets as 'verify' noise; enable after tuning.
STARTUP_PENDING_RECONCILE_ENABLED = _env_bool("STARTUP_PENDING_RECONCILE_ENABLED", False)
STARTUP_PENDING_RECONCILE_LOOKBACK_SEC = _env_int("STARTUP_PENDING_RECONCILE_LOOKBACK_SEC", "1800")

# ── Dead-at-startup session alert (v6.07, sweep #29) ───────────────────
# A session that is in a *_SESSION_PRIORITY list AND in sessions.yaml but is NOT active
# in HyperBot at startup is invisible to EVERY monitor: get_sessions() filters to
# active-only so it never appears, the session watchdog only ever sees TRANSITIONS out
# of a set seeded from what was already active, and the scheduled check_session_health
# defers entirely while main.py is up. So placement silently shops a shorter list (and
# the AFL fan-out splits the unit across fewer accounts -> more falls outside the
# remaining liability brackets -> unfilled -> manual) for days, with the root cause
# never named. This seeds such sessions into the watchdog's pending-drop set after a
# grace window so the EXISTING 15-min Critical / recovery machinery owns them.
# ALERT-ONLY: it never starts, places or re-places anything.
STARTUP_DEAD_SESSION_ALERT = _env_bool("STARTUP_DEAD_SESSION_ALERT", True)
STARTUP_DEAD_SESSION_GRACE_SEC = _env_int("STARTUP_DEAD_SESSION_GRACE_SEC", "180")
STARTUP_DEAD_SESSION_MAX = _env_int("STARTUP_DEAD_SESSION_MAX", "10")

# ── SAFE-MODE placement governor (Wilson 2026-07-17) ───────────────────
# Hard money-safety bounds on REAL bet PLACEMENTS ONLY. Price checks and every
# other HyperBot endpoint are UNRESTRICTED — these apply solely to the client's
# place_* methods (place_single_sports_bet / place_sgm_bet / place_racing_bet /
# place_betfair_bsp), the single choke point every real bet passes through, so NO
# placement path can bypass them. Set alongside removing the per-bet confirmation
# prompt: instead of confirming each bet, cap the blast radius.
#   GLOBAL_MAX_STAKE:  every placement's stake/size is CLAMPED to <= this. 0 = off.
#   GLOBAL_DAILY_PLACEMENT_CAP: at most this many placement ATTEMPTS per calendar
#     day (AEST/local). The (N+1)th place_* call is REFUSED (returns a clean
#     failure, no POST) + logged until the cap is raised ("explicit permission").
#     0 = off. Counts ATTEMPTS (atomic, so a fan-out's per-account calls AND any
#     failed attempts each count) -> a HARD ceiling that can never be exceeded.
#     Persisted across restarts (survives the freeze-watchdog os._exit).
# BOTH DEFAULT 0 (off) in committed code so the sportsbot fork is unaffected; the
# live values ($1 / 10) live in tipbot's local .env.
GLOBAL_MAX_STAKE = _env_float("GLOBAL_MAX_STAKE", "0")
GLOBAL_DAILY_PLACEMENT_CAP = _env_int("GLOBAL_DAILY_PLACEMENT_CAP", "0")

# ── Racing TRACK-NAME resolver (B-full, 2026-07-06) ───────────────────
# When a racing tip HAS a track but the price shop returns NOTHING (a catalog
# NAME mismatch — most often SYNTHETIC surfaces: 'Pakenham Synthetic' vs HB's
# 'Pakenham'/'Pakenham (Synthetic)'), racing_placer.place_racing_tip probes
# deterministic name variants (STAGE 1) then an opt-in runner-name-anchored
# web-search (STAGE 2), each DATE-AWARE (dated tip: its date; undated: the same
# next-(R)-meeting lookahead), and re-prices. Any resolution forces a NAME match
# (saddle-only disabled) so a wrong track/race -> manual, never a wrong-horse bet.
# Fires ONLY on the no-price failure path — never changes a tip that already
# prices. RACING_TRACK_RESOLVE=false is the KILL-SWITCH. STAGE 2 web-search is
# OPT-IN (default OFF) + additionally gated by CLAUDE_WEBSEARCH_RESOLVE.
RACING_TRACK_RESOLVE = os.getenv("RACING_TRACK_RESOLVE", "true").strip().lower() in ("1", "true", "yes", "on")
RACING_TRACK_RESOLVE_WEBSEARCH = os.getenv("RACING_TRACK_RESOLVE_WEBSEARCH", "false").strip().lower() in ("1", "true", "yes", "on")

# v6.06 (Wilson 2026-07-25): SA-TRACK DATA-GROUNDED PROBE. Zak is SA-thoroughbred-only
# and often posts a runner+race with NO track. The upstream web-search resolver can
# time out (2026-07-25: all 4 tips timed out) and its blind no-data fallback then
# GUESSES a single SA track from model priors (biased to 'Gawler'), so the whole card
# dropped to manual (Morphettville was the real meeting). This adds a data-grounded
# STAGE inside the B-full track resolver: when a Zak tip prices nothing, probe today's
# candidate SA thoroughbred meetings against the LIVE HB catalog (read-only, NAME-match
# forced) and use the ONE that actually carries the runner. A track with no meeting
# prices nothing, so it can never be picked. Metropolitan-first order = usually 1 probe.
# KILL-SWITCH RACING_SA_TRACK_PROBE. The SA candidate list is shared with the upstream
# resolver (main.py) so both stay in sync.
RACING_SA_TRACK_PROBE = os.getenv("RACING_SA_TRACK_PROBE", "true").strip().lower() in ("1", "true", "yes", "on")
SA_THOROUGHBRED_TRACKS = [
    "Morphettville", "Morphettville Parks", "Gawler", "Murray Bridge",
    "Strathalbyn", "Port Augusta", "Mount Gambier", "Balaklava", "Oakbank",
    "Naracoorte", "Bordertown", "Port Lincoln", "Penola",
]

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
    # Late Mail Leroy — TEXT racing tips (saddle#+units) placed via Betfair BSP
    # (back) on LEROY_BETFAIR_SESSION. A CHANNEL (no bot sender) -> no bot_id, so
    # every post is treated as a tip + noise-filtered by parsers/leroy.py. NOT
    # image_tips (text), NOT in any *_SESSION_PRIORITY — Leroy bypasses the
    # fixed-odds racing pipeline entirely (see main._process_leroy_betfair_tip). v5.93.
    -1003535060447: {
        "name": "Late Mail Leroy",
        "parser": "leroy",
        "default_units": 1.0,
        "unit_size": LEROY_UNIT_SIZE,
        "sport": "racing",
        "betfair_bsp": True,
    },
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
    "ADE": "Adelaide", "ADEL": "Adelaide", "CROWS": "Adelaide",
    "BRI": "Brisbane Lions", "BR": "Brisbane Lions", "BL": "Brisbane Lions", "BRIS": "Brisbane Lions", "LIONS": "Brisbane Lions",
    "CAR": "Carlton", "CARL": "Carlton", "BLUES": "Carlton",
    "COL": "Collingwood", "COLL": "Collingwood", "PIES": "Collingwood", "MAGPIES": "Collingwood",
    "ESS": "Essendon", "ESSE": "Essendon", "BOMBERS": "Essendon",
    "FRE": "Fremantle", "FREM": "Fremantle", "FREO": "Fremantle", "DOCKERS": "Fremantle",
    "GEE": "Geelong", "GEEL": "Geelong", "CATS": "Geelong",
    "GC": "Gold Coast", "GCS": "Gold Coast", "GCFC": "Gold Coast", "SUNS": "Gold Coast",
    # "GWS GIANTS" (and the full nickname form) are the compound strings the vision
    # parser emits from logo tips; without these the whole-string AFL_TEAMS lookup in
    # resolve_afl_event misses (bare "GWS"/"GIANTS" map, the 2-word compound did not),
    # routing eddie_afl's GWS handicap to manual on 2026-06-25.
    "GWS": "Greater Western Sydney", "GIANTS": "Greater Western Sydney",
    "GWS GIANTS": "Greater Western Sydney", "GREATER WESTERN SYDNEY GIANTS": "Greater Western Sydney",
    "HAW": "Hawthorn", "HAWI": "Hawthorn", "HAWTH": "Hawthorn", "HAWKS": "Hawthorn",
    "MEL": "Melbourne", "MELB": "Melbourne", "DEES": "Melbourne", "DEMONS": "Melbourne",
    # NWM is intentionally NOT mapped here — it collides with the AFL player
    # nickname "NWM" = Nasiah Wanganeen-Milera (St Kilda). NM is the
    # canonical North Melbourne code; Saiyan only uses NM in his messages.
    "NM": "North Melbourne", "NMFC": "North Melbourne", "KANGAS": "North Melbourne", "KANGAROOS": "North Melbourne", "ROOS": "North Melbourne",
    "PA": "Port Adelaide", "PAFC": "Port Adelaide", "PORT": "Port Adelaide", "POWER": "Port Adelaide",
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
        # v6.08s (2026-08-14): AFLW COLLISION. Sportsbet carries an AFLW fixture under
        # the SAME plain team names, and "Fremantle v Adelaide" resolved to the AFLW
        # match: 6 markets, 98 disposals selections, ALL over, zero unders, no Rory
        # Laird. "Fremantle v Adelaide Crows" is the men's game: 109 markets, 831
        # selections, 16 unders, Laird present. So a DisposalsModel tip on Laird went to
        # manual with "line 22.5 not carried in catalog" while Sportsbet was carrying it
        # the whole time, on an event we never looked at.
        #
        # It failed safe only because no AFLW player is called Laird. A shared surname
        # would have priced, and possibly placed, against the WRONG COMPETITION.
        #
        # Scoped to Adelaide because that is what is MEASURED. Blanket nicknames are
        # actively harmful: "North Melbourne v Geelong" returns 109 markets as-is and
        # collapses to 9 as "North Melbourne Kangaroos v Geelong Cats". Add a club here
        # only after probing the live catalog both ways. `Port Adelaide` is a separate
        # key and is NOT affected, because _bookie_event now aliases each whole side.
        # REVERTED 2026-08-14 (v6.08v). Two aliases were tried here in one afternoon,
        # "Adelaide" -> "Adelaide Crows" and then "Fremantle" -> "Fremantle Dockers", and
        # NEITHER works, because the problem is not naming: HyperBot's matching between
        # the AFL and AFLW fixture of the same teams is NON-DETERMINISTIC. The identical
        # string returned the men's board at 12:12, the AFLW board at 12:37 and the men's
        # again at 12:58. An alias that works half the time is worse than none, because it
        # reads like a fix. Superseded by _price_check_afl_verified, which checks
        # start_time_iso against Squiggle's real bounce and retries name forms until the
        # fixture is genuinely the right one.

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
# v5.94 (Wilson 2026-07-05, from the 07-04 failure review): the redistribute is now a
# BOUNDED MULTI-PASS — a re-403'd top-up crumb re-shops onto a proven-healthy sibling
# instead of dropping (the old single pass lost $143 on 07-04), and it also fires for a
# single-account NON-CAP failure (403/508) on under/total/goalscorer markets, not just
# disposals-overs (that lost another $1,267 on 07-04). Cap the passes so it can't spin.
AFL_REDISTRIBUTE_MAX_PASSES = int(_env_float("AFL_REDISTRIBUTE_MAX_PASSES", "3"))

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
# ── STALE-TIP GUARD (v6.07) — DEFAULT OFF, Wilson 2026-07-31 ──────────
# Optional guard: route a MATCHED EasyMoneyAFL tip older than X_MAX_TWEET_AGE_MIN to a
# MANUAL alert instead of auto-placing it (~$600 on Sportsbet).
#
# WHY IT EXISTS: _poll_cycle forwards any unseen tweet regardless of age, so after a blind
# period (2026-07-30/31: 14h) the whole backlog looks "new" and the RECOVERY itself would
# auto-place tips on games already started or finished.
#
# WHY IT DEFAULTS OFF (Wilson's call, and he is right): the placement path already gates
# this. A finished or in-play AFL game does not carry the player-prop market on Sportsbet,
# so the tip fails to resolve/price and routes to MANUAL on its own; and the global 1.25x
# MAX_ODDS_MULT ceiling catches a line that has moved badly (a too-good price reads as a
# wrong selection). So the guard mostly duplicates existing protection while costing real
# manual work on legitimate tips that were simply posted well before the bounce.
#
# Flip X_TWEET_AGE_GUARD=true in .env if the watcher ever goes blind for hours again and
# you want the backlog to land as alerts rather than bets. The logic stays unit-tested
# either way; with the guard OFF an unusually old tip is still LOGGED (not alerted).
X_TWEET_AGE_GUARD = _env_bool("X_TWEET_AGE_GUARD", False)
X_MAX_TWEET_AGE_MIN = _env_int("X_MAX_TWEET_AGE_MIN", "60")
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

# Dello AFL — gated on DELLO_ENABLED (kill-switch, default OFF): the channel is
# NOT monitored until Wilson flips it, so this ships DORMANT. Ingested via
# telethon; parser "dello_afl" has NO regex parser (generic AFL LLM parse), and
# place_tip routes dello_afl through _place_dello_single (SINGLES only, band +
# rule-3 gates, $1 test). bot_id from DELLO_SENDER_ID if set (a group with other
# posters), else omitted (broadcast/own-forward channel = accept all senders).
if DELLO_ENABLED and DELLO_TG_CHANNEL.lstrip("-").isdigit():
    _dello_cfg = {
        "name": "Dello AFL",
        "parser": "dello_afl",
        "default_units": 1.0,
        "unit_size": DELLO_UNIT_SIZE,
        "sport": "afl",
    }
    _dello_sender = os.getenv("DELLO_SENDER_ID", "").strip()
    if _dello_sender.lstrip("-").isdigit():
        _dello_cfg["bot_id"] = int(_dello_sender)
    TIPSTER_CHANNELS[int(DELLO_TG_CHANNEL)] = _dello_cfg

# v5.9x (Wilson 2026-07-12): Wilson's own bet channel. Registered with a `self_bet`
# flag; main.py's handler dispatches that flag to _process_self_bet (6-field parse
# -> per-sport fan-out for AFL/NBA, manual for racing + MLB). Gated on SELF_BET_ENABLED.
if SELF_BET_ENABLED and SELF_BET_CHANNEL_ID:
    TIPSTER_CHANNELS[SELF_BET_CHANNEL_ID] = {
        "name": "Wilson Self-Bet",
        "parser": "self_bet",
        "self_bet": True,
        "sport": "",          # the sport is the message's FIRST field, not fixed
        "unit_size": 1.0,
        "default_units": 1.0,
    }

# DisposalsModel (v6.08) — the AFL disposals machine feed. Registered with a
# `machine_feed` flag; main.py's handler dispatches that flag to
# _process_disposals_model_tip (regex BET|v2 parse -> AFL fan-out, Sportsbet-locked,
# EXACT-line-or-manual) and RETURNS, so the message never reaches the LLM text path.
# Gated on DISPOSALS_MODEL_ENABLED, so it ships DORMANT.
#
# bot_id is REQUIRED here, unlike Dello: this is a shared group and the sender filter
# in main.py's handler is the ONLY thing stopping another member's message that
# happens to contain a `BET|` line from placing real money. Without a bot_id the
# filter is a no-op (`if expected_bot and ...`), so refuse to register rather than
# monitor the group unauthenticated.
if (DISPOSALS_MODEL_ENABLED
        and DISPOSALS_MODEL_TG_CHANNEL.lstrip("-").isdigit()
        and DISPOSALS_MODEL_SENDER_ID.lstrip("-").isdigit()):
    TIPSTER_CHANNELS[int(DISPOSALS_MODEL_TG_CHANNEL)] = {
        "name": "DisposalsModel",
        "parser": "disposals_model",
        "machine_feed": True,
        "bot_id": int(DISPOSALS_MODEL_SENDER_ID),
        "sport": "afl",
        # The message carries DOLLARS of stake, so unit_size is set per-tip from the
        # BET| line; these are inert placeholders kept for handler-shape parity.
        "unit_size": 1.0,
        "default_units": 1.0,
    }
elif DISPOSALS_MODEL_ENABLED:
    # Enabled but mis-configured. Loud at import rather than a silently deaf channel
    # (the 2026-05-16 Shook class: a chat-id flip made the listener silently blind).
    import logging as _logging
    _logging.getLogger("tipbot.config").error(
        "DISPOSALS_MODEL_ENABLED=true but DISPOSALS_MODEL_TG_CHANNEL=%r / "
        "DISPOSALS_MODEL_SENDER_ID=%r are not both numeric — channel NOT registered, "
        "the feed is NOT being monitored. Both are required (the sender id is the "
        "only guard against another group member's BET| line placing real money).",
        DISPOSALS_MODEL_TG_CHANNEL, DISPOSALS_MODEL_SENDER_ID,
    )

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
