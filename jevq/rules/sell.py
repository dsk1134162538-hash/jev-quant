from dataclasses import dataclass


@dataclass(frozen=True)
class SellDecision:
    sell: bool
    reason: str = ""
