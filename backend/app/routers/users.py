from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    normalize_phone,
    verify_password,
)
from app.database import get_db
from app.models import User, UserRole
from app.schemas import Token, UserCreate, UserOut

router = APIRouter(prefix="/auth", tags=["users"])

# Roles a person may choose for themselves. ADMIN is deliberately absent:
# an open /register that accepted role=admin would hand every caller full
# control (logistics, facility onboarding, ...). Create admins with
# `python -m app.create_admin` instead.
SELF_REGISTER_ROLES = {UserRole.FARMER, UserRole.STUDENT_AGENT, UserRole.BUYER}


@router.post("/register", response_model=UserOut, status_code=201)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    if payload.role not in SELF_REGISTER_ROLES:
        raise HTTPException(status_code=403, detail="This role cannot be self-registered")
    phone = normalize_phone(payload.phone_number)
    if db.query(User).filter(User.phone_number.in_({phone, payload.phone_number})).first():
        raise HTTPException(status_code=409, detail="Phone number already registered")
    user = User(
        full_name=payload.full_name,
        phone_number=phone,
        hashed_password=hash_password(payload.password),
        role=payload.role,
        preferred_language=payload.preferred_language,
        latitude=payload.latitude,
        longitude=payload.longitude,
        village=payload.village,
        district=payload.district,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # OAuth2PasswordRequestForm.username carries the phone number here.
    # Accept any common spelling of the number; also try the raw value so
    # accounts created before normalisation existed can still sign in.
    phone = normalize_phone(form_data.username)
    user = db.query(User).filter(User.phone_number.in_({phone, form_data.username})).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect phone number or password")
    token = create_access_token(subject=user.id, extra_claims={"role": user.role.value})
    return Token(access_token=token)


@router.get("/me", response_model=UserOut)
def read_current_user(user: User = Depends(get_current_user)):
    return user
