"""An asynchronous peer-to-peer gossip network, with partitions.

What this simulates and what it does not: the transport is in-process asyncio
queues with an artificial latency, not real sockets. Everything above the
transport -- peer topology, gossip flooding with deduplication, mempool
management, block propagation, fork choice and chain reconciliation -- is real
logic, and that is where the lab's content lies.

Blocks here are added with `check_pow=False` and mining is modelled as a delay
drawn from an exponential distribution. Actual hashing is covered in `pow.py`;
repeating it here would make the simulation slow without teaching anything new.
The exponential delay preserves the property that matters: block arrivals are a
Poisson process, so competing blocks and forks occur at realistic rates.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

from .chain import Block, Chain, State, Transaction, genesis_block, make_block

# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #


@dataclass
class Message:
    kind: str  # "tx" | "block" | "head" | "getchain" | "chain"
    payload: Any
    sender: str


# --------------------------------------------------------------------------- #
# The network fabric
# --------------------------------------------------------------------------- #


class Network:
    """Delivers messages between nodes, subject to latency and partitions."""

    def __init__(self, latency: float = 0.01, seed: int = 0) -> None:
        self.nodes: dict[str, Node] = {}
        self.latency = latency
        self.rng = random.Random(seed)
        #: None means fully connected. Otherwise, a list of disjoint groups;
        #: messages between different groups are dropped.
        self.partitions: list[set[str]] | None = None
        self.delivered = 0
        self.dropped = 0

    def register(self, node: Node) -> None:
        self.nodes[node.node_id] = node
        node.network = self

    def can_reach(self, a: str, b: str) -> bool:
        if self.partitions is None:
            return True
        for group in self.partitions:
            if a in group and b in group:
                return True
        return False

    def partition(self, groups: list[set[str]]) -> None:
        """Split the network. Messages across groups are silently dropped.

        Silence, not rejection, is the realistic model: a partitioned node
        cannot distinguish a peer that is unreachable from one that is merely
        slow. That indistinguishability is exactly what the FLP result is about.
        """
        self.partitions = groups

    def heal(self) -> None:
        self.partitions = None

    async def deliver(self, source: str, target: str, message: Message) -> None:
        if not self.can_reach(source, target):
            self.dropped += 1
            return
        jitter = self.latency * (0.5 + self.rng.random())
        await asyncio.sleep(jitter)
        node = self.nodes.get(target)
        if node is not None:
            self.delivered += 1
            await node.inbox.put(message)


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


@dataclass
class NodeStats:
    blocks_mined: int = 0
    blocks_received: int = 0
    txs_received: int = 0
    txs_rejected: int = 0
    duplicates_suppressed: int = 0
    reorgs: int = 0


class Node:
    """A full node: mempool, chain, gossip relay and (optionally) a miner."""

    def __init__(
        self,
        node_id: str,
        *,
        initial_balances: dict[str, int],
        hash_power: float = 1.0,
        block_reward: int = 50,
        genesis: Block | None = None,
    ) -> None:
        self.node_id = node_id
        self.network: Network | None = None
        self.peers: list[str] = []

        self.chain = Chain(genesis or genesis_block())
        self.initial_balances = dict(initial_balances)
        self.block_reward = block_reward

        self.mempool: dict[str, Transaction] = {}
        #: Deduplication filter. Without it, gossip flooding would loop forever:
        #: every node would re-forward every message it received from every peer.
        self.seen: set[str] = set()

        self.hash_power = hash_power
        self.inbox: asyncio.Queue[Message] = asyncio.Queue()
        self.stats = NodeStats()
        self.running = False
        self._rng = random.Random(hash(node_id) & 0xFFFF)

    # -- state -------------------------------------------------------------- #

    def state(self) -> State:
        return self.chain.replay_state(self.initial_balances, reward=self.block_reward)

    @property
    def tip_hash(self) -> str:
        return self.chain.tip_hash

    @property
    def height(self) -> int:
        return self.chain.height

    # -- gossip ------------------------------------------------------------- #

    async def broadcast(self, message: Message, *, exclude: str | None = None) -> None:
        assert self.network is not None
        await asyncio.gather(
            *(
                self.network.deliver(self.node_id, peer, message)
                for peer in self.peers
                if peer != exclude
            )
        )

    async def submit_transaction(self, tx: Transaction) -> bool:
        """Accept a transaction from a user and gossip it."""
        if not self._accept_transaction(tx):
            return False
        await self.broadcast(Message("tx", tx, self.node_id))
        return True

    def _accept_transaction(self, tx: Transaction) -> bool:
        if tx.txid in self.seen:
            self.stats.duplicates_suppressed += 1
            return False
        self.seen.add(tx.txid)
        self.stats.txs_received += 1

        # Validate against the current tip. A transaction that is invalid now
        # may become valid later (its nonce gap could be filled), which is why
        # real implementations keep a queued/pending split. Here it is simply
        # rejected, and the "stuck transaction" behaviour follows.
        reason = self.state().validate(tx)
        if reason:
            self.stats.txs_rejected += 1
            return False

        self.mempool[tx.txid] = tx
        return True

    # -- message handling --------------------------------------------------- #

    async def handle(self, message: Message) -> None:
        if message.kind == "tx":
            if self._accept_transaction(message.payload):
                await self.broadcast(message, exclude=message.sender)

        elif message.kind == "block":
            await self._handle_block(message)

        elif message.kind == "head":
            head: Block = message.payload
            if not self.chain.has(head.hash):
                assert self.network is not None
                await self.network.deliver(
                    self.node_id,
                    message.sender,
                    Message("getchain", self.chain.height, self.node_id),
                )

        elif message.kind == "getchain":
            assert self.network is not None
            blocks = self.chain.canonical()[1:]  # skip genesis
            await self.network.deliver(
                self.node_id, message.sender, Message("chain", blocks, self.node_id)
            )

        elif message.kind == "chain":
            self._absorb_chain(message.payload)

    async def _handle_block(self, message: Message) -> None:
        block: Block = message.payload
        if self.chain.has(block.hash):
            self.stats.duplicates_suppressed += 1
            return
        try:
            moved = self.chain.add_block(block, check_pow=False)
        except KeyError:
            # Unknown parent. Rather than buffering orphans, ask the sender for
            # its whole chain -- adequate for a simulation of this size.
            assert self.network is not None
            await self.network.deliver(
                self.node_id, message.sender, Message("getchain", 0, self.node_id)
            )
            return

        self.stats.blocks_received += 1
        if moved:
            self._prune_mempool()
        await self.broadcast(message, exclude=message.sender)

    def _absorb_chain(self, blocks: list[Block]) -> None:
        """Apply a peer's canonical chain, keeping whichever branch is heavier.

        This is the reconciliation path used after a partition heals. Note that
        adopting a heavier branch may DISCARD locally confirmed blocks -- which
        is precisely the reorganisation that makes proof-of-work finality
        probabilistic rather than absolute.
        """
        before = self.chain.reorg_count
        for block in blocks:
            if self.chain.has(block.hash):
                continue
            try:
                self.chain.add_block(block, check_pow=False)
            except (KeyError, ValueError):
                continue
        self.stats.reorgs += self.chain.reorg_count - before
        self._prune_mempool()

    def _prune_mempool(self) -> None:
        """Drop transactions already included in the canonical chain.

        Transactions on an orphaned branch are NOT dropped: they return to the
        mempool automatically because they are no longer in the canonical chain.
        """
        included = {tx.txid for block in self.chain.canonical() for tx in block.txs}
        self.mempool = {k: v for k, v in self.mempool.items() if k not in included}

    # -- mining ------------------------------------------------------------- #

    def _select_transactions(self, limit: int = 4) -> tuple[Transaction, ...]:
        """Pick transactions that are valid in sequence, highest fee first."""
        state = self.state()
        chosen: list[Transaction] = []
        for tx in sorted(self.mempool.values(), key=lambda t: (-t.fee, t.nonce)):
            if len(chosen) >= limit:
                break
            if state.validate(tx) is None:
                state.apply(tx)
                chosen.append(tx)
        return tuple(chosen)

    async def mine_forever(self, mean_block_time: float) -> None:
        """Produce blocks at exponentially distributed intervals.

        The rate is proportional to this node's hash power, so a node with twice
        the share finds blocks twice as often -- the fair-lottery property of a
        memoryless search.
        """
        while self.running:
            rate = self.hash_power / max(mean_block_time, 1e-6)
            await asyncio.sleep(self._rng.expovariate(rate))
            if not self.running:
                return

            block = make_block(
                self.chain,
                txs=self._select_transactions(),
                miner=self.node_id,
                timestamp=asyncio.get_running_loop().time(),
            )
            self.chain.add_block(block, check_pow=False)
            self.stats.blocks_mined += 1
            self._prune_mempool()
            await self.broadcast(Message("block", block, self.node_id))

    async def run(self) -> None:
        self.running = True
        while self.running:
            try:
                message = await asyncio.wait_for(self.inbox.get(), timeout=0.1)
            except TimeoutError:
                continue
            await self.handle(message)

    async def announce_head(self) -> None:
        await self.broadcast(Message("head", self.chain.tip, self.node_id))

    def stop(self) -> None:
        self.running = False


# --------------------------------------------------------------------------- #
# Scenario driver
# --------------------------------------------------------------------------- #


@dataclass
class SimulationResult:
    nodes: dict[str, Node]
    network: Network
    events: list[str] = field(default_factory=list)

    def heights(self) -> dict[str, int]:
        return {nid: n.height for nid, n in self.nodes.items()}

    def tips(self) -> dict[str, str]:
        return {nid: n.tip_hash[:12] for nid, n in self.nodes.items()}

    def converged(self) -> bool:
        return len({n.tip_hash for n in self.nodes.values()}) == 1

    def report(self) -> str:
        lines = ["node    height  tip           mined  recv  txs  reorgs"]
        lines.append("-" * 58)
        for nid, node in sorted(self.nodes.items()):
            s = node.stats
            lines.append(
                f"{nid:<8}{node.height:>6}  {node.tip_hash[:12]}"
                f"{s.blocks_mined:>7}{s.blocks_received:>6}"
                f"{s.txs_received:>5}{s.reorgs:>8}"
            )
        lines.append("")
        lines.append(f"messages delivered : {self.network.delivered}")
        lines.append(f"messages dropped   : {self.network.dropped}  (partitioned away)")
        lines.append(f"consensus reached  : {self.converged()}")
        return "\n".join(lines)


def build_network(
    node_count: int = 8,
    *,
    peers_per_node: int = 3,
    latency: float = 0.01,
    seed: int = 7,
    initial_balances: dict[str, int] | None = None,
) -> tuple[Network, dict[str, Node]]:
    """Create a randomly connected but fully reachable topology.

    Every node is first linked into a ring, guaranteeing connectivity, then
    extra random edges are added. Real clients do something comparable: they
    deliberately spread peer selection across address ranges so that an attacker
    cannot easily surround a node -- the eclipse attack.
    """
    rng = random.Random(seed)
    balances = initial_balances or {f"user{i}": 1000 for i in range(4)}
    genesis = genesis_block()

    network = Network(latency=latency, seed=seed)
    nodes: dict[str, Node] = {}
    for i in range(node_count):
        node = Node(f"node{i}", initial_balances=balances, genesis=genesis)
        nodes[node.node_id] = node
        network.register(node)

    ids = list(nodes)
    for i, nid in enumerate(ids):
        neighbour = ids[(i + 1) % len(ids)]
        nodes[nid].peers.append(neighbour)
        nodes[neighbour].peers.append(nid)

    for nid in ids:
        while len(nodes[nid].peers) < peers_per_node:
            other = rng.choice(ids)
            if other != nid and other not in nodes[nid].peers:
                nodes[nid].peers.append(other)
                nodes[other].peers.append(nid)

    for node in nodes.values():
        node.peers = sorted(set(node.peers))

    return network, nodes
