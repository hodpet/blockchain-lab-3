"""Runnable scenarios for Lab 03."""

from __future__ import annotations

import argparse
import asyncio

from .chain import Transaction
from .network import Network, Node, SimulationResult, build_network
from .pos import (
    GWEI,
    Attestation,
    BeaconState,
    Checkpoint,
    honest_attestations,
)
from .pow import retarget, simulate_mining

RULE = "=" * 78


def _heading(text: str) -> None:
    print(RULE)
    print(text)
    print(RULE)


# --------------------------------------------------------------------------- #
# Part A -- Proof of Work
# --------------------------------------------------------------------------- #


def demo_pow(blocks: int = 30, workers: int | None = None) -> None:
    _heading("PART A -- Proof of Work: parallel mining and difficulty retargeting")

    print("Retargeting arithmetic (no mining involved):")
    print(f"  window took half the target time -> {retarget(1000, 300, 600)}  (difficulty up)")
    print(f"  window took twice the target time -> {retarget(1000, 1200, 600)}  (difficulty down)")
    print(f"  window took 1/100th of the target -> {retarget(1000, 6, 600)}  (clamped at 4x)")
    print(f"  window took 100x the target       -> {retarget(1000, 60000, 600)}  (clamped at 1/4)")
    print()
    print("  The clamp bounds one adjustment to a factor of four either way. Without")
    print("  it, a single manipulated or anomalous window could move difficulty to a")
    print("  value the chain cannot mine its way out of.")
    print()

    print(f"Mining {blocks} blocks (retarget every 10, target 0.30 s/block)...")
    chain, stats = simulate_mining(blocks=blocks, workers=workers)
    print()
    print(stats.report(target_seconds=0.30))
    print()
    print(f"chain height  : {chain.height}")
    print(f"cumulative work: {chain.total_work}")
    print()
    print("  Median well below the mean, and a standard deviation close to the mean,")
    print("  are the fingerprints of an exponential distribution. They follow from the")
    print("  search being memoryless: no attempt brings you closer to a solution, so")
    print("  a small miner's expected share equals its hash-rate share exactly.")
    print()
    print("  This is also why a two-hour gap between Bitcoin blocks is not a fault.")
    print()


# --------------------------------------------------------------------------- #
# Part B -- Proof of Stake
# --------------------------------------------------------------------------- #


def demo_pos() -> None:
    _heading("PART B -- Proof of Stake: Casper FFG finality and slashing")

    print(">>> Scenario 1: honest majority reaches finality")
    state = BeaconState(validator_count=64)
    # The chain of checkpoints starts at the genesis checkpoint, which is the
    # only one justified to begin with. A link whose SOURCE is not already
    # justified carries no weight -- that is what makes justification a chain
    # rather than a free-standing vote.
    checkpoints = [state.genesis_checkpoint] + [Checkpoint(e, f"root-{e}") for e in range(1, 4)]

    for epoch in range(1, 4):
        state.attest_many(
            honest_attestations(state, checkpoints[epoch - 1], checkpoints[epoch])
        )
        result = state.process_epoch(epoch)
        print(
            f"  epoch {epoch}: justified={result['justified']}  finalized={result['finalized']}"
        )
    print()
    print(f"  {state.finalized} is final. Two phases were needed: a checkpoint is")
    print("  justified by a 2/3 link, and finalised only once its successor is also")
    print("  justified. That second phase is why Ethereum finality takes ~12.8 minutes.")
    print()

    print(">>> Scenario 2: a single validator equivocates (double vote)")
    state = BeaconState(validator_count=64)
    src, tgt_a = Checkpoint(0, "genesis"), Checkpoint(1, "chain-A")
    tgt_b = Checkpoint(1, "chain-B")

    state.attest_many(honest_attestations(state, src, tgt_a, validators=list(range(63))))
    state.attest(Attestation(63, src, tgt_a))
    state.attest(Attestation(63, src, tgt_b))  # the same validator, two targets

    balance_before = state.validators[63].balance
    state.process_epoch(1)
    balance_after = state.validators[63].balance

    print(f"  validator 63 balance : {balance_before / GWEI:.4f} -> {balance_after / GWEI:.4f} ETH")
    print(f"  slashed              : {state.validators[63].slashed}")
    print(f"  loss                 : {(balance_before - balance_after) / GWEI:.4f} ETH")
    print()
    print("  A lone equivocator -- typically a misconfigured setup running one key on")
    print("  two machines -- loses a small fraction. The network was never at risk.")
    print()

    print(">>> Scenario 3: a coordinated attack (many validators equivocate together)")
    state = BeaconState(validator_count=64)
    attackers = list(range(20))  # nearly a third of the stake
    state.attest_many(honest_attestations(state, src, tgt_a))
    for index in attackers:
        state.attest(Attestation(index, src, tgt_b))

    before = state.validators[0].balance
    state.process_epoch(1)
    after = state.validators[0].balance

    print(f"  {len(attackers)} validators slashed simultaneously")
    print(f"  each balance         : {before / GWEI:.4f} -> {after / GWEI:.4f} ETH")
    print(f"  loss per validator   : {(before - after) / GWEI:.4f} ETH")
    print()
    print("  Same offence, vastly larger penalty. The correlated term scales with the")
    print("  fraction of stake that misbehaved at once, so punishment tracks SYSTEMIC")
    print("  RISK rather than individual error. Accidents stay cheap; attacks do not.")
    print()

    print(">>> Scenario 4: a surround vote")
    state = BeaconState(validator_count=32)
    wide = Attestation(5, Checkpoint(0, "g"), Checkpoint(6, "far"))
    inner = Attestation(5, Checkpoint(2, "b"), Checkpoint(4, "near"))
    state.attest(wide)
    state.attest(inner)
    state.process_epoch(6)
    print(f"  validator 5 slashed  : {state.validators[5].slashed}")
    print(f"  rule                 : {state.slashings[0].rule}")
    print()
    print("  Harder to spot than a double vote: one vote's source-target span strictly")
    print("  encloses the other's, asserting two incompatible histories over the same")
    print("  range. Both are cryptographically provable from the signatures alone.")
    print()

    print(">>> Scenario 5: the inactivity leak")
    state = BeaconState(validator_count=64)
    absent = set(range(40))  # more than a third are offline: no 2/3 quorum
    present = [i for i in range(64) if i not in absent]

    for epoch in range(1, 12):
        target = Checkpoint(epoch, f"root-{epoch}")
        source = Checkpoint(epoch - 1, f"root-{epoch - 1}") if epoch > 1 else state.genesis_checkpoint
        state.attest_many(honest_attestations(state, source, target, validators=present))
        result = state.process_epoch(epoch)
        if epoch in (1, 5, 8, 11):
            active_share = (
                sum(state.validators[i].effective_balance for i in present)
                / state.total_active_balance
            )
            print(
                f"  epoch {epoch:>2}: finalized={str(result['finalized']):<16} "
                f"leaked={result['inactivity_leak'] / GWEI:>7.3f} ETH  "
                f"voting share={active_share:.1%}"
            )
    print()
    print("  Casper chooses safety over liveness: without a 2/3 quorum it simply stops")
    print("  finalising. The leak restores liveness by shrinking absent validators'")
    print("  stake until the participating remainder passes 2/3 again. Honest nodes")
    print("  stranded by a partition also lose -- that is the price of recovering.")
    print()
    print(state.summary())
    print()


# --------------------------------------------------------------------------- #
# Part C -- gossip network
# --------------------------------------------------------------------------- #


async def _run_gossip(
    node_count: int, mean_block_time: float, seed: int
) -> SimulationResult:
    balances = {f"user{i}": 1000 for i in range(4)}
    network, nodes = build_network(
        node_count=node_count, latency=0.005, seed=seed, initial_balances=balances
    )
    result = SimulationResult(nodes=nodes, network=network)

    tasks = [asyncio.create_task(node.run()) for node in nodes.values()]
    # Only some nodes mine, as in a real network where most nodes only validate.
    # Miners are spread across the node list rather than taken from the front,
    # so that the partition in phase 2 cuts BETWEEN miners. If every miner ended
    # up on one side, the other side would merely stall rather than build a
    # competing chain, and no reorganisation would occur on healing.
    ordered = list(nodes.values())
    stride = max(1, node_count // 3)
    miners = ordered[::stride]
    for node in miners:
        node.running = True
        tasks.append(asyncio.create_task(node.mine_forever(mean_block_time)))

    async def announcer() -> None:
        while True:
            await asyncio.sleep(0.15)
            for node in nodes.values():
                await node.announce_head()

    tasks.append(asyncio.create_task(announcer()))

    ids = list(nodes)

    # --- phase 1: normal operation -------------------------------------- #
    await asyncio.sleep(0.4)
    for i in range(4):
        tx = Transaction(sender="user0", recipient="user1", amount=10, nonce=i, fee=1)
        await nodes[ids[i % node_count]].submit_transaction(tx)
    await asyncio.sleep(1.2)

    heights = result.heights()
    result.events.append(
        f"phase 1 (connected)  heights={sorted(set(heights.values()))} "
        f"converged={result.converged()}"
    )

    # --- phase 2: partition ---------------------------------------------- #
    group_a = set(ids[: node_count // 2])
    group_b = set(ids[node_count // 2 :])
    network.partition([group_a, group_b])
    result.events.append(f"PARTITION  A={sorted(group_a)}  B={sorted(group_b)}")

    await asyncio.sleep(1.5)
    tips = {n.tip_hash for n in nodes.values()}
    result.events.append(
        f"phase 2 (split)      distinct tips={len(tips)} "
        f"heights={sorted(set(result.heights().values()))}"
    )

    # --- phase 3: heal ----------------------------------------------------- #
    #
    # Convergence is not instantaneous. If the two branches happen to carry
    # EQUAL cumulative work, neither displaces the other: the tip only moves on
    # strictly greater work, so each node keeps whichever branch it saw first.
    # That is correct protocol behaviour, not a bug -- Bitcoin behaves the same
    # way. The tie is broken by the NEXT block, which lands on one branch and
    # makes it heavier. The wait below is long enough for that to happen.
    network.heal()
    result.events.append("HEAL")
    await asyncio.sleep(1.0)
    result.events.append(
        f"  just after heal    distinct tips={len({n.tip_hash for n in nodes.values()})} "
        f"(a tie at equal work is resolved by the next block, not by the protocol)"
    )
    await asyncio.sleep(2.0)
    result.events.append(
        f"phase 3 (healed)     distinct tips={len({n.tip_hash for n in nodes.values()})} "
        f"converged={result.converged()}"
    )

    for node in nodes.values():
        node.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    return result


def demo_gossip(node_count: int = 8, seed: int = 7) -> None:
    _heading("PART C -- asynchronous gossip, network partition and reorganisation")

    print(f"Building {node_count} nodes, ring topology plus random edges.")
    print("Mining is modelled as an exponential delay; hashing itself is Part A.")
    print()

    result = asyncio.run(_run_gossip(node_count, mean_block_time=0.25, seed=seed))

    for event in result.events:
        print(f"  {event}")
    print()
    print(result.report())
    print()

    total_reorgs = sum(n.stats.reorgs for n in result.nodes.values())
    print(f"  reorganisations observed across all nodes: {total_reorgs}")
    print()
    print("  During the split each side built on its own tip, so both sides had a")
    print("  perfectly valid chain and neither was wrong. On healing, the heavier")
    print("  branch won and the lighter one's blocks were discarded -- taking their")
    print("  transactions back to the mempool.")
    print()
    print("  This is the CAP trade-off made visible. Proof of work chooses AVAILABILITY:")
    print("  both partitions kept accepting transactions, and consistency was restored")
    print("  only afterwards, by reverting one side. Casper FFG in Part B chooses the")
    print("  opposite: it refuses to finalise rather than risk contradicting itself.")
    print()


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="consensus", description="Lab 03 simulations")
    parser.add_argument(
        "part", nargs="?", default="all", choices=["all", "pow", "pos", "gossip"]
    )
    parser.add_argument("--blocks", type=int, default=30, help="blocks to mine in Part A")
    parser.add_argument("--nodes", type=int, default=8, help="nodes in Part C")
    parser.add_argument("--workers", type=int, default=None, help="mining processes")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    if args.part in ("all", "pow"):
        demo_pow(blocks=args.blocks, workers=args.workers)
    if args.part in ("all", "pos"):
        demo_pos()
    if args.part in ("all", "gossip"):
        demo_gossip(node_count=args.nodes, seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
