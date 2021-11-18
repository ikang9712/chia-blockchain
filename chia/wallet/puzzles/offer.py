from dataclasses import dataclass
from typing import List, Optional, Dict, Set
from blspy import G2Element

from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.announcement import Announcement
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.ints import uint64
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD,
    SpendableCAT,
    construct_cat_puzzle,
    match_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.load_clvm import load_clvm
from chia.wallet.payment import Payment

OFFER_MOD = load_clvm("settlement_payments.clvm")
ZERO_32 = bytes32([0] * 32)


@dataclass(frozen=True)
class NotarizedPayment(Payment):
    nonce: bytes32 = ZERO_32

    def as_condition(self) -> Program:
        return Program.to([self.nonce, *self.as_condition_args()])

    @classmethod
    def from_condition(cls, condition: Program) -> "NotarizedPayment":
        p = Payment.from_condition(condition)
        return cls(*p.as_condition_args(), bytes32(next(condition.as_iter()).as_python()))


@dataclass(frozen=True)
class Offer:
    requested_payments: Dict[bytes32, List[NotarizedPayment]]  # The key is the asset id of the asset being requested
    bundle: SpendBundle

    @staticmethod
    def ph():
        return OFFER_MOD.get_tree_hash()

    @staticmethod
    def notarize_payments(
        requested_payments: Dict[Optional[bytes32], List[Payment]],  # `None` means you are requesting XCH
        coins: List[Coin],
    ) -> Dict[Optional[bytes32], List[NotarizedPayment]]:
        sorted_coins = sorted(coins, key=Coin.name)
        sorted_coin_list = [c.as_list() for c in sorted_coins]
        nonce = Program.to(sorted_coin_list).get_tree_hash()

        for tail_hash, payments in requested_payments.items():
            requested_payments[tail_hash] = [NotarizedPayment(*p.as_condition_args(), nonce) for p in payments]

        return requested_payments

    @staticmethod
    def calculate_announcements(
        notarized_payments: Dict[Optional[bytes32], List[NotarizedPayment]],
    ) -> List[Announcement]:
        announcements = []
        for tail, payments in notarized_payments.items():
            if tail:
                settlement_ph = construct_cat_puzzle(CAT_MOD, tail, OFFER_MOD).get_tree_hash()
            else:
                settlement_ph = OFFER_MOD.get_tree_hash()

            messages = [p.as_condition().get_tree_hash() for p in payments]
            announcements.extend([Announcement(settlement_ph, msg) for msg in messages])

        return announcements

    def __post_init__(self):
        # Verify that there is at least something being offered
        offered_coins = self.get_offered_coins()
        if offered_coins == {}:
            raise ValueError("Bundle is not offering anything")

        # Verify that there are no duplicate payments
        for payments in self.requested_payments.values():
            payment_programs = [p.name() for p in payments]
            if len(set(payment_programs)) != len(payment_programs):
                raise ValueError("Bundle has duplicate requested payments")

    def get_offered_coins(self) -> Dict[bytes32, List[Coin]]:
        offered_coins = {}

        for addition in self.bundle.additions():
            parent_puzzle = list(
                filter(lambda cs: cs.coin.name() == addition.parent_coin_info, self.bundle.coin_spends)
            )[0].puzzle_reveal
            matched, curried_args = match_cat_puzzle(parent_puzzle)
            if matched:
                _, tail_hash, _ = curried_args
                tail_hash = bytes32(tail_hash.as_python())
                offer_ph = construct_cat_puzzle(CAT_MOD, tail_hash, OFFER_MOD).get_tree_hash()
            else:
                tail_hash = None
                offer_ph = OFFER_MOD.get_tree_hash()

            if addition.puzzle_hash == offer_ph:
                if tail_hash in offered_coins:
                    offered_coins[tail_hash].append(addition)
                else:
                    offered_coins[tail_hash] = [addition]

        return offered_coins

    def get_offered_amounts(self) -> Dict[Optional[bytes32], int]:
        offered_coins = self.get_offered_coins()
        offered_amounts = {}
        for asset_id, coins in offered_coins.items():
            offered_amounts[asset_id] = uint64(sum([c.amount for c in coins]))
        return offered_amounts

    def get_requested_payments(self) -> Dict[bytes32, List[NotarizedPayment]]:
        return self.requested_payments

    def get_requested_amounts(self) -> Dict[Optional[bytes32], int]:
        requested_amounts = {}
        for asset_id, coins in self.requested_payments.items():
            requested_amounts[asset_id] = uint64(sum([c.amount for c in coins]))
        return requested_amounts

    def arbitrage(self) -> Dict[Optional[bytes32], int]:
        offered_amounts = self.get_offered_amounts()
        requested_amounts = self.get_requested_amounts()

        arbitrage_dict = {}
        for asset_id in [*requested_amounts.keys(), *offered_amounts.keys()]:
            arbitrage_dict[asset_id] = offered_amounts.get(asset_id, 0) - requested_amounts.get(asset_id, 0)

        return arbitrage_dict

    def is_valid(self) -> bool:
        return all([value >= 0 for value in self.arbitrage().values()])

    @staticmethod
    def aggregate(offers: List["Offer"]) -> "Offer":
        total_requested_payments: Dict[Optional[bytes32], List[NotarizedPayment]] = {}
        total_bundle = SpendBundle([], G2Element())
        for offer in offers:
            # First check for any overlap in inputs
            total_inputs: Set[Coin] = {cs.coin for cs in total_bundle.coin_spends}
            offer_inputs: Set[Coin] = {cs.coin for cs in offer.bundle.coin_spends}
            if total_inputs & offer_inputs:
                raise ValueError("The aggregated offers overlap inputs")

            # Next, do the aggregation
            for tail, payments in offer.requested_payments.items():
                if tail in total_requested_payments:
                    total_requested_payments[tail].extend(payments)
                else:
                    total_requested_payments[tail] = payments

            total_bundle = SpendBundle.aggregate([total_bundle, offer.bundle])

        return Offer(total_requested_payments, total_bundle)

    def to_valid_spend(self, arbitrage_ph: bytes32) -> SpendBundle:
        if not self.is_valid():
            raise ValueError("Offer is currently incomplete")

        completion_spends = []
        for tail_hash, payments in self.requested_payments.items():
            offered_coins = self.get_offered_coins()[tail_hash]
            arbitrage_amount = self.arbitrage()[tail_hash]
            all_payments = payments.copy()
            if arbitrage_amount > 0:
                all_payments.append(NotarizedPayment(arbitrage_ph, arbitrage_amount))

            for coin in offered_coins:
                inner_solution = Program.to(
                    [np.as_condition() for np in all_payments] if coin == offered_coins[0] else []
                )
                if tail_hash:
                    parent_spend = list(
                        filter(lambda cs: cs.coin.name() == coin.parent_coin_info, self.bundle.coin_spends)
                    )[0]
                    parent_coin = parent_spend.coin
                    matched, curried_args = match_cat_puzzle(parent_spend.puzzle_reveal)
                    assert matched
                    _, _, inner_puzzle = curried_args
                    spendable_cat = SpendableCAT(
                        coin,
                        tail_hash,
                        OFFER_MOD,
                        inner_solution,
                        lineage_proof=LineageProof(
                            parent_coin.parent_coin_info, inner_puzzle.get_tree_hash(), parent_coin.amount
                        ),
                    )
                    solution = (
                        unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable_cat]).coin_spends[0].solution
                    )
                else:
                    solution = inner_solution
                completion_spends.append(
                    CoinSpend(
                        coin,
                        construct_cat_puzzle(CAT_MOD, tail_hash, OFFER_MOD) if tail_hash else OFFER_MOD,
                        solution,
                    )
                )

        return SpendBundle.aggregate([SpendBundle(completion_spends, G2Element()), self.bundle])

    @classmethod
    def parse(cls, f) -> "Offer":
        parsed_bundle = SpendBundle.parse(f)
        return cls.from_bytes(bytes(parsed_bundle))

    def stream(self, f):
        as_spend_bundle = SpendBundle.from_bytes(bytes(self))
        as_spend_bundle.stream(f)

    def __bytes__(self) -> bytes:
        additional_coin_spends = []
        for tail_hash, payments in self.requested_payments.items():
            puzzle_reveal = construct_cat_puzzle(CAT_MOD, tail_hash, OFFER_MOD) if tail_hash else OFFER_MOD
            additional_coin_spends.append(
                CoinSpend(
                    Coin(
                        ZERO_32,
                        puzzle_reveal.get_tree_hash(),
                        0,
                    ),
                    puzzle_reveal,
                    Program.to([np.as_condition() for np in payments]),
                )
            )

        return bytes(
            SpendBundle.aggregate(
                [
                    SpendBundle(additional_coin_spends, G2Element()),
                    self.bundle,
                ]
            )
        )

    @classmethod
    def from_bytes(cls, as_bytes: bytes) -> "Offer":
        bundle = SpendBundle.from_bytes(as_bytes)
        requested_payments = {}
        leftover_coin_spends = []
        for coin_spend in bundle.coin_spends:
            if coin_spend.coin.parent_coin_info == ZERO_32:
                matched, curried_args = match_cat_puzzle(coin_spend.puzzle_reveal)
                if matched:
                    _, tail_hash, _ = curried_args
                    tail_hash = bytes32(tail_hash.as_python())
                else:
                    tail_hash = None
                requested_payments[tail_hash] = [
                    NotarizedPayment.from_condition(condition)
                    for condition in coin_spend.solution.to_program().as_iter()
                ]
            else:
                leftover_coin_spends.append(coin_spend)

        return Offer(requested_payments, SpendBundle(leftover_coin_spends, bundle.aggregated_signature))