# Lab 03 — Consensus: Proof of Work, Proof of Stake and gossip networking

**Theory:** [[07 - P2P Networks, State Propagation and Consensus Theory]], [[08 - Consensus - Proof of Work, Mining and Proof of Stake]]
**Stack:** Python 3.11+ (uv), standard library only — `asyncio`, `multiprocessing`, `hashlib`
**Points:** 20

---

## Objectives

You will implement consensus twice, and then break it.

Part A is a multiprocess proof-of-work miner with live difficulty retargeting. The output is not a chain but a *distribution*: you will measure the inter-block intervals and confirm empirically that they are exponentially distributed, which is what makes mining a fair lottery rather than a game the largest participant always wins.

Part B is a Casper FFG state machine — justification, finalisation, slashing, inactivity leak. The centrepiece is the correlated penalty: the same offence committed alone costs a fraction of a percent, and committed by a third of the validator set costs everything. Penalty tracks systemic risk, not individual error, and that idea is worth carrying well beyond blockchain.

Part C runs an asynchronous gossip network of eight nodes, splits it in half, lets both halves build incompatible chains, and heals it. You will watch a reorganisation happen and see confirmed transactions return to the mempool.

The three parts share one ledger (`chain.py`), so the same state machine is driven by both consensus mechanisms — which is precisely the point made in the theory notes: consensus decides *ordering*, and everything above it is unchanged.

---

## Prerequisites

- `uv` installed (see `../README.md`)
- Weeks 7 and 8 of the theory notes read — in particular safety vs. liveness, the FLP result, and the CAP trade-off
- A machine with at least two CPU cores (Part A parallelises across processes)

---

## Setup

```powershell
uv sync
uv run pytest
```

42 tests should pass in about four seconds.

Run the demonstrations:

```powershell
uv run python -m consensus              # all three parts
uv run python -m consensus pow          # Part A
uv run python -m consensus pos          # Part B
uv run python -m consensus gossip       # Part C

uv run python -m consensus pow --blocks 100 --workers 4
uv run python -m consensus gossip --nodes 12 --seed 42
```

---

## Part A — Proof of Work

**Read:** `consensus/pow.py`. **Run:** `uv run python -m consensus pow --blocks 60`.

### Difficulty retargeting

The retarget function is a negative feedback loop with a clamp:

$$\text{target}_{\text{new}} = \text{target}_{\text{old}} \times \frac{\text{actual}}{\text{expected}}$$

Difficulty is the reciprocal of target, hence the inverted ratio in code. The clamp bounds one adjustment to a factor of four in either direction. Its purpose is not cosmetic: without it, a single window with manipulated timestamps — or a genuine hash-rate collapse — could move difficulty to a value from which the chain cannot mine its way back in useful time.

### The statistics are the lesson

The demo reports two ratios you should check against theory:

| Statistic | Exponential prediction | Why |
|---|---|---|
| median / mean | ≈ 0.693 (that is, $\ln 2$) | the exponential distribution is heavily right-skewed |
| stdev / mean | ≈ 1.0 | for an exponential, $\sigma = \mu$ |

A representative run of 20 blocks gives 0.741 and 0.897 — close, and noisy because the sample is small. Increase `--blocks` and watch them tighten.

This follows from **memorylessness**: every hash attempt is independent, so no attempt brings you closer to a solution. Two consequences matter. A small miner's expected reward share equals its hash-rate share exactly, with no way for a large miner to accumulate partial progress — the puzzle is a fair lottery. And a two-hour gap between Bitcoin blocks is not a malfunction; it is the tail of the distribution doing what tails do.

### Parallel mining

`mine_parallel` distributes the search across processes — not threads, because CPython's global interpreter lock makes threads useless for a CPU-bound hash loop. Workers take **strided** nonce ranges (worker $i$ takes $i, i+W, i+2W, \dots$) so that all workers explore uniformly. A shared flag lets losers stop promptly, checked every few thousand hashes rather than every hash, since the check would otherwise dominate the cost.

Because the search is memoryless, doubling the workers halves the expected time exactly. There is no coordination overhead and no diminishing return from overlap — the same property that lets a mining pool pay out proportionally to submitted shares.

**For your report:** run with `--blocks 100`, record both ratios, and plot a histogram of the intervals. Explain why the median is below the mean, and what would go wrong with the fairness of mining if the puzzle were *not* memoryless.

---

## Part B — Proof of Stake

**Read:** `consensus/pos.py`. **Run:** `uv run python -m consensus pos`.

Five scenarios run in sequence.

**Scenario 1 — finality.** A checkpoint becomes *justified* when more than two thirds of active stake links to it from an already-justified source, and *finalised* only when its immediate successor is also justified. Note the chaining requirement: a link from an unjustified source carries no weight, which is what makes justification a chain rather than a set of free-standing votes. The second phase is why Ethereum finality takes two epochs, roughly 12.8 minutes.

**Scenario 2 — a lone equivocator** loses about 2.4 of 32 ETH. This is the misconfiguration case: one key running on two machines. The network was never at risk, and the penalty reflects that.

**Scenario 3 — a coordinated attack.** Twenty validators commit the *same* offence simultaneously and each loses 29.4 of 32 ETH.

$$\text{penalty} = \text{balance} \times \min\left(1,\; 3 \times \frac{\text{slashed stake}}{\text{total stake}}\right)$$

Same rule violation, twelve times the loss. The correlated term is the most elegant mechanism in the chapter: it prices **systemic risk** rather than individual error, so accidents stay survivable while collusion is ruinous.

**Scenario 4 — a surround vote.** Harder to detect than equivocation: one vote's source–target span strictly encloses another's, asserting two incompatible histories over the same range. Both violations are provable from the signatures alone, with no need to know which chain is "really" correct.

**Scenario 5 — the inactivity leak.** With more than a third of validators absent there is no quorum, so Casper simply stops finalising. This is the CP choice: safety over liveness. The leak then drains absent validators until the participating remainder passes two thirds again, and the demo shows the voting share climbing from 37.5% upward. Honest validators stranded on the wrong side of a partition also lose — contested, but the price of being able to recover from one at all.

**For your report:** produce a table of loss-per-validator against number of simultaneous offenders (1, 5, 10, 20, 30) and plot it. Explain the shape, and argue whether you consider the mechanism fair.

---

## Part C — Gossip networking and partition

**Read:** `consensus/network.py`. **Run:** `uv run python -m consensus gossip`.

Eight nodes in a ring plus random edges, each maintaining a mempool, a block tree and a deduplication filter. Transactions and blocks flood by gossip; each node forwards to every peer except the one it heard from.

A representative run:

```
phase 1 (connected)  heights=[13, 14] converged=False
PARTITION  A=['node0'..'node3']  B=['node4'..'node7']
phase 2 (split)      distinct tips=2 heights=[22, 26]
HEAL
  just after heal    distinct tips=2  (a tie at equal work is resolved by the next block)
phase 3 (healed)     distinct tips=1 converged=True
reorganisations observed across all nodes: 4
```

Four things in that trace deserve attention.

**During the split both sides were correct.** Each built a valid chain on a valid parent. Neither was wrong; they simply could not see each other. A node cannot distinguish a partitioned peer from a slow one, which is exactly the indistinguishability the FLP result is built on.

**On healing, the heavier branch won and the lighter one's blocks were discarded.** The minority side's nodes each registered a reorganisation. This is probabilistic finality made concrete: those blocks *were* confirmed, and then they were not.

**The discarded transactions returned to the mempool.** `_prune_mempool` removes only transactions in the canonical chain, so anything on an orphaned branch becomes pending again automatically.

**A tie does not resolve itself.** Immediately after healing there were still two tips, because the tip moves only on *strictly greater* work. Equal-work branches are both kept and each node holds whichever it saw first. Bitcoin behaves identically; the next block breaks the tie. Do not be tempted to add a deterministic tie-break — it would make the demo tidier and the lesson wrong.

**This is the CAP trade-off made visible.** Proof of work chose **availability**: both partitions kept accepting transactions, and consistency was restored afterwards by reverting one side. Casper FFG in Part B chose the opposite, refusing to finalise rather than risk contradicting itself.

> [!NOTE]
> The transport is in-process `asyncio` queues with artificial latency, not real sockets, and mining here is modelled as an exponential delay rather than actual hashing (Part A covers hashing). Everything above the transport — topology, flooding, deduplication, mempool policy, fork choice, reconciliation — is real logic. The exponential delay preserves the property that matters: block arrivals stay a Poisson process, so forks occur at realistic rates.

**For your report:** run with `--nodes 12` and three different seeds. Record the reorganisation depths and explain why deeper reorganisations become more likely as the partition lasts longer.

---

## `[STUDENT TASK]` — What you must implement yourself

Submit your own implementation in a separate repository, satisfying:

1. **Ledger** — account state with balance and sequential-nonce validation; a block tree with heaviest-chain fork choice that *retains* losing branches and reports reorganisation depth.
2. **Proof of work** — header hashing, target/difficulty conversion, a clamped retarget function, and a multiprocess miner using strided nonce ranges with early termination.
3. **Interval statistics** — mean, median and standard deviation of block intervals, compared against the exponential prediction.
4. **Casper FFG** — justification with a strict two-thirds threshold, finalisation requiring consecutive justified checkpoints, and rejection of links from unjustified sources.
5. **Slashing** — double-vote and surround-vote detection, the initial penalty, and the correlated penalty scaling with simultaneously slashed stake.
6. **Inactivity leak** — engaging only after a grace period, sparing participants.
7. **Gossip network** — asynchronous nodes with mempool, deduplication, block relay, partition support, and post-heal reconciliation.

`tests/test_consensus.py` is a valid specification for all seven. If your implementation passes it unchanged, the functional requirements are met.

You may not use a blockchain library, a networking framework beyond `asyncio`, or a consensus library. Standard library only.

---

## Deliverables

A PDF report plus a repository link. The report must contain:

1. Part A: interval histogram for at least 100 blocks, both ratios, and the memorylessness argument.
2. Part A: a difficulty plot across at least ten retarget windows, with a comment on oscillation and why Bitcoin uses a two-week window rather than adjusting every block.
3. Part B: the loss-versus-offenders table and plot, with your fairness argument.
4. Part B: a trace of the inactivity leak showing the voting share recovering, and an explanation of why the protocol prefers to stall.
5. Part C: partition traces for three seeds, with reorganisation depths.
6. A section of at most 500 words placing your three implementations in the CAP and safety/liveness frame: **which property does each sacrifice, under what conditions, and how would you observe the sacrifice happening in production?**

---

## Grading rubric (20 points)

| Criterion | Points |
|---|---|
| Ledger and fork choice correct, including retained losing branches and reorg depth | 3 |
| Parallel miner works; strided search and early termination implemented | 3 |
| Retarget correct, including the clamp and the floor at difficulty 1 | 2 |
| Interval statistics computed and correctly interpreted against theory | 2 |
| Casper justification and finalisation correct, including the source-chaining rule | 3 |
| Slashing detection correct for both rules; correlated penalty implemented | 3 |
| Gossip network with deduplication, partition and successful reconciliation | 3 |
| Report quality, especially the CAP / safety-liveness analysis | 1 |

---

## Troubleshooting

**Part A hangs or is very slow** — you are mining at too high a difficulty. `GENESIS_TARGET` in `chain.py` is `MAX >> 16`, about 65,000 expected hashes per block. Raising the shift makes it dramatically slower.

**`mine_parallel` raises "timed out"** — the `spawn` start method re-imports the module in each child. If you moved the worker function inside a class or a closure it cannot be pickled. Keep it a module-level function.

**Statistics look nothing like exponential** — small samples are extremely noisy for a heavy-tailed distribution. Use at least 100 blocks before concluding anything.

**Nothing ever justifies in Part B** — your source checkpoint is not in the justified set. Epoch 1 must link *from the genesis checkpoint*, not from a fresh `Checkpoint(0, ...)` with a different root. This is the most common error, and there is a test for it.

**Justification happens at exactly two thirds** — the threshold is *strictly greater than* two thirds. With nine validators, six must not suffice and seven must.

**Part C never converges** — check whether the tips carry equal work. That is correct behaviour and resolves on the next block; let the simulation run longer. If they carry *different* work and still disagree, your reconciliation is not running: nodes must announce their heads periodically and request a peer's chain when they see an unknown tip.

**Part C converges but no reorganisations occur** — all your miners are on one side of the partition, so the other side merely stalls. Spread miners across the topology.

**`pytest` reports async tests as skipped** — `asyncio_mode = "auto"` must be set in `pyproject.toml` and `pytest-asyncio` installed. Run `uv sync`.
