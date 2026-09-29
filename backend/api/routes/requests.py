"""
api/routes/requests.py — Shift Swap, Vacation & Revision Request Endpoints (FSM Powered).

Endpoints:
- `GET /api/v1/requests`: List requests filtered by hospital, status, or physician.
- `POST /api/v1/requests`: Create a new shift swap, vacation, or revision request (`DRAFT` or `PROPOSED`).
- `POST /api/v1/requests/{request_id}/peer-response`: Peer doctor (`target_doctor_id`) accepts
  (`PEER_ACCEPTED`) or rejects (`PEER_REJECTED`) an incoming swap offer.
- `POST /api/v1/requests/{request_id}/adjudicate`: Hospital-scoped `SENIOR_RESIDENT` or global
  `HEAD_OF_DEPT` approves (`APPROVED`), rejects (`REJECTED_BY_MANAGER`), or requests revisions
  (`REVISION_REQUESTED`). Transitioning to `APPROVED` atomically swaps the underlying shifts!
- `POST /api/v1/requests/{request_id}/transition`: General FSM transition endpoint for doctor
  revisions (`REVISION_REQUESTED -> PROPOSED`) and cancellations (`CANCELLED`).
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, require_role, verify_hospital_access
from database import atomic_transaction, get_session
from models import (
    AuditAction,
    AuditLog,
    RequestStatus,
    RequestType,
    Shift,
    ShiftRequest,
    ShiftStatus,
    User,
    UserRole,
    utc_now_ts,
)
from services.fsm import (
    FSMError,
    InvalidStateTransitionError,
    RBACPermissionDeniedError,
    ShiftSwapFSMService,
    SwapConflictError,
)

router = APIRouter(prefix="/requests", tags=["Shift Swaps & Schedule Requests (FSM)"])


# =====================================================================
# 1. REQUEST & RESPONSE SCHEMAS
# =====================================================================

class ShiftRequestCreate(BaseModel):
    """Payload for a physician creating a Shift Swap, Vacation, or Revision request."""
    request_type: RequestType = Field(default=RequestType.SHIFT_SWAP)
    hospital_id: int
    target_doctor_id: Optional[int] = Field(
        default=None,
        description="Required for SHIFT_SWAP requests; peer doctor invited to swap/cover",
    )
    source_shift_id: Optional[int] = Field(
        default=None,
        description="Shift owned by requester to be swapped or revised",
    )
    target_shift_id: Optional[int] = Field(
        default=None,
        description="Optional reciprocal shift owned by target_doctor_id in a two-way swap",
    )
    requested_start_ts: Optional[int] = None
    requested_end_ts: Optional[int] = None
    reason: Optional[str] = Field(default=None, max_length=1000)
    submit_immediately: bool = Field(
        default=True,
        description="If True, creates directly in PROPOSED state; otherwise saves as DRAFT",
    )


class PeerDecisionRequest(BaseModel):
    """Payload for Doctor B responding to a proposed SHIFT_SWAP."""
    accept: bool = Field(..., description="True -> PEER_ACCEPTED, False -> PEER_REJECTED")
    note: Optional[str] = Field(default=None, max_length=500)


class ManagerAdjudicationRequest(BaseModel):
    """Payload for Senior Resident (scoped) or Head of Dept reviewing a request."""
    decision: RequestStatus = Field(
        ...,
        description="Must be APPROVED, REJECTED_BY_MANAGER, or REVISION_REQUESTED",
    )
    manager_notes: Optional[str] = Field(default=None, max_length=500)


class FSMTransitionPayload(BaseModel):
    """Generic FSM transition payload (e.g., DRAFT -> PROPOSED, or -> CANCELLED)."""
    target_status: RequestStatus
    notes: Optional[str] = Field(default=None, max_length=500)


class ShiftRequestResponse(BaseModel):
    """Serialized ShiftRequest with FSM state and adjudication metadata."""
    id: int
    request_type: RequestType
    status: RequestStatus
    hospital_id: int
    requester_id: int
    target_doctor_id: Optional[int]
    source_shift_id: Optional[int]
    target_shift_id: Optional[int]
    requested_start_ts: Optional[int]
    requested_end_ts: Optional[int]
    reason: Optional[str]
    peer_response_note: Optional[str]
    peer_responded_at: Optional[int]
    reviewed_by_id: Optional[int]
    manager_notes: Optional[str]
    reviewed_at: Optional[int]
    created_at: int
    updated_at: int


def _to_request_response(req: ShiftRequest) -> ShiftRequestResponse:
    return ShiftRequestResponse(
        id=req.id or 0,
        request_type=req.request_type,
        status=req.status,
        hospital_id=req.hospital_id,
        requester_id=req.requester_id,
        target_doctor_id=req.target_doctor_id,
        source_shift_id=req.source_shift_id,
        target_shift_id=req.target_shift_id,
        requested_start_ts=req.requested_start_ts,
        requested_end_ts=req.requested_end_ts,
        reason=req.reason,
        peer_response_note=req.peer_response_note,
        peer_responded_at=req.peer_responded_at,
        reviewed_by_id=req.reviewed_by_id,
        manager_notes=req.manager_notes,
        reviewed_at=req.reviewed_at,
        created_at=req.created_at,
        updated_at=req.updated_at,
    )


def _raise_http_from_fsm_error(exc: FSMError) -> None:
    """Translates domain FSM exceptions into HTTP 403 / 409 / 422 responses."""
    if isinstance(exc, RBACPermissionDeniedError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if isinstance(exc, InvalidStateTransitionError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if isinstance(exc, SwapConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


# =====================================================================
# 2. FSM REQUEST ENDPOINTS
# =====================================================================

@router.get(
    "",
    response_model=List[ShiftRequestResponse],
    status_code=status.HTTP_200_OK,
    summary="List Shift Swap, Vacation & Revision Requests",
)
async def list_requests(
    hospital_id: Optional[int] = Query(default=None),
    status_filter: Optional[RequestStatus] = Query(default=None, alias="status"),
    mine_only: bool = Query(
        default=False,
        description="If True, returns only requests initiated by or targeting current_user",
    ),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> List[ShiftRequestResponse]:
    conditions = []
    if hospital_id is not None:
        conditions.append(ShiftRequest.hospital_id == hospital_id)
    if status_filter is not None:
        conditions.append(ShiftRequest.status == status_filter)
    if mine_only or current_user.role == UserRole.DOCTOR:
        conditions.append(
            or_(
                ShiftRequest.requester_id == current_user.id,
                ShiftRequest.target_doctor_id == current_user.id,
            )
        )

    stmt = (
        select(ShiftRequest)
        .where(and_(*conditions))
        .order_by(ShiftRequest.updated_at.desc(), ShiftRequest.id.desc())
    )
    result = await session.execute(stmt)
    return [_to_request_response(r) for r in result.scalars().all()]


@router.post(
    "",
    response_model=ShiftRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Shift Swap, Vacation, or Revision Request",
)
async def create_request(
    payload: ShiftRequestCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ShiftRequestResponse:
    """
    Allows a physician to initiate a `SHIFT_SWAP`, `VACATION`, or `REVISION` request.
    Starts in `DRAFT` or transitions immediately to `PROPOSED` if `submit_immediately=True`.
    """
    if current_user.id is None:
        raise HTTPException(status_code=401, detail="Invalid user session.")

    if payload.request_type == RequestType.SHIFT_SWAP:
        if payload.source_shift_id is None or payload.target_doctor_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="SHIFT_SWAP requests require both source_shift_id and target_doctor_id.",
            )
        if payload.target_doctor_id == current_user.id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot propose a shift swap with yourself.",
            )

    async with atomic_transaction(session):
        if payload.source_shift_id is not None:
            source_shift = await session.get(Shift, payload.source_shift_id)
            if source_shift is None or source_shift.status == ShiftStatus.CANCELLED:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Source Shift #{payload.source_shift_id} not found or cancelled.",
                )
            if source_shift.doctor_id != current_user.id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only initiate swap or revision requests for your own shifts.",
                )
            if source_shift.hospital_id != payload.hospital_id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Request hospital_id must match the source shift's hospital_id.",
                )

        now_ts = utc_now_ts()
        initial_status = (
            RequestStatus.PROPOSED if payload.submit_immediately else RequestStatus.DRAFT
        )

        new_req = ShiftRequest(
            request_type=payload.request_type,
            status=initial_status,
            hospital_id=payload.hospital_id,
            requester_id=current_user.id,
            target_doctor_id=payload.target_doctor_id,
            source_shift_id=payload.source_shift_id,
            target_shift_id=payload.target_shift_id,
            requested_start_ts=payload.requested_start_ts,
            requested_end_ts=payload.requested_end_ts,
            reason=payload.reason,
            created_at=now_ts,
            updated_at=now_ts,
        )
        session.add(new_req)
        await session.flush()

        session.add(
            AuditLog(
                actor_id=current_user.id,
                actor_role=current_user.role,
                hospital_id=payload.hospital_id,
                entity_type="ShiftRequest",
                entity_id=new_req.id or 0,
                action=AuditAction.CREATE,
                previous_state=None,
                new_state={
                    "request_type": new_req.request_type.value,
                    "status": new_req.status.value,
                    "source_shift_id": new_req.source_shift_id,
                    "target_doctor_id": new_req.target_doctor_id,
                },
                description=(
                    f"Doctor #{current_user.id} created {new_req.request_type.value} "
                    f"request #{new_req.id} in state {new_req.status.value}."
                ),
                created_at=now_ts,
            )
        )
        await session.flush()

        return _to_request_response(new_req)


@router.post(
    "/{request_id}/peer-response",
    response_model=ShiftRequestResponse,
    status_code=status.HTTP_200_OK,
    summary="Peer Doctor Accepts or Rejects an Incoming Shift Swap",
)
async def respond_to_swap_offer(
    request_id: int,
    payload: PeerDecisionRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ShiftRequestResponse:
    """
    Transitions a `PROPOSED` shift swap request to `PEER_ACCEPTED` or `PEER_REJECTED`.
    Enforces that `current_user.id == request.target_doctor_id`.
    """
    target_status = (
        RequestStatus.PEER_ACCEPTED if payload.accept else RequestStatus.PEER_REJECTED
    )
    try:
        updated = await ShiftSwapFSMService.transition_request_state(
            session=session,
            request_id=request_id,
            target_status=target_status,
            actor=current_user,
            notes=payload.note,
        )
        return _to_request_response(updated)
    except FSMError as exc:
        _raise_http_from_fsm_error(exc)


@router.post(
    "/{request_id}/adjudicate",
    response_model=ShiftRequestResponse,
    status_code=status.HTTP_200_OK,
    summary="Senior Resident (Scoped) or Head of Dept Approves/Rejects Request",
)
async def adjudicate_request(
    request_id: int,
    payload: ManagerAdjudicationRequest,
    current_user: User = Depends(
        require_role([UserRole.SENIOR_RESIDENT, UserRole.HEAD_OF_DEPT])
    ),
    session: AsyncSession = Depends(get_session),
) -> ShiftRequestResponse:
    """
    Manager adjudication endpoint:
    - Enforces `require_role([SENIOR_RESIDENT, HEAD_OF_DEPT])`.
    - Verifies `verify_hospital_access(current_user, shift_request.hospital_id)` so a
      Senior Resident can ONLY adjudicate requests belonging to their `assigned_hospital_id`.
    - When `payload.decision == RequestStatus.APPROVED`, `ShiftSwapFSMService` atomically
      swaps the `doctor_id` on the shifts (and child OR tasks) and writes an `AuditLog`.
    """
    if payload.decision not in {
        RequestStatus.APPROVED,
        RequestStatus.REJECTED_BY_MANAGER,
        RequestStatus.REVISION_REQUESTED,
    }:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Manager decision must be APPROVED, REJECTED_BY_MANAGER, or REVISION_REQUESTED.",
        )

    shift_req = await session.get(ShiftRequest, request_id)
    if shift_req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ShiftRequest #{request_id} not found.",
        )

    # Explicit hospital-level RBAC gate before invoking FSM
    verify_hospital_access(current_user, shift_req.hospital_id)

    try:
        updated = await ShiftSwapFSMService.transition_request_state(
            session=session,
            request_id=request_id,
            target_status=payload.decision,
            actor=current_user,
            notes=payload.manager_notes,
        )
        return _to_request_response(updated)
    except FSMError as exc:
        _raise_http_from_fsm_error(exc)


@router.post(
    "/{request_id}/transition",
    response_model=ShiftRequestResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute FSM State Transition (Propose, Request Revision, Cancel)",
)
async def transition_request(
    request_id: int,
    payload: FSMTransitionPayload,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ShiftRequestResponse:
    """
    General FSM transition endpoint allowing doctors to submit `DRAFT -> PROPOSED`,
    request revisions (`REVISION_REQUESTED`), resubmit (`REVISION_REQUESTED -> PROPOSED`),
    or cancel (`CANCELLED`).
    """
    try:
        updated = await ShiftSwapFSMService.transition_request_state(
            session=session,
            request_id=request_id,
            target_status=payload.target_status,
            actor=current_user,
            notes=payload.notes,
        )
        return _to_request_response(updated)
    except FSMError as exc:
        _raise_http_from_fsm_error(exc)
