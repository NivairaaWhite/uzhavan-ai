import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require_role
from app.database import get_db
from app.models import LogisticsPool, User, UserRole
from app.schemas import LogisticsPoolOut
from app.tasks import run_logistics_pooling

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/logistics", tags=["logistics"])


@router.post("/run", response_model=dict)
def trigger_pooling(user: User = Depends(require_role(UserRole.ADMIN))):
    """Manually trigger a pooling pass (normally runs hourly via Celery
    beat — see app/celery_app.py)."""
    try:
        result = run_logistics_pooling.delay()
    except Exception as exc:  # broker (Redis) unreachable, serializer error, ...
        logger.exception("Could not enqueue logistics pooling")
        raise HTTPException(status_code=503, detail="Task queue unavailable — is Redis / the Celery worker running?") from exc
    return {"task_id": result.id, "status": "queued"}


@router.get("/pools", response_model=List[LogisticsPoolOut])
def list_pools(db: Session = Depends(get_db), status: str = "ready"):
    return db.query(LogisticsPool).filter(LogisticsPool.status == status).all()
