"""
TipBot configuration.
Loads secrets from .env and defines constants for tipster channels,
team mappings, stat mappings, and staking rules.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── v6.59: new Sportsbet accounts (Wilson 2026-10-10) ────────────────
# "all my sportsbet accounts got capped ... i will be looking to retire and add new
# accounts soon ... by default set them up with daniel sportsbet limits". Fill a slot in
# .env with the account's HyperBot session id (and a name):
#     NEW_SB_ACCOUNT_1_SESSION_ID=123456
#     NEW_SB_ACCOUNT_1_NAME=Sam
# and restart. The account then (a) gets a copy of Daniel Sportsbet's (68723) liability
# limits from sessions.yaml unless sessions.yaml already has its own block, and (b) is
# appended to every priority list below that is set (the lists Daniel was in on
# 2026-10-10). Done here, before any module reads the lists. Empty slot = nothing.
NEW_SB_TEMPLATE_SESSION = os.getenv("NEW_SB_TEMPLATE_SESSION", "68723").strip() or "68723"
NEW_SB_ACCOUNT_SLOTS = 8
NEW_SB_JOIN_LISTS = (
    "NBA_SESSION_PRIORITY", "NBA_SGM_SESSION_PRIORITY", "AFL_SESSION_PRIORITY",
    "AFL_SGM_SESSION_PRIORITY", "MLB_SGM_SESSION_PRIORITY", "NFL_SESSION_PRIORITY",
    "NBL_SESSION_PRIORITY",
    "SESSION_PRIORITY",
)
# NBA_MODEL_SESSION_PRIORITY is NOT joined (v6.69, nba-v3 merge): the NBA props model's account
# list is explicit and owned by the model's switch-on with Wilson; .env already holds the five.


def new_sb_accounts() -> list:
    """[(session_id, name)] for each filled NEW_SB_ACCOUNT_<n> slot (digits only)."""
    out = []
    for n in range(1, NEW_SB_ACCOUNT_SLOTS + 1):
        sid = os.getenv(f"NEW_SB_ACCOUNT_{n}_SESSION_ID", "").strip()
        if not sid:
            continue
        if not sid.isdigit():
            raise ValueError(f"NEW_SB_ACCOUNT_{n}_SESSION_ID must be a HyperBot session id "
                             f"(digits), got: {sid!r}")
        name = os.getenv(f"NEW_SB_ACCOUNT_{n}_NAME", "").strip() or f"New SB {n}"
        if sid not in [s for s, _ in out]:
            out.append((sid, name))
    return out


# v6.61 (Wilson 2026-10-10): "wilson can be used but only as a fill up for sports if the
# new accounts can't cover the stake", then "total wager / active accounts (5) because
# exclude all old accounts". Fill-up sessions stay LAST in every sports list (a new account
# is inserted ahead of them) and session_priority.drop_fillup_when_others holds them back from
# every sports placement while any other listed account is active. They are used only when
# none is. (They do NOT top up a shortfall of the new accounts: not built.)
SPORTS_FILLUP_SESSIONS = [s.strip() for s in os.getenv("SPORTS_FILLUP_SESSIONS", "").split(",")
                          if s.strip()]


def _join_new_sb_accounts_to_lists() -> None:
    for sid, _name in new_sb_accounts():
        for var in NEW_SB_JOIN_LISTS:
            raw = os.getenv(var, "").strip()
            if not raw:
                continue                      # an empty list means "none/manual": leave it
            ids = [s.strip() for s in raw.split(",") if s.strip()]
            if sid in ids:
                continue
            fill = [i for i, x in enumerate(ids) if x in SPORTS_FILLUP_SESSIONS]
            at = fill[0] if fill else len(ids)
            os.environ[var] = ",".join(ids[:at] + [sid] + ids[at:])


_join_new_sb_accounts_to_lists()

# ── Version ──────────────────────────────────────────────────────────
# Bump on every meaningful change. Logged + Telegram'd at startup so it's
# always clear which build is running. The code fingerprint computed in
# main.py (_code_fingerprint) complements this — it catches partial/stale
# deploys even when this string wasn't bumped.
TIPBOT_VERSION = "v6.271 (2026-11-08: a trailing NZ on a horse's name is the same horse (Im Boo Nz = Im Boo); before that: v6.270 (2026-11-08: no-price same-game multis go to manual (NO_PRICE_SGM_MANUAL); before that: v6.267 (2026-11-07: player names: ONE rule (player_names.py) for a name a book spells another way: an exact name wins, then an alias table (both directions, shared with the resulter's PLAYER_ALIAS), then a shortened first name (same surname, one first name a prefix of the other, 3 letters at least); exactly one player may fit; middle names, hyphens, a Jr against a Sr and different first names never match. bet365's card lookup (Alex Sarr finds Alexandre Sarr), the Sportsbet / Sportmarket catalog matcher and the resulter use it; a bet365 card with the same surname under a first name the rule refuses writes one WARNING line (no alert, nothing placed); the ledger keeps the tip's name; two spellings that are both on tipbot's roster, a NEVER_SAME pair (James / Jameson Williams, Jay / Jayden Higgins) and a Jr against a Sr never match; NFL market keys use the rule and the bet goes out under the catalog row's own key and spelling; scripts/audit_b365_names.py lists card names the roster lacks; no switch), then v6.265 (2026-11-07: Dabble 339917 gets the v6.257 one retry at the balance left after a clean insufficient-funds refusal (the live text with outcome_code and promo_applied passes Dabble vetting and the funds check); the retry reads the accepted stake the Dabble way (a part accept spills the rest), a RISK_LIMIT or an unclear reply to it behaves as the first attempt would, a failed balance read means no retry, trader accounts stay out; switch RACING_RETRY_AT_BALANCE), then v6.264 (2026-11-07: A Leg Up card with a stake of 7 units (4 locked in + 3 more at the current price) seen for the first time with nothing in the units ledger for the horse now bets ONLY the additional 3u: parse_tip_line reads locked_units only when that exact wording adds up to the units read, _process_legup_post counts the locked-in 4u as held for legup_ledger.decide (10u cap included), records the ledger at 7 and says in the info section that the 4u locked in were NOT bet by the bot; a horse with a ledger entry behaves as before), then v6.266 (2026-11-07: a sports bet with no tipster price is not bet under 1.50 (NO_PRICE_MIN_ODDS) on Sportsbet, bet365 or Sportmarket: the row goes to manual with the reason, the target_odds sent is never under 1.50 so 1.50 fills and 1.49 cannot; Shook 1.60, NFL 1.6 and Eddie 1.75 stand where higher; a priced tip, racing, the NBA model and twin / milestone rows are untouched (Wilson)), then v6.268 (2026-11-07: the Bet Record feed finds a bet365 sports bet's HyperBot result by its correlation_id when the receipt bet_id finds none (HyperBot ids those bets by the correlation id), a stake that disagrees is logged and the result kept, a transaction is never given to two rows, no switch), then v6.269 (2026-11-07: Betdeluxe s99999 sits out a thoroughbred NIGHT meeting until 14:00 local: a meeting whose first race (earliest race_time on the warm HyperBot race list, never a blocking read) is at or after 16:00 local starts the law at 14:00, not 09:00 or VIC 07:00; times that cannot be read count as a night meeting; harness stays 10:00), then v6.262 (2026-11-07: The Leg Up tips page: the price left the card head on 5 Nov, so a card read 'WIN' with no price and went to the manual list; page_card_line now reads it from the card's own sentences, in order: 'Add N more units at the current price ($P with B)', then 'Members locked in N units at $P ... with B', then 'Rated vs Market: ... vs $P Available with B'; an unreadable or conflicting sentence stays no price (manual); a head with a price, TO+ or a Price / Bookie meta is unchanged; the page key ignores price and bookie for a Pro Tip, so a price move alone does not forward a card again and a changed stake (a top-up) still does; no switch), then v6.263 (2026-11-07: Betdeluxe s99999 is priced and bet on racing only from the minimum bet law start time in the race's own state (thoroughbred VIC 07:00, other states 09:00; harness 10:00; a later or earlier race day not started), not from 00:00 on race day; before it the stake goes to the other accounts; greyhounds, NT, an unknown state and a non-Australian race are unchanged, TAB and BetRight keep mbl_in_force; switch BETDELUXE_MBL_START_GATE), then v6.258 (2026-11-07: a player-prop tip with no tipster price goes to manual before any POST unless its matched catalog row has a usable price (a number above 1.0), whatever the read did (failed, empty, crashed, no row, no price); a tip with a price, the bridge, AFL and the NBA model are untouched, switch NO_PRICE_NEEDS_CATALOG), then v6.260 (2026-11-07: trader-referral watching, report only: a balance rise reads as a rise and is only logged, the reconcile NOT FOUND line no longer says caller may spill, and a follow whose last look falls in the HyperBot off window looks once more after it ends; a follow whose HyperBot session is missing from the session list inside that window waits for it to end and reads the list again instead of ending as session gone), then v6.255 (2026-11-07: an NFL tip within 24 h after either team's last kickoff goes to manual (NFL_LATE_TIP_HOURS), and a tip the NFL limits refused, or one that found no usable game or market, goes to manual again if re-posted after the 3-day resend guard, the same tip next week goes to manual once (Wilson)), then v6.259 (2026-11-07: a manual gate adds its reason after the parser's instead of replacing it (the AusBets McCollum correction was hidden), and a Shook 'same unit alt' post sends one manual alert with his previous message and places nothing (Wilson), then v6.256 (2026-11-07: after Sportsbet fills what it can, the remainder tries the TIPPED line on every book before a book whose line is worse for us (an over higher, an under lower, a spread with fewer points for us); each group stays best price first, so a Sportmarket row on 61.5 no longer takes a tipped 60.5 bet ahead of bet365 on 60.5 (Wilson); a bet365 with no quote has no tipped-line row and takes no place in the first group, a deferred Shook tip is judged against his ORIGINAL line and side not the moved board line, a threshold tip with no side word is an over; bet365's attempt may still take a rung inside its own rules after its tipped row fails the band; switch SHORTFALL_TIPPED_LINE_FIRST), then v6.257 (2026-11-07: racing: after a clean insufficient-funds refusal (no hold wording from Dabble's list or the extra words) the account's balance is read and ONE more bet is sent for the balance (rounded down to the cent, at least $1, under the refused stake), one retry after the max-stake rebet if there was one; any refusal of the retry or an unclear answer ends the account for that tip, an unclear answer is maybe-placed and never re-bet; the first failed balance read ends balance reads for the tip; trader accounts, Dabble 339917 and same-price group legs have no retry; switch RACING_RETRY_AT_BALANCE), then v6.261 (2026-11-07: Bet Record resulter and labels: Marquise Brown = Hollywood Brown in the ESPN box, bet365 hb under rows read their stat from the key, HyperBot-settled NBA bets wait for basketball-reference instead of storing an empty record fix, Sportsbet Alt rung names show the player once), then v6.254 (2026-11-07: NFL game lines on a whole number go on bet365 and in the near-line step, a push is a void (Wilson); the near-line step tries every whole and half point within 1, better numbers first, nearest first; no NFL bet more than 4 days before kickoff, NFL_MAX_DAYS_AHEAD), then v6.250 (2026-11-06: bet365 takes NFL game lines (spread incl. alt handicap, game total, team total, moneyline; half lines only) from the HyperBot card, and an NFL spread, game total or team total that still has a shortfall tries the lines within 1 point on bet365 and Sportmarket after the tipped line, the better number first, each at no more than 10% under the tipped price (a whole-number line, a tip with no price of its own, a moneyline and player props get no near step; switch NFL_GAME_LINE_NEAR_ENABLED)), then v6.251 (2026-11-06: a Sportmarket ask with cents rounds up to the next whole dollar, tennis too), then v6.253 (2026-11-06: MMA on Sportmarket is on), then v6.252 (2026-11-06: MMA has no start-time cut-off on any book, bets until ESPN shows the fight started or the book refuses), then v6.249 (2026-11-06: Shook's tipped price is the median of his published prices; bet365 spills send the 10% band and may take the next milestone up; no top limit for Shook; catalog name and side match fixed), then v6.248 (2026-11-06: a deferred Shook NBA player prop with no tipster price may bet bet365 (overs) and Sportmarket on a line 1 away from the current line in the shortfall step when the current line has no row on that book: the better line first, never more than SHOOK_DEFER_MAX_WORSE worse than his original line (a better line has no limit, so a bet can sit 2 from the tipped line), at a price over 1.60; Tarken stays exact, the better step never takes a near row (bet365's v6.188 rung rules still apply there), and a threshold tip stays exact only until its line moves), then v6.247 (2026-11-05: a Shook NBA player-prop single gets Sportmarket steps (a better step before Tarken, then the unfilled rest), every deferred Shook bet stays within SHOOK_DEFER_MAX_WORSE of his ORIGINAL line, and a known-down Tarken no longer stops Shook betting bet365 and Sportmarket), then v6.246 (2026-11-05: an image tip's explicit date beats its weekday; v6.245 a Dabble RISK_LIMIT refusal moves on to the next account), then v6.237 (2026-11-04: a late-found Sportsbet bet is written at its real line with one alert, and the late check stops nothing; a look that finds a bet already in the ledger leaves the reservation alone; orphan sweep compares the period), then v6.244 (2026-11-04: NBA_MODEL_V5_ENABLED on, so tipbot accepts the model's BET|v5 lines), then v6.234 (2026-11-04: NBA model BET|v5 line: every Sportmarket leg places only at or above the line's sm_floor, x means no leg; switch NBA_MODEL_V5_ENABLED off until the model is told), then v6.238 (2026-11-04: Tarken known down lets bet365 and Sportmarket bet under the shortfall rules, a failed session read stays manual, a maybe stops the other book at every step; MMA can go to bet365 and Sportmarket, its Sportmarket leg off until the bridge is ready; a model line with no sm_floor gets no Sportmarket leg while Sportsbet is down), then v6.239 (2026-11-04: on the v4 singles path a closed partner line, or a fresh 538 on it where every POST was fast, goes on to the milestone on the same account; stake bound off the main price), then v6.236 (2026-11-04: racing deferred verify anchored on the send time and it says CHECK THE ACCOUNT on a miss; a short record is booked at its own stake; trader refusals remembered; Daniel bet365 116964 out of racing in code), then v6.240 (2026-11-04: Late Mail Leroy removed: channel, parser, Betfair BSP placement, settings; Betfair 99997 keeps launching through BETFAIR_AUTOSTART_SESSION; an unregistered chat is ignored and a positive channel id is an error at startup), then v6.241 (2026-11-04: an alert when a tipster edits a post; it never parses, places or changes a bet; sportsbot sync list completed), then v6.243 (2026-11-04: racing prices and places by HyperBot's race key, after 7 $1 tests; the coverage skip stays off, the race list leaves off books that are pricing), then v6.235 (2026-11-04: Dabble 339917 is priced and bet only on racing tips whose race day is today in the race's own state; an earlier, later or unreadable date drops it, an undated tip counts as today) + v6.233 (2026-11-04: the deferred sports verify matches pending_bets from the bet's SEND time instead of the schedule time, so a late-indexed bet is no longer read as absent; a Sportmarket bridge bet it finds late is claimed so another tip's exact-line reconcile excludes it; the look gates the line like the first look, a Sportmarket crash reports its real send time, a bet the look finds is written to the ledger with one info message, one validated stall clock; a bet the gated look misses but a line-free look finds is UNCERTAIN (one critical, nothing recorded or released), never absent; the late row carries the posted line / market / selection; bet_ledger marks an id only after a successful write) + v6.230 (2026-11-03: Leroy: a race header the parser cannot read (a bracketed state or country, a digit in the track) ends the current race and goes to a manual alert instead of folding its runners into the previous race) + v6.221 (2026-11-03: racing prices and places by the race's pk from HyperBot's race list, and skips a bookie the list does not carry on the race; switches RACING_PK_ENABLED / RACING_COVERAGE_SKIP_ENABLED, both off) + v6.220 (2026-11-03: racing price check logs 'runner on card, no price yet' apart from 'runner not found'; an unpriced runner on the card no longer starts the wrong-race search) + v6.219 (2026-11-03: racing same-price ties fill the bet365 accounts first, then the rest, each in racing priority order; a bet365 account that covers the tip alone takes it, else the v6.205 random pick over the others; an account refused at placement within RACING_REFUSER_LAST_SEC (6 h) goes last; switch RACING_TIE_B365_FIRST) + v6.224 review fixes (v6.222-v6.224) (2026-11-03: a digit in a team name (49ers, 76ers) is no longer read as a reconcile line (pre-existing, also on Sportsbet); deferred verify exact line + per-poll claims for bridge bets; kickoff check ignores stale ESPN, reads ms epochs; NBA model rebet re-checks its close; safe switch parsing) + v6.224 (2026-11-03: every Sportmarket bet (a v6.208 rung and the tipped line) carries the bridge board's own event_id from the /v3/price read that picked its line, so the bridge never bets a similarly named game; none when there was no read; never a HyperBot id; the bridge's [event_not_carried] = nothing sent) + v6.223 (2026-11-03: NFL / NBL / AusBets NBA sends to Sportmarket stop 90 s before the game's scheduled start (earliest of the bridge's board start, the catalog start, the ESPN schedule; unknown start = no change, logged once; switch SPORTMARKET_KICKOFF_CHECK_ENABLED), the rest goes to Sportsbet) + v6.222 (2026-11-03: a Sportmarket reconcile only accepts a bridge bet that is provably this attempt's: the bridge's own named order ids, else an exact line, never a bet another tip's attempt holds; the within-1 gate stays for Sportsbet) + v6.232 (2026-11-03: Sportsbet re-line guard: after every Sportsbet single fill with a bet id the real line is read from HyperBot's own bet receipt log; a receipt line different from the line sent is a confirmed move (true line on the ledger row, that tip's further Sportsbet sends stopped, one alert, end of tip says stopped), the same line changes nothing whatever the price did, an unreadable receipt with a fill over 5% above the odds checked alerts without stopping; Fergal tennis Sportsbet included; stops persist in logs/sb_reline_stops.json; the 538 learner skips a moved send) + v6.231 (2026-11-03: Fergal tennis asks Sportmarket again after a confirmed partial fill, each pass at the live price but never under 2% below the tipped odds (to 3 decimals: 1.911 for a 1.95 tip), at most 5 follow-up asks (0 = off), never after a maybe / not_placed / uncertain / pending_bets answer, a book with a strictly better price takes over and Sportmarket gets one more pass after the other books, the time is checked again before the POST, a walk cut off by a restart is alerted once; config FERGAL_TENNIS_SM_STEPDOWN_PCT / _PASSES) + v6.229 (2026-11-03: Shook NBA player-prop singles are recorded (logs/shook_deferred.json) and bet 60 min before tip-off at the current Sportsbet line, only if it has not moved more than 1 stat point against us (a move our way is bet; a tip inside the hour is checked at once; too far = logged only); switches SHOOK_DEFER_TO_1H_ENABLED, SHOOK_DEFER_MINUTES, SHOOK_DEFER_MAX_WORSE) + v6.227 (2026-11-03: BetRight's [odds_moved] dividend changed reply is a clean refusal at any speed on a racing trader account, the stake spills; every other bookie and wording keeps the speed rule) + v6.226 (2026-11-03: bet365 and Sportmarket take the stake FIRST at a price EQUAL to or better than Sportsbet's, ties go to them, Sportsbet gets the rest: the main better step, the v6.201 NBA first-half Sportmarket rule and the v6.208 better-line rung price rule; the better-mode fill floor is Sportsbet's own price, not a cent above; Fergal tennis unchanged) + v6.225 (2026-11-03: Sportsbet SPORTS placement goes to Tarken 295513 only: the seven 1% accounts (Neel, Talal, Sahil, Mario, Aaron, Tom, Matthew) are dropped from every non-racing list, the NBA model, Fergal tennis and MLB singles (SPORTS_EXCLUDED_SESSIONS); racing unchanged) + v6.212 (2026-11-02: Dabble 339917 is a racing-only bookie in the best-price walk: first and alone on a tie, the whole stake in one attempt, only the accepted stake HyperBot states counts and the rest goes on, an unclear reply is maybe-placed and never spilled; no sports path can pick it; switch DABBLE_RACING_ENABLED, default on) + v6.211 (2026-11-02: the Sportsbet stake estimate (sb_limits) gets a fixed per-account size from sessions.yaml sb_limit_pct (1 / 10 / 30 / ... / unlimited = 40%; Matthew and Tom now sized; a 538 only logs the implied size and notes it once a day when it is over 30% off), NFL player props on the 24h / 4h ladder, and full-game game lines for NBA / NBL / NFL (head to head, main handicap, game total, team total over); the estimate is only ever a ceiling, min(the ask, the estimate)) + v6.218 (2026-11-03: NBA model v4 shortfall under $1 does not page) + v6.228 (2026-11-03: NBL betting is off for every tipster and every book (NBL_BETTING_ENABLED, default off): a Bettors Edge post is dropped before parsing with a log line only, and every NBL send path refuses locally) + v6.217 (2026-11-03: every Sportsbet account is a 1% account except Tarken 295513 (10%): Aaron 319979 (was 10%), Tom 328827 and Matthew 319970 (were unlimited) are seeded at 1% in the stake estimate, and Matthew joins the SB_LIMITED_SESSIONS default (max-first)) + v6.216 (2026-11-02: NBA model bets go up to Sportsbet's own start (scheduled + 10 min, or its board start when earlier), no 90 s lead; Sportmarket at the scheduled tip-off) + v6.213 (2026-11-02: NBA model: Matthew 319970 bets only when the model bet ARRIVES within 2 h of tip-off (NBA_MODEL_LATE_ONLY_SESSIONS / NBA_MODEL_LATE_WINDOW_MIN, v3 and v4, a model re-send is a new arrival, an unknown tip drops him); Tom 328827 joins the limited group in the SB_LIMITED_SESSIONS default) + v6.210 (2026-11-02: NBA model post-538 close counts Sportsbet's cap steps to Sportsbet's own start, not the scheduled tip: reopens were 10 min early) + v6.214 (2026-11-02: the Bet Record auto-resulter settles cross-game moneyline multis: market multi, every leg '<Team> ML', each leg as a single head-to-head by its league's own path, any leg lost = Loss, all won = Win at the bet's odds, a tie / void / unknown leg = a human) + v6.209 (2026-11-01: racing trader accounts: when the pending_bets look finds ONE bet with a bet id and a stake SMALLER than the reserved one (TAB auto-capped it), it is booked as PLACED at the found stake and the rest is unfilled, not re-bet, not spilled, no hold) + v6.208 (2026-11-01: Sportmarket game lines (spread, game total, team total) try the bridge's better-number rungs of the same selection first, each priced strictly better than Sportsbet on the tipped line, best number first, at most 3; a partial or refused rung moves on, then the tipped line, then Sportsbet; switch SPORTMARKET_BETTER_LINE_ENABLED) + v6.207 (2026-11-01: Bettors Edge NBL captions ignore a trailing sign-off line (No more, That's it) and alert once when a caption disagrees with verified slip stakes; NFL same-name players settled by position when the tip names no team (not passing TD / interceptions), four known collisions seeded, a shrunk Sleeper pull never overwrites the roster; bet365 /v3/price reads retry once for callers that may wait, bet365-only legs wait out a pause once when the start is known, nothing is pulled or sent inside the last 2 minutes) + v6.206 (2026-11-01: NBA overs: the same-line Alt rung and plain main row are both bet on the same account (own limits), then the milestone; switch SB_ALT_MAIN_TWIN_ENABLED) + v6.205 (2026-11-01: racing same-price groups fill each tied account to its cap in priority order, fewer bets; Sportsbet Mario 295510 out of all placement) + v6.202 (2026-11-01: every NBA model fills record carries Sportsbet capacity: sb_liability_left, sb_price, sb_reopen_at, sb_zero_reason, sb_capacity_v, so the model re-sends a same-line top-up only when it can fill) + v6.204 (2026-11-01: bet365 NFL priced from HyperBot's /v3/markets card switched ON, like NBA / NBL) + v6.203 (2026-11-01: racing trader accounts: thoroughbred MBL in force all of race day from 00:00 local in every state (harness 10:00, WA all day, NT never); undated tips use the saturday: caps too; a slow 538 / odds changed / 756 is never freed, the look can only upgrade it to placed) + v6.201 (2026-11-01: NBA first-half spreads / totals (AusBets) go to the Sportmarket bridge only when its price on the same half line is strictly higher than the best HyperBot book's, the v6.172 better-step flow, no shortfall step; reconcile never matches a first-half bet to a full-game one or the reverse; switch SPORTMARKET_FIRST_HALF_ENABLED, default on) + v6.199 (2026-11-01: bet365 NBL (on) and NFL (switch off) priced from HyperBot's /v3/markets card; card bets placed by epk + market_ref; player names match with punctuation squashed) + v6.198 (2026-11-01: racing trader accounts 71265 / 78280 / 99999 / 99996 bet within the class MBL in parallel: MBL table by venue and state with its clock (thoroughbreds 09:00, VIC 07:00, harness 10:00, WA all day, NT never), judged at the bookie's own quote; async sends with reserved stake, referral = unconfirmed, never re-bet, one look, persisted follow, ledger-first durability and an in-flight file; fast 538 / odds changed / 756 = failure, slow = unconfirmed unless a 30 s window proves it absent; state-local race dates; faster price shop; switch RACING_TRADER_ASYNC) + v6.196 (2026-11-01: NBA model BET|v4: a Pinnacle-decided Tier 1 REB Under goes to the Sportmarket bridge first at >= sm_floor, the remainder to Sportsbet only at >= its own sb_floor (x = not at all), no other spill; v4 fills never retryable; every v4 key short of its stake pages once and never invites a hand-place; a v3 offer after v4 at the same selection is refused; switch NBA_MODEL_V4_ENABLED, default off) + v6.195 (2026-10-31: bet365 NBA props priced from HyperBot's GET /v3/markets card: combos PRA / PR / PA / RA, steals, blocks and every player, full-game overs only; /v3/price as the fallback) + v6.194 (2026-10-31: TipBot-DiskGuard deletes the test suite's own logs in Temp over 200 MB once quiet for 10 min, and stale per-process trader in-flight test files after 72 h) + v6.193 (2026-10-31: bet365 NBA props: an over with no bet365 row at its rung takes the rung below at Sportsbet's price or better, or the rung above at Sportsbet / 0.8 or better; steals / blocks placed by name; Shook NBA single overs wired; NBA bet365 cap 1000 to win) + v6.192 (2026-10-31: Fergal tennis Sportsbet to-win caps per account, $400 match winner / $200 every other market, or the account's own lower max) + v6.191 (2026-10-31: Fergal tennis bets Sportsbet only on the five working accounts; the four banned ones are dropped even if the override names them) + v6.190 (2026-10-31: racing: a fast Sportsbet 756 'not permitted for your account' is a clean refusal for that account and the walk goes on to the next account; a slow one is still reconciled and, confirmed absent, spends that account only; one alert a day per account) + v6.187 (2026-10-31: bet365 racing: Wilson Bet365 338081 (wolfhound) joins racing just in front of Daniel 116964, racing caps copied from 116964) + v6.186 (2026-10-31: Fergal tennis: a Sportsbet 756 'not permitted for your account' is a clean refusal for that account, and the walk goes on to the next account) + v6.185 (2026-10-31: a multi-leg tip not marked as an SGM goes to manual for every tipster, and is keyed on all its legs for the resend guard; switch MULTI_LEG_NON_SGM_TO_MANUAL, default on) + v6.184 (2026-10-31: a timed-out Sportmarket partial fill is recorded by its order id once every order the bridge sent is closed; the client forwards the bridge's timeout order keys) + v6.183 (2026-10-31: the NBA milestone twin takes its side from the matcher, so a Sportsbet row whose text carries the line (Robinson / Murray-Boyles Over N) gets its twin, and every skipped twin is logged with the reason; racing: a not-found or not-carried on one Sportsbet account spends that account only, Sportsbet is dropped for the tip at the second)"

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
# hb_autorun.py (Wilson 2026-10-24): what to type when the HyperBot launcher asks at start
# whether to update. "y" (default) keeps HyperBot current; "n" starts the build already on disk.
# Read by the autorunner only (not main.py); anything other than y/yes/n/no falls back to "y".
HB_AUTO_UPDATE_ANSWER = os.getenv("HB_AUTO_UPDATE_ANSWER", "y").strip().lower()

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
# v6.63 (Wilson 2026-10-11: "going forward for the claude api parser, send it to haiku
# 5.5"): Claude Haiku 5.5. Exact ID, no date suffix. Override via .env only if migrating.
CLAUDE_PARSER_MODEL = os.getenv("CLAUDE_PARSER_MODEL", "claude-haiku-5-5")   # v6.63 (Wilson 2026-10-11)

# ── Claude per-call FALLBACK (v5.80) ─────────────────────────────────
# Independent of TIP_PARSER_PROVIDER (the global swap). This is the per-call
# RECOVERY LAYER: Groq runs first; Claude fires ONLY when Groq fully fails on a
# GENUINE bet (repair-failed / 0-tips-on-a-bet-looking msg / empty vision) — and
# NEVER on the v5.58/v5.59 no-bet / summary / chatter branches (that would
# re-open the AusBets "$400 on a no-bet message" hole). Every Claude-recovered
# tip re-enters the IDENTICAL placement/roster/floor/dedup gates. OFF by default.
CLAUDE_FALLBACK_ENABLED = os.getenv("CLAUDE_FALLBACK_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
# The model EVERY tip is parsed with when CLAUDE_PRIMARY=true (the name dates from
# v5.80, when Claude only ran after a Groq failure and Opus 4.8 was chosen for that
# rare path). v6.41 (Wilson 2026-09-25): Sonnet 5; v6.63 (Wilson 2026-10-11): Haiku 5.5,
# after a subagent test on real posts and screenshots. Exact ID, no date suffix. Parse is prompt-guided + JSON-repair
# (schema NOT enforced); a repair failure returns the empty sentinel -> manual.
CLAUDE_FALLBACK_MODEL = os.getenv("CLAUDE_FALLBACK_MODEL", "claude-haiku-5-5")   # v6.63 (Wilson 2026-10-11)
# v5.81: web-search resolvers (player/track) used SONNET 4.6. v6.99 (Wilson 2026-10-21
# "can we websearch on haiku instead?"): Sonnet 4.6 timed out on every October resolve,
# Haiku 5.5 answers in 3-8s (claude_parser gives Haiku the basic web_search tool). The
# search/loop caps keep each resolve to an estimated 2 cents.
CLAUDE_WEBSEARCH_MODEL = os.getenv("CLAUDE_WEBSEARCH_MODEL", "claude-haiku-5-5")
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

# v6.98 (Wilson 2026-10-21): Shook MLB OFF (tips ignored) and Shook NFL to MANUAL.
# Shook's channel is sport "auto", so these are decided per parsed tip in main.py.
SHOOK_MLB_ENABLED = _env_bool("SHOOK_MLB_ENABLED", False)
SHOOK_NFL_MANUAL = _env_bool("SHOOK_NFL_MANUAL", True)

# v6.229 (Wilson 3 Nov 2026: "for shook, record his tips and only bet on them when 1 hour from tip if
# the line has not moved by more than 1 stat point (instead of betting 10 hours before game starts
# when he randomly posts early)"): a Shook NBA player-prop single is recorded (logs/shook_deferred.json)
# and bet SHOOK_DEFER_MINUTES before tip-off at the CURRENT line, only if that line is no more than
# SHOOK_DEFER_MAX_WORSE stat points worse for us than the tipped one (an over on a higher line, an
# under on a lower one; a move our way always passes). A tip posted inside the window is checked at
# once. A line that moved too far is only logged. Code default on; the test conftest pins it off.
SHOOK_DEFER_TO_1H_ENABLED = _env_bool("SHOOK_DEFER_TO_1H_ENABLED", True)
SHOOK_DEFER_MINUTES = _env_float("SHOOK_DEFER_MINUTES", "60")
SHOOK_DEFER_MAX_WORSE = _env_float("SHOOK_DEFER_MAX_WORSE", "1.0")
# v6.247 (Wilson 5 Nov 2026: "tarken, bet365 and sportmarket for nba ... whatever else is spilled onto
# sportmarket, or even sportmarket first depending on odds/lines"): a Shook NBA player-prop single in
# _place_singles_v4 gets Sportmarket steps like bet365 does (a better step before Tarken, the unfilled
# rest after it, and the whole stake when Tarken is known down), for a tip that went through the 1-hour check.
# False restores the earlier ROUTING only (the Sportmarket session is dropped by the Sportsbet-only priority
# filter, Tarken down = manual). The original-line limit (Shook only bets his tipped line, better, or at most
# SHOOK_DEFER_MAX_WORSE worse) stays on for every deferred Shook bet whatever this switch says.
# v6.248 (Wilson 5 Nov 2026: "allow one worse on b365 and sportmarket aswell"): under this same switch, a deferred
# Shook player prop whose tipster gave NO price may take bet365 (overs only) and Sportmarket on a line 1 away
# (B365_SHORTFALL_MAX_LINE_DIFF) in the shortfall step, when the tipped line has no row on that book: the line
# better for us first, then the worse one, both inside the original-line limit and above the 1.60 no-comparison
# floor. A row on the tipped line always goes first, a threshold ('N+') tip stays exact, and Tarken and the
# better step are unchanged. No new setting.
SHOOK_SPORTMARKET_ENABLED = _env_bool("SHOOK_SPORTMARKET_ENABLED", True)

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
# v6.36: HyperBot rejects some Sportsbet player props with [selection_not_carried] when its
# catalog row has player=None (A1: Jadarian Price 09-18, Noah Fant 09-19, Quinshon Judkins
# 09-25), even though our payload carries the exact proposition_id. Retry ONCE without `player`
# (same selection, line, proposition_id). Kill switch.
HB_RETRY_WITHOUT_PLAYER = _env_bool("HB_RETRY_WITHOUT_PLAYER", True)
# v6.38: same idea for `stat`: HB filters on it and Sportsbet rows carry none (NBL doubles:
# the Hickey triple double 2026-09-25). Retry once without `stat`. Kill switch.
HB_RETRY_WITHOUT_STAT = _env_bool("HB_RETRY_WITHOUT_STAT", True)
# v6.40 (Wilson 2026-09-25: "stake too high means it isnt actually bet, so no need to wait to
# check, just rebet at the max stake amount"). A Sportsbet 538 that STATES a max stake is
# rebet at that max even when it came back slow, up to this many seconds. Beyond it the old
# maybe-landed handling stays: the 2026-05-03 Erasmus 538 took 33s and DID land (before HB
# returned a max). 0 = only fast 538s rebet (the pre-v6.40 behaviour).
SB_SLOW_538_REBET_MAX_SEC = float(os.getenv("SB_SLOW_538_REBET_MAX_SEC", "15"))
# v6.116 (Wilson 2026-10-24: "yes rebet at the stated max" for sports singles too): a Sportsbet 538
# that states a max but came back slower than SB_SLOW_538_REBET_MAX_SEC is checked against
# /api/pending_bets first (the racing group's v6.115 rule): on the account = placed; proven absent =
# the one rebet at that max on the SAME account; uncertain = maybe landed, as before. Never a spill
# to another account (RECONCILE_SPILL stays off). Set false to go back to skipping the rebet.
SB_SLOW_538_RECONCILE_REBET = os.getenv("SB_SLOW_538_RECONCILE_REBET", "true").strip().lower() in ("1", "true", "yes")
# v6.117 (Wilson 2026-10-25: "we should pause all sports placement for a few days as the sb accounts
# all got restricted from sports"): every SPORTS bet (AFL, NBA, NBL, NFL, MLB, MMA, every tipster and
# the NBA model) is NOT sent to HyperBot. The tip still arrives: a normal sports tip goes to the
# Manual Bets chat marked PAUSED; an NBA model BET is answered with a "refused" fills record. Racing is
# NOT affected. Turn off with SPORTS_PLACEMENT_PAUSED=false + restart.
SPORTS_PLACEMENT_PAUSED = os.getenv("SPORTS_PLACEMENT_PAUSED", "false").strip().lower() in ("1", "true", "yes")
# The NBA model on its own (nba-player-props-model session, Wilson 2026-10-25): its BET lines stay
# tips, each answered with a "refused" fills record (placed 0, requested 0), nothing sent anywhere.
# SPORTS_PLACEMENT_PAUSED implies it.
NBA_MODEL_PLACEMENT_PAUSED = os.getenv("NBA_MODEL_PLACEMENT_PAUSED", "false").strip().lower() in ("1", "true", "yes")
# v6.119 (Wilson 2026-10-25, before adding fresh Sportsbet accounts; the old five were restricted
# after two weeks of identical bets from all accounts within ~1 s): every SPORTS fan-out places its
# accounts ONE AT A TIME in a random order with a random SPORTS_STAGGER_MIN_SEC..MAX_SEC gap, and
# splits the stake unevenly (each share = even share x a random factor in 1 +- SPORTS_STAKE_JITTER_PCT,
# whole dollars, the total still the unit). Wilson's numbers: 15-60 s, 20%, whole dollars.
SPORTS_STAGGER_ENABLED = os.getenv("SPORTS_STAGGER_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# v6.125 (Wilson 2026-10-27: "for sb lets do 20-30 seconds delay each for now"; was 15-60)
SPORTS_STAGGER_MIN_SEC = float(os.getenv("SPORTS_STAGGER_MIN_SEC", "20"))
SPORTS_STAGGER_MAX_SEC = float(os.getenv("SPORTS_STAGGER_MAX_SEC", "30"))
SPORTS_STAKE_JITTER_PCT = float(os.getenv("SPORTS_STAKE_JITTER_PCT", "0.20"))
# v6.134 (Wilson 2026-10-28: "5-10 sec delay between the limited sb accounts too"): the gap between
# two limited Sportsbet accounts in a max-first fan-out (none before the first one)
SB_LIMITED_STAGGER_MIN_SEC = float(os.getenv("SB_LIMITED_STAGGER_MIN_SEC", "5"))
SB_LIMITED_STAGGER_MAX_SEC = float(os.getenv("SB_LIMITED_STAGGER_MAX_SEC", "10"))
# v6.137 (Wilson 2026-10-28: "happy to overstake by up to $5 to ensure the $5 condition is met"):
# what is left of a unit, asked of a $5-step Sportsbet account, is rounded UP to the step when that
# adds no more than this (else down); caps and Sportsbet's max still round down. 0 = always down.
SB_STEP_ROUND_UP_MAX = float(os.getenv("SB_STEP_ROUND_UP_MAX", "5"))
# v6.125 (Wilson 2026-10-27: "the splitting by 20% thing should only apply to new accounts, the
# current accounts we have placing, they are all limited so should get on at their max stake
# possible"): these Sportsbet sessions are LIMITED. In a fan-out they go first, one at a time,
# each asked for the whole unfilled remainder (capped by its own caps) so Sportsbet's stake-too-
# high answer rebets at that account's max; only the other (new) accounts get the +-20% split of
# what is left.
# v6.126 (Wilson 2026-10-27, the new accounts Matthew 319970 / Aaron 319979 / Tom 328827): "make
# sure the bets on these accounts always place at a multiple of $5 and try to differ their stakes
# ... split that into 3 different stakes differing by 10-20% each but all round in $5 and add up
# to $400 stake". The new (non-limited) accounts' shares are whole multiples of SB_NEW_STAKE_STEP,
# each one SB_NEW_SPREAD_MIN..MAX above the next smaller share. The limited accounts fill first
# "with little to no delay" (no gap between them); the 20-30 s gap is only before a new account.
SB_NEW_STAKE_STEP = float(os.getenv("SB_NEW_STAKE_STEP", "5"))
# the accounts whose every stake is a SB_NEW_STAKE_STEP multiple (Matthew, Aaron, Tom)
SB_STEP_SESSIONS = {s.strip() for s in os.getenv(
    "SB_STEP_SESSIONS", "319970,319979,328827").split(",") if s.strip()}
# v6.142 (Wilson 2026-10-29): a Sportsbet account is SENT its estimated max on the row (whole $,
# sb_limits.py: the measured limits ladder x the account's size) instead of over-asking for a 538 and
# rebetting at the max. v6.211: the size is `sb_limit_pct` on the account's sessions.yaml block (1 / 10 /
# 30 / ... / unlimited = 40%, fixed; an account with none keeps the learned / seeded size), Matthew and
# Tom included, and the estimate is only ever a CEILING: min(the ask, the estimate), never a raise.
# v6.217 (3 Nov): every Sportsbet account is 1% except Tarken 295513 (10%).
SB_LIMIT_ESTIMATE_ENABLED = os.getenv("SB_LIMIT_ESTIMATE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# every sport, for every tipster on the Sportsbet fan-out (Wilson 2026-10-29: "hooked up to all tipsters
# for all sports"); a sport / market only gets an estimate once sb_limits.BASE_10PCT holds its measured row
# (v6.211: NBA / NBL / NFL player props and NBA / NBL / NFL full-game lines; AFL has no rows yet)
SB_LIMIT_ESTIMATE_SPORTS = os.getenv("SB_LIMIT_ESTIMATE_SPORTS", "nba,nbl,nfl,afl")
SB_NEW_SPREAD_MIN = float(os.getenv("SB_NEW_SPREAD_MIN", "0.10"))
SB_NEW_SPREAD_MAX = float(os.getenv("SB_NEW_SPREAD_MAX", "0.20"))
# v6.128 (Wilson 2026-10-28): after an account fills a NBA/NBL main O/U row up to Sportsbet's
# limit, the rest of its ask goes on the same-line, same-odds milestone row ("To Record 6+
# Rebounds" = over 5.5), which has its own limit (measured 2026-10-28). Off = main row only.
SB_MILESTONE_TWIN_ENABLED = os.getenv("SB_MILESTONE_TWIN_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# v6.133 (Wilson 2026-10-28: "allow a 5 cent gap"): the twin row may be priced up to this far from
# the main row (either way); it bets at its OWN price
SB_MILESTONE_TWIN_MAX_ODDS_GAP = float(os.getenv("SB_MILESTONE_TWIN_MAX_ODDS_GAP", "0.05"))
# v6.206 (Wilson 2026-11-01: the Alt and main rows "have separate limits ... bet both of them on the
# same account"): an NBA over matched on the plain O/U row also takes the same-line Alt rung, and one
# matched on an Alt rung the plain row, on the same account (within the gap above), before the
# milestone. Off = the milestone twin only, as before.
SB_ALT_MAIN_TWIN_ENABLED = os.getenv("SB_ALT_MAIN_TWIN_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# v6.258 (Wilson 7 Nov 2026: "a sports player-prop tip with no tipster price must never be sent to the book when the
# catalog read gave nothing: it goes to manual"): a no-price player prop (NBA / NBL / MLB / NFL, not AFL, not a bridge
# session) whose catalog read failed, came back empty or crashed is refused to manual before any POST.
# Off = the old behaviour (the POST went out with no price).
NO_PRICE_NEEDS_CATALOG = os.getenv("NO_PRICE_NEEDS_CATALOG", "true").strip().lower() in ("1", "true", "yes")
# v6.132 (Wilson 2026-10-28): Aaron (319979) is limited now (Sportsbet max $105 on the Luka row,
# the 10% account 295513 $108.70): he bets with the limited group; Matthew + Tom keep the split.
# v6.213 (Wilson 2 Nov 2026): Tom (328827) is limited now too, a 10% account like Aaron and Tarken:
# he joins the limited group. Matthew (319970, the 40% account) stays out and keeps the split.
# v6.217 (Wilson 3 Nov: "all sb are now 1% except tarken"): Matthew is cut to 1% too (2 Nov NBL:
# "SB max $10.00" on every leg), so he joins the limited group: every Sportsbet account is limited.
SB_LIMITED_SESSIONS = {s.strip() for s in os.getenv(
    "SB_LIMITED_SESSIONS", "295471,295475,295478,295510,295513,319970,319979,328827").split(",")
    if s.strip()}
# v6.119 (Wilson: "rebet once, then close"): after a Sportsbet "stake too high" (538) on a sports
# bet, the one rebet at the stated max still runs, then THAT account never tries THAT line again
# for SB_LINE_CLOSED_TTL_SEC (persisted, survives a restart). Other accounts are unaffected.
SB_CLOSE_LINE_AFTER_538 = os.getenv("SB_CLOSE_LINE_AFTER_538", "true").strip().lower() in ("1", "true", "yes")
SB_LINE_CLOSED_TTL_SEC = float(os.getenv("SB_LINE_CLOSED_TTL_SEC", "86400"))
# v6.232 (Wilson 3 Nov 2026): Sportsbet can re-line a prop on the SAME outcome id (Cunningham Over 22.5 ->
# 23.5) while HyperBot's read of the event is 10 s or more behind, so it "matches" the old line and
# sends; Sportsbet fills its live line and price, and the receipt carries no line. A Sportsbet fill whose
# odds are checked against HyperBot's own bet receipt (sb_receipt.py): a receipt line that differs from the
# line sent is a CONFIRMED move (the tip's fan-out stops, one alert, the true line goes on the ledger row);
# only when the receipt cannot be read and the fill is MORE than SB_RELINE_ODDS_PCT above the odds checked
# is there an alert (no stop). A stop is remembered until the event's start + 6 h (24 h when the start is not
# known) in logs/sb_reline_stops.json, so a restart keeps it. Plain constants on purpose (no .env switch);
# conftest pins them.
SB_RELINE_GUARD = True
SB_RELINE_ODDS_PCT = 0.05
SB_RELINE_STOP_TTL_SEC = 86400.0          # a stop's life when the event's start is not known
SB_RELINE_STOP_AFTER_START_SEC = 21600.0  # ... else until the event's start + this (6 h)
# The re-line guard reads the REAL line of a Sportsbet fill from HyperBot's own receipt log (sb_receipt.py):
# <root>\<account email>\<date>\*.log, polled up to SB_RECEIPT_WAIT_SEC. Plain constants (no .env).
SB_RECEIPT_LOG_ROOT = r"C:\Users\Wilson\BET337\logs\sportsbet"
SB_RECEIPT_WAIT_SEC = 6.0
# v6.119 (Wilson: "flat 500 to win per account per single bet"): NFL singles were uncapped.
NFL_MAX_TO_WIN_PER_BET = float(os.getenv("NFL_MAX_TO_WIN_PER_BET", "500"))
# v6.120 (Wilson 2026-10-25): sports PLAYER PROPS on bet365 (the big-limit test account), ONLY when
# bet365's price for the exact same bet is equal to or better than Sportsbet's (v6.226; bet365 takes the stake
# first, up to its cap, the rest goes to the Sportsbet fan-out) or Sportsbet does not fill it (the
# unfilled remainder, never maybe-placed stake, goes to bet365 up to its cap). Game lines, totals,
# h2h, handicaps, SGMs and MMA never use this path. OFF unless B365_SPORTS_ENABLED=true.
B365_SPORTS_ENABLED = _env_bool("B365_SPORTS_ENABLED", False)
B365_SPORTS_SESSION = os.getenv("B365_SPORTS_SESSION", "314653").strip()
# Tipsters whose path is wired (NFL 4th and +EV / A1 via _place_nfl_fanout, Bettors Edge NBL via the
# same fan-out, AusBets NBA singles via _place_afl_fanout).
# v6.188 (Wilson 2026-10-31: "wire Shook too"): Shook NBA singles are wired in _place_singles_v4.
B365_SPORTS_TIPSTERS = set(
    t.strip().lower() for t in os.getenv(
        "B365_SPORTS_TIPSTERS",
        "fourthandev_nfl,a1_fantasy_nfl,bettorsedge_nbl,ausbets_nba,shook").split(",")
    if t.strip())
# Caps, TO-WIN per bet (stake x (odds - 1)), on the bet365 account per line, sized at the HIGHEST
# price the bet can fill at (the max_odds ceiling sent). Wilson's numbers.
B365_MAX_TO_WIN_NFL = float(os.getenv("B365_MAX_TO_WIN_NFL", "1200"))
# v6.188 (Wilson 2026-10-31: "player props on bet365 on the milestone markets are up to $1000
# liability is fine each bet"): was 800
B365_MAX_TO_WIN_NBA = float(os.getenv("B365_MAX_TO_WIN_NBA", "1000"))
# v6.188 (Wilson 2026-10-31): an NBA OVER whose own rung bet365 does not carry (o23.5 = 24+, the
# points ladder is 20+/25+) may take the rung one below or one above, in the strictly-better step:
#   rung below (o25.5 -> 25+): bet365 at Sportsbet's price or better ("same price or better")
#   rung above (o23.5 -> 25+): bet365 at Sportsbet's price / (1 - B365_NBA_HARDER_RUNG_DROP) or
#     better ("20% better to account for the 1+ bump": Sportsbet 1.80 -> bet365 2.25)
# The rung above's max_odds ceiling is B365_NBA_HARDER_RUNG_MAX_ODDS_MULT x the tipster's price
# (Sportsbet's when the tipster gave none): Wilson's NFL alt-rung ceiling (NFL_ALT_MAX_ODDS_MULT 1.5),
# since the normal 1.25 ceiling sits exactly on the 1.25 the rule needs.
B365_NBA_RUNG_ALTS = _env_bool("B365_NBA_RUNG_ALTS", True)
# v6.194 (Wilson 2026-10-31): price bet365 NBA props from HyperBot's GET /v3/markets card (every
# player, combos PRA / PR / PA / RA, steals, blocks) before the /v3/price Bet Builder catalog.
# Off by default under TIPBOT_TESTING, so the suite never reads the live HyperBot API.
B365_NBA_HB_MARKETS = _env_bool("B365_NBA_HB_MARKETS", not os.getenv("TIPBOT_TESTING"))
# v6.197 (Wilson 2026-11-01): bet365 NBL from the same card (overs-only ladders like NBA), on.
B365_NBL_HB_MARKETS = _env_bool("B365_NBL_HB_MARKETS", not os.getenv("TIPBOT_TESTING"))
# v6.197 (Wilson 2026-11-01: "yes do nfl as well but tomorrow there will be a bunch of bets being
# placed on nfl"): bet365 NFL from the card (named markets, main O/U unders + overs + alt ladder
# rungs) instead of /v3/price's unlabelled groups. v6.204 (Wilson 2026-11-01: "i want nba and nfl
# all on for b365"): on, like NBA / NBL (off under TIPBOT_TESTING).
B365_NFL_HB_MARKETS = _env_bool("B365_NFL_HB_MARKETS", not os.getenv("TIPBOT_TESTING"))
# v6.197 (Wilson 2026-11-01, "yeh we should do this"): a card row is placed by epk + market_ref
# (HyperBot's exact form; $1 tests 1 Nov: Maxey 25+ points, DJ Moore 60+ receiving, Henry under
# 18.5 rushing attempts), not by event name + proposition_id.
B365_PLACE_BY_MARKET_REF = _env_bool("B365_PLACE_BY_MARKET_REF", not os.getenv("TIPBOT_TESTING"))
# v6.250 (Wilson 6 Nov 2026, 4th and +EV Jaguars -19.5 @ 4.05: Sportsbet took its capped share, Sportmarket had no -19.5
# row and $277 went to manual because bet365 was never asked): bet365 takes NFL GAME LINES (spread incl. the alt
# handicap, game total, team total, moneyline) from HyperBot's /v3/markets card, on the same rules as a prop (first at
# equal or better than Sportsbet, a spill for the remainder, the 10% floor, B365_MAX_TO_WIN_NFL). Whole and half
# points for spread / total / team total (v6.254, Wilson 7 Nov: "push is a void at -3.0"); needs B365_NFL_HB_MARKETS.
B365_NFL_GAME_LINES_ENABLED = _env_bool("B365_NFL_GAME_LINES_ENABLED", True)
# v6.250 part B (Wilson 6 Nov 2026, Jaguars team total o24.5 @ 1.89: Sportmarket's board showed 24.5 but both betslips
# refused it, Tarken took $367 and $433 went to manual, because a team-total shortfall is exact-line only and the
# bet365 team-total ladder is sparse): "within 1, 10% floor". An NFL spread / game total / team total with a half-point
# line and a price of the tipster's own, after the tipped line, tries the better number then the worse one (one point
# each) on bet365 AND Sportmarket, at no more than 10% under the TIPPED price. SHORTFALL stage only; the better step
# never takes a near row. False restores the exact-line shortfall exactly.
NFL_GAME_LINE_NEAR_ENABLED = _env_bool("NFL_GAME_LINE_NEAR_ENABLED", True)
# v6.254 (Wilson 7 Nov 2026: "we will only bet up to 4 days ahead of match starting"): an NFL tip whose game kicks off
# further ahead than this goes to manual, on every book. It also catches a late tip the resolver reads as a next game
# over the limit away, but not one inside it (a Sunday game then a Thursday game; v6.255 NFL_LATE_TIP_HOURS covers that).
# 0 or less turns the check off.
NFL_MAX_DAYS_AHEAD = _env_float("NFL_MAX_DAYS_AHEAD", "4")
# v6.255 (Wilson 7 Nov 2026: "thats fine it can go manual", 24 hours): a tip that arrives within this many hours after
# EITHER team's last kickoff goes to manual. The resolver reads a tip over 5 h after a Sunday kickoff as the team's next
# game (Thursday), which is inside the 4-day limit, and a real Thursday tip looks the same. A schedule page that failed
# also goes to manual. 0 or less turns the check off.
NFL_LATE_TIP_HOURS = _env_float("NFL_LATE_TIP_HOURS", "24")
# v6.266 (Wilson 7 Nov 2026: "1.5 floor for default no price floor that gets sent to manual"): a sports bet whose tipster
# gave NO price of his own is not bet under this price, on any book, and the target_odds sent is never under it (1.50 itself
# passes). Only the default: Shook's 1.60, NFL's 1.6 and Eddie's 1.75 no-odds band stay where they are, the higher one
# stands. A tip with a price, racing and the NBA model are untouched; a twin / milestone row keeps its own 5c rule.
# 1.0 or under turns it off.
NO_PRICE_MIN_ODDS = _env_float("NO_PRICE_MIN_ODDS", "1.50")
# v6.270 (Wilson 8 Nov 2026: "no price sgm go manual"): a same-game multi with no price read from its tip is not
# auto-placed, on any path or sport. A tip re-joined into an SGM keeps its first leg's price, and that counts as a price (
# a 2+ HRRBI single turned into an MLB SGM has no tipped price, so it goes manual too).
# False restores the old auto-placement.
NO_PRICE_SGM_MANUAL = _env_bool("NO_PRICE_SGM_MANUAL", True)
# v6.256 (Wilson 7 Nov 2026, Parker Washington o60.5 on 6 Nov: Sportsbet took $272 at 60.5, bet365 had 60.5 at 1.870,
# Sportmarket only 61.5 at 1.892, and best price alone sent all $928 to Sportmarket on 61.5; he finished on 61): after
# Sportsbet fills what it can, the remainder tries the TIPPED line on every book before any book whose line is worse for
# us. Books on a line worse than the tip (an over higher, an under lower, a spread with fewer points for us) go after
# the others; each group stays best price first. A better number, a moneyline and an unreadable line keep today's
# order. Only the order changes: every book keeps its own price rules, bands, caps and line limits. False restores
# plain best-price order.
SHORTFALL_TIPPED_LINE_FIRST = _env_bool("SHORTFALL_TIPPED_LINE_FIRST", True)
B365_NBA_HARDER_RUNG_DROP = float(os.getenv("B365_NBA_HARDER_RUNG_DROP", "0.20"))
B365_NBA_HARDER_RUNG_MAX_ODDS_MULT = float(os.getenv("B365_NBA_HARDER_RUNG_MAX_ODDS_MULT", "1.5"))
B365_MAX_TO_WIN_NBL = float(os.getenv("B365_MAX_TO_WIN_NBL", "500"))
# v6.122 (Wilson 2026-10-26): "bet365 should kick in whenever sb doesnt fully fill the bet even
# if b365 is worse odds/line as long as line is within +-1", milestones +-1 too ("o13.5 points ...
# 15+ points milestone"). The shortfall only; the tipster's odds are the reference. The price band
# is v6.127's below.
B365_SHORTFALL_MAX_LINE_DIFF = float(os.getenv("B365_SHORTFALL_MAX_LINE_DIFF", "1.0"))
# v6.127 (Wilson 2026-10-28: "whatever is left, try on b365 if odds are within 10% if not, go to
# manual"; "10% under tipster, all lines"): one band for the tipped line, an O/U a line away and a
# milestone rung a line away alike (was 20% O/U, 30% milestone either way).
B365_SHORTFALL_ODDS_DROP_PCT = float(os.getenv("B365_SHORTFALL_ODDS_DROP_PCT", "0.10"))
# v6.249 (Wilson 6 Nov 2026, Franz Wagner o29.5 PA: tip 1.80, the card read 1.833, the live price 1.74, HyperBot
# refused "Odds too low: live 1.74 < floor 1.80"): a bet365 spill sends the 10% band's lowest price as its
# target_odds (tip 1.80 -> 1.62), not the tip's own price. The better step is unchanged.
B365_SHORTFALL_BAND_FLOOR = _env_bool("B365_SHORTFALL_BAND_FLOOR", True)
# v6.249 (Wilson 6 Nov 2026: "if theres only that line on tarken/sb, and its spilt over, feel free to go on the
# next milestone UP from the bet that was sent ... odds at 1.1x for each higher increment, eg. if bet was o22.5 at
# 1.8, o24.5 at 2.16 min on b365"): a bet365 SPILL of a priced NBA / NBL / NFL over may take the next milestone
# rung above the tipped line at the tipster's price x (1 + B365_MILESTONE_UP_PER_POINT x points up), LINEAR per
# point (1.80 at o22.5 -> 2.16 at o24.5), with no top price limit (the max_odds ceiling is 10% over the quoted price).
B365_MILESTONE_UP = _env_bool("B365_MILESTONE_UP", True)
B365_MILESTONE_UP_PER_POINT = float(os.getenv("B365_MILESTONE_UP_PER_POINT", "0.10"))
# v6.127 (Wilson 2026-10-28: "if b365 is better, place bet365 and only if theres spill over, then
# we will place sb if sb is within 10% tipped and same or +-1 line"): bet365 better no longer means
# bet365 ONLY (v6.124); what its cap leaves goes to Sportsbet (limited accounts, then the new ones)
# when Sportsbet's price is no more than this under the tipster's and its line this close to the tip.
SB_AFTER_B365_ODDS_DROP_PCT = float(os.getenv("SB_AFTER_B365_ODDS_DROP_PCT", "0.10"))
SB_AFTER_B365_MAX_LINE_DIFF = float(os.getenv("SB_AFTER_B365_MAX_LINE_DIFF", "1.0"))
# v6.124: the legs of one 4th&EV NFL post are placed side by side, at most this many at once
# (a leg that goes to manual alerts in seconds instead of waiting behind the others' fan-outs)
NFL_CARD_CONCURRENT_LEGS = int(os.getenv("NFL_CARD_CONCURRENT_LEGS", "4"))
# v6.37 (Wilson 2026-09-25): an NFL count-stat line 1 worse than tipped (rush/pass attempts,
# completions, receptions) auto-places at this fraction of the stake. 0 disables it.
NFL_WORSE_LINE_STAKE_MULT = float(os.getenv("NFL_WORSE_LINE_STAKE_MULT", "0.75"))
# v6.54 (Wilson 2026-10-02, A1 'Lamar Jackson u7.5 rush attempts' went manual: "sb doesnt
# have rush attempts for lamar, maybe we can stake whatever rushing yards line sb offers but
# at 80% of the recommended stake ... ONLY IF RUSH ATTEMPTS ISNT OFFERED FOR THAT PLAYER").
# v6.133 (Wilson 2026-10-28: "rush attempt with no attempts market should go manual now. retire
# the yards fallback"; Sportsbet added rush attempts mid season): 0 = off
NFL_RUSH_ATT_FALLBACK_STAKE_MULT = float(os.getenv("NFL_RUSH_ATT_FALLBACK_STAKE_MULT", "0"))
# v6.43 (Wilson 2026-09-27): a yardage N+ tip with no exact Sportsbet rung (120+ when SB
# ladders 110+/125+) places on the nearest rung within this many yards, full stake. 0 disables.
NFL_ALT_RUNG_MAX_YDS = float(os.getenv("NFL_ALT_RUNG_MAX_YDS", "5"))
# v6.43: odds ceiling for an NFL 'N+' ladder rung, as a multiple of the tipster's price
# (the global MAX_ODDS_MULT 1.25 refused Garrett Wilson 150+ at SB $12 vs his $9).
# v6.46: 1.5 (Wilson 2026-09-27: "dont make decisions yourself. change to up to 1.5x"; v6.43
# had picked 2.0 on its own).
NFL_ALT_MAX_ODDS_MULT = float(os.getenv("NFL_ALT_MAX_ODDS_MULT", "1.5"))

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

# v6.26 (2026-09-16, Wilson: "i need it to parse the mass tips msg to able to
# extract the proper tips one by one and then place them"). 4th and +EV
# NFL TEXT-TIP PLACEMENT kill-switch, same shape as EDDIE_TEXT_PLACE_ENABLED
# above but real stakes ($400/u) from the moment it ships, per Wilson's
# explicit request -- unlike Eddie's speculative "ships DORMANT" build, this
# went through an xhigh-effort adversarial code review (which found and
# fixed a real blocker: a whole-number spread could have resolved to
# Sportsbet's 3-way "Handicap Betting (Inc. Tie)" market instead of the
# 2-way one the tipster meant) before ever running live. Defaults ON. Kept
# as a real env-gated switch (not just a design note) so a problem in this
# path's FIRST live use can be killed with a .env edit + restart, no code
# change or redeploy needed, mirroring the Eddie precedent's own reasoning
# for a brand-new real-money text-parsing path.
FOURTHANDEV_TEXT_PLACE_ENABLED = _env_bool("FOURTHANDEV_TEXT_PLACE_ENABLED", True)

# v6.32 (Wilson 2026-09-23: "build nbl please"): The Bettors Edge - NBL channel
# (-1003319708262) auto-places on Sportsbet via main._route_bettorsedge_nbl_text
# (NBL-only matching, same concurrent fan-out as NFL). Env-gated so a problem in
# its first live use can be killed with a .env edit + restart. When off, every
# post with a price in it is sent to Wilson as one manual alert instead.
BETTORSEDGE_NBL_PLACE_ENABLED = _env_bool("BETTORSEDGE_NBL_PLACE_ENABLED", True)
# v6.228 (Wilson 3 Nov 2026: "turn off nbl betting for all"; then "ignore completely"): every NBL
# bet is off, on every book (Sportsbet, bet365, Sportmarket) and every route: a Bettors Edge post
# is dropped before it is parsed (log line only, no alert), and every NBL send path refuses
# locally. A new key, so the live .env (which sets BETTORSEDGE_NBL_PLACE_ENABLED=true) cannot turn
# it back on by accident; NBL_BETTING_ENABLED=true in .env + a restart switches NBL back on.
# Off by default everywhere (review: never open because of a stray TIPBOT_TESTING); the root
# conftest turns it on for the suite so the NBL tests keep their meaning.
NBL_BETTING_ENABLED = _env_bool("NBL_BETTING_ENABLED", False)

# 2026-10-10 (Wilson): since 10 Oct The Bettors Edge posts Sportsbet betslip SCREENSHOTS
# (no caption) followed by a units text ("1.5 and 0.5"). Image support in
# main._bettorsedge_nbl_image_dispatch: units come FIRST from the stake typed in the slip
# ("$1.50" = 1.5 units), the follow-up units message is only a fallback (numbers map in
# order to the images still waiting). SHIPS OFF: with it off a no-caption image is a manual
# alert and a units text is dropped as chatter, exactly as before. Enable after validating
# on real screenshots.
BETTORSEDGE_IMAGES_ENABLED = _env_bool("BETTORSEDGE_IMAGES_ENABLED", False)
# Wilson's caps for an image bet. v6.72 (Wilson 2026-10-18: "change nbl bettors edge max units
# to 5"; 17 Oct a 5u Andrew Carr card went wholly to manual against the old 4u): up to TWICE the
# cap places the cap and the rest is a manual alert (units_cap.py); above twice is a misread and
# the whole bet goes to manual. The profit cap (stake x (odds - 1) per bet) also cuts the stake.
# A cap that is not positive sends every image bet to manual.
BETTORSEDGE_IMAGE_MAX_UNITS = _env_float("BETTORSEDGE_IMAGE_MAX_UNITS", "5")
BETTORSEDGE_IMAGE_MAX_PROFIT = _env_float("BETTORSEDGE_IMAGE_MAX_PROFIT", "2000")
# How long an image with no stake on its slip waits for the units message before it goes
# to a manual alert (Wilson: "1 minute").
BETTORSEDGE_IMAGE_UNITS_WAIT_SEC = _env_float("BETTORSEDGE_IMAGE_UNITS_WAIT_SEC", "60")

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
# v6.30 (Wilson 2026-09-20: "turn off kev for now as his tips are for NBL,
# but we haven't set that up yet"). Kev's channel is configured as NBA but
# his actual current tips are for a sport TipBot doesn't parse/place at all
# (NBL) -- kept OFF by default until NBL support exists, mirroring Dello's
# own off-by-default kill-switch. Flip via .env when NBL is wired up.
KEV_NBA_ENABLED = _env_bool("KEV_NBA_ENABLED", False)
AUSBETS_UNIT_SIZE = _env_float("AUSBETS_UNIT_SIZE", "100")
# v6.56: the one Telegram sender tipbot accepts in the AusBets group (see TIPSTER_CHANNELS).
# Blank/0 keeps the old forwarder's id: 0 would switch the sender filter OFF.
AUSBETS_BOT_ID = _env_int("AUSBETS_BOT_ID", "0") if os.getenv("AUSBETS_BOT_ID", "").strip() else 0
AUSBETS_BOT_ID = AUSBETS_BOT_ID or 7964646743
# v6.157 (Wilson 2026-10-29): tipbot reads AusBets on Discord itself (ausbets_discord.py) and
# places from there; the forwarder's posts in the Telegram group are the record only. false =
# back to placing from the group (restart tipbot AND the Discord forwarder).
AUSBETS_DISCORD_DIRECT = _env_bool("AUSBETS_DISCORD_DIRECT", True)
SHOOK_UNIT_SIZE = _env_float("SHOOK_UNIT_SIZE", "300")
TEST_UNIT_SIZE = _env_float("TEST_UNIT_SIZE", "1")

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

# ── NBA props model (tipbot contract v3, machine feed) ────────────────
# nba-v3 branch. The NBA twin of the DisposalsModel block above: the NBA props model posts
# one bet per Telegram message as a fixed 18-field `BET|v3|...` line (parsers/nba_model.py,
# NBA_MODEL_CONTRACT_v3.md), derived from the AFL v2 envelope. Tier 1 = REB UNDER only.
#
# EVERYTHING HERE SHIPS OFF. NBA_MODEL_ENABLED is the kill-switch: the channel is not
# monitored, no NBA code path is reachable, and the AFL / NFL / NBL behaviour is exactly what
# it was without this block. Core logic lives in nba_model_core.py and reads these at call time.
#
# Parsing is STRICT and never crashes startup (an AFL-only deployment with a typo in an NBA_*
# variable must still boot): a bad value falls back to its default, is logged at ERROR and
# recorded in NBA_MODEL_CONFIG_ERRORS, and while NBA_MODEL_ENABLED is true any recorded error
# makes the model REFUSE to place (nba_model_core.placement_refusal).
NBA_MODEL_CONFIG_ERRORS: list = []


def _nba_err(msg: str) -> None:
    import logging as _logging
    NBA_MODEL_CONFIG_ERRORS.append(msg)
    _logging.getLogger("tipbot.config").error("NBA config: %s", msg)


def _nba_flag(name: str, default: bool, unrecognised: bool) -> bool:
    """A boolean env var. Only 1/true/yes/on and 0/false/no/off are recognised (case
    insensitive); empty is `default`; ANYTHING else is an error and yields `unrecognised`,
    which each caller sets to its SAFE value (_env_bool would read 'on' as false)."""
    raw = os.getenv(name, "").strip().lower()
    if raw == "":
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    _nba_err(f"{name}={raw!r} is not a recognised boolean (use true/false); using {unrecognised}")
    return unrecognised


def _nba_float(name: str, default: str) -> float:
    """A finite float env var, else the default (logged). A non-finite value ('nan', 'inf')
    would make every comparison False, silently switching a price gate or a stake cap OFF."""
    import math as _math
    raw = os.getenv(name, default)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = float("nan")
    if not _math.isfinite(v):
        _nba_err(f"{name}={raw!r} is not a finite number; using the default {default}")
        return float(default)
    return v


def _nba_int(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        _nba_err(f"{name}={raw!r} is not an integer; using the default {default}")
        return int(default)


def _nba_in_range(name: str, value, default: str, lo=None, hi=None, lo_open: bool = False):
    """`value` if it lies in the allowed range, else the default (logged and recorded as a
    config error, so the feed refuses to place while it is ON). A syntactically valid but
    absurd value (a 90% worse-gate, 1.01 odds floor, a 0 stake cap) is as dangerous as a typo."""
    ok = ((lo is None or (value > lo if lo_open else value >= lo))
          and (hi is None or value <= hi))
    if ok:
        return value
    _nba_err(f"{name}={value!r} is outside the allowed range "
             f"{'(' if lo_open else '['}{lo}, {hi}]; using the default {default}")
    return type(value)(default) if not isinstance(value, bool) else value


NBA_MODEL_ENABLED = _nba_flag("NBA_MODEL_ENABLED", False, False)
# BET|v4 (Wilson 2026-10-31: the Tier 1 REB Under is judged at Sportsbet AND Pinnacle through the
# Sportmarket bridge, and placed at the book with the higher EV). The model sends v3 unchanged for
# every bet decided at Sportsbet and v4 ONLY when the deciding book is Sportmarket. A v4 line carries
# a per-book floor price (`sb_floor`, `sm_floor`); Sportmarket is asked first at the exact tipped
# line, and Sportsbet takes the remainder only at or above its own floor (a floor of `x` means it
# takes nothing). OFF by default and while off EVERY BET|v4 line is refused to manual with a refused
# fills record ("v4 disabled"). Only 1/true/yes switch it on (_env_bool): anything else reads as OFF.
NBA_MODEL_V4_ENABLED = _env_bool("NBA_MODEL_V4_ENABLED", False)
# v6.234 BET|v5 (Wilson 4 Nov 2026: a v3 line's Sportmarket legs, the better step and the shortfall,
# never checked the model's 2% edge at Pinnacle): v5 is a Sportsbet-decided bet placed exactly as v3,
# plus `sm_floor` (a price, or `x`). EVERY Sportmarket leg of a v5 line places only at a live price at
# or above sm_floor, with the bridge's target_odds never under it; `x`, or a floor that cannot be read,
# means no Sportmarket leg at all. OFF by default and while off EVERY BET|v5 line is refused to manual
# with a refused fills record ("v5 disabled"), as v4 is. Only 1/true/yes switch it on (_env_bool).
NBA_MODEL_V5_ENABLED = _env_bool("NBA_MODEL_V5_ENABLED", True)   # v6.244 on (Wilson 4 Nov); inert until the model sends v5
# Channel + sender id live in .env (never hardcoded). OPEN (PIPELINE-DESIGN 9.5): whether NBA
# signals share the AFL channel or get their own. Both work: a different channel registers its
# own entry; the SAME channel and sender as DISPOSALS_MODEL_* is routed by schema version
# (a `BET|v3|` line goes to the NBA path, `BET|v2|` stays on the AFL path).
NBA_MODEL_TG_CHANNEL = os.getenv("NBA_MODEL_TG_CHANNEL", "").strip()
NBA_MODEL_SENDER_ID = os.getenv("NBA_MODEL_SENDER_ID", "").strip()

# ── Sportmarket bridge (v6.154) ──
# v6.154 (2026-10-29): Sportmarket (a broker: pin88 for props, many books for game lines) is reached
# through a local bridge (sportmarket-bridge repo) that answers HyperBot's four calls for ONE
# account. tipbot treats it as one more HyperBot-style session, reached by hb_router. Empty URL = off:
# no bridge client is built and nothing here changes behaviour. The session id is deliberately not
# numeric so it can never be confused with a HyperBot id.
SPORTMARKET_BRIDGE_URL = os.getenv("SPORTMARKET_BRIDGE_URL", "").strip()          # "" = off
SPORTMARKET_BRIDGE_API_KEY = os.getenv("SPORTMARKET_BRIDGE_API_KEY", "")
SPORTMARKET_SESSION_ID = os.getenv("SPORTMARKET_SESSION_ID", "sportmarket").strip() or "sportmarket"
SPORTMARKET_ACCOUNT_ID = os.getenv("SPORTMARKET_ACCOUNT_ID", "sportmarket").strip() or "sportmarket"
SPORTMARKET_BOOKIE = "sportmarket"
# Tipsters / sports that may use the Sportmarket session (milestone 2 reads these; this milestone
# only defines them). Lower case.
SPORTMARKET_TIPSTERS = set(
    t.strip().lower() for t in os.getenv("SPORTMARKET_TIPSTERS", "ausbets_nba,nba_model").split(",")
    if t.strip()
)
# v6.160 (Wilson 2026-10-30: "turned on for all sports from now"): both lists come from .env; "*" in
# SPORTMARKET_TIPSTERS means every tipster. The bridge refuses a sport it does not carry (NBA only
# today, NFL and tennis in progress there) cleanly and without its browser.
SPORTMARKET_SPORTS = set(
    s.strip().lower() for s in os.getenv("SPORTMARKET_SPORTS", "nba").split(",") if s.strip()
) or {"nba"}
# v6.201 (Wilson 2026-11-01: "yes if higher odds"): an NBA FIRST-HALF spread or total (AusBets NBA)
# goes to the bridge only when Sportmarket's price on the same line is equal to or higher (v6.226) than the best
# HyperBot book's. Default ON; false restores the previous behaviour exactly (no first-half leg ever
# touches the bridge). NBL is never routed (the bridge has no NBL first-half feed).
SPORTMARKET_FIRST_HALF_ENABLED = os.getenv("SPORTMARKET_FIRST_HALF_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")

# v6.223 (Wilson 3 Nov, "do them all"): the kickoff check for NFL / NBL / AusBets NBA sends to Sportmarket,
# the rule the NBA model has had since v6.216: no Sportmarket send from 90 s before the game's scheduled
# start (the bridge's own board start, the catalog start tipbot already read, or the ESPN schedule it
# already cached; the earliest known). The rest of the stake goes to Sportsbet as before. An unknown
# start changes nothing (logged once). Default ON; false restores the previous behaviour exactly.
# v6.224 review: only an exact off word switches it off; an unrecognised value keeps the check ON and warns once.
_KICKOFF_RAW = os.getenv("SPORTMARKET_KICKOFF_CHECK_ENABLED", "true").strip().lower()
if _KICKOFF_RAW in ("0", "false", "no", "off"):
    SPORTMARKET_KICKOFF_CHECK_ENABLED = False
else:
    SPORTMARKET_KICKOFF_CHECK_ENABLED = True
    if _KICKOFF_RAW not in ("1", "true", "yes", "on", ""):
        import logging as _kick_logging
        _kick_logging.getLogger("tipbot.config").warning(
            "SPORTMARKET_KICKOFF_CHECK_ENABLED=%r is not a recognised value (use true/false): "
            "keeping the kickoff check ON", _KICKOFF_RAW[:40])

# v6.208 (Wilson 2026-11-01: "check lower lines for liquidity/higher odds from now on"): before the
# tipped line, a full-game spread / game total / team total tries the bridge's rungs of the SAME
# selection that are a better number for us (over: lower, under: higher, spread: more points) at a
# price that still beats Sportsbet on the tipped line, best number first. Default ON; false restores
# the tipped-line-only step exactly. MAX_TRIES bounds the extra bridge tries (each a betslip read).
SPORTMARKET_BETTER_LINE_ENABLED = os.getenv("SPORTMARKET_BETTER_LINE_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
try:
    SPORTMARKET_BETTER_LINE_MAX_TRIES = max(0, int(os.getenv("SPORTMARKET_BETTER_LINE_MAX_TRIES", "3")))
except ValueError:
    SPORTMARKET_BETTER_LINE_MAX_TRIES = 3
# v6.238 (Wilson 4 Nov 2026: Tarken down, "build it so bet365 and Sportmarket can still bet"): when the AFL /
# NFL fan-out has no usable Sportsbet session, Sportmarket and bet365 take the tip alone under the shortfall
# rules (no better step: no Sportsbet price), best price first, the rest to manual. False restores the manual exit.
SPORTSBET_DOWN_EXTERNAL_ENABLED = _env_bool("SPORTSBET_DOWN_EXTERNAL_ENABLED", True)
# v6.238 (Wilson 4 Nov 2026: MMA on bet365 and Sportmarket): the tipbot Sportmarket MMA moneyline leg.
# v6.253 (Wilson 6 Nov 2026, "yes switch it on"): default ON. The bridge prices and places MMA since its main
# 779e795, and a A$1.75 test bet filled (order 2461783115). False turns the leg off.
SPORTMARKET_MMA_ENABLED = _env_bool("SPORTMARKET_MMA_ENABLED", True)
# v6.251 (Wilson 6 Nov 2026: "a Sportmarket ask with cents goes UP to the next whole dollar", $326.10 asks $327; it may
# pass the target by under $1; a whole-dollar ask that lands a few cents short stays short). Every tipster, in
# main._sm_attempt_inner. A cap still wins (tipbot's liability cap, a tip-level cap, the bridge's own max_stake).
# Default ON; false restores the exact-cents ask.
SPORTMARKET_ROUND_UP_STAKE = _env_bool("SPORTMARKET_ROUND_UP_STAKE", True)
# the walk holds Sportsbet's turn: once this many seconds have passed no new rung starts (the tipped
# line and Sportsbet then go on as before)
try:
    SPORTMARKET_BETTER_LINE_BUDGET_SEC = max(0.0, float(os.getenv("SPORTMARKET_BETTER_LINE_BUDGET_SEC", "90")))
except ValueError:
    SPORTMARKET_BETTER_LINE_BUDGET_SEC = 90.0

# v6.171 (Wilson 2026-10-30): Fergal's Tip Titans tennis tips (titan FB) placed on Sportmarket
# through the bridge (tiptitans_tennis.py). OFF until Wilson says go: while off his tips are dropped
# silently as before. Stake: Wilson bets 4x Fergal's tipped stake (units x $200), so a unit is $800
# (measured: My Bet = units x 800 on every recent FB tip; Wilson 2026-10-30: full stake from the first
# bet).
def _fergal_num(name: str, default: float, lo: float, hi: float) -> float:
    """A positive finite number in (lo, hi]; anything else (NaN, 0, negative, junk) is the default, so
    a bad .env value can never switch a cap off or stop the bot starting."""
    try:
        v = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        v = float("nan")
    if not (lo < v <= hi):            # False for NaN too
        print(f"[config] {name}={os.getenv(name)!r} is not in ({lo}, {hi}]; using {default}")
        return float(default)
    return v


FERGAL_TENNIS_ENABLED = os.getenv("FERGAL_TENNIS_ENABLED", "false").strip().lower() in ("1", "true", "yes")
FERGAL_TENNIS_UNIT_SIZE = _fergal_num("FERGAL_TENNIS_UNIT_SIZE", 800, 0, 5000)
# Units over this place the cap and the rest is a manual alert (Wilson 2026-10-30: "he only really bets
# max 1U total on a single player/match").
FERGAL_TENNIS_MAX_UNITS = _fergal_num("FERGAL_TENNIS_MAX_UNITS", 1.0, 0, 10)
# Hard dollar cap per bet whatever the units / unit size say (Wilson 2026-10-30: "cap fergal at max $1200
# a bet though in case of typos").
FERGAL_TENNIS_MAX_STAKE = _fergal_num("FERGAL_TENNIS_MAX_STAKE", 1200, 0, 1200)
# The Tip Titans price rules: never more than 10% under the tipster's price (tipster x 0.9), and a
# price 1.5x the tipster's or more is a likely wrong match (the racing ceiling).
FERGAL_TENNIS_ODDS_DROP_PCT = _fergal_num("FERGAL_TENNIS_ODDS_DROP_PCT", 0.10, 0, 0.5)
FERGAL_TENNIS_CEILING_X = _fergal_num("FERGAL_TENNIS_CEILING_X", 1.5, 1.0, 5.0)
# Sportmarket step-down (Wilson 2026-11-03, the Sonmez partial: a bridge fill of AUD 85.91 of 800 stopped at
# [odds_below_target] and the rest went manual): after a CONFIRMED partial fill the walk asks Sportmarket
# again for the remainder, each pass at the live price but never under this fraction below the TIPPED odds
# (tipped x (1 - pct), rounded UP to 3 decimals: the bridge orders at 3), at most _PASSES follow-up passes.
# The first pass keeps the FERGAL_TENNIS_ODDS_DROP_PCT logic. Both are read from the environment like the
# neighbouring settings (defaults 0.02 and 5); FERGAL_TENNIS_SM_STEPDOWN_PASSES=0 switches the step-down off.
def _fergal_count(name: str, default: int, lo: int, hi: int) -> int:
    """A whole number in [lo, hi] (0 allowed when lo is 0); anything else (junk, NaN, fractions,
    out of range) is the default."""
    raw = os.getenv(name)
    try:
        v = float(raw if raw is not None else default)
    except (TypeError, ValueError):
        v = float("nan")
    if not (lo <= v <= hi) or v != int(v):
        if raw is not None:
            print(f"[config] {name}={raw!r} is not a whole number in [{lo}, {hi}]; using {default}")
        return int(default)
    return int(v)


FERGAL_TENNIS_SM_STEPDOWN_PCT = _fergal_num("FERGAL_TENNIS_SM_STEPDOWN_PCT", 0.02, 0, 0.5)
FERGAL_TENNIS_SM_STEPDOWN_PASSES = _fergal_count("FERGAL_TENNIS_SM_STEPDOWN_PASSES", 5, 0, 20)

# OPTIONAL dedicated account list for the NBA MODEL ONLY (comma-separated session ids, in
# priority order, parsed exactly like NBA_SESSION_PRIORITY in session_priority.py). NBA_SESSION_PRIORITY
# is shared with the human NBA tipsters; when this is EMPTY the model falls back to it. Malformed
# (a token that is not a plain session number, or only separators) is a CONFIG ERROR: while the
# feed is ON that refuses every placement, and the fan-out also fails closed to manual.
NBA_MODEL_SESSION_PRIORITY = os.getenv("NBA_MODEL_SESSION_PRIORITY", "").strip()
if NBA_MODEL_SESSION_PRIORITY:
    _nba_sp_tokens = [t.strip() for t in NBA_MODEL_SESSION_PRIORITY.split(",") if t.strip()]
    if not _nba_sp_tokens or not all(t.isdigit() or t == SPORTMARKET_SESSION_ID for t in _nba_sp_tokens):
        _nba_err(f"NBA_MODEL_SESSION_PRIORITY={NBA_MODEL_SESSION_PRIORITY!r} is not a "
                 f"comma-separated list of session numbers; placing nothing until it is fixed")

# v6.213 (Wilson 2 Nov 2026: "allow matthew to also bet nba model but only within the 2 hour from
# tip window if the odds are still there"): these sessions are LATE-ONLY for the NBA model. A model
# bet takes them only when the bet ARRIVED (the message being processed, so a model re-send is its
# own arrival) within NBA_MODEL_LATE_WINDOW_MIN minutes of tip-off. A bet that arrived earlier never
# uses them, however long it keeps retrying inside the fan-out; an unknown or non-finite tip time
# drops them. Only the model's own fan-out is gated. The default holds Matthew 319970, so adding him
# to NBA_MODEL_SESSION_PRIORITY is safe from the first second.
# The ONLY way to switch the gate off is the exact word `off` (any case), logged loudly at startup.
# A blank or separators-only value, a non-numeric token, a non-ASCII digit or an absurdly long number
# keeps the default {319970} and logs a WARNING: a typo must never open the gate. It is deliberately
# NOT a config error (NBA_MODEL_CONFIG_ERRORS would refuse all model placement): a bad value here is
# already fail-closed for Matthew, so the other accounts keep betting. Tokens are ASCII digits kept as
# str(int(t)), so "0319970" is 319970.
import logging as _late_logging


def _late_token(t: str):
    """An ASCII-digit token without its leading zeros (the same form as nba_model_core.norm_sid),
    at most 18 significant digits, else None (never raises)."""
    if not (t.isascii() and t.isdigit()):
        return None
    n = t.lstrip("0") or "0"
    return n if len(n) <= 18 else None


_nba_late_raw = os.getenv("NBA_MODEL_LATE_ONLY_SESSIONS", "319970")
_nba_late_tokens = [t.strip() for t in _nba_late_raw.split(",") if t.strip()]
_nba_late_norm = [_late_token(t) for t in _nba_late_tokens]
if _nba_late_raw.strip().lower() == "off":
    NBA_MODEL_LATE_ONLY_SESSIONS = set()
    _late_logging.getLogger("tipbot.config").warning(
        "NBA_MODEL_LATE_ONLY_SESSIONS=off: the NBA model's late-only gate is OFF, every account in "
        "NBA_MODEL_SESSION_PRIORITY bets whatever the time to tip-off")
elif _nba_late_tokens and all(n is not None for n in _nba_late_norm):
    NBA_MODEL_LATE_ONLY_SESSIONS = set(_nba_late_norm)
else:
    # blank / separators only / a bad token: the default group, never an empty gate
    _late_logging.getLogger("tipbot.config").warning(
        "NBA_MODEL_LATE_ONLY_SESSIONS=%r is not a comma-separated list of ASCII session numbers "
        "(or the word off); using the default 319970", _nba_late_raw[:80])
    NBA_MODEL_LATE_ONLY_SESSIONS = {"319970"}
# minutes before tip-off, 0 to 720, fractions fine; anything else is the default 120 with a WARNING
# (not a config error, for the same reason)
try:
    _nba_late_win_raw = os.getenv("NBA_MODEL_LATE_WINDOW_MIN", "120").strip()
    if not _nba_late_win_raw.isascii():          # float() reads full-width / Arabic-Indic digits
        raise ValueError("not ASCII")
    NBA_MODEL_LATE_WINDOW_MIN = float(_nba_late_win_raw)
    if not (0.0 <= NBA_MODEL_LATE_WINDOW_MIN <= 720.0):          # also False for nan
        raise ValueError("outside 0 to 720")
except ValueError:
    _late_logging.getLogger("tipbot.config").warning(
        "NBA_MODEL_LATE_WINDOW_MIN=%r is not a number from 0 to 720; using the default 120",
        os.getenv("NBA_MODEL_LATE_WINDOW_MIN", "")[:40])
    NBA_MODEL_LATE_WINDOW_MIN = 120.0

# The market and tag allow-lists. Config-driven so another prop can be added later, but a
# market added here is still REFUSED at placement until it has a liability ladder
# (nba_model_core.LADDER_CONFIG_NAMES) and a stat mapping (parsers/nba_model.MARKET_TO_STAT).
NBA_MODEL_MARKETS = tuple(
    m.strip().lower() for m in os.getenv("NBA_MODEL_MARKETS", "player_rebounds").split(",")
    if m.strip()) or ("player_rebounds",)
NBA_MODEL_TAGS = tuple(
    t.strip() for t in os.getenv("NBA_MODEL_TAGS", "T1-REB-U2").split(",") if t.strip()
) or ("T1-REB-U2",)


def _parse_ladder(raw: str, default: tuple, record: bool = False) -> tuple:
    """A comma-separated, strictly descending list of positive liabilities, else `default`.
    A malformed .env value falls back to the placeholder, and the placeholder is never
    "confirmed" on its own (see NBA_REB_LADDER_CONFIRMED)."""
    import math as _math
    try:
        vals = tuple(float(x) for x in raw.split(",") if x.strip())
    except ValueError:
        vals = ()
    if (vals and all(_math.isfinite(v) and v > 0 for v in vals)
            and all(a > b for a, b in zip(vals, vals[1:]))):
        return vals
    if raw.strip() and record:
        _nba_err(f"NBA_REB_LIABILITY_LADDER={raw!r} is not a strictly descending list of "
                 f"positive finite numbers; using the placeholder")
    return default


# ===== PLACEHOLDER, NOT WILSON'S NUMBERS =============================================
# The rebounds LIABILITY ladder (PIPELINE-DESIGN 9.2: "open (TBC); build with a config
# placeholder; M4 cannot go live until it is set"). Per-account LIABILITY rungs, top bracket
# first, converted to a stake at the live price: stake = floor(liability / (odds - 1)), the
# same arithmetic as the AFL disposals ladder [124, 99, 74, 50]. The values below are
# DELIBERATELY CONSERVATIVE (about 40% of the disposals ladder). NOTE WHAT IT IS (this differs
# from the AFL ladder): the TOP rung is each account's LIABILITY CAP ON ONE SELECTION for the
# whole life of the bet. tipbot tracks, per selection and per account, the liability already
# carried (confirmed fills at their odds, maybe-landed stakes at the highest allowed fill) and
# sizes every placement and retry to the REMAINING cap; the lower rungs are only the fallback
# when Sportsbet rejects a stake as too high. When every account is at its cap the outcome is
# `capped`. See NBA_MODEL_CONTRACT_v3.md section 6.
#
# And the model REFUSES TO PLACE ANYTHING while NBA_REB_LADDER_CONFIRMED is false (its
# default). To go live: set NBA_REB_LIABILITY_LADDER in .env to Wilson's real values AND set
# NBA_REB_LADDER_CONFIRMED=true. Setting only the flag keeps the placeholder in force; it is
# a deliberate two-step so the flag cannot be flipped without the numbers being looked at.
NBA_REB_LIABILITY_LADDER_PLACEHOLDER = (50.0, 40.0, 30.0, 20.0)
NBA_REB_LIABILITY_LADDER = _parse_ladder(
    os.getenv("NBA_REB_LIABILITY_LADDER", ""), NBA_REB_LIABILITY_LADDER_PLACEHOLDER,
    record=True)
NBA_REB_LADDER_CONFIRMED = _nba_flag("NBA_REB_LADDER_CONFIRMED", False, False)
if NBA_REB_LADDER_CONFIRMED and NBA_REB_LIABILITY_LADDER == NBA_REB_LIABILITY_LADDER_PLACEHOLDER:
    import logging as _logging
    _logging.getLogger("tipbot.config").warning(
        "NBA_REB_LADDER_CONFIRMED=true but NBA_REB_LIABILITY_LADDER is unset or invalid, so "
        "the PLACEHOLDER ladder %s is in force. Set NBA_REB_LIABILITY_LADDER to the real "
        "values. Treating the ladder as UNCONFIRMED: the NBA model will place nothing.",
        NBA_REB_LIABILITY_LADDER_PLACEHOLDER)
    NBA_REB_LADDER_CONFIRMED = False

# Hard per-message stake ceiling (the accidental-extra-zero guard), enforced at PLACEMENT.
# Tier 1 stakes are 1u = $500 (PIPELINE-DESIGN 9.1): $500 / $750 / $1,000 / $1,250 bases and
# $250 add-ons, so the largest legitimate message is $1,250. If the unit size changes, change
# these with it. The per-selection ceiling is the independent backstop across all keys.
NBA_MODEL_MAX_STAKE = _nba_in_range(
    "NBA_MODEL_MAX_STAKE", _nba_float("NBA_MODEL_MAX_STAKE", "1500"), "1500", lo=0, hi=5000,
    lo_open=True)
NBA_MODEL_MAX_SELECTION_STAKE = _nba_in_range(
    "NBA_MODEL_MAX_SELECTION_STAKE", _nba_float("NBA_MODEL_MAX_SELECTION_STAKE", "1500"), "1500",
    lo=0, hi=5000, lo_open=True)
# Test mode: ignore the posted stake and place this flat amount. Defaults ON, like the AFL
# feed, so the first live messages are small whatever the model sends.
#
# FAIL-SAFE DIRECTION: only an explicit 0/false/no/off turns test mode OFF. Anything
# unrecognised (a typo, 'of', 'disabled') is logged as an error and treated as test mode ON,
# the small-stake side. (_env_bool would have read 'on' as false.)
NBA_MODEL_TEST_MODE = _nba_flag("NBA_MODEL_TEST_MODE", True, True)
NBA_MODEL_TEST_STAKE = _nba_in_range(
    "NBA_MODEL_TEST_STAKE", _nba_float("NBA_MODEL_TEST_STAKE", "7"), "7", lo=0, hi=250, lo_open=True)
# Price gates, as the AFL feed: the model refuses to tip under 1.70 (PIPELINE-DESIGN 3.3), so
# tipbot refuses to place under it; live more than WORSE_GATE below the posted price -> manual.
NBA_MODEL_MIN_ODDS = _nba_in_range(
    "NBA_MODEL_MIN_ODDS", _nba_float("NBA_MODEL_MIN_ODDS", "1.70"), "1.70", lo=1.0, hi=5.0,
    lo_open=True)
NBA_MODEL_WORSE_GATE = _nba_in_range(
    "NBA_MODEL_WORSE_GATE", _nba_float("NBA_MODEL_WORSE_GATE", "0.10"), "0.10", lo=0.0, hi=0.5)
# The WORSE gate is measured against the price the selection's FIRST message was posted at
# (kept in the ledger), not against a retry's re-posted quote.
# The HyperBot catalog we price against must start within this many seconds of the pinned
# tip-off (guards a playoff series / home-and-home where one name matches two nights).
NBA_MODEL_EVENT_START_TOLERANCE_SEC = _nba_in_range(
    "NBA_MODEL_EVENT_START_TOLERANCE_SEC",
    _nba_float("NBA_MODEL_EVENT_START_TOLERANCE_SEC", "3600"), "3600", lo=0, hi=21600,
    lo_open=True)
# Refuse a message whose `start` (UTC tip-off) has passed, plus this much slack. Clamped to
# clock-skew size: a larger value would place a bet after tip-off (in play).
NBA_MODEL_START_SLACK_SEC = _nba_float("NBA_MODEL_START_SLACK_SEC", "0")
# Minimum lead before the bets' close: a message arriving closer than this is refused, for EVERY
# kind including retries. v6.216 (Wilson 2026-11-02: "just allow any bets even if within 90
# seconds as long as it is bettable"): the close is Sportsbet's own start (scheduled + 10 min,
# nba_model_core.bet_close_epoch) and the default lead is 0 (was 90 s before the scheduled tip).
# Env configurable; never negative.
NBA_MODEL_MIN_LEAD_SEC = _nba_float("NBA_MODEL_MIN_LEAD_SEC", "0")
# v6.73 (Wilson 2026-10-18: "the nba player prop model should only send tips within 12 hours of
# tipoff"): a BET line (any kind) whose start is more than this far away is refused. NBA model
# only. 0 = no limit.
NBA_MODEL_MAX_LEAD_SEC = _nba_in_range(
    "NBA_MODEL_MAX_LEAD_SEC", _nba_float("NBA_MODEL_MAX_LEAD_SEC", "43200"), "43200", lo=0)
# v6.73 (Wilson: "test on a random one of the 5 accounts ... switch it up per time so its $1
# stakes, test up to 10 times total"): in TEST MODE each bet goes to ONE account, rotating
# through the model's account list, and test mode stops after this many test bets.
NBA_MODEL_TEST_MAX_BETS = _nba_in_range(
    "NBA_MODEL_TEST_MAX_BETS", _nba_int("NBA_MODEL_TEST_MAX_BETS", "10"), "10", lo=1)
NBA_MODEL_TEST_STATE_PATH = os.getenv(
    "NBA_MODEL_TEST_STATE_PATH", "logs/nba_model_test_rotation.json").strip() \
    or "logs/nba_model_test_rotation.json"
if NBA_MODEL_MIN_LEAD_SEC < 0:
    _nba_err(f"NBA_MODEL_MIN_LEAD_SEC={NBA_MODEL_MIN_LEAD_SEC} is negative; using 0")
    NBA_MODEL_MIN_LEAD_SEC = 0.0
# The slack must stay strictly BELOW the lead, or it would reopen the window the lead closes.
if NBA_MODEL_START_SLACK_SEC >= NBA_MODEL_MIN_LEAD_SEC and NBA_MODEL_START_SLACK_SEC > 0:
    _nba_err(f"NBA_MODEL_START_SLACK_SEC={NBA_MODEL_START_SLACK_SEC} is not below "
             f"NBA_MODEL_MIN_LEAD_SEC={NBA_MODEL_MIN_LEAD_SEC}; using 0")
    NBA_MODEL_START_SLACK_SEC = 0.0
if NBA_MODEL_START_SLACK_SEC > 300.0:
    import logging as _logging
    _logging.getLogger("tipbot.config").warning(
        "NBA_MODEL_START_SLACK_SEC=%s is too permissive (it would allow a bet that far PAST "
        "tip-off, i.e. in play): clamping to 300s.", NBA_MODEL_START_SLACK_SEC)
    NBA_MODEL_START_SLACK_SEC = 300.0
# Distinct players per fixture (correlation bound). 0 = OFF: the NBA design specifies no
# per-game cap, so none is invented. Set it once Wilson decides one.
NBA_MODEL_MAX_PLAYERS_PER_EVENT = _nba_int("NBA_MODEL_MAX_PLAYERS_PER_EVENT", "0")
# kind=retry re-offers an under-filled tranche; safe only because the ledger caps cumulative
# placed per selection at the tranche target.
NBA_MODEL_RETRY_ENABLED = _nba_flag("NBA_MODEL_RETRY_ENABLED", True, False)
# v6.110: a kind=retry may RAISE the selection's target (never lower it), up to
# NBA_MODEL_MAX_SELECTION_STAKE, when money lands (the model re-sizes a bet up as its edge grows).
# false = a differing target is always refused, as before.
NBA_MODEL_RETRY_RAISE_TARGET_ENABLED = _nba_flag("NBA_MODEL_RETRY_RAISE_TARGET_ENABLED", True, False)
# HyperBot is OFF 23:00-07:00 SYDNEY time (an explicit zone, not the machine clock). Inside the window nothing can be placed; the model also
# holds signals there, so this is the second guard.
NBA_MODEL_QUIET_START_HOUR = _nba_in_range(
    "NBA_MODEL_QUIET_START_HOUR", _nba_int("NBA_MODEL_QUIET_START_HOUR", "23"), "23", lo=0, hi=23)
NBA_MODEL_QUIET_END_HOUR = _nba_in_range(
    "NBA_MODEL_QUIET_END_HOUR", _nba_int("NBA_MODEL_QUIET_END_HOUR", "7"), "7", lo=0, hi=23)
# Persisted state: per-selection exposure + permanent key dedup + the pid identity registry,
# and the fills file the model reads back (contract section 4).
NBA_MODEL_STATE_PATH = os.getenv(
    "NBA_MODEL_STATE_PATH", "logs/nba_model_state.json").strip()
NBA_MODEL_FILLS_PATH = os.getenv(
    "NBA_MODEL_FILLS_PATH", "logs/nba_model_fills.jsonl").strip()
# v6.71 (Wilson 2026-10-18): NBA model liveness. The model sends a `NOOP|v3|...` heartbeat each
# run (even with no bets); any message from its locked sender counts. On an NBA game day, from
# the end of the quiet window until the day's last tip-off (Sydney), more than
# NBA_MODEL_SILENCE_HOURS without one sends ONE alert per outage (Wilson chose 2h).
NBA_MODEL_LIVENESS_ENABLED = _env_bool("NBA_MODEL_LIVENESS_ENABLED", True)
NBA_MODEL_SILENCE_HOURS = _env_float("NBA_MODEL_SILENCE_HOURS", "2")
NBA_MODEL_LIVENESS_POLL_SEC = _env_int("NBA_MODEL_LIVENESS_POLL_SEC", "900")
NBA_MODEL_LIVENESS_PATH = os.getenv(
    "NBA_MODEL_LIVENESS_PATH", "logs/nba_model_liveness.json").strip() or "logs/nba_model_liveness.json"
# v6.111 (Wilson via the NBA model session: "stop posting into the gc, throw a critical alert if
# the model dies"): the model records its 45-minute heartbeat in this file instead of posting NOOP
# lines to the chat (fields seq, last_beat_at UTC ISO 'Z', last_beat_mode). While a file is set,
# the model is alive if the chat OR the file shows a sign of life inside NBA_MODEL_HEARTBEAT_STALE_MIN
# (the 90 the model session suggested), and the file counts only if it is readable with a fresh
# last_beat_at AND mtime. Empty = the old chat-only detector (NBA_MODEL_SILENCE_HOURS).
# The alert goes to the CRITICAL chat unless NBA_MODEL_LIVENESS_CRITICAL=false (maintenance chat).
NBA_MODEL_HEARTBEAT_FILE = os.getenv(
    "NBA_MODEL_HEARTBEAT_FILE", r"C:\repos\NBA Player Props Model\Data\live\heartbeat.json").strip()
NBA_MODEL_HEARTBEAT_STALE_MIN = _env_float("NBA_MODEL_HEARTBEAT_STALE_MIN", "90")
NBA_MODEL_LIVENESS_CRITICAL = _env_bool("NBA_MODEL_LIVENESS_CRITICAL", True)

# v6.240 (Wilson 4 Nov: "there are no more Leroy tips, take out the Leroy placement logic and old
# settings"): the Late Mail Leroy channel, parser and Betfair BSP placement are gone, and with them
# LEROY_UNIT_SIZE / LEROY_MAX_UNITS / LEROY_BETFAIR_SESSION / LEROY_ENABLED. HyperBot used to launch
# the Betfair session only because Leroy placed on it (hb_launch_sessions added LEROY_BETFAIR_SESSION
# while LEROY_ENABLED). Wilson holds a Betfair balance there, so it keeps launching through this plain
# setting: comma-separated ids, empty = launch none. No bet is placed through it. Drop it when the
# balance no longer needs a live session.
BETFAIR_AUTOSTART_SESSION = os.getenv("BETFAIR_AUTOSTART_SESSION", "99997")

# The Leg Up Australia (v6.72, Wilson 2026-10-18): a racing tips EMAIL. gmail_watcher.py reads a
# dedicated Gmail inbox (Wilson's own inbox forwards The Leg Up to it) and posts the Quick Take
# Pro Tips + Future Tips into LEGUP_TG_CHANNEL; main.py places Pro Tips through the racing
# pipeline (own price shop, the tip's named bookie is ignored). "TO+" (best tote + 10%) tips,
# Future Tips and anything unreadable go to manual. $100/u, max 10u per tip (over that: 10u
# placed, the rest manual). LEGUP_ENABLED is the kill-switch.
LEGUP_ENABLED = _env_bool("LEGUP_ENABLED", True)
LEGUP_TG_CHANNEL = os.getenv("LEGUP_TG_CHANNEL", "").strip()
LEGUP_FORWARD_BOT_TOKEN = os.getenv("LEGUP_FORWARD_BOT_TOKEN", "").strip()
LEGUP_FORWARD_BOT_ID = _env_int("LEGUP_FORWARD_BOT_ID", "0")
LEGUP_UNIT_SIZE = _env_float("LEGUP_UNIT_SIZE", "100")
LEGUP_MAX_UNITS = _env_float("LEGUP_MAX_UNITS", "10")
# v6.75 (Wilson 2026-10-18: "85% name matching with saddle number + the race number as a third
# check if the name doesn't match exactly"): a Leg Up name this similar to the card's runner
# places when the saddle number and race agree (the track always comes from the tip).
# 0 turns it off.
LEGUP_FUZZY_NAME_MIN = _env_float("LEGUP_FUZZY_NAME_MIN", "0.85")
LEGUP_GMAIL_ADDRESS = os.getenv("LEGUP_GMAIL_ADDRESS", "").strip()

# Just Bleed MMA - The Bettors Edge (v6.112, Wilson 2026-10-22): UFC cards, read by the regex
# parser parsers/mma_bleed.py and placed by main._process_mma_post as TEST bets, one bet per tip
# on Sportsbet (session 295510) or bet365 (test session 314653), whichever prices the exact same
# selection higher (bet365 only when strictly better, never both). A tip that is not provably ONE
# exact market on a book is a manual alert, never a guess. MMA_ENABLED is the kill-switch and
# defaults to FALSE: the channel is not even monitored until it is true. The accounts that may
# take an MMA bet are MMA_SESSION_PRIORITY (.env, e.g. 295510,314653); empty -> nothing placed.
MMA_ENABLED = _env_bool("MMA_ENABLED", False)
MMA_ENABLED_REQUESTED = MMA_ENABLED      # what .env asked for; MMA_ENABLED is forced off on a config error
# A malformed MMA_* value must never crash `import config` (an AFL-only deployment with a typo
# here must still boot). Like the NBA block: fall back to the default, log at ERROR, record it
# in MMA_CONFIG_ERRORS, and when MMA was asked for, FAIL CLOSED (MMA_ENABLED -> False, channel
# not registered). main.py announces that at startup.
MMA_CONFIG_ERRORS: list = []


def _mma_err(msg: str) -> None:
    import logging as _logging
    MMA_CONFIG_ERRORS.append(msg)
    _logging.getLogger("tipbot.config").error("MMA config: %s", msg)


def _mma_float(name: str, default: str, lo: float, lo_open: bool, hi: float | None = None) -> float:
    """A finite float env var at or above `lo` (strictly above when lo_open), else the default
    (recorded). A nan / inf / non-positive stake, unit cap or window would switch a guard off."""
    import math as _math
    raw = os.getenv(name, default)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = float("nan")
    if (not _math.isfinite(v) or (v <= lo if lo_open else v < lo)
            or (hi is not None and v > hi)):
        _mma_err(f"{name}={raw!r} is not a finite number {'>' if lo_open else '>='} {lo:g}"
                 f"{'' if hi is None else f' and <= {hi:g}'}; using the default {default}")
        return float(default)
    return v


MMA_TG_CHANNEL = os.getenv("MMA_TG_CHANNEL", "-1003940779426").strip()
if not MMA_TG_CHANNEL.lstrip("-").isdigit():
    _mma_err(f"MMA_TG_CHANNEL={MMA_TG_CHANNEL!r} is not a Telegram channel id")
MMA_UNIT_SIZE = _mma_float("MMA_UNIT_SIZE", "1.0", 0.0, True)          # dollars per unit (a TEST stake)
MMA_MAX_UNITS = _mma_float("MMA_MAX_UNITS", "5", 0.0, True)            # per tip; over it -> alert, no bet (typo guard)
# v6.252 (Wilson 6 Nov 2026: "just bet up to whenever bettable, no cut off time, as MMA time start is often wrong,
# it normally starts later"): the listed start no longer refuses an MMA bet on any book. MMA_START_CUTOFF_ENABLED
# (default OFF) true restores the old rule: nothing inside MMA_MIN_LEAD_SEC of the earliest start (ESPN's or the
# book's). Kept either way: ESPN must still show the fight as not started, and a book start more than
# MMA_START_TOLERANCE_SEC from ESPN's is a different fight.
MMA_START_CUTOFF_ENABLED = _env_bool("MMA_START_CUTOFF_ENABLED", False)
MMA_MIN_LEAD_SEC = _mma_float("MMA_MIN_LEAD_SEC", "300", 0.0, False)   # with the cut-off switch on: never inside this long before the start
MMA_START_TOLERANCE_SEC = _mma_float("MMA_START_TOLERANCE_SEC", "86400", 0.0, True)  # book start vs ESPN card start
# Posted-price floor (Wilson 2026-10-22: 20%): a bet is not placed when the chosen book's price is
# below the tipster's posted price x (1 - this). A book price ABOVE the posted price is always fine.
MMA_MAX_PRICE_DROP = _mma_float("MMA_MAX_PRICE_DROP", "0.20", 0.0, False, 0.9)
MMA_SEEN_TTL_SEC = _mma_float("MMA_SEEN_TTL_SEC", str(7 * 24 * 3600), 0.0, True, 8 * 24 * 3600)  # resend / duplicate memory; seen_bets prunes after 8 days
if MMA_CONFIG_ERRORS and MMA_ENABLED:
    MMA_ENABLED = False
    import logging as _mma_logging
    _mma_logging.getLogger("tipbot.config").error(
        "MMA config: MMA_ENABLED is true but the config is bad (%s); MMA is DISABLED", "; ".join(MMA_CONFIG_ERRORS))

LEGUP_GMAIL_APP_PASSWORD = os.getenv("LEGUP_GMAIL_APP_PASSWORD", "").replace(" ", "")
LEGUP_SENDER = os.getenv("LEGUP_SENDER", "").strip().lower()
# Gmail must show a DKIM pass signed by the sender's domain or a DMARC pass for it (a forged
# From line could otherwise place real money). Off only if a real email shows neither.
LEGUP_REQUIRE_DKIM = _env_bool("LEGUP_REQUIRE_DKIM", True)
LEGUP_POLL_SEC = _env_int("LEGUP_POLL_SEC", "30")  # retry/fallback wait only
# gmail_watcher waits with IMAP IDLE (Gmail pushes new mail within seconds); a full re-check
# still runs at least this often, a safety net for a dropped IDLE connection.
LEGUP_IDLE_SEC = _env_int("LEGUP_IDLE_SEC", "300")
LEGUP_STATE_PATH = os.getenv("LEGUP_STATE_PATH", "logs/gmail_watcher_state.json").strip()

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
# 4th and +EV NFL (2026-09-10, Wilson: "NFL Tips from 4thandEV" -- image-tip
# channel; renamed from an initial "A1" mislabel once Wilson clarified A1
# Fantasy Sports is a SEPARATE, TEXT-only tipster, see A1_FANTASY_UNIT_SIZE
# below). v6.22 (2026-09-11): main._route_image_nfl_tips AUTO-PLACES via
# _attempt_nfl_auto_place when the tip is one of the 4 live-mapped stats and
# the live line is within 2 of the tipster's -- real stakes, this value IS
# read. Anything that doesn't match still falls to the unchanged manual
# alert. $400/u is Wilson's real, live unit size, set directly in .env (the
# "1" below is only the code-level fallback if .env is ever missing the key).
FOURTHANDEV_UNIT_SIZE = _env_float("FOURTHANDEV_UNIT_SIZE", "1")

# A1 Fantasy Sports NFL (2026-09-10, Wilson): a SEPARATE tipster from
# 4th&EV above, TEXT-only, group -4890384626 / bot 8024788433. v6.22
# (2026-09-11): main._route_a1_nfl_text AUTO-PLACES via
# _attempt_nfl_auto_place under the same live-market/line-within-2 rules as
# 4th&EV; A1's rush-attempts tips still never match (Sportsbet doesn't carry
# that market at all) and fall to the unchanged manual alert, same as any
# other non-matching tip. $600/u is Wilson's real, live unit size.
A1_FANTASY_UNIT_SIZE = _env_float("A1_FANTASY_UNIT_SIZE", "1")

# The Bettors Edge - NBL (v6.32). Wilson 2026-09-23: $200/unit (he posts 0.25-5u).
# Real value lives in .env; "1" is only the code fallback if the key is missing.
BETTORSEDGE_NBL_UNIT_SIZE = _env_float("BETTORSEDGE_NBL_UNIT_SIZE", "1")

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
# overlap. Bounded so tips x accounts threads don't explode; was a soft-launch 3,
# now 4 like TT (v6.33). IMAGE_RACING_CONCURRENT=false
# reverts to the sequential loop (kill-switch).
IMAGE_RACING_CONCURRENT = _env_bool("IMAGE_RACING_CONCURRENT", True)
# v6.33 (Wilson 2026-09-23: "place 4 bets even concurrently"): 3 -> 4, same as TT.
IMAGE_RACING_MAX_CONCURRENCY = int(_env_float("IMAGE_RACING_MAX_CONCURRENCY", "4"))

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
# v6.241 (Wilson 4 Nov 2026, 4th and EV edited a Murray 325+ post after it was parsed): one info alert
# when a watched tipster post's TEXT (or image) really changes: tipster, old text, new text, how long
# after posting. ALERT ONLY: nothing is parsed, placed, changed or cancelled (edit_alert.py). Ops settings,
# not betting numbers: the original text of each post is kept in logs/edited_tip_posts.json for 7 days
# (the MMA_SEEN_TTL_SEC resend memory), at most 2000 posts; a post edited with no stored original
# alerts once only within 24 h of posting (the SB_RELINE_STOP_TTL_SEC / SB_LINE_CLOSED_TTL_SEC day).
EDITED_TIP_ALERT_ENABLED = _env_bool("EDITED_TIP_ALERT_ENABLED", True)
EDITED_TIP_STORE_MAX_AGE_SEC = 7 * 24 * 3600.0
EDITED_TIP_STORE_MAX_POSTS = 2000
EDITED_TIP_UNKNOWN_RECENT_SEC = 86400.0

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
# v6.55 (Wilson 2026-10-03, Tip Titans 'Seaside Pond' Gloucester Park R10 was really R9;
# saddle #1 in R10 was 'Seagrass'): when NO bookie finds the tipped horse in the tipped race,
# search the meeting's other races on one fast bookie and move the tip there only if a runner
# with the SAME NAME and the SAME SADDLE NUMBER is in exactly one race ("find in another race
# only if both number and name and price is within bounds"; the price goes through the normal
# shop). RACING_WRONG_RACE_MAX = highest race number searched.
RACING_WRONG_RACE_SEARCH = os.getenv("RACING_WRONG_RACE_SEARCH", "true").strip().lower() in ("1", "true", "yes", "on")
RACING_WRONG_RACE_MAX = int(os.getenv("RACING_WRONG_RACE_MAX", "12"))
# v6.221 (the October measurement: 86 of 265 racing tips had a bookie fail its price check, about 550
# "race not in catalog" reads and 102 timeouts, and bet365's 5-minute cooldown after a failure knocked it
# out of the next tips). Both switches read HyperBot's race list (GET /v3/races, 60 calls a minute, cached
# by racing_pk.py). v6.243 (Wilson 4 Nov: approve as long as tested with a couple of $1 bets): the pk switch is ON, 5 $1 bets by pk
# landed on the right race and showed in pending_bets as fast as track-form bets. The coverage skip stays OFF: on 4 Nov the list
# left bet365 off 12 sampled races and bet365 was pricing 7 of them. false = the old behaviour exactly.
#   RACING_PK_ENABLED: price and place by the race's pk (from the list) instead of track + race + date,
#     only when the list names the race unambiguously; unclear = today's form.
#   RACING_COVERAGE_SKIP_ENABLED: skip a bookie the list does not carry on the race (an instant skip with
#     a logged reason, no cooldown); HyperBot binds books progressively, so it is read from a snapshot no
#     older than RACING_COVERAGE_TTL_SEC. A list that cannot be read = skip nobody.
RACING_PK_ENABLED = _env_bool("RACING_PK_ENABLED", True)
RACING_COVERAGE_SKIP_ENABLED = _env_bool("RACING_COVERAGE_SKIP_ENABLED", False)
RACING_RACELIST_TTL_SEC = float(os.getenv("RACING_RACELIST_TTL_SEC", "120") or 120)
RACING_COVERAGE_TTL_SEC = float(os.getenv("RACING_COVERAGE_TTL_SEC", "45") or 45)
RACING_RACELIST_MAX_PER_MIN = int(os.getenv("RACING_RACELIST_MAX_PER_MIN", "36") or 36)   # limit is 60
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
    "fourthandev_nfl": "nfl",
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
        # v6.56: discord_forwarder.py posts as this same bot (@ausbetsnba_bot,
        # AUSBETS_BOT_TOKEN); the old forward using it stopped 2026-06-16. Only one
        # process should hold that token, or two forwarders could post one tip.
        "bot_id": AUSBETS_BOT_ID,
        "default_units": 1.0,
        "unit_size": AUSBETS_UNIT_SIZE,
        "sport": "nba",
        # v6.157: placed from Discord directly; posts here are never placed.
        "discord_direct": AUSBETS_DISCORD_DIRECT,
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
    # 4th and +EV NFL (2026-09-10, Wilson: "NFL Tips from 4thandEV", initially
    # mislabeled "A1" until he clarified A1 Fantasy Sports is a SEPARATE text
    # tipster -- see the -4890384626 entry below). Image-tip channel, no
    # bot_id (broadcast channel, sender filter intentionally skipped, see the
    # image-tips note above). v6.22 (2026-09-11): main._route_image_nfl_tips
    # AUTO-PLACES via _attempt_nfl_auto_place (live-market match, line within
    # 2, real $400/u stakes); a non-matching tip still falls to the unchanged
    # manual alert. IMAGE_PROMPT_NFL (groq_parser.py) and the market mapping
    # were validated against a real 4th&EV card before this went live (a
    # branded BET/UNITS/BOOKIE-labelled graphic) -- the same bootstrap
    # Eddie/Zak/Trial went through before their own auto-place paths shipped.
    -1004359137317: {
        "name": "4th and +EV NFL",
        "parser": "fourthandev_nfl",
        "default_units": 1.0,
        "unit_size": FOURTHANDEV_UNIT_SIZE,
        "sport": "nfl",
        "image_tips": True,
    },
    # A1 Fantasy Sports NFL (2026-09-10, Wilson): confirmed via ForwardMsgBot
    # lookup -- a real Telegram GROUP (has a bot sender, unlike the broadcast
    # image channels above), TEXT tips, never automated before v6.22 (checked:
    # zero prior code/git history for "a1_fantasy"/-4890384626/8024788433).
    # v6.22 (2026-09-11): main._route_a1_nfl_text AUTO-PLACES via
    # _attempt_nfl_auto_place (real $600/u); a non-matching tip (including
    # every rush-attempts tip, a market Sportsbet doesn't carry at all) still
    # falls to the unchanged manual alert (full raw text + a best-effort
    # headline). Routed to its OWN pipeline (main.py's dispatch checks
    # parser == "a1_fantasy_nfl" before the generic _process_tip catch-all),
    # NOT through buffer_messages/image_tips -- see _route_a1_nfl_text's
    # docstring for why the generic sports pipeline's structured-legs-only
    # manual alert would lose the prose nuance A1's real messages carry.
    -4890384626: {
        "name": "A1 Fantasy Sports NFL",
        "parser": "a1_fantasy_nfl",
        "bot_id": 8024788433,
        "default_units": 1.0,
        "unit_size": A1_FANTASY_UNIT_SIZE,
        "sport": "nfl",
    },
    # The Bettors Edge - NBL (v6.32, Wilson 2026-09-23). A broadcast CHANNEL (no
    # bot sender), NBL only, plain-text posts of 1-2 games each. Routed to its own
    # pipeline (main.py checks parser == "bettorsedge_nbl" before the generic
    # _process_tip catch-all): parse -> NBL-only player/team matching against the
    # live Sportsbet catalog -> the shared NFL/NBL concurrent fan-out.
    -1003319708262: {
        "name": "The Bettors Edge NBL",
        "parser": "bettorsedge_nbl",
        "default_units": 1.0,
        "unit_size": BETTORSEDGE_NBL_UNIT_SIZE,
        "sport": "nbl",
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
# NFL (v6.22, 2026-09-11, Wilson: real stakes from day one -- "make sure we
# auto place if line is within 2"). Read directly by session_priority.py via
# os.getenv; this copy exists only for consistency with the other priority
# vars above.
NFL_SESSION_PRIORITY = os.getenv("NFL_SESSION_PRIORITY", "")

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

# v6.114 (Wilson 2026-10-23: "aus bets plays should be divided equally and placed by the active
# sports sportsbet sessions rather than one by one accounts"): AusBets NBA singles go through the
# same concurrent fan-out as AFL and the NBA model (_place_afl_fanout): the stake is split EQUALLY
# across the active Sportsbet accounts on the NBA singles priority list and all of them fire at
# once, instead of _place_singles_v4 walking the accounts one at a time. Set
# AUSBETS_NBA_CONCURRENT_FANOUT=false to go back to the sequential path. Restart to apply.
AUSBETS_NBA_CONCURRENT_FANOUT = os.getenv("AUSBETS_NBA_CONCURRENT_FANOUT", "true").strip().lower() in ("1", "true", "yes")
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
# if TAB/PointsBet transactions are ever enabled.) v6.88: 71275 (Jocelyn) added, the session was
# deleted (HyperBot 403 "does not belong to this user"), so its old ledger rows stay manual-result.
BET_RECORD_FEED_SKIP_SESSIONS = [s.strip() for s in os.getenv(
    "BET_RECORD_FEED_SKIP_SESSIONS", "71265,78280,76341,71275").split(",") if s.strip()]

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

# ── v6.19: durable already-backed guard for racing ───────────────────
# A racing tip whose horse+market is ALREADY backed for at least the stake this tip
# intends is routed to MANUAL instead of being placed again. Keyed on (runner, market)
# only -- see the long note above bet_ledger.racing_runner_exposure for why track, race
# and date are deliberately NOT in the key (place_racing_tip mutates track and date
# in place, and the ledger stores the post-mutation values).
#
# ON BY DEFAULT: Zak re-posted the same image tips hours later on 2026-07-12, 08-28 and
# 08-30 and they placed a second time, doubling those losses (~$2,525). Every existing
# dedup layer is in-memory and 10-30 min, so none of the three could see it.
RACING_DUP_RUNNER_GUARD = _env_bool("RACING_DUP_RUNNER_GUARD", True)
# v6.97 (Wilson 2026-10-20: "for cownie parallel placement, just like how nfl places across 4/5
# bookies at once ... place across all / max stake etc to fill the cownie tip"): a racing tip's
# first shot goes to EVERY non-TAB account on the best price at once (even share each, each
# sized and capped like a normal leg, one Sportsbet max-stake rebet); the rest is walked as
# before. Kill-switch RACING_SAME_PRICE_FANOUT=false. Titans it applies to: comma list,
# "*" = every racing tip (default "OC" = Cownie on Tip Titans).
RACING_SAME_PRICE_FANOUT = _env_bool("RACING_SAME_PRICE_FANOUT", True)
# v6.205 (Wilson 2026-11-01): a same-price group fills each tied account to its cap in priority order
# (fewer, bigger legs) instead of an even split across every tied account. False = the v6.97 split
# (and the default under TIPBOT_TESTING, so the v6.97 group tests keep their meaning).
RACING_SAME_PRICE_FILL_CAPS = _env_bool("RACING_SAME_PRICE_FILL_CAPS", not os.getenv("TIPBOT_TESTING"))
# v6.219 (Wilson 3 Nov: "yes fill b365 first if odds same"): within a same-price tie the bet365 accounts
# fill first (racing priority order among them), then the rest; a bet365 account that can cover the
# tip alone takes it. False = the v6.205 order exactly. The group order and the cover-alone pick only act
# while RACING_SAME_PRICE_FILL_CAPS is on; the plain-walk bookie pick (bet365 before the other tied
# bookies) and the bet365 session sort (a recent refuser last) use this switch whether it is on or not.
RACING_TIE_B365_FIRST = _env_bool("RACING_TIE_B365_FIRST", True)
# v6.257 (Wilson 7 Nov 2026): after a clean "insufficient funds" refusal on a racing account the walk reads that
# account's balance and sends ONE more bet for the balance (rounded down to the cent) when it is at least $1 and
# under the refused stake. False = give the account up at the refusal, as before.
RACING_RETRY_AT_BALANCE = _env_bool("RACING_RETRY_AT_BALANCE", True)
# v6.263 (Wilson 7 Nov 2026): Betdeluxe s99999 is priced and bet on racing only from the minimum bet law START TIME in
# the race's own state (thoroughbred VIC 07:00 / 09:00 elsewhere, harness 10:00), not from 00:00 on race day; before
# that it gets no quote and the stake goes to the other accounts. False = as before (Betdeluxe all of race day).
BETDELUXE_MBL_START_GATE = _env_bool("BETDELUXE_MBL_START_GATE", True)
# v6.219 review: with the switch on, an account whose placement failed with a session-level error
# (bet365:116964's "[schema_changed] confirmbet ... no 'cp'") within this many seconds goes LAST in a
# tie and is never the single cover-alone bet, so its refused share is not stranded when no tied
# account sat out. Default 6 h. A landed bet on the account forgets it at once.
def _clamp_refuser_window(v: float) -> float:
    """inf / nan / negative -> the 6 h default (inf would make a refuser last forever); capped at 7 days.
    0 stays 0 (nobody counts as a refuser)."""
    if v != v or v in (float("inf"), float("-inf")) or v < 0:
        return 21600.0
    return min(v, 7 * 86400.0)


RACING_REFUSER_LAST_SEC = _clamp_refuser_window(_env_float("RACING_REFUSER_LAST_SEC", "21600"))
RACING_SAME_PRICE_FANOUT_TITANS = os.getenv("RACING_SAME_PRICE_FANOUT_TITANS", "OC")
# v6.212 (Wilson 2 Nov): Dabble (HyperBot session 339917, "WW - Wilson Dabble") is a RACING-ONLY bookie, a
# normal account in the best-price walk. Opt-in = the session being in RACING_SESSION_PRIORITY. On a tie for
# the best price Dabble goes first and on its own (never one leg of a same-price group), takes the whole
# remaining stake, and only what HyperBot says it ACCEPTED counts: the rest goes on to the other accounts.
# DABBLE_RACING_ENABLED=false drops every Dabble session from racing placement (kill switch);
# DABBLE_RACING_SESSION is the one Dabble session id allowed to place (any other Dabble session is dropped).
DABBLE_RACING_ENABLED = _env_bool("DABBLE_RACING_ENABLED", True)
DABBLE_RACING_SESSION = os.getenv("DABBLE_RACING_SESSION", "339917").strip() or "339917"
# How far back to look. v6.19a: was 24h, which MISSED the 2026-09-05 re-sends because
# both sat at 40.2 hours (Suit Of Wands and Common Interest, tipped 09-03 and re-sent
# 09-05). Wilson: "should block all extra rebets on the same horse for the same units
# from the past week". 7 days it is; the same-units key plus the race-date relaxer are
# what keep a genuine next-start from being blocked at this range.
RACING_DUP_RUNNER_LOOKBACK_HOURS = float(
    os.getenv("RACING_DUP_RUNNER_LOOKBACK_HOURS", "168"))
# Tolerance on "already has at least what this tip wants", in dollars. Bookie rounding
# and cap-driven partial fills mean the prior total lands a few cents under the intended
# stake; without this a $600 tip already fully backed at $599.86 would place AGAIN.
RACING_DUP_RUNNER_TOLERANCE = float(
    os.getenv("RACING_DUP_RUNNER_TOLERANCE", "1.00"))

# ── v6.19b: the unfilled/manual alert floor ──────────────────────────
# Was a HARDCODED $10 in two places, which silently swallowed a real 7.3% shortfall:
# 2026-09-04 tip 62918 Snip Of Beauty WIN placed $92.73 of $100.00 with $7.27 unfilled.
# racing_placer even logged "No more eligible bookies, unfilled=$7.27 -> manual", but no
# branch fired because 7.27 < 10, and the placed_stake==0 safety net did not apply either
# because $92.73 HAD gone on. So the tip reported success and the shortfall vanished.
#
# MEASURED over 1,345 completed placements: 109 fell in the silent 0 < unfilled < $10
# band, worth $80.12. The split at $1 is remarkably clean and is why Wilson picked it:
#   * 102 of them are cent-level rounding crumbs, $28.28 total, LARGEST $0.80
#   * 7 are real shortfalls, $51.84 total, SMALLEST $1.00 (up to 100% of the stake)
# A $1 floor therefore surfaces every genuine shortfall while still suppressing the
# rounding noise the $10 floor was built for.
RACING_UNFILLED_ALERT_MIN = float(os.getenv("RACING_UNFILLED_ALERT_MIN", "1.00"))
RACING_NO_SPILL_BOOKIES = set(
    b.strip().lower() for b in os.getenv("RACING_NO_SPILL_BOOKIES", "pointsbet").split(",") if b.strip()
)
# v6.117 (Wilson 2026-10-24: RECONCILE_SPILL switched on): the sports twin of the racing carve-out
# the June 2026 reconcile design made a PREREQUISITE for the sports spill. A bookie whose bets can
# sit in trader review past the ~30 s pending_bets window (Pointsbet always; TAB "intercept") can
# land a bet that read as absent, so a confirmed absence on these never re-places the stake on
# another account: it stays maybe placed. Comma list, lower case.
SPORTS_NO_SPILL_BOOKIES = set(
    b.strip().lower() for b in os.getenv("SPORTS_NO_SPILL_BOOKIES", "pointsbet,tab,bet365,sportmarket").split(",") if b.strip()
)
# v6.120: bet365 bets never appear in /api/pending_bets (live tests 2026-10-19), so an "absent" there
# proves nothing: bet365 is ALWAYS in the set, whatever .env lists.
SPORTS_NO_SPILL_BOOKIES.add("bet365")

# ── Handicap-SGM safety (2026-05-31) ─────────────────────────────────
# Route any SGM that contains a handicap (line / first_half_line) leg to MANUAL
# instead of attempting placement. Handicap legs inside SGMs (pick_own_line
# resolution) have been unreliable; until that's hardened, alert rather than
# risk a mis-placed leg. Set false to re-enable auto-placement of handicap SGMs.
AUTO_MANUAL_HANDICAP_SGM = os.getenv("AUTO_MANUAL_HANDICAP_SGM", "true").strip().lower() in ("1", "true", "yes")

# ── Multi-leg tip that is not an SGM -> MANUAL (Wilson 2026-10-31) ───
# Every non-SGM placement path reads tip.legs[0] only, so a parser bug that yields 2+ legs with
# is_sgm=False would put the WHOLE stake on leg 1 and silently drop the rest. place_tip sends such
# a tip to the Manual Bets chat instead (the retired ETR fan-out had the same guard). No parser
# builds this shape today. Set false to restore the old behaviour (not recommended).
def _multi_leg_guard_on(raw) -> bool:
    """Fails CLOSED: the guard is off only for an explicit 0/false/no/off (any case, stripped);
    unset, empty or a typo ('flase') keeps it on."""
    return (raw or "").strip().lower() not in ("0", "false", "no", "off")


MULTI_LEG_NON_SGM_TO_MANUAL = _multi_leg_guard_on(os.getenv("MULTI_LEG_NON_SGM_TO_MANUAL"))

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

# The Leg Up (v6.72): registered only once LEGUP_TG_CHANNEL AND the forward bot's id are set
# and the feed is on. Fails CLOSED without the sender lock (review H1): with no bot_id any
# member of the group could post a "THE LEG UP" message and place real money.
if (LEGUP_ENABLED and LEGUP_TG_CHANNEL.lstrip("-").isdigit()
        and LEGUP_FORWARD_BOT_ID > 0):
    TIPSTER_CHANNELS[int(LEGUP_TG_CHANNEL)] = {
        "name": "The Leg Up",
        "parser": "legup",
        "bot_id": LEGUP_FORWARD_BOT_ID,
        "default_units": 1.0,
        "unit_size": LEGUP_UNIT_SIZE,
        "sport": "racing",
    }

# Just Bleed MMA (v6.112): a broadcast CHANNEL (admins only post), so no bot_id (like Leroy).
# Registered ONLY when MMA_ENABLED: with it off the channel is not monitored at all.
if MMA_ENABLED and MMA_TG_CHANNEL.lstrip("-").isdigit():
    TIPSTER_CHANNELS[int(MMA_TG_CHANNEL)] = {
        "name": "Just Bleed MMA",
        "parser": "mma_bleed",
        "default_units": 1.0,
        "unit_size": MMA_UNIT_SIZE,
        "sport": "mma",
    }

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

# A1 Fantasy Sports NFL -- kill-switch A1_NFL_ENABLED (v6.60, Wilson 2026-10-10: "turn
# off A1 all bets"). false = the channel is not monitored at all (no auto-place, no
# manual alerts), same pattern as Kev below. Default true so an unset var changes nothing.
A1_NFL_ENABLED = _env_bool("A1_NFL_ENABLED", True)
if not A1_NFL_ENABLED:
    TIPSTER_CHANNELS.pop(-4890384626, None)

# Kev NBA — gated on KEV_NBA_ENABLED (kill-switch, default OFF, v6.30): his
# real current tips are for NBL, which TipBot has no parser/placement path
# for at all, so the channel is NOT monitored until Wilson flips this (once
# NBL support exists) or confirms he's back to posting NBA.
if KEV_NBA_ENABLED:
    TIPSTER_CHANNELS[-4634143283] = {
        "name": "Kev NBA",
        "parser": "kev_nba",
        "bot_id": 8085856336,
        "default_units": 1.0,
        "unit_size": KEV_UNIT_SIZE,
        "sport": "nba",
    }

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
# A sender id of 0 is all digits but FALSY: the handler's `if expected_bot and ...` would then
# skip the sender lock entirely, so 0 is refused here (a non-zero id is required).
if (DISPOSALS_MODEL_ENABLED
        and DISPOSALS_MODEL_TG_CHANNEL.lstrip("-").isdigit()
        and DISPOSALS_MODEL_SENDER_ID.lstrip("-").isdigit()
        and int(DISPOSALS_MODEL_SENDER_ID) != 0):
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

# NBA props model (nba-v3). Registered ONLY when NBA_MODEL_ENABLED; with it off this block
# does nothing and TIPSTER_CHANNELS is exactly what it was. bot_id is REQUIRED for the same
# reason as above (the sender filter is the only thing stopping another group member's BET|
# line placing real money). A channel already registered by another tipster is never
# overwritten: the one allowed overlap is the DisposalsModel channel with the SAME sender,
# which is flagged `nba_model_shared` and routed per message by schema version in main.py.
if NBA_MODEL_ENABLED:
    import logging as _logging
    if (NBA_MODEL_TG_CHANNEL.lstrip("-").isdigit()
            and NBA_MODEL_SENDER_ID.lstrip("-").isdigit()
            and int(NBA_MODEL_SENDER_ID) != 0):
        _nba_cid = int(NBA_MODEL_TG_CHANNEL)
        _nba_sender = int(NBA_MODEL_SENDER_ID)
        _nba_existing = TIPSTER_CHANNELS.get(_nba_cid)
        if _nba_existing is None:
            TIPSTER_CHANNELS[_nba_cid] = {
                "name": "NBAModel",
                "parser": "nba_model",
                "machine_feed": True,
                "nba_model": True,
                "bot_id": _nba_sender,
                "sport": "nba",
                "unit_size": 1.0,
                "default_units": 1.0,
            }
        elif (_nba_existing.get("parser") == "disposals_model"
                and _nba_existing.get("bot_id") == _nba_sender):
            _nba_existing["nba_model_shared"] = True
        else:
            _logging.getLogger("tipbot.config").error(
                "NBA_MODEL_ENABLED=true but channel %s is already registered to %r with "
                "bot_id %r (NBA sender %s): NBA channel NOT registered. Use a different "
                "channel, or the same channel AND the same sender as the AFL feed.",
                _nba_cid, _nba_existing.get("name"), _nba_existing.get("bot_id"), _nba_sender)
    else:
        _logging.getLogger("tipbot.config").error(
            "NBA_MODEL_ENABLED=true but NBA_MODEL_TG_CHANNEL=%r / NBA_MODEL_SENDER_ID=%r are "
            "not both numeric: channel NOT registered, the NBA feed is NOT being monitored. "
            "Both are required.", NBA_MODEL_TG_CHANNEL, NBA_MODEL_SENDER_ID)

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
