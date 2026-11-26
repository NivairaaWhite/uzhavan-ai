"""
Background jobs: price scraping, fair-price recomputation, logistics
pooling, notifications.

Honesty note on scraping: Agmarknet and quick-commerce sites change their
markup often, and none of them are reachable from this build environment
to verify live selectors against. SOURCES config below is written against
Agmarknet's actual public "Price and Arrival Report" HTML report
structure (per DAC&FW documentation) and is the right place to fix
selectors first if a scrape starts returning zero rows — don't scatter
scraping logic elsewhere.
"""
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.celery_app import celery_app
from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    MarketPrice, CropListing, LogisticsPool, LogisticsPoolMember, Order, OrderStatus,
    ProcessingUnit, User,
)
from app.services.listing_pricing import apply_fair_price
from app.services.matching import cluster_listings, PoolCandidate
from app.services.order_stock import cancel_pending_order_and_restock

logger = logging.getLogger(__name__)
settings = get_settings()

TRACKED_CROPS = ["tomato", "onion", "paddy", "brinjal", "banana", "chilli"]
TN_DISTRICTS = ["Coimbatore", "Madurai", "Salem", "Erode", "Tiruchirappalli"]


@celery_app.task(name="app.tasks.scrape_agmarknet_prices")
def scrape_agmarknet_prices():
    """Pulls the daily wholesale 'Price and Arrival Report' for tracked
    crops across Tamil Nadu markets. Prefer the OGD India (data.gov.in)
    JSON API over HTML scraping when OGD_INDIA_API_KEY is set — it's more
    stable than parsing the report page."""
    inserted = 0
    with SessionLocal() as db:
        for crop in TRACKED_CROPS:
            try:
                rows = _fetch_agmarknet_for_crop(crop)
            except Exception as exc:  # noqa: BLE001 — scraping must never crash the beat schedule
                logger.warning("Agmarknet scrape failed for %s: %s", crop, exc)
                continue
            for row in rows:
                recorded = row.get("date") or datetime.now(timezone.utc)
                day_start = recorded.replace(hour=0, minute=0, second=0, microsecond=0)
                # One row per crop/market/day: re-running the scrape (beat +
                # a manual run, or a retry) updates the day's price instead of
                # stacking duplicates.
                existing = (
                    db.query(MarketPrice)
                    .filter(
                        MarketPrice.crop_type == crop,
                        MarketPrice.market_name == row["market_name"],
                        MarketPrice.district == row["district"],
                        MarketPrice.source == "agmarknet",
                        MarketPrice.recorded_date >= day_start,
                        MarketPrice.recorded_date < day_start + timedelta(days=1),
                    )
                    .first()
                )
                if existing:
                    existing.wholesale_price_per_kg = row["price_per_kg"]
                    continue
                db.add(MarketPrice(
                    crop_type=crop,
                    market_name=row["market_name"],
                    district=row["district"],
                    wholesale_price_per_kg=row["price_per_kg"],
                    source="agmarknet",
                    recorded_date=recorded,
                ))
                inserted += 1
        db.commit()
    logger.info("scrape_agmarknet_prices inserted %d rows", inserted)
    return inserted


def _fetch_agmarknet_for_crop(crop: str) -> list[dict]:
    """Uses the OGD India data.gov.in resource for Agmarknet's daily
    variety-wise arrivals/prices when an API key is configured (JSON,
    stable). Falls back to a documented-but-unverified HTML scrape
    otherwise — that path is the one to actively maintain."""
    if settings.OGD_INDIA_API_KEY:
        url = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"
        params = {
            "api-key": settings.OGD_INDIA_API_KEY,
            "format": "json",
            "filters[commodity]": crop,
            "filters[state]": "Tamil Nadu",
            "limit": 50,
        }
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            records = resp.json().get("records", [])
        rows = []
        for r in records:
            price = _rupees_per_quintal_to_per_kg(r.get("modal_price"))
            if price is None:
                continue  # missing / "NA" price — skip the row, not the whole crop
            try:
                arrived = datetime.strptime(r["arrival_date"], "%d/%m/%Y") if r.get("arrival_date") else None
            except ValueError:
                arrived = None
            rows.append({
                "market_name": r.get("market", "Unknown"),
                "district": r.get("district", "Unknown"),
                "price_per_kg": price,
                "date": arrived or datetime.now(timezone.utc),
            })
        return rows

    # Fallback: HTML report scrape. SELECTOR CONFIG — verify/update against
    # the live agmarknet.gov.in "Price and Arrival Report" table before
    # relying on this path in production.
    url = f"{settings.AGMARKNET_BASE_URL}/PriceAndArrivalReports/PriceAndArrivalReport.aspx"
    with httpx.Client(timeout=15) as client:
        resp = client.get(url, params={"commodity": crop, "state": "TN"})
        resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = []
    for tr in soup.select("table#ctl00_cphBody_gridRecords tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) >= 5:
            price = _rupees_per_quintal_to_per_kg(cells[4])
            if price is None:
                continue
            rows.append({
                "market_name": cells[1],
                "district": cells[0],
                "price_per_kg": price,
            })
    return rows


def _rupees_per_quintal_to_per_kg(raw) -> Optional[float]:
    """Agmarknet/OGD quote Rs per quintal (100 kg). Returns None for blank,
    non-numeric ("NA", "-") or non-positive values."""
    try:
        value = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return value / 100 if value > 0 else None


@celery_app.task(name="app.tasks.scrape_retail_prices")
def scrape_retail_prices():
    """Retail ceiling price from quick-commerce apps. These platforms do
    not offer public price APIs, so in production this must go through
    each platform's official partner/affiliate feed if one exists, or be
    replaced with manual daily entry — scraping their consumer apps
    directly generally breaches their Terms of Service. Wired here as a
    pluggable no-op so the pipeline shape is correct; see docs/ARCHITECTURE.md."""
    logger.info(
        "scrape_retail_prices: no compliant public retail price feed configured — "
        "see docs/ARCHITECTURE.md for manual-entry / partner-API options"
    )
    return 0


@celery_app.task(name="app.tasks.recompute_fair_prices")
def recompute_fair_prices():
    """Recomputes fair_price_per_kg for every open crop_listing using the
    latest MarketPrice row for its crop, preferring the farmer's own district
    and falling back to the newest price anywhere (see
    services/listing_pricing.py)."""
    updated = 0
    with SessionLocal() as db:
        rows = (
            db.query(CropListing, User.district)
            .join(User, User.id == CropListing.farmer_id)
            .filter(CropListing.status == "available")
            .all()
        )
        for listing, district in rows:
            if apply_fair_price(db, listing, district):
                updated += 1
        db.commit()
    logger.info("recompute_fair_prices updated %d listings", updated)
    return updated


@celery_app.task(name="app.tasks.run_logistics_pooling")
def run_logistics_pooling():
    """Groups available same-crop listings into logistics pools once enough
    *different* nearby farmers have unsold stock. A pool's district is the
    most common district among its member farmers."""
    created = 0
    with SessionLocal() as db:
        crop_types = [r[0] for r in db.query(CropListing.crop_type).distinct()]
        for crop in crop_types:
            rows = (
                db.query(CropListing, User.district)
                .join(User, User.id == CropListing.farmer_id)
                .filter(CropListing.crop_type == crop, CropListing.status == "available")
                .all()
            )
            if not rows:
                continue
            district_by_listing = {l.id: (d or "Unknown") for l, d in rows}
            candidates = [
                PoolCandidate(l.id, l.latitude, l.longitude, l.quantity_kg, farmer_id=l.farmer_id)
                for l, _ in rows
            ]
            for pool in cluster_listings(candidates):
                district = Counter(
                    district_by_listing[i] for i in pool.member_listing_ids
                ).most_common(1)[0][0]
                lp = LogisticsPool(
                    district=district,
                    crop_type=crop,
                    centroid_lat=pool.centroid_lat,
                    centroid_lng=pool.centroid_lng,
                    total_quantity_kg=pool.total_quantity_kg,
                    status="ready",
                )
                db.add(lp)
                db.flush()  # get lp.id
                for listing_id in pool.member_listing_ids:
                    db.add(LogisticsPoolMember(pool_id=lp.id, listing_id=listing_id))
                    db.query(CropListing).filter(CropListing.id == listing_id).update({"status": "pooled"})
                created += 1
        db.commit()
    logger.info("run_logistics_pooling created %d pools", created)
    return created


@celery_app.task(name="app.tasks.expire_stale_pending_orders")
def expire_stale_pending_orders():
    """Cancels PENDING orders that were never paid and returns their reserved
    stock to the listing (see services/order_stock.py)."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.PENDING_ORDER_TTL_MINUTES)
    expired = 0
    with SessionLocal() as db:
        stale = (
            db.query(Order)
            .filter(Order.status == OrderStatus.PENDING, Order.created_at < cutoff)
            .all()
        )
        for order in stale:
            if cancel_pending_order_and_restock(db, order):
                expired += 1
        db.commit()
    logger.info("expire_stale_pending_orders cancelled %d orders", expired)
    return expired


@celery_app.task(name="app.tasks.reset_daily_processing_intake")
def reset_daily_processing_intake():
    """Processing units quote a *daily* intake capacity
    (daily_intake_capacity_kg); remaining_intake_today_kg is what the
    post-harvest advisor actually allocates against and decrements over
    the day. Runs once at midnight IST so every unit starts the day at
    full quoted capacity again."""
    with SessionLocal() as db:
        updated = db.query(ProcessingUnit).filter(ProcessingUnit.is_active.is_(True)).update(
            {ProcessingUnit.remaining_intake_today_kg: ProcessingUnit.daily_intake_capacity_kg}
        )
        db.commit()
    logger.info("reset_daily_processing_intake reset %d units", updated)
    return updated
