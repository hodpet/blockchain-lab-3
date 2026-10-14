"""Blocks, transactions, account state and fork choice.

A deliberately small account-based ledger, so that the consensus mechanisms in
`pow.py`, `pos.py` and `network.py` have something concrete to agree about.

The state model follows Ethereum rather than Bitcoin: balances and a per-account
sequential nonce. The nonce is what makes replay protection explicit here, and
it is also what produces the "stuck transaction" behaviour you will observe in
the gossip simulation.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace

MAX_TARGET = (1 << 256) - 1

#: Difficulty 1 corresponds to this target. Chosen far easier than Bitcoin's so
#: that the simulation produces blocks in milliseconds rather than minutes.
GENESIS_TARGET = MAX_TARGET >> 16


def sha256d(data: bytes) -> bytes:
    """Double SHA-256, as Bitcoin uses for block hashing."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Transaction:
    sender: str
    recipient: str
    amount: int
    nonce: int
    fee: int = 0

    @property
    def txid(self) -> str:
        payload = json.dumps(
            {
                "sender": self.sender,
                "recipient": self.recipient,
                "amount": self.amount,
                "nonce": self.nonce,
                "fee": self.fee,
            },
            sort_keys=True,
        ).encode()
        return sha256d(payload).hex()

    def __repr__(self) -> str:  # pragma: no cover - display only
        return f"Tx({self.sender}->{self.recipient} {self.amount} n={self.nonce})"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


class State:
    """Account balances and nonces: the replicated state machine's state.

    `apply` is the state transition function. It must be deterministic -- no
    clock, no randomness, no I/O -- otherwise two nodes replaying the same
    blocks would diverge, which is the whole failure mode consensus exists to
    prevent.
    """

    def __init__(self, balances: dict[str, int] | None = None) -> None:
        self.balances: dict[str, int] = dict(balances or {})
        self.nonces: dict[str, int] = {}

    def copy(self) -> State:
        clone = State(self.balances)
        clone.nonces = dict(self.nonces)
        return clone

    def balance_of(self, account: str) -> int:
        return self.balances.get(account, 0)

    def nonce_of(self, account: str) -> int:
        return self.nonces.get(account, 0)

    def validate(self, tx: Transaction) -> str | None:
        """Return None if the transaction is applicable, else the reason."""
        if tx.amount < 0 or tx.fee < 0:
            return "negative amount"
        if self.balance_of(tx.sender) < tx.amount + tx.fee:
            return "insufficient balance"
        expected = self.nonce_of(tx.sender)
        if tx.nonce != expected:
            return f"nonce mismatch: expected {expected}, got {tx.nonce}"
        return None

    def apply(self, tx: Transaction) -> None:
        reason = self.validate(tx)
        if reason:
            raise ValueError(f"invalid transaction {tx}: {reason}")
        self.balances[tx.sender] = self.balance_of(tx.sender) - tx.amount - tx.fee
        self.balances[tx.recipient] = self.balance_of(tx.recipient) + tx.amount
        self.nonces[tx.sender] = tx.nonce + 1

    def apply_block(self, block: Block, reward: int = 0) -> None:
        for tx in block.txs:
            self.apply(tx)
        if block.miner:
            total_fees = sum(tx.fee for tx in block.txs)
            self.balances[block.miner] = self.balance_of(block.miner) + reward + total_fees


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Block:
    height: int
    prev_hash: str
    timestamp: float
    txs: tuple[Transaction, ...] = ()
    difficulty: int = 1
    nonce: int = 0
    miner: str = ""

    @property
    def target(self) -> int:
        """Difficulty and target are reciprocal views of the same quantity."""
        return GENESIS_TARGET // max(1, self.difficulty)

    @property
    def merkle_root(self) -> str:
        """A one-level digest of the transaction set.

        Not a real Merkle tree -- lab 04 builds one of those. Here it only needs
        to bind the transaction set into the header so that changing a
        transaction changes the block hash.
        """
        if not self.txs:
            return "00" * 32
        return sha256d("".join(tx.txid for tx in self.txs).encode()).hex()

    def header_bytes(self, nonce: int | None = None) -> bytes:
        """Canonical header serialisation, the thing that actually gets hashed."""
        payload = json.dumps(
            {
                "height": self.height,
                "prev": self.prev_hash,
                "ts": round(self.timestamp, 6),
                "root": self.merkle_root,
                "diff": self.difficulty,
                "miner": self.miner,
                "nonce": self.nonce if nonce is None else nonce,
            },
            sort_keys=True,
        ).encode()
        return payload

    @property
    def hash(self) -> str:
        return sha256d(self.header_bytes()).hex()

    @property
    def work(self) -> int:
        """Expected number of hashes to produce this block.

        Cumulative work, not chain length, is what fork choice compares. On a
        chain with varying difficulty the longer chain is not necessarily the
        heavier one, and it is the heavier one that wins.
        """
        return MAX_TARGET // max(1, self.target)

    def is_valid_pow(self) -> bool:
        return int.from_bytes(sha256d(self.header_bytes()), "big") < self.target

    def with_nonce(self, nonce: int) -> Block:
        return replace(self, nonce=nonce)


def genesis_block(difficulty: int = 1) -> Block:
    """A fixed genesis block. Timestamp is pinned so every node agrees on it."""
    return Block(height=0, prev_hash="00" * 32, timestamp=0.0, difficulty=difficulty)


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #


@dataclass
class ChainEntry:
    block: Block
    total_work: int


class Chain:
    """A block tree with heaviest-chain fork choice.

    A blockchain is a tree, not a list. The "chain" is whichever path from
    genesis accumulates the most work; the rest are forks that may become
    canonical later if their branch outgrows the current tip. Storing the tree
    is what makes a reorganisation possible at all.
    """

    def __init__(self, genesis: Block | None = None) -> None:
        genesis = genesis or genesis_block()
        self.entries: dict[str, ChainEntry] = {
            genesis.hash: ChainEntry(genesis, genesis.work)
        }
        self.genesis_hash = genesis.hash
        self.tip_hash = genesis.hash
        #: Reorganisations observed, for reporting.
        self.reorg_count = 0
        self.reorg_depths: list[int] = []

    # -- accessors ---------------------------------------------------------- #

    @property
    def tip(self) -> Block:
        return self.entries[self.tip_hash].block

    @property
    def height(self) -> int:
        return self.tip.height

    @property
    def total_work(self) -> int:
        return self.entries[self.tip_hash].total_work

    def has(self, block_hash: str) -> bool:
        return block_hash in self.entries

    def canonical(self) -> list[Block]:
        """Walk back from the tip to genesis, then reverse."""
        out: list[Block] = []
        cursor = self.tip_hash
        while True:
            entry = self.entries[cursor]
            out.append(entry.block)
            if cursor == self.genesis_hash:
                break
            cursor = entry.block.prev_hash
        return list(reversed(out))

    def path_to(self, block_hash: str) -> list[Block]:
        out: list[Block] = []
        cursor = block_hash
        while cursor in self.entries:
            entry = self.entries[cursor]
            out.append(entry.block)
            if cursor == self.genesis_hash:
                break
            cursor = entry.block.prev_hash
        return list(reversed(out))

    # -- mutation ----------------------------------------------------------- #

    def add_block(self, block: Block, *, check_pow: bool = True) -> bool:
        """Insert a block. Returns True if it caused the tip to move.

        Rejects blocks whose parent is unknown (an orphan) and blocks with an
        invalid proof of work. Note that a valid block that does NOT become the
        tip is still stored: it may be extended later and win.
        """
        if block.hash in self.entries:
            return False
        parent = self.entries.get(block.prev_hash)
        if parent is None:
            raise KeyError(f"unknown parent {block.prev_hash[:12]} for block {block.height}")
        if block.height != parent.block.height + 1:
            raise ValueError("height must be parent height + 1")
        if check_pow and not block.is_valid_pow():
            raise ValueError("invalid proof of work")

        total = parent.total_work + block.work
        self.entries[block.hash] = ChainEntry(block, total)

        if total > self.entries[self.tip_hash].total_work:
            depth = self._reorg_depth(block)
            if depth > 0:
                self.reorg_count += 1
                self.reorg_depths.append(depth)
            self.tip_hash = block.hash
            return True
        return False

    def _reorg_depth(self, new_tip: Block) -> int:
        """How many blocks the current canonical chain would lose.

        Zero when the new block simply extends the current tip.
        """
        if new_tip.prev_hash == self.tip_hash:
            return 0
        old = {b.hash for b in self.canonical()}
        cursor = new_tip.prev_hash
        while cursor not in old and cursor in self.entries:
            cursor = self.entries[cursor].block.prev_hash
        fork_height = self.entries[cursor].block.height if cursor in self.entries else 0
        return self.height - fork_height

    # -- derived ------------------------------------------------------------ #

    def replay_state(self, initial: dict[str, int], reward: int = 50) -> State:
        """Recompute state by replaying the canonical chain from genesis.

        Identical initial state plus identical ordering must give identical
        final state -- that is the entire premise of a replicated state machine.
        """
        state = State(initial)
        for block in self.canonical()[1:]:
            state.apply_block(block, reward=reward)
        return state

    def block_intervals(self) -> list[float]:
        blocks = self.canonical()
        return [b.timestamp - a.timestamp for a, b in zip(blocks, blocks[1:], strict=False)]


def make_block(
    chain: Chain,
    *,
    txs: tuple[Transaction, ...] = (),
    difficulty: int | None = None,
    miner: str = "",
    timestamp: float | None = None,
) -> Block:
    """Build an unmined candidate block on top of the current tip."""
    tip = chain.tip
    return Block(
        height=tip.height + 1,
        prev_hash=tip.hash,
        timestamp=time.time() if timestamp is None else timestamp,
        txs=txs,
        difficulty=tip.difficulty if difficulty is None else difficulty,
        miner=miner,
    )
