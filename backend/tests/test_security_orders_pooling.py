"""Regression tests for bugs found while troubleshooting the project:
privilege escalation on /auth/register, order overselling, payment-confirm
state handling, the offline-sync round trip, pooling, and migrations."""
import hmac
import hashlib
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.database import SessionLocal
from app.models import (
    CropListing, LogisticsPool, MarketPrice, Order, OrderStatus, User, UserRole,
)
from app.services.payments import razorpay_client

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _listing_body(crop, qty=100, lat=11.0, lng=77.0, **extra):
    return {"crop_type": crop, "grade": "grade_a", "quantity_kg": qty,
            "target_price_per_kg": 20, "latitude": lat, "longitude": lng, **extra}


@pytest.fixture(autouse=True)
def _no_razorpay_keys(monkeypatch):
    monkeypatch.setattr(razorpay_client, "key_id", "")
    monkeypatch.setattr(razorpay_client, "key_secret", "")


# ---------------------------------------------------------------- auth

def test_admin_cannot_self_register(client):
    resp = client.post("/auth/register", json={
        "full_name": "Mallory", "phone_number": "+919500000001",
        "password": "secret123", "role": "admin",
    })
    assert resp.status_code == 403
    with SessionLocal() as db:
        assert db.query(User).filter(User.phone_number == "+919500000001").first() is None


def test_phone_number_spellings_are_one_account(client):
    payload = {"full_name": "N", "password": "secret123", "role": "farmer"}
    assert client.post("/auth/register", json={**payload, "phone_number": "9500000002"}).status_code == 201
    # Same person, different spelling -> must not create a second account...
    assert client.post("/auth/register", json={**payload, "phone_number": "+919500000002"}).status_code == 409
    # ...and every spelling can log in.
    for spelling in ("9500000002", "919500000002", "+919500000002", "+91 95000 00002"):
        resp = client.post("/auth/login", data={"username": spelling, "password": "secret123"})
        assert resp.status_code == 200, spelling


def test_create_admin_cli_creates_a_working_admin(client):
    from app.create_admin import main
    assert main(["+919500000003", "--name", "Ops", "--password", "longpassword1"]) == 0
    resp = client.post("/auth/login", data={"username": "+919500000003", "password": "longpassword1"})
    assert resp.status_code == 200
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {resp.json()['access_token']}"}).json()
    assert me["role"] == "admin"


def test_production_refuses_default_jwt_secret():
    from app.config import Settings
    with pytest.raises(ValueError):
        Settings(ENV="production", JWT_SECRET_KEY="CHANGE_ME_IN_PRODUCTION")
    with pytest.raises(ValueError):
        Settings(ENV="production", JWT_SECRET_KEY="short")
    assert Settings(ENV="production", JWT_SECRET_KEY="x" * 40).ENV == "production"
    Settings(ENV="development", JWT_SECRET_KEY="CHANGE_ME_IN_PRODUCTION")  # dev is fine


# ---------------------------------------------------------- listings/sync

def test_coordinates_are_range_checked(client, make_user):
    h = make_user("+919500000010", "farmer")
    assert client.post("/crop-listings", json=_listing_body("c1", lat=999), headers=h).status_code == 422
    assert client.post("/crop-listings", json=_listing_body("c1", lng=-500), headers=h).status_code == 422


def test_crop_type_is_normalised(client, make_user):
    h = make_user("+919500000011", "farmer")
    resp = client.post("/crop-listings", json=_listing_body("  Tomato "), headers=h)
    assert resp.status_code == 201
    assert resp.json()["crop_type"] == "tomato"


def test_sync_echoes_offline_uuid_so_the_phone_can_clear_its_queue(client, make_user):
    h = make_user("+919500000012", "farmer")
    resp = client.post("/crop-listings/sync", headers=h, json={"listings": [
        _listing_body("synccrop", offline_scan_uuid="uuid-A"),
        _listing_body("synccrop", offline_scan_uuid="uuid-B"),
    ]})
    assert resp.status_code == 200
    assert {r["offline_scan_uuid"] for r in resp.json()} == {"uuid-A", "uuid-B"}
    # Retrying is idempotent.
    again = client.post("/crop-listings/sync", headers=h, json={"listings": [
        _listing_body("synccrop", offline_scan_uuid="uuid-A")]})
    assert again.json()[0]["id"] == [r for r in resp.json() if r["offline_scan_uuid"] == "uuid-A"][0]["id"]


def test_sync_dedupe_is_scoped_to_the_calling_user(client, make_user):
    a = make_user("+919500000013", "farmer")
    b = make_user("+919500000014", "farmer")
    mine = client.post("/crop-listings/sync", headers=a, json={"listings": [
        _listing_body("scopecrop", offline_scan_uuid="shared-uuid")]}).json()[0]
    theirs = client.post("/crop-listings/sync", headers=b, json={"listings": [
        _listing_body("scopecrop", offline_scan_uuid="shared-uuid")]}).json()[0]
    assert theirs["id"] != mine["id"]
    assert theirs["farmer_id"] != mine["farmer_id"]


def test_public_marketplace_hides_farmer_identity_and_coarsens_location(client, make_user):
    h = make_user("+919500000015", "farmer")
    client.post("/crop-listings", headers=h, json=_listing_body("publiccrop", lat=11.123456, lng=77.987654))
    item = client.get("/crop-listings", params={"crop_type": "publiccrop"}).json()[0]
    assert "farmer_id" not in item
    assert (item["latitude"], item["longitude"]) == (11.12, 77.99)
    # The owner still sees full precision on /mine.
    mine = [l for l in client.get("/crop-listings/mine", headers=h).json() if l["crop_type"] == "publiccrop"][0]
    assert mine["latitude"] == 11.123456


def test_new_listing_gets_a_fair_price_immediately(client, make_user):
    with SessionLocal() as db:
        db.add(MarketPrice(crop_type="pricecrop", market_name="M", district="Coimbatore",
                           wholesale_price_per_kg=20.0, source="manual",
                           recorded_date=datetime.now(timezone.utc)))
        db.commit()
    h = make_user("+919500000016", "farmer", district="coimbatore")  # case differs on purpose
    body = _listing_body("pricecrop")
    body.pop("target_price_per_kg")
    listing = client.post("/crop-listings", json=body, headers=h).json()
    assert listing["fair_price_per_kg"] is not None and listing["fair_price_per_kg"] > 0


# ---------------------------------------------------------------- orders

def _listing(client, farmer_headers, crop, qty=100):
    return client.post("/crop-listings", json=_listing_body(crop, qty), headers=farmer_headers).json()["id"]


def test_orders_cannot_oversell_a_listing(client, make_user):
    farmer = make_user("+919500000020", "farmer")
    buyer = make_user("+919500000021", "buyer")
    lid = _listing(client, farmer, "orderscrop", qty=100)

    first = client.post("/orders", headers=buyer, json={"listing_id": lid, "quantity_kg": 60})
    assert first.status_code == 201
    too_much = client.post("/orders", headers=buyer, json={"listing_id": lid, "quantity_kg": 60})
    assert too_much.status_code == 400  # only 40 kg left

    rest = client.post("/orders", headers=buyer, json={"listing_id": lid, "quantity_kg": 40})
    assert rest.status_code == 201
    with SessionLocal() as db:
        listing = db.get(CropListing, lid)
        assert listing.quantity_kg == 0 and listing.status == "sold"
    # Sold-out listings leave the marketplace and can't be ordered.
    assert client.post("/orders", headers=buyer, json={"listing_id": lid, "quantity_kg": 1}).status_code == 404


def test_razorpay_outage_returns_502_and_releases_the_stock(client, make_user, monkeypatch):
    farmer = make_user("+919500000022", "farmer")
    buyer = make_user("+919500000023", "buyer")
    lid = _listing(client, farmer, "outagecrop", qty=50)

    monkeypatch.setattr(razorpay_client, "key_id", "rzp_test_x")
    monkeypatch.setattr(razorpay_client, "key_secret", "secret")

    async def boom(*a, **k):
        raise httpx.ConnectError("provider down")
    monkeypatch.setattr(razorpay_client, "create_order", boom)

    resp = client.post("/orders", headers=buyer, json={"listing_id": lid, "quantity_kg": 50})
    assert resp.status_code == 502
    with SessionLocal() as db:
        listing = db.get(CropListing, lid)
        assert listing.quantity_kg == 50 and listing.status == "available"


def _signed(order_id, payment_id, secret="secret"):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def _paid_order_setup(client, make_user, monkeypatch, suffix):
    farmer = make_user(f"+91951000{suffix}1", "farmer")
    buyer = make_user(f"+91951000{suffix}2", "buyer")
    monkeypatch.setattr(razorpay_client, "key_id", "rzp_test_x")
    monkeypatch.setattr(razorpay_client, "key_secret", "secret")

    async def fake_create(amount, receipt, notes=None):
        return {"id": f"order_{receipt}"}
    monkeypatch.setattr(razorpay_client, "create_order", fake_create)

    lid = _listing(client, farmer, f"paycrop{suffix}", qty=10)
    order = client.post("/orders", headers=buyer, json={"listing_id": lid, "quantity_kg": 5}).json()
    return farmer, buyer, order


def test_confirm_payment_happy_path_and_idempotent_retry(client, make_user, monkeypatch):
    _, buyer, order = _paid_order_setup(client, make_user, monkeypatch, "30")
    sig = _signed(order["razorpay_order_id"], "pay_1")
    url = f"/orders/{order['id']}/confirm-payment"
    params = {"razorpay_payment_id": "pay_1", "razorpay_signature": sig}
    resp = client.post(url, headers=buyer, params=params)
    assert resp.status_code == 200 and resp.json()["status"] == "escrow_held"
    assert client.post(url, headers=buyer, params=params).status_code == 200  # retry is safe


def test_confirm_payment_rejects_bad_signature_other_users_and_wrong_state(client, make_user, monkeypatch):
    farmer, buyer, order = _paid_order_setup(client, make_user, monkeypatch, "31")
    url = f"/orders/{order['id']}/confirm-payment"
    good = {"razorpay_payment_id": "pay_2", "razorpay_signature": _signed(order["razorpay_order_id"], "pay_2")}

    assert client.post(url, headers=buyer, params={**good, "razorpay_signature": "nope"}).status_code == 400
    # Someone who isn't the buyer can't confirm (or even see) the order.
    assert client.post(url, headers=farmer, params=good).status_code == 404

    # A released order can't be dragged back into escrow by replaying a signature.
    with SessionLocal() as db:
        o = db.get(Order, order["id"])
        o.status = OrderStatus.RELEASED
        db.commit()
    assert client.post(url, headers=buyer, params=good).status_code == 409
    with SessionLocal() as db:
        assert db.get(Order, order["id"]).status == OrderStatus.RELEASED


def test_confirm_payment_without_payment_session_is_a_clean_409(client, make_user):
    farmer = make_user("+919510003201", "farmer")
    buyer = make_user("+919510003202", "buyer")
    lid = _listing(client, farmer, "devmodecrop", qty=10)
    order = client.post("/orders", headers=buyer, json={"listing_id": lid, "quantity_kg": 1}).json()
    assert order["razorpay_order_id"] is None  # dev mode: no keys
    resp = client.post(f"/orders/{order['id']}/confirm-payment", headers=buyer,
                       params={"razorpay_payment_id": "p", "razorpay_signature": "s"})
    assert resp.status_code == 409  # was an unhandled 500


def test_farmer_can_see_orders_placed_on_their_listings(client, make_user):
    farmer = make_user("+919500000040", "farmer")
    buyer = make_user("+919500000041", "buyer")
    lid = _listing(client, farmer, "receivedcrop", qty=10)
    client.post("/orders", headers=buyer, json={"listing_id": lid, "quantity_kg": 2})
    received = client.get("/orders/received", headers=farmer).json()
    assert [o["listing_id"] for o in received] == [lid]
    assert client.get("/orders/received", headers=buyer).status_code == 403


def test_stale_pending_orders_are_cancelled_and_stock_restored(client, make_user):
    from app.tasks import expire_stale_pending_orders
    farmer = make_user("+919500000050", "farmer")
    buyer = make_user("+919500000051", "buyer")
    lid = _listing(client, farmer, "expirecrop", qty=30)
    fresh = client.post("/orders", headers=buyer, json={"listing_id": lid, "quantity_kg": 10}).json()
    stale = client.post("/orders", headers=buyer, json={"listing_id": lid, "quantity_kg": 20}).json()
    with SessionLocal() as db:
        db.get(Order, stale["id"]).created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()

    assert expire_stale_pending_orders() == 1
    with SessionLocal() as db:
        assert db.get(Order, stale["id"]).status == OrderStatus.CANCELLED
        assert db.get(Order, fresh["id"]).status == OrderStatus.PENDING
        listing = db.get(CropListing, lid)
        assert listing.quantity_kg == 20 and listing.status == "available"  # 30 - 10 still reserved


def test_logistics_trigger_returns_503_when_the_broker_is_down(client, make_user, monkeypatch):
    from app.tasks import run_logistics_pooling
    admin = make_user("+919500000060", "admin")

    def broken(*a, **k):
        raise ConnectionError("redis down")
    monkeypatch.setattr(run_logistics_pooling, "delay", broken)
    assert client.post("/logistics/run", headers=admin).status_code == 503


# --------------------------------------------------------------- pooling

def test_pool_district_comes_from_the_member_farmers(client, make_user):
    from app.tasks import run_logistics_pooling
    for i in range(3):
        h = make_user(f"+91952000000{i}", "farmer", district="Erode")
        client.post("/crop-listings", headers=h, json=_listing_body("poolcrop", lat=11.0 + i * 0.002, lng=77.0))
    assert run_logistics_pooling() >= 1
    with SessionLocal() as db:
        pools = db.query(LogisticsPool).filter(LogisticsPool.crop_type == "poolcrop").all()
        assert len(pools) == 1
        assert pools[0].district == "Erode"  # was always "Unknown"


def test_one_farmer_with_many_listings_does_not_form_a_pool(client, make_user):
    from app.tasks import run_logistics_pooling
    h = make_user("+919520000100", "farmer", district="Erode")
    for i in range(4):
        client.post("/crop-listings", headers=h, json=_listing_body("solocrop", lat=11.0 + i * 0.001, lng=77.0))
    run_logistics_pooling()
    with SessionLocal() as db:
        assert db.query(LogisticsPool).filter(LogisticsPool.crop_type == "solocrop").count() == 0


def test_matching_counts_distinct_farmers():
    from app.services.matching import PoolCandidate, cluster_listings
    same = [PoolCandidate(f"L{i}", 11.0 + i * 0.001, 77.0, 10, farmer_id="F1") for i in range(3)]
    assert cluster_listings(same, radius_km=8, min_farmers=3) == []
    mixed = [PoolCandidate(f"L{i}", 11.0 + i * 0.001, 77.0, 10, farmer_id=f"F{i}") for i in range(3)]
    assert len(cluster_listings(mixed, radius_km=8, min_farmers=3)) == 1


# ------------------------------------------------------------ migrations

def _alembic(args, db_url):
    env = {**os.environ, "DATABASE_URL": db_url}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND_DIR, env=env,
                          capture_output=True, text=True)


_PG_URL = os.environ.get("MIGRATION_TEST_DATABASE_URL")


@pytest.mark.parametrize("dialect", [
    "sqlite",
    pytest.param("postgres", marks=pytest.mark.skipif(
        not _PG_URL, reason="set MIGRATION_TEST_DATABASE_URL to an empty Postgres database")),
])
def test_migrations_upgrade_downgrade_upgrade(tmp_path, dialect):
    """Runs on SQLite always. With MIGRATION_TEST_DATABASE_URL pointing at an
    empty Postgres database it also covers enum-type cleanup — the bug fixed
    in the initial migration's downgrade only exists on Postgres."""
    url = f"sqlite:///{tmp_path / 'mig.db'}" if dialect == "sqlite" else _PG_URL
    for args in (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"], ["check"]):
        result = _alembic(args, url)
        assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr[-1500:]}"
