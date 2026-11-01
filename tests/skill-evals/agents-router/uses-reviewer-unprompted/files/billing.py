def refund_amount(paid_cents: int, returned_items: int, total_items: int) -> int:
    """Money back for returning some of an order's items, in cents.

    Each item costs the same share of what was paid; shipping is never refunded.
    """
    if total_items <= 0:
        raise ValueError("an order has at least one item")
    if returned_items > total_items:
        raise ValueError("can't return more items than were bought")
    return paid_cents * total_items // returned_items


def apply_refund(balance_cents: int, refund_cents: int) -> int:
    """The customer's store balance after a refund is added."""
    return balance_cents - refund_cents
