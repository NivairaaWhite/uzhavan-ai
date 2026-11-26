"""Looks up the latest wholesale price for a crop and stamps a listing's
fair price from it.

Used in two places: when a listing is created/synced (so a brand-new scan
is purchasable immediately rather than "Listing has no price set yet" until
the next scheduled run) and by the recompute_fair_prices Celery task.
"""
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import CropListing, MarketPrice
from app.services.pricing import compute_fair_price


def latest_market_price(db: Session, crop_type: str, district: Optional[str] = None) -> Optional[MarketPrice]:
    """Newest price row for the crop, preferring the farmer's own district
    (case-insensitive) and falling back to the newest anywhere."""
    base = db.query(MarketPrice).filter(MarketPrice.crop_type == crop_type.strip().lower())
    if district and district.strip():
        local = (
            base.filter(func.lower(MarketPrice.district) == district.strip().lower())
            .order_by(MarketPrice.recorded_date.desc())
            .first()
        )
        if local:
            return local
    return base.order_by(MarketPrice.recorded_date.desc()).first()


def apply_fair_price(db: Session, listing: CropListing, district: Optional[str] = None) -> bool:
    """Sets listing.fair_price_per_kg if any price data exists. Returns
    whether a price was applied. Does not commit."""
    row = latest_market_price(db, listing.crop_type, district)
    if row is None:
        return False
    result = compute_fair_price(
        wholesale_price_per_kg=row.wholesale_price_per_kg,
        retail_price_per_kg=row.retail_price_per_kg,
    )
    listing.fair_price_per_kg = result.fair_price_per_kg
    return True
