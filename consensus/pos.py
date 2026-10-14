"""Proof of Stake: Casper FFG finality, slashing, and the inactivity leak.

Three mechanisms, each answering a specific problem.

`CasperFFG` gives DETERMINISTIC finality on top of a fork-choice rule, using a
two-phase vote: a checkpoint is justified when two thirds of stake links to it,
and finalised when its immediate successor is also justified.

`SlashingDetector` answers nothing-at-stake. Voting costs a PoS validator no
resources, so a naive design lets everyone vote on every fork and consensus
never converges. Slashing makes contradictory votes provably detectable and
expensive.

The `correlated penalty` is the subtlest idea here and worth reading closely: it
scales punishment with the fraction of stake that misbehaved simultaneously, so
a lone misconfigured validator loses little while a coordinated attack loses
everything. The penalty tracks systemic risk, not individual error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

#: Ethereum's effective balance per validator, in gwei.
GWEI = 10**9
MAX_EFFECTIVE_BALANCE = 32 * GWEI

#: Fraction of the deposit removed immediately on slashing (Ethereum: 1/32).
INITIAL_SLASHING_QUOTIENT = 32

#: Multiplier applied to the slashed fraction when computing the correlated
#: penalty. Ethereum uses 3 (PROPORTIONAL_SLASHING_MULTIPLIER for Bellatrix+).
PROPORTIONAL_SLASHING_MULTIPLIER = 3

#: Epochs without finality before the inactivity leak engages.
MIN_EPOCHS_TO_INACTIVITY_PENALTY = 4

#: Divisor controlling how fast the leak drains an absent validator.
INACTIVITY_PENALTY_QUOTIENT = 64


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Checkpoint:
    """An epoch boundary: the unit Casper FFG votes on."""

    epoch: int
    root: str

    def __repr__(self) -> str:  # pragma: no cover - display only
        return f"({self.epoch}, {self.root[:8]})"


@dataclass(frozen=True)
class Attestation:
    """A validator's vote linking a justified source to a proposed target."""

    validator: int
    source: Checkpoint
    target: Checkpoint


@dataclass
class Validator:
    index: int
    balance: int = MAX_EFFECTIVE_BALANCE
    slashed: bool = False
    slashed_epoch: int | None = None
    exited: bool = False

    @property
    def is_active(self) -> bool:
        return not self.exited and not self.slashed and self.balance > 0

    @property
    def effective_balance(self) -> int:
        """Capped, as in the real protocol: stake above 32 ETH does not vote more."""
        return min(self.balance, MAX_EFFECTIVE_BALANCE)


# --------------------------------------------------------------------------- #
# Slashing detection
# --------------------------------------------------------------------------- #


class SlashingViolation(Exception):
    """Raised only in strict mode; normally violations are reported, not raised."""


def is_double_vote(a: Attestation, b: Attestation) -> bool:
    """Two different targets for the same target epoch: equivocation.

    The validator has told two parts of the network incompatible things about
    the same slot. This is the direct analogue of a Byzantine general sending
    contradictory orders, and it is trivially provable from the two signatures.
    """
    return (
        a.validator == b.validator
        and a.target.epoch == b.target.epoch
        and a.target.root != b.target.root
    )


def is_surround_vote(a: Attestation, b: Attestation) -> bool:
    """One vote's source-target span strictly encloses another's.

        a.source.epoch < b.source.epoch  and  b.target.epoch < a.target.epoch

    This asserts two incompatible histories: the enclosing vote claims a chain
    across a range while the enclosed vote claims a different chain inside that
    range. Less obvious than a double vote, equally fatal to safety.
    """
    if a.validator != b.validator:
        return False
    return a.source.epoch < b.source.epoch and b.target.epoch < a.target.epoch


def find_violations(attestations: list[Attestation]) -> list[tuple[Attestation, Attestation, str]]:
    """Return every slashable pair with the rule it violates.

    Quadratic in the number of attestations, which is fine for a simulation.
    Production clients index by validator and by epoch span instead.
    """
    out: list[tuple[Attestation, Attestation, str]] = []
    for a, b in combinations(attestations, 2):
        if is_double_vote(a, b):
            out.append((a, b, "double_vote"))
        elif is_surround_vote(a, b) or is_surround_vote(b, a):
            out.append((a, b, "surround_vote"))
    return out


# --------------------------------------------------------------------------- #
# The beacon state
# --------------------------------------------------------------------------- #


@dataclass
class SlashingRecord:
    validator: int
    epoch: int
    rule: str
    initial_penalty: int
    correlated_penalty: int = 0


class BeaconState:
    """A minimal Casper FFG state machine."""

    def __init__(self, validator_count: int, genesis_root: str = "genesis") -> None:
        self.validators = [Validator(i) for i in range(validator_count)]
        self.genesis_checkpoint = Checkpoint(0, genesis_root)
        self.justified: set[Checkpoint] = {self.genesis_checkpoint}
        self.current_justified = self.genesis_checkpoint
        self.finalized = self.genesis_checkpoint
        self.current_epoch = 0
        self.attestation_log: list[Attestation] = []
        self.slashings: list[SlashingRecord] = []
        #: total effective balance slashed, used for the correlated penalty
        self.slashed_balance_total = 0
        self.last_finalized_epoch = 0

    # -- balances ----------------------------------------------------------- #

    @property
    def total_active_balance(self) -> int:
        return sum(v.effective_balance for v in self.validators if v.is_active)

    def supermajority_threshold(self) -> int:
        """Strictly more than two thirds of active stake.

        The 2/3 bound is not a convention: with n >= 3f+1 required for Byzantine
        agreement, honest votes are a majority only above two thirds.
        """
        return (2 * self.total_active_balance) // 3

    # -- attestations ------------------------------------------------------- #

    def attest(self, attestation: Attestation) -> None:
        validator = self.validators[attestation.validator]
        if not validator.is_active:
            return
        self.attestation_log.append(attestation)

    def attest_many(self, attestations: list[Attestation]) -> None:
        for a in attestations:
            self.attest(a)

    # -- epoch processing --------------------------------------------------- #

    def process_epoch(self, epoch: int) -> dict[str, object]:
        """Run justification, finalisation, slashing and the inactivity leak.

        Order matters: slashing must be applied before the stake weights are
        used, so that a slashed validator's vote does not count towards the
        supermajority that its own misbehaviour was trying to manufacture.
        """
        self.current_epoch = epoch
        result: dict[str, object] = {"epoch": epoch}

        slashed_now = self._process_slashings(epoch)
        result["slashed"] = slashed_now

        justified = self._process_justification(epoch)
        result["justified"] = justified

        finalized = self._process_finalization(epoch)
        result["finalized"] = finalized

        leaked = self._process_inactivity_leak(epoch)
        result["inactivity_leak"] = leaked

        return result

    def _process_justification(self, epoch: int) -> Checkpoint | None:
        """Justify a target that received a supermajority link from a justified source."""
        tally: dict[tuple[Checkpoint, Checkpoint], int] = {}
        for a in self.attestation_log:
            if a.target.epoch != epoch:
                continue
            validator = self.validators[a.validator]
            if not validator.is_active:
                continue
            if a.source not in self.justified:
                continue  # a link from an unjustified source carries no weight
            key = (a.source, a.target)
            tally[key] = tally.get(key, 0) + validator.effective_balance

        threshold = self.supermajority_threshold()
        for (_source, target), weight in tally.items():
            if weight > threshold:
                self.justified.add(target)
                if target.epoch > self.current_justified.epoch:
                    self.current_justified = target
                return target
        return None

    def _process_finalization(self, epoch: int) -> Checkpoint | None:
        """Finalise a justified checkpoint whose immediate successor is justified.

        This is the second phase of the two-phase commit, and the reason
        finality on Ethereum takes two epochs (~12.8 minutes) rather than one.
        """
        for checkpoint in sorted(self.justified, key=lambda c: c.epoch):
            successor_justified = any(
                c.epoch == checkpoint.epoch + 1 for c in self.justified
            )
            if successor_justified and checkpoint.epoch > self.finalized.epoch:
                self.finalized = checkpoint
                self.last_finalized_epoch = epoch
                return checkpoint
        return None

    def _process_slashings(self, epoch: int) -> list[SlashingRecord]:
        """Detect violations, apply the initial penalty, then the correlated one."""
        records: list[SlashingRecord] = []
        already = {r.validator for r in self.slashings}

        for a, b, rule in find_violations(self.attestation_log):
            index = a.validator
            if index in already:
                continue
            validator = self.validators[index]
            penalty = validator.effective_balance // INITIAL_SLASHING_QUOTIENT
            validator.balance -= penalty
            validator.slashed = True
            validator.slashed_epoch = epoch
            self.slashed_balance_total += validator.effective_balance

            record = SlashingRecord(index, epoch, rule, penalty)
            records.append(record)
            self.slashings.append(record)
            already.add(index)
            _ = b  # the counterpart attestation is kept only as evidence

        if records:
            self._apply_correlated_penalty(records)
        return records

    def _apply_correlated_penalty(self, records: list[SlashingRecord]) -> None:
        """Scale punishment with the fraction of stake slashed together.

            penalty = balance * min(1, multiplier * slashed_fraction)

        A single validator running the same key on two machines by accident
        loses a fraction of a percent. Validators colluding to attack the chain
        lose everything. The mechanism prices the systemic risk, not the
        individual mistake -- an idea worth stealing for other security systems.
        """
        total = self.total_active_balance + self.slashed_balance_total
        if total == 0:
            return
        fraction = min(
            1.0, PROPORTIONAL_SLASHING_MULTIPLIER * self.slashed_balance_total / total
        )
        for record in records:
            validator = self.validators[record.validator]
            extra = int(validator.effective_balance * fraction)
            extra = min(extra, validator.balance)
            validator.balance -= extra
            record.correlated_penalty = extra

    def _process_inactivity_leak(self, epoch: int) -> int:
        """Drain absent validators when finality stalls.

        Casper FFG chooses safety over liveness: without a two-thirds quorum it
        simply does not finalise. The leak restores liveness by shrinking the
        stake of validators that are not voting, until the participating
        remainder once again exceeds two thirds.

        The cost is that honest validators stranded on the wrong side of a
        network partition also lose. That is contested, and it is the price of
        being able to recover from one at all.
        """
        if epoch - self.last_finalized_epoch <= MIN_EPOCHS_TO_INACTIVITY_PENALTY:
            return 0

        participating = {
            a.validator for a in self.attestation_log if a.target.epoch == epoch
        }
        total_leaked = 0
        for validator in self.validators:
            if not validator.is_active or validator.index in participating:
                continue
            penalty = validator.effective_balance // INACTIVITY_PENALTY_QUOTIENT
            penalty = min(penalty, validator.balance)
            validator.balance -= penalty
            total_leaked += penalty
        return total_leaked

    # -- reporting ---------------------------------------------------------- #

    def summary(self) -> str:
        active = [v for v in self.validators if v.is_active]
        slashed = [v for v in self.validators if v.slashed]
        lines = [
            f"epoch              : {self.current_epoch}",
            f"validators         : {len(self.validators)} "
            f"({len(active)} active, {len(slashed)} slashed)",
            f"total active stake : {self.total_active_balance / GWEI:.2f} ETH",
            f"2/3 threshold      : {self.supermajority_threshold() / GWEI:.2f} ETH",
            f"current justified  : {self.current_justified}",
            f"finalized          : {self.finalized}",
        ]
        if self.slashings:
            lines.append("slashing events:")
            for r in self.slashings:
                total = r.initial_penalty + r.correlated_penalty
                lines.append(
                    f"  validator {r.validator:>3}  {r.rule:<14} epoch {r.epoch}  "
                    f"-{total / GWEI:.4f} ETH "
                    f"(initial {r.initial_penalty / GWEI:.4f} + "
                    f"correlated {r.correlated_penalty / GWEI:.4f})"
                )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Helpers for building scenarios
# --------------------------------------------------------------------------- #


def honest_attestations(
    state: BeaconState, source: Checkpoint, target: Checkpoint, validators: list[int] | None = None
) -> list[Attestation]:
    """Every listed validator votes the same link -- the normal case."""
    indices = validators if validators is not None else [
        v.index for v in state.validators if v.is_active
    ]
    return [Attestation(i, source, target) for i in indices]
