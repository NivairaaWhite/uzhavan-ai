import logging
from typing import List

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_role
from app.database import get_db
from app.models import CropListing, Order, OrderStatus, User, UserRole
from app.schemas import OrderCreate, OrderOut
from app.services.order_stock import cancel_pending_order_and_restock, reserve_quantity
from app.services.payments import razorpay_client, PaymentConfigError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])


def _create_reserved_order(db: Session, payload: OrderCreate, user: User) -> Order:
    """Validates, reserves stock and inserts the PENDING order in one
    transaction. Deliberately synchronous (no `await` between taking the row
    lock and committing) so the lock is never held across a network call."""
    listing = (
        db.query(CropListing)
        .filter(CropListing.id == payload.listing_id)
        .with_for_update()  # serialises concurrent buyers on Postgres
        .first()
    )
    if not listing or listing.status != "available":
        raise HTTPException(status_code=404, detail="Listing not available")
    if payload.quantity_kg > listing.quantity_kg:
        raise HTTPException(status_code=400, detail="Requested quantity exceeds listing")

    price = listing.fair_price_per_kg or listing.target_price_per_kg
    if not price:
        raise HTTPException(status_code=422, detail="Listing has no price set yet")

    order = Order(
        listing_id=listing.id,
        buyer_id=user.id,
        quantity_kg=payload.quantity_kg,
        agreed_price_per_kg=price,
        total_amount=round(price * payload.quantity_kg, 2),
        delivery_deadline=payload.delivery_deadline,
        status=OrderStatus.PENDING,
    )
    reserve_quantity(listing, payload.quantity_kg)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


@router.post("", response_model=OrderOut, status_code=201)
async def create_order(
    payload: OrderCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.BUYER)),
):
    order = _create_reserved_order(db, payload, user)

    try:
        rp_order = await razorpay_client.create_order(order.total_amount, receipt=f"order-{order.id}")
    except PaymentConfigError:
        # Sandbox/dev without Razorpay keys configured: keep the order so the
        # rest of the flow is testable; it stays PENDING with no payment
        # session and is released again by the stale-order sweeper.
        return order
    except httpx.HTTPError as exc:
        # Razorpay down / rejected the request. Give the stock back and tell
        # the client it's an upstream problem, not a bug in this server.
        logger.warning("Razorpay order creation failed for %s: %s", order.id, exc)
        cancel_pending_order_and_restock(db, order)
        db.commit()
        raise HTTPException(status_code=502, detail="Payment provider unavailable, please try again")

    order.razorpay_order_id = rp_order["id"]
    db.commit()
    db.refresh(order)
    return order


@router.post("/{order_id}/confirm-payment", response_model=OrderOut)
async def confirm_payment(
    order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    # 404 (not 403) for someone else's order so ids can't be probed.
    if not order or order.buyer_id != user.id:
        raise HTTPException(status_code=404, detail="Order not found")

    # Idempotent retry of an already-confirmed payment (e.g. flaky network).
    if order.status == OrderStatus.ESCROW_HELD and order.razorpay_payment_id == razorpay_payment_id:
        return order
    # A valid signature must not be able to drag a released / disputed /
    # cancelled order back into escrow.
    if order.status != OrderStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Order is {order.status.value}; payment can no longer be confirmed",
        )
    if not order.razorpay_order_id:
        raise HTTPException(status_code=409, detail="This order has no payment session (payments are not configured)")

    try:
        valid = await razorpay_client.verify_payment_signature(
            order.razorpay_order_id, razorpay_payment_id, razorpay_signature
        )
    except PaymentConfigError:
        raise HTTPException(status_code=503, detail="Payments are not configured on this server")
    if not valid:
        raise HTTPException(status_code=400, detail="Payment signature verification failed")

    order.razorpay_payment_id = razorpay_payment_id
    order.status = OrderStatus.ESCROW_HELD
    db.commit()
    db.refresh(order)
    return order


@router.get("/mine", response_model=List[OrderOut])
def my_orders(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.query(Order).filter(Order.buyer_id == user.id).order_by(Order.created_at.desc()).all()


@router.get("/received", response_model=List[OrderOut])
def received_orders(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.FARMER, UserRole.STUDENT_AGENT)),
):
    """Orders placed on the caller's own listings — without this a farmer
    has no way to see that anything sold."""
    return (
        db.query(Order)
        .join(CropListing, Order.listing_id == CropListing.id)
        .filter(CropListing.farmer_id == user.id)
        .order_by(Order.created_at.desc())
        .all()
    )
