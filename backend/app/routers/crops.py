from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_role
from app.database import get_db
from app.models import CropListing, User, UserRole
from app.schemas import (
    CropListingBulkSync,
    CropListingCreate,
    CropListingOut,
    CropListingPublicOut,
)
from app.services.listing_pricing import apply_fair_price

router = APIRouter(prefix="/crop-listings", tags=["crop-listings"])


def _new_listing(db: Session, user: User, payload: CropListingCreate) -> CropListing:
    listing = CropListing(farmer_id=user.id, synced_at=datetime.now(timezone.utc), **payload.model_dump())
    # Price it now, so the listing is purchasable straight away instead of
    # waiting for the next scheduled recompute_fair_prices run.
    apply_fair_price(db, listing, user.district)
    db.add(listing)
    return listing


@router.post("", response_model=CropListingOut, status_code=201)
def create_listing(
    payload: CropListingCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.FARMER, UserRole.STUDENT_AGENT)),
):
    listing = _new_listing(db, user, payload)
    db.commit()
    db.refresh(listing)
    return listing


@router.post("/sync", response_model=List[CropListingOut])
def bulk_sync(
    payload: CropListingBulkSync,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.FARMER, UserRole.STUDENT_AGENT)),
):
    """The mobile app calls this once when connectivity returns, flushing
    every offline scan queued in local SQLite/Hive. Dedupes on
    offline_scan_uuid (per user) so a retried sync never double-creates
    listings."""
    created = []
    for item in payload.listings:
        if item.offline_scan_uuid:
            # Scoped to the caller: a uuid is a client-chosen string, so
            # without this filter one user could submit another user's uuid
            # and be handed that user's listing back.
            existing = (
                db.query(CropListing)
                .filter(
                    CropListing.offline_scan_uuid == item.offline_scan_uuid,
                    CropListing.farmer_id == user.id,
                )
                .first()
            )
            if existing:
                created.append(existing)
                continue
        listing = _new_listing(db, user, item)
        db.flush()
        created.append(listing)
    db.commit()
    for listing in created:
        db.refresh(listing)
    return created


@router.get("", response_model=List[CropListingPublicOut])
def list_listings(
    crop_type: Optional[str] = None,
    status: str = "available",
    db: Session = Depends(get_db),
):
    query = db.query(CropListing).filter(CropListing.status == status)
    if crop_type:
        query = query.filter(CropListing.crop_type == crop_type.strip().lower())
    return query.order_by(CropListing.created_at.desc()).limit(200).all()


@router.get("/mine", response_model=List[CropListingOut])
def my_listings(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.query(CropListing).filter(CropListing.farmer_id == user.id).all()
