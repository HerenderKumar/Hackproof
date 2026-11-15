"""Track a shared trip budget: who paid what, and who owes whom."""

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class Expense:
    payer: str
    amount_cents: int
    shared_by: tuple[str, ...]


def balances(expenses: list[Expense]) -> dict[str, int]:
    owed: dict[str, int] = defaultdict(int)
    for expense in expenses:
        share, extra = divmod(expense.amount_cents, len(expense.shared_by))
        owed[expense.payer] += expense.amount_cents
        for index, person in enumerate(sorted(expense.shared_by)):
            owed[person] -= share + (1 if index < extra else 0)
    return dict(owed)


def settle(owed: dict[str, int]) -> list[tuple[str, str, int]]:
    debtors = sorted((cents, name) for name, cents in owed.items() if cents < 0)
    creditors = sorted(((cents, name) for name, cents in owed.items() if cents > 0), reverse=True)
    moves = []
    while debtors and creditors:
        debt, debtor = debtors.pop(0)
        credit, creditor = creditors.pop(0)
        paid = min(-debt, credit)
        moves.append((debtor, creditor, paid))
        if -debt > paid:
            debtors.insert(0, (debt + paid, debtor))
        if credit > paid:
            creditors.insert(0, (credit - paid, creditor))
    return moves
