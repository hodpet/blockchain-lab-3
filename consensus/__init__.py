"""Lab 03 -- consensus mechanisms and peer-to-peer state propagation.

Three subsystems over a shared ledger:

    chain.py    blocks, transactions, account state, heaviest-chain fork choice
    pow.py      parallel mining, difficulty retargeting, interval statistics
    pos.py      Casper FFG justification/finalisation, slashing, inactivity leak
    network.py  asyncio gossip network with partitions and reconciliation

Standard library only.
"""

from . import chain, network, pos, pow  # noqa: A004,F401

__all__ = ["chain", "network", "pos", "pow"]
__version__ = "1.0.0"
