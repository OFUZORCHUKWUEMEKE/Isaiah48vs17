# Intelligence plan

Status: stage 1 in progress. Stages 2+ not started.

## The gap this closes

The bot records what it decided (`PaperLedger`: symbol, strategy, tier, score,
entry/exit price) and nothing about *why*, and nothing about tokens it
rejected. `PaperPosition` doesn't store the feature values the rule engine
actually scored — `top10_pct`, `pro_traders`, `volume_spike_ratio`,
`fib_retracement`, etc. — so even the trades that were taken can't be
analyzed against their own inputs after the fact.

Consequences of this, concretely:

- Every threshold in `config.json`'s `rules` block (`min_pro_traders: 5`,
  `min_5m_to_1m_ratio: 0.5`, ...) is a guess. Several were literally
  calibrated against fabricated constants before the GMGN integration
  (see `docs/gmgn-integration-plan.md`) — there was no live data to
  calibrate against, ever.
- There is no way to tell whether a rejected token would have won. The
  bot only ever sees outcomes for the narrow slice of tokens it already
  chose to alert on — massive survivorship bias built into the only data
  we have.
- "Is this bot actually good" has no evidence-based answer, only vibes
  from watching Telegram.

None of this requires machine learning to start fixing. It requires
recording decisions and their outcomes, in one place, in a form that can
be queried. ML (stage 5) is the *last* stage, not the first, and it's
worthless without stages 1-3 already running.

## Stage 1 — Decision journal (this PR)

Record the full feature snapshot + verdict for every token the bot
evaluates, pass or fail. Implemented as `DecisionJournal`
(`src/agent/journal.py`), appending one JSON line per evaluation to
`data/decisions.jsonl` (gitignored, like the paper ledger and wallet
scores — Railway rebuilds it from scratch on each deploy for now; that's
a real cost, addressed in stage 2's storage note below).

Two record kinds, since the pipeline has two rejection points
(`_evaluate_candidates` in `loop.py`):

- **`momentum_reject`** — token failed `passes_momentum_filter` before
  ever reaching the rule engine. Cheap, high-volume, low information
  (structural filter, not a strategy judgment) — recorded anyway because
  it's nearly free and stage 3's calibration report needs to know the
  full funnel, not just what survived to scoring.
- **`evaluated`** — token reached `RuleEngine.evaluate()` and got a real
  `Verdict` (pass or fail, whichever strategy scored highest). This is
  the useful record: full feature vector, which strategy fired, its
  score, and every reason/failure string the engine produced.

Each record carries a stable `id` so stage 2 can attach an outcome to it
without re-deriving anything. Nothing about tokens that don't even reach
`_evaluate_candidates` (e.g. malformed discovery results with no
address) is recorded — there's no decision to journal there.

This stage changes no runtime behavior. It only writes data. If the
journal write fails for any reason, it logs and moves on — a full scan
cycle must never fail because journaling did.

## Stage 2 — Outcome tracking

For every journaled token (alerted *and* rejected), sample price/mcap at
fixed horizons after the decision — 15m, 1h, 4h, 24h — and attach the
result to that record. This is what turns "we rejected this" into "we
rejected this and it did +340% in an hour" or "-90%", the only way to
tell if a rejection threshold is wrong.

Needs a scheduled job (reuse the existing `apscheduler` dependency
already in `requirements.txt`) that walks journal entries needing a
not-yet-filled horizon and fetches current price for their address via
whichever data source is enabled (GMGN first, DexScreener fallback —
same precedence as enrichment).

Storage note: `data/decisions.jsonl` growing unboundedly and being wiped
on every Railway redeploy (per the current `.gitignore` policy) becomes
a real problem once stage 2 depends on it for a rolling multi-day
window. Before stage 2 ships, either stop gitignoring it (accept the
git-history growth) or move it off the ephemeral filesystem (a small
SQLite file mounted on a Railway volume, or the free tier of a hosted
Postgres). Decide this only when stage 2 is actually being built —
premature to lock in now.

## Stage 3 — Calibration report (`/report` command)

Once stage 2 has outcomes, compute without any ML:

- Per-rule hit rate (of tokens that passed rule X, what fraction were
  net positive by each horizon).
- Per-feature separation between winning and losing outcomes (e.g. is
  the median `top10_pct` meaningfully different between winners and
  losers, or is that threshold not actually discriminating anything).
- Funnel counts: discovered → survived momentum filter → passed a rule
  → alerted → profitable.

This is the first point where "is `min_pro_traders: 5` right" gets an
evidence-based answer instead of a guess. Ships as a Telegram command so
checking it doesn't require SSH/log access.

## Stage 4 — Threshold tuning

Grid/random search over the journal for threshold values that would have
improved historical hit rate, holding the rule structure fixed. Proposes
changes via Telegram for manual approval — this never auto-writes
`config.json`. Needs stage 3's report as a sanity check on any proposal
before it's shown.

## Stage 5 — Learned scorer

Once there are enough closed outcomes (rough floor: ~500, to have any
chance of a signal above noise on a class-imbalanced, high-variance
target like memecoin returns), train a model on the journal directly:
features in, "was this net positive by horizon X" out. Start with
logistic regression specifically because it's interpretable — it tells
you *which* feature carried weight, which is auditable in a way a
black-box gradient-boosted model isn't for a first pass. Runs alongside
the existing rule engine as a second opinion before ever replacing it.

## Stage 6 — New GMGN signals as journaled features

Fold in the GMGN endpoints not yet wired (`token_signal`, `trenches`,
`wallet_profits`, `created_tokens` — see the skills survey in chat) as
additional journaled features from day one of adding them, so their
actual predictive value gets measured by stage 3's report rather than
assumed the way the original 18 features were.

## Why this order

Stages 1-3 are pure instrumentation — no behavior change, no risk, and
every day they aren't running is training data lost permanently. Stage 4
is manual-approval only, so it can't make things worse on its own. Stage
5 is gated on having actual data to train on; shipping it earlier would
just be curve-fitting noise. Stage 6 is cheap to fold in once 1-3 exist,
and pointless before they do — the whole point is that the endpoints
prove their own value or don't, rather than being wired in on faith the
way the original integration had to be.
