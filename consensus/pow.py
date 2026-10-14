"""Proof of Work: parallel mining, difficulty retargeting, and the statistics.

Two things this module is meant to make visible.

First, mining is a memoryless search. Every hash attempt is independent, so
block arrivals form a Poisson process and inter-block times are exponentially
distributed. That is not a curiosity: it is what makes mining a fair lottery in
which a small miner's expected share equals their hash-rate share exactly, with
no way to accumulate partial progress. A puzzle without that property would hand
an enormous structural advantage to large miners.

Second, difficulty retargeting is a negative feedback loop with a clamp, and
watching it converge (and oscillate) explains why Bitcoin uses a two-week window
rather than adjusting every block.
"""

from __future__ import annotations

import multiprocessing as mp
import statistics
import time
from dataclasses import dataclass

from .chain import GENESIS_TARGET, MAX_TARGET, Block, Chain, make_block, sha256d

# --------------------------------------------------------------------------- #
# Difficulty
# --------------------------------------------------------------------------- #

#: Bitcoin clamps a single retarget to a factor of four in either direction, so
#: that no single window can move difficulty arbitrarily far.
MAX_ADJUSTMENT_FACTOR = 4


def target_from_difficulty(difficulty: int) -> int:
    return GENESIS_TARGET // max(1, difficulty)


def difficulty_from_target(target: int) -> int:
    return GENESIS_TARGET // max(1, target)


def retarget(
    current_difficulty: int,
    actual_seconds: float,
    target_seconds: float,
    *,
    clamp: int = MAX_ADJUSTMENT_FACTOR,
) -> int:
    """Recompute difficulty from how long the last window actually took.

        target_new = target_old * actual / expected

    Difficulty is the reciprocal of target, hence the inverted ratio here. If
    the window was fast, difficulty rises; if slow, it falls.

    The clamp bounds a single adjustment to a factor of `clamp` either way. Its
    purpose is to limit the damage from a timestamp manipulation attack or from
    a sudden hash-rate collapse: without it, one crafted window could drive
    difficulty to a value from which the chain cannot recover in useful time.
    """
    if actual_seconds <= 0:
        actual_seconds = target_seconds / clamp

    ratio = actual_seconds / target_seconds
    ratio = max(1 / clamp, min(clamp, ratio))

    return max(1, int(current_difficulty / ratio))


# --------------------------------------------------------------------------- #
# Single-process mining
# --------------------------------------------------------------------------- #


def mine_range(block: Block, start: int, stop: int, step: int = 1) -> int | None:
    """Search a strided nonce range. Returns the winning nonce or None.

    Striding rather than partitioning is how the parallel miner divides work:
    worker i takes nonces i, i+W, i+2W, ... so every worker explores the whole
    space uniformly and none is stuck in a barren region.
    """
    target = block.target
    for nonce in range(start, stop, step):
        if int.from_bytes(sha256d(block.header_bytes(nonce)), "big") < target:
            return nonce
    return None


def mine(block: Block, max_nonce: int = 2**32) -> Block:
    """Mine single-threaded. Raises if the nonce space is exhausted.

    Bitcoin's 32-bit nonce is exhausted in milliseconds by modern hardware; real
    miners then vary the coinbase extraNonce, which changes the Merkle root and
    so opens a fresh nonce space. Here the difficulty is low enough that the
    plain nonce always suffices.
    """
    nonce = mine_range(block, 0, max_nonce)
    if nonce is None:
        raise RuntimeError("nonce space exhausted; vary the timestamp or coinbase")
    return block.with_nonce(nonce)


# --------------------------------------------------------------------------- #
# Parallel mining
# --------------------------------------------------------------------------- #


def _worker(header_template: bytes, target: int, offset: int, stride: int, found, queue) -> None:
    """Run in a child process: search a strided nonce range until someone wins.

    `found` is a shared flag. Checking it periodically -- not on every hash,
    which would dominate the cost -- is what lets the losing workers stop
    promptly once a winner appears.
    """
    import json

    header = json.loads(header_template)
    nonce = offset
    while not found.value:
        for _ in range(4096):
            header["nonce"] = nonce
            payload = json.dumps(header, sort_keys=True).encode()
            if int.from_bytes(sha256d(payload), "big") < target:
                found.value = 1
                queue.put(nonce)
                return
            nonce += stride
        if nonce > 2**32:
            return


def mine_parallel(block: Block, workers: int | None = None, timeout: float = 60.0) -> Block:
    """Mine across several processes.

    Processes rather than threads: CPython's global interpreter lock makes
    threads useless for a CPU-bound hash loop.

    Note the shape of the speed-up. Because the search is memoryless, doubling
    the workers halves the expected time exactly -- there is no coordination
    overhead and no diminishing return from overlap, since every attempt is
    independent. This is the same reason a mining pool can pay out
    proportionally to submitted shares.
    """
    import json

    workers = workers or max(1, mp.cpu_count() - 1)
    if workers == 1:
        return mine(block)

    header_template = json.dumps(
        json.loads(block.header_bytes(0).decode()), sort_keys=True
    )

    ctx = mp.get_context("spawn")
    found = ctx.Value("i", 0)
    queue: mp.Queue = ctx.Queue()

    procs = [
        ctx.Process(
            target=_worker,
            args=(header_template, block.target, i, workers, found, queue),
            daemon=True,
        )
        for i in range(workers)
    ]
    for p in procs:
        p.start()

    try:
        nonce = queue.get(timeout=timeout)
    except Exception as exc:  # pragma: no cover - timeout path
        raise RuntimeError("parallel mining timed out") from exc
    finally:
        found.value = 1
        for p in procs:
            p.join(timeout=2.0)
            if p.is_alive():
                p.terminate()

    mined = block.with_nonce(nonce)
    if not mined.is_valid_pow():  # pragma: no cover - defensive
        raise RuntimeError("worker returned an invalid nonce")
    return mined


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #


@dataclass
class MiningStats:
    intervals: list[float]
    difficulties: list[int]
    hashes_estimated: list[int]

    @property
    def mean_interval(self) -> float:
        return statistics.fmean(self.intervals) if self.intervals else 0.0

    @property
    def median_interval(self) -> float:
        return statistics.median(self.intervals) if self.intervals else 0.0

    @property
    def stdev_interval(self) -> float:
        return statistics.pstdev(self.intervals) if len(self.intervals) > 1 else 0.0

    def report(self, target_seconds: float) -> str:
        lines = [
            f"blocks mined        : {len(self.intervals)}",
            f"target interval     : {target_seconds:.3f} s",
            f"mean interval       : {self.mean_interval:.3f} s",
            f"median interval     : {self.median_interval:.3f} s",
            f"stdev of intervals  : {self.stdev_interval:.3f} s",
            f"min / max interval  : {min(self.intervals):.3f} / {max(self.intervals):.3f} s",
            f"difficulty start/end: {self.difficulties[0]} -> {self.difficulties[-1]}",
        ]
        if self.mean_interval > 0:
            ratio = self.median_interval / self.mean_interval
            lines.append(f"median / mean       : {ratio:.3f}   (exponential predicts ~0.693)")
            lines.append(
                f"stdev / mean        : {self.stdev_interval / self.mean_interval:.3f}"
                "   (exponential predicts ~1.0)"
            )
        return "\n".join(lines)


def simulate_mining(
    blocks: int = 30,
    *,
    retarget_interval: int = 10,
    target_seconds: float = 0.30,
    initial_difficulty: int = 1,
    workers: int | None = None,
    miner: str = "miner-0",
) -> tuple[Chain, MiningStats]:
    """Mine a chain, retargeting difficulty periodically, and collect statistics.

    The reported median/mean and stdev/mean ratios are the empirical test of the
    exponential distribution: for a Poisson process the median is ln(2) ~ 0.693
    of the mean, and the standard deviation equals the mean. Small samples are
    noisy -- increase `blocks` if the numbers look far off.
    """
    from .chain import genesis_block

    chain = Chain(genesis_block(initial_difficulty))
    intervals: list[float] = []
    difficulties: list[int] = [initial_difficulty]
    hashes: list[int] = []

    difficulty = initial_difficulty
    window_start = time.perf_counter()

    for i in range(blocks):
        started = time.perf_counter()
        candidate = make_block(chain, difficulty=difficulty, miner=miner, timestamp=started)
        mined = mine_parallel(candidate, workers=workers) if workers != 1 else mine(candidate)
        elapsed = time.perf_counter() - started

        chain.add_block(mined)
        intervals.append(elapsed)
        hashes.append(MAX_TARGET // mined.target)

        if (i + 1) % retarget_interval == 0:
            window = time.perf_counter() - window_start
            difficulty = retarget(difficulty, window, target_seconds * retarget_interval)
            window_start = time.perf_counter()
        difficulties.append(difficulty)

    return chain, MiningStats(intervals, difficulties, hashes)
