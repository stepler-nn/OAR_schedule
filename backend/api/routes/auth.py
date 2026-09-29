"""
api/routes/auth.py — Authentication & Current User Profile Routes.

Endpoints:
- `POST /api/v1/auth/login`: Authenticates a physician via email/password (supports both
  OAuth2 form-urlencoded and JSON payloads) and issues a signed JWT access token.
- `GET /api/v1/auth/me`: Returns the authenticated physician's profile, RBAC role,
  `assigned_hospital_id`, and clinical specialty skills.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user
from core.security import TokenResponse, create_access_token, verify_password
from database import get_session
from models import User, UserRole

router = APIRouter(prefix="/auth", tags=["Authentication"])


# =====================================================================
# 1. REQUEST & RESPONSE SCHEMAS
# =====================================================================

class LoginRequest(BaseModel):
    """JSON login payload alternative for SPA/PWA clients."""
    email: EmailStr
    password: str


class UserProfileResponse(BaseModel):
    """Public profile returned for the currently authenticated physician."""
    id: int
    email: str
    full_name: str
    role: UserRole
    assigned_hospital_id: Optional[int]
    skills: List[str]
    contract_weekly_hours: int
    is_active: bool


# =====================================================================
# 2. AUTHENTICATION ENDPOINTS
# =====================================================================

async def _authenticate_physician(
    session: AsyncSession,
    email: str,
    password: str,
) -> User:
    """Looks up user by email and verifies Argon2/Bcrypt password hash."""
    stmt = select(User).where(User.email == email.lower().strip())
    result = await session.execute(stmt)
    user = result.scalars().first()

    if user is None or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Physician account is inactive. Contact Head of Department.",
        )

    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="OAuth2 Form Login (returns JWT Access Token)",
)
async def login_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """
    Standard OAuth2 password flow (`username` field carries the physician's email).
    Issues a JWT containing `sub`, `role`, and `assigned_hospital_id`.
    """
    user = await _authenticate_physician(session, form_data.username, form_data.password)
    if user.id is None:
        raise HTTPException(status_code=500, detail="User record missing primary key.")

    token, expires_in = create_access_token(
        user_id=user.id,
        email=user.email,
        role=user.role,
        assigned_hospital_id=user.assigned_hospital_id,
    )
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        user_id=user.id,
        full_name=user.full_name,
        role=user.role,
        assigned_hospital_id=user.assigned_hospital_id,
    )


@router.post(
    "/login/json",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="JSON Payload Login for Vue 3 SPA / PWA",
)
async def login_json(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """JSON-friendly login endpoint for the Vue 3 Pinia auth store."""
    user = await _authenticate_physician(session, payload.email, payload.password)
    if user.id is None:
        raise HTTPException(status_code=500, detail="User record missing primary key.")

    token, expires_in = create_access_token(
        user_id=user.id,
        email=user.email,
        role=user.role,
        assigned_hospital_id=user.assigned_hospital_id,
    )
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        user_id=user.id,
        full_name=user.full_name,
        role=user.role,
        assigned_hospital_id=user.assigned_hospital_id,
    )


@router.get(
    "/me",
    response_model=UserProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Current Authenticated Physician Profile",
)
async def read_current_user(
    current_user: User = Depends(get_current_user),
) -> UserProfileResponse:
    """Returns the current user's identity, RBAC role, and hospital scope."""
    return UserProfileResponse(
        id=current_user.id or 0,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        assigned_hospital_id=current_user.assigned_hospital_id,
        skills=current_user.skills,
        contract_weekly_hours=current_user.contract_weekly_hours,
        is_active=current_user.is_active,
    )
