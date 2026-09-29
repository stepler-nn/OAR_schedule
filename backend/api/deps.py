"""
api/deps.py — FastAPI Authentication & Role-Based Access Control (RBAC) Dependencies.

RBAC Enforcement Matrix:
1. `get_current_user`:
   - Extracts Bearer token from `Authorization` header, verifies JWT signature & expiration,
     and loads the active `User` record from SQLite.
2. `require_role(allowed_roles)`:
   - Dependency factory that restricts endpoint execution to specific roles, e.g.:
     `Depends(require_role([UserRole.HEAD_OF_DEPT]))`
3. `verify_hospital_access(user, hospital_id)` & `require_hospital_access`:
   - Enforces hospital scoping on mutations:
     * `HEAD_OF_DEPT`: Global access across all hospitals.
     * `SENIOR_RESIDENT`: Access permitted ONLY if `user.assigned_hospital_id == hospital_id`.
       Raises `403 Forbidden` if attempting to create/edit/approve data for another hospital.
     * `DOCTOR`: Read-only schedule visibility; raises `403 Forbidden` on hospital management actions.
"""

from __future__ import annotations

from typing import Callable, Coroutine, Any, Optional, Sequence

from fastapi import Depends, HTTPException, Path, Query, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import decode_access_token
from database import get_session
from models import User, UserRole

# OAuth2 token endpoint pointing to our v1 login route
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    session: AsyncSession = Depends(get_session),
    token: str = Depends(oauth2_scheme),
) -> User:
    """
    FastAPI dependency that validates the Bearer JWT token and fetches the current User.
    Raises 401 Unauthorized if the token is invalid, expired, or the user is inactive.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials or token has expired.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        user_id = int(payload.sub)
    except (ValueError, TypeError) as exc:
        raise credentials_exception from exc

    user = await session.get(User, user_id)
    if user is None:
        raise credentials_exception

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated.",
        )

    return user


def require_role(
    allowed_roles: Sequence[UserRole],
) -> Callable[..., Coroutine[Any, Any, User]]:
    """
    Dependency factory enforcing that the authenticated user holds one of `allowed_roles`.

    Usage:
        @router.post("/users", dependencies=[Depends(require_role([UserRole.HEAD_OF_DEPT]))])
    """
    allowed_set = set(allowed_roles)

    async def _role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_set:
            allowed_names = ", ".join(r.value for r in allowed_roles)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Insufficient permissions: role '{current_user.role.value}' is not authorized. "
                    f"Required role(s): [{allowed_names}]."
                ),
            )
        return current_user

    return _role_checker


def verify_hospital_access(user: User, target_hospital_id: int) -> User:
    """
    Synchronous helper & guard verifying whether `user` has management/write authority
    over `target_hospital_id`:
      - `HEAD_OF_DEPT`: Granted access to all hospitals.
      - `SENIOR_RESIDENT`: Granted access ONLY when `user.assigned_hospital_id == target_hospital_id`.
      - `DOCTOR`: Denied management/write access (403 Forbidden).
    """
    if user.role == UserRole.HEAD_OF_DEPT:
        return user

    if user.role == UserRole.SENIOR_RESIDENT:
        if user.assigned_hospital_id is None or user.assigned_hospital_id != target_hospital_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Hospital scope violation: Senior Resident assigned to hospital_id="
                    f"{user.assigned_hospital_id} has read-only access to hospital_id="
                    f"{target_hospital_id} and cannot modify its schedule or approve its requests."
                ),
            )
        return user

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Role DOCTOR has read-only schedule access and cannot perform hospital management actions.",
    )


def require_hospital_access(
    hospital_id_param: str = "hospital_id",
) -> Callable[..., Coroutine[Any, Any, User]]:
    """
    FastAPI Dependency factory that extracts `hospital_id` from path or query parameters
    and enforces Senior Resident hospital scoping (`assigned_hospital_id == hospital_id`)
    or Head of Department global access.

    Usage:
        @router.post("/hospitals/{hospital_id}/shifts")
        async def create_hospital_shift(
            hospital_id: int,
            current_user: User = Depends(require_hospital_access()),
        ): ...
    """
    async def _hospital_access_dependency(
        hospital_id: Optional[int] = Query(
            default=None,
            description="Target hospital ID for RBAC scope validation",
        ),
        current_user: User = Depends(get_current_user),
    ) -> User:
        if hospital_id is not None:
            verify_hospital_access(current_user, hospital_id)
        elif current_user.role not in {UserRole.SENIOR_RESIDENT, UserRole.HEAD_OF_DEPT}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Requires SENIOR_RESIDENT or HEAD_OF_DEPT role.",
            )
        return current_user

    return _hospital_access_dependency


async def require_path_hospital_access(
    hospital_id: int = Path(..., description="Target Hospital ID"),
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Direct FastAPI dependency for routes containing `{hospital_id}` in the URL path.
    """
    return verify_hospital_access(current_user, hospital_id)
