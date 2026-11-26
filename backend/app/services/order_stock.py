"""Stock reservation helpers for orders.

An order *reserves* stock by reducing the listing's remaining quantity at
creation time; the listing flips to "sold" when nothing is left. Cancelling
a still-PENDING order (payment provider failed, or the buyer never paid
within PENDING_ORDER_TTL_MINUTES) gives the stock back. Without this,
every buyer could order the full quantity of the same listing.
"""
from sqlalchemy.orm import Session

from app.models import CropListing, Order, OrderStatus

_EPS = 1e-6


def cancel_pending_order_and_restock(db: Session, order: Order) -> bool:
    """Cancels a PENDING order and returns its quantity to the listing.
    Returns False (and does nothing) for any other status. Does not commit."""
    if order.status != OrderStatus.PENDING:
        return False
    listing = (
        db.query(CropListing)
        .filter(CropListing.id == order.listing_id)
        .with_for_update()
        .first()
    )
    if listing is not None:
        listing.quantity_kg = round(listing.quantity_kg + order.quantity_kg, 3)
        if listing.status == "sold":
            listing.status = "available"
    order.status = OrderStatus.CANCELLED
    return True


def reserve_quantity(listing: CropListing, quantity_kg: float) -> None:
    """Caller must already hold the row lock and have validated availability."""
    listing.quantity_kg = round(listing.quantity_kg - quantity_kg, 3)
    if listing.quantity_kg <= _EPS:
        listing.quantity_kg = 0.0
        listing.status = "sold"
