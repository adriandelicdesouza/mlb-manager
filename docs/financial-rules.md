# League financial rules (v0.9.0)

Every league save carries its own financial rules. The default is the actual
MLB structure: **no salary cap, no salary floor, a Competitive Balance Tax
(CBT), revenue sharing and a league minimum salary**. Leagues can switch to
custom rules (soft or hard cap, floor, different tax) when they are created or
when they are loaded.

All money is in $ millions inside the game (`0.78` = $780,000). Rules live in
`src/mlb_manager/finance.py`; nothing else hard-codes a cap, a floor, a tax or a
minimum salary.

## Standard MLB rules (default)

| Setting | 2026 value |
| --- | --- |
| Salary cap | None (unlimited payroll) |
| Salary floor | None |
| Competitive Balance Tax | Enabled, $244M threshold |
| Tax rates | 20% / 30% / 50% for the 1st / 2nd / 3rd-or-later consecutive season over |
| Surcharges | +12% on the overage beyond $20M, +42% beyond $40M, +60% beyond $60M |
| Draft penalty | $40M+ over: the club's top first-round pick moves back 10 places (top-6 picks protected) |
| CBT payroll | Contract AAV, prorated by days on the roster, + dead money + player benefits (~$17M) |
| Revenue sharing | 48% of local revenue pooled and split equally |
| Minimum salary | $780,000 |
| Hard payroll restrictions | None |

The rules are **season-aware**. The MLB table covers the 2017–2021 CBA
(thresholds $195M–$210M, minimums $535,000–$570,500, 42.5%/45% second
surcharge) and the 2022–2026 CBA (thresholds $230M, $233M, $237M, $241M, $244M;
minimums $700,000–$780,000). A league started in, say, 2024 plays under the 2024
terms and moves to the 2025 and 2026 terms as the seasons pass.

**After 2026.** The 2022–2026 CBA expires on December 1, 2026 and no successor
agreement has been finalised. Seasons from 2027 on use the 2026 terms, marked
*provisional* in every rules summary.

**Proposals are not rules.** MLB has proposed a salary cap and a salary floor
for 2028. They are kept in `finance.PROPOSED_TERMS` and shown as "Proposed – not
adopted" in rule summaries. They are never applied to a league. A user who wants
a cap or floor can choose custom rules.

## Custom rules

| Option | Meaning |
| --- | --- |
| No salary cap | Payroll is unlimited. Only the CBT (if enabled), budgets (CPU) and the floor apply. |
| Hard cap | No transaction may take a club's payroll above the cap. Moves that do not add payroll are always allowed, so a club already over the cap can still shed salary. League-mandated signings (draft picks, minimum-salary depth fills) are exempt but flagged. |
| Soft cap | Exceeding the cap is allowed only through enabled exceptions: re-signing/extending/arbitration with your own players, minimum-salary contracts, draft contracts, and trades that take back ≤ the matching share (default 125%) of outgoing salary. At season end the season-average payroll above the cap is charged at the penalty rate (default 100%). |
| Salary floor | At season end a club whose season-average payroll is below the floor pays the shortfall × the charge rate (default 100%). Moves that drop payroll below the floor are allowed but warned. CPU clubs below the floor get budget priority. |
| CBT | Threshold, the three consecutive-year rates; surcharges and the draft penalty follow the agreement used as the template. |
| Revenue sharing | On/off and the pooled share. |
| Minimum salary | The league minimum for new contracts. Existing contracts below a raised minimum are lifted when the next season starts. |

Inputs accept `244`, `$244M`, `244,000,000`, `780k` or `$780,000`. Percentages
accept `50%`, `50` or `0.5`. Validation is centralised in
`finance.validate_rules()`. It covers amount ranges, a floor below the cap, rates
that do not decrease for repeat offenders, a cap that can pay a 26-man roster
the minimum, and so on.

## How transactions are checked

`finance.check_transaction(league, team, kind, added, ...)` is the single
gate. It is used by free agency, re-signing, extensions, arbitration, trades,
the draft and depth signings. It returns `allowed`, a `reason` when blocked,
`warnings` (soft-cap exception used, floor shortfall, projected CBT bill and
its change, draft-penalty trigger) and the exception used. UIs show these
messages, and the trade screen lists them per club with the projected tax
before and after the trade.

Transaction kinds are `free_agent`, `re-sign`, `extension`, `arbitration`,
`trade`, `draft` and `minimum`.

## Payroll measures

* **Payroll** (cap, floor, displays): current-season cash salaries of players
  under contract plus dead money. Pending free agents in the offseason are not
  counted.
* **CBT payroll**: contract AAV accrued each regular-season day, which prorates
  it by time on the roster, plus dead money and the player-benefits charge.
  Contracts of 3+ years are back-loaded (cash rises 5% of AAV per season around
  the AAV), so cash and CBT payroll genuinely differ. A deadline acquisition
  counts only for the days he is with the club.
* During the offseason, transactions count toward the **next** season
  (`finance.contract_season`).

## Season settlement

When the World Series ends (`finance.settle_season`), for every club:
local revenue (market index × win percentage, plus postseason rounds), central
revenue, the revenue-sharing contribution and distribution, the CBT bill
(consecutive-year counter updated: it increments when over and **resets to
zero after a season under**), the soft-cap penalty and the floor shortfall.
The result is a ledger row kept for 12 seasons. The user sees the bill in
the news and in *Front Office › League finances*.

## CPU behaviour

CPU clubs spend against an owners' **payroll budget**: projected net revenue
(after revenue sharing) × a market-dependent share × a strategy factor, raised
to the floor and limited by a cap. Payroll **plus projected luxury tax and
soft-cap penalty** must fit the budget, so revenue sharing and the CBT change
spending without hard limits. Budgets are a guide only for the user's club.

## Changing rules on an existing league

The load screen (GUI and CLI) shows the save's current rules and lets you edit
them. A change:

* applies from the current contract season (the next season during the
  offseason). Earlier seasons keep their rules as separate *eras*;
* never modifies contracts, payrolls, dead money, CBT streaks, ledgers or
  history;
* is previewed first (`finance.preview_change`): which clubs are over a new
  cap or under a new floor, your projected tax before and after, minimum-salary
  lifts, and budget effects;
* is recorded in the league's rule-change log.

## Saves and migration

Save format **v5** stores `league.finance` (rule eras, legacy cap, CBT
streaks, accruals, ledger, draft penalties, change log) and each player's
`contract_aav` / `contract_raise`. Saves from v1–v4 load with the standard MLB
rules of their season:

* contracts and history are unchanged;
* each AAV equals the existing (flat) salary;
* mid-season CBT accruals are estimated from today's payroll;
* the old hard cap (default $125M) is remembered, and the load screen offers to
  restore it.

## Extending for a future CBA

Add an `Agreement` to `finance.AGREEMENTS` (seasons, thresholds, minimums,
rates, surcharges). Seasons it covers stop being provisional. If the new
agreement introduces a cap or floor, give it `cap_type`, per-season `caps` and/or
`floors`. Every check, CPU budget and UI summary then picks them up. No other
module needs to change.
