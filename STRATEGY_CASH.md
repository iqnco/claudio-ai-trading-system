# CashMoney investment policy (the careful account)

Luck's job is to find fast moves and cut them quickly. This account's job is different:
**keep the money, compound it, and never need rescuing.** Slower, fewer decisions, wider
stops, longer holds. One notch less risk than Luck, not two — the trailing stops and the
asymmetry stay.

## What this account is not
Not a second copy of Luck. No momentum breakouts, no oversold bounces, no post-earnings
gaps. Those pay for quick reflexes, and quick reflexes cost money here.

## Account and hard limits (enforced in code, not negotiable)
- Schwab cash account "CashMoney" only. Long only. Settled cash. No margin, shorting,
  options, leveraged or inverse ETFs, OTC, or stocks under $10.
- **The owner's own holdings are invisible to this strategy.** HOLDA, HOLDB, HOLDC and HOLDD
  are his positions, his convictions, his calls. They are excluded from the account value
  the strategy sizes against, and it never buys or sells them. The one exception is a
  protective stop where he has asked for one (HOLDC), which only ever moves up.
- Every position the strategy opens has a live stop order at Schwab from the moment it is
  bought. Stops only move up.
- 1% of managed equity risked per position (Luck risks 1.5%).
- Maximum 4% of managed equity at risk across all positions at once.
- Maximum 8 satellite positions, 12% of managed equity each. In practice the 4% heat
  cap allows four full-size positions at a time; room for the fifth appears when a
  winner's stop ratchets up and stops consuming risk budget.
- At least 2% of the account is never spent. Being under-invested is allowed here.
- Maximum one new position per day, four per month. Haste is the enemy here.
- Drawdown limit: **-8% from the account's peak halts everything** until the owner resumes.
  A -4% day pauses new buying until tomorrow.

## Shape of the account
**A core, plus satellites, plus cash that earns something.**

1. **Core (target 45% of managed equity).** A broad index ETF, held while the index is in
   an uptrend: its monthly close above its 200-day average. When it closes a month below,
   the core is sold and waits; it returns on the first monthly close back above, with a 2%
   buffer so a wobble around the line doesn't cause repeated round trips. Checked once a
   month, on purpose — this rule is famous for being ruined by people who check it daily.
   The core is not stop-protected and is not counted in the risk budget: the monthly
   trend rule is what protects it. It is rebalanced back to target only when it has
   drifted more than 10% away from it, so ordinary drift costs nothing in commissions
   or taxes. The fund is VOO (S&P 500, 0.03% a year).
2. **Satellites (up to 8 positions, 12% each).** Individual companies bought on the value
   rules below.
3. **Idle cash.** Cash sitting in the sweep earns almost nothing. Whatever is not deployed
   belongs in a money market fund. The strategy reports the idle balance and what it is
   costing; moving it is the owner's action, not an automated one.

## What it buys (one setup, deliberately)
**Quality at a discount, already turning up.** All five must be true:

1. **Cheap against its own sector.** P/E at or below 80% of the sector median, positive
   earnings.
2. **Growing.** Revenue not shrinking over the trailing year, earnings growth positive.
   Earnings are the hard test here; a flat-revenue business earning more is fine.
3. **Not fragile.** Debt-to-equity under 150%. No going-concern or dilution stories.
4. **In an uptrend.** Price above its 200-day average; the 200-day average no lower than
   it was a month ago; and price no more than 30% above it, because that is a momentum
   trade wearing a value label. This is the rule that separates "cheap" from "falling and
   cheap for a reason", and it is the single most important line in this document.
5. **Liquid and real.** Market cap above $2B, at least $20M traded a day, listed three
   years or more.
6. **Not a cycle at its peak.** P/E no lower than 35% of the sector median, earnings growth
   under 120% year on year, the most recent quarter's net margin no worse than three
   quarters of the trailing year's, and return on equity above 8%.

Being cheap is never enough on its own. A stock below its 200-day average is not a bargain
this account is allowed to catch, however good the story.

### Why rule 6 exists

A bare P/E screen reliably finds commodity producers at the top of their earnings cycle. A
gold miner after gold has run shows earnings up 700%, a net margin of 37% and a P/E of 7 —
and the P/E is small because the numerator is peaking, not because the market missed
something. Then earnings normalise and the discount turns out to have been arithmetic. The
first time this screen was run live it returned three gold miners, a tanker operator and an
aluminium producer in its top eight. Rule 6 is what was added in response, and it removed
seven of those eight.

The same logic sets the floor on cheapness. A P/E a third of its sector's is not an
oversight; it is the market pricing in an earnings collapse, and the market is usually right
about that.

## How it sells
- **Initial stop:** 2.5 ATR below entry, never more than 18% away. Wide on purpose — the
  positions are meant to survive ordinary noise for months.
- **Breakeven:** once a position closes at +1R, the stop moves to the entry price.
- **Trailing:** above +1R, the stop trails 2% under the 200-day average, and never moves
  down. Slow, and meant to be: this is what lets a good position run for a year.
- **No partial sales.** Luck sells a third at +3R. This account does not: the trailing
  stop is the profit taker, and a position sold in pieces cannot become the one that pays
  for the other seven.
- **No time stops.** A position that goes nowhere for three months but holds its trend is
  doing nothing wrong.
- **Earnings are held through.** Positions are small and the horizon is long; dodging every
  report would mean trading constantly, which is the opposite of the point.
- **Thesis breaks sell it.** Fraud, investigation, guidance withdrawn, dilution, a debt
  problem, an acquisition at a fixed price: out, at the next open, regardless of price.
- **The trend breaking sells it.** A monthly close below the 200-day average ends the
  position even if the stop has not been hit.

## Market regime
- **Uptrend** (index above its 200-day average): core invested, satellites active.
- **Downtrend** (index below its 200-day average on a monthly close): core sold, no new
  satellite positions. Existing satellites keep their trailing stops and are allowed to
  work. The account is permitted to be mostly cash for months at a time; that is a
  position, and in this account it is often the right one.

## The portfolio manager is the last step

Nothing is bought that the AI has not approved. The rules engine screens, sizes and computes
stops; the last thing that happens before any order exists is a call that reviews every
surviving candidate and can reject it. **If that call fails, nothing is approved** — the
engine never falls through to the raw screen's output. A mechanical screen with no judgment
on top of it is how an account ends up owning six versions of the same bet.

## How the AI should think here
- **Patience is the edge.** The best week is usually the one with no trades. Never
  recommend a position to "put cash to work". Cash in a money fund is a fine outcome.
- **Judge the business, not the chart pattern.** Is this company cheap because the market
  is wrong about its future, or because its future is genuinely worse? Say which, in one
  sentence, and say what would prove you wrong.
- **Respect the trend filter absolutely.** If a company is a screaming bargain below its
  200-day average, the answer is "not yet", not "an exception".
- **Never touch the owner's four holdings.** They are not in your book.
- **Concentration is the risk that kills careful accounts.** Two positions with the same
  driver are one position with twice the size.
- **Say when nothing qualifies.** A memo that reads "nothing this week, here's why" is a
  good memo.

## What the owner sees
- A weekly email: what it owns, where every stop sits, what it is watching, what it
  rejected and why, idle cash and what that cash is costing.
- An alert only when something needs him: a stop moved a lot, a thesis broke, the trend
  filter flipped, the drawdown limit tripped.
- Monthly: the core's trend check, stated plainly, with the decision it implies.

## What this policy cannot promise
The value screen has never been tested on history, because the fundamentals available to
this system are today's numbers, not what was known at the time. The trend filter, the
stops and the core timing rule can be and have been tested. Treat the stock selection as
a judgment call wearing a rulebook, and size it accordingly — which is exactly why each
position risks 1% instead of 1.5%, and why the core exists.

## How it runs (as of 2026-09-24)

The engine is built and tested but **fenced off from the account**. It runs as a second
profile of the same code: `CLAUDIO_PROFILE=cash` swaps in this rulebook, its own database,
and — deliberately — an account list that comes only from its own environment, so it cannot
inherit Luck's account by accident. With no account granted it fails closed: no balance, no
orders, nothing.

Until the owner grants access it runs on demand and only reports:

    CLAUDIO_PROFILE=cash CLAUDIO_DB=data/cash.db venv312/bin/python -m engine.cashrun \
        --notional 25000 --email

That prints (and optionally emails) exactly the memo it would produce live: the monthly core
decision with the numbers behind it, every name that passed the screen, what each position
would be at that account size, what would be rejected and why, and what the idle cash is
costing.

Going live is three things, in this order: add the account's last four digits to the cash
engine's `CLAUDIO_ACCOUNTS`, set `CLAUDIO_TRADING=1`, and turn on its schedule. Until all
three happen this strategy cannot place an order, whatever it decides.
