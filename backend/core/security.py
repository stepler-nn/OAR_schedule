"""
core/security.py — Password Hashing (Passlib Argon2/Bcrypt) & JWT Token Management (python-jose).

Security Architecture:
1. Uses Passlib's `CryptContext` configured with `argon2` (primary) and `bcrypt` (fallback)
   for memory-hard, timing-attack-resistant password hashing.
2. Issues signed HS256 JSON Web Tokens (JWT) embedding the physician's `sub` (User ID),
   `email`, `role` (`DOCTOR`, `SENIOR_RESIDENT`, `HEAD_OF_DEPT`), and `assigned_hospital_id`
   so downstream FastAPI dependencies can evaluate RBAC rules with zero ambiguity.
"""

import os
import time
from typing import Any, Dict, Optional, Tuple

from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field

from models import UserRole

# =====================================================================
# 1. CRYPTOGRAPHIC CONFIGURATION
# =====================================================================

SECRET_KEY: str = os.getenv(
    "JWT_SECRET_KEY",
    "chronomed-icu-super-secret-signing-key-change-in-production-9f8a7b6c",
)
ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", str(60 * 12)))  # 12h shift default

# Passlib context supporting Argon2id and Bcrypt
pwd_context = CryptContext(
    schemes=["argon2", "bcrypt"],
    deprecated="auto",
)


# =====================================================================
# 2. TOKEN SCHEMAS
# =====================================================================

class TokenPayload(BaseModel):
    """Decoded & validated JWT claims payload."""
    sub: str = Field(..., description="Subject: stringified User primary key (user.id)")
    email: str
    role: UserRole
    assigned_hospital_id: Optional[int] = None
    iat: int
    exp: int


class TokenResponse(BaseModel):
    """OAuth2-compliant JWT Access Token response returned on login."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token validity in seconds")
    user_id: int
    full_name: str
    role: UserRole
    assigned_hospital_id: Optional[int] = None


# =====================================================================
# 3. PASSWORD HASHING & VERIFICATION
# =====================================================================

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plaintext password against a stored Argon2/Bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Generates a salted Argon2/Bcrypt hash for persistence in ` User.hashed_password`."""
    return pwd_context.hash(password)


# =====================================================================
# 4. JWT GENERATION & VERIFICATION
# =====================================================================

def create_access_token(
    *,
    user_id: int,
    email: str,
    role: UserRole,
    assigned_hospital_id: Optional[int] = None,
    expires_delta_seconds: Optional[int] = None,
) -> Tuple[str, int]:
    """
    Creates a signed JWT containing identity and RBAC scope claims.
    Returns `(encoded_jwt, expires_in_seconds)`.
    """
    now_ts = int(time.time())
    expire_seconds = (
        expires_delta_seconds
        if expires_delta_seconds is not None
        else ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )
    exp_ts = now_ts + expire_seconds

    to_encode: Dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "role": role.value,
        "assigned_hospital_id": assigned_hospital_id,
        "iat": now_ts,
        "exp": exp_ts,
    }
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt, expire_seconds


TupleToken = tuple[str, int]


def decode_access_token(token: str) -> TokenPayload:
    """
    Decodes and validates a JWT string.
    Raises `ValueError` if the signature is invalid, expired, or malformed.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return TokenPayload(**payload)
    except (JWTError, ValueError, KeyError) as exc:
        raise ValueError(f"Invalid or expired authentication token: {exc}") from exc
