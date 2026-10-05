#!/usr/bin/env bash
# ==============================================================================
# scripts/seed_data.py — Standalone Database Seeder for ChronoMed
# ==============================================================================
"""
Populates the SQLite database with realistic clinical data:
1. 3 Hospitals:
   - "Hospital One" (HOSP-1)
   - "Hospital Three" (HOSP-3)
   - "Hospital Four" (HOSP-4)
2. 18 Workplaces (5 ORs and 1 ICU Duty Station per hospital):
   - 15 Operating Rooms across the 3 hospitals
   - 3 Intensive Care Units (1 per hospital)
3. 30 Users/Physicians with hashed passwords ('password123'):
   - 1 HEAD_OF_DEPT (Global administrator)
   - 3 SENIOR_RESIDENTS (1 assigned to each hospital for scoped approvals)
   - 26 DOCTORS (Staff Anesthesiologists & Intensivists)
4. 1 Month of realistic shift schedules across all 3 hospitals, intentionally containing:
   - 2 ICU Coverage Gaps (missing 24h ICU duty doctor for analytics testing)
   - 3 Over-24h continuous shifts (24h ICU + 8h daytime OR = 32h continuous duty
     triggering FATIGUE_RISK_HIGH)
   - Parent 24h Base Shifts + nested Child OR tasks in matching hospitals
   - Realistic Shift Swap FSM proposals and TimeLog attendance entries
5. Executable standalone via: `python scripts/seed_data.py` (uses asyncio.run(seed()))
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

# Ensure backend directory is in python search path
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
BACKEND_DIR = PROJECT_ROOT / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# SQLAlchemy & SQLModel imports
from sqlalchemy import text, delete
from sqlmodel import select

from database import AsyncSessionLocal, init_db, engine
from models import (
    AuditAction,
    AuditLog,
    Hospital,
    RequestStatus,
    RequestType,
    Shift,
    ShiftRequest,
    ShiftStatus,
    ShiftType,
    TimeLog,
    User,
    UserRole,
    Workplace,
    WorkplaceType,
    utc_now_ts,
)
from core.security import get_password_hash


async def seed() -> None:
    print("=" * 70)
    print("  ChronoMed Anesthesiology & ICU Database Seeder")
    print("=" * 70)

    # 1. Initialize SQLite tables, WAL mode, and Foreign Keys
    print("\n[1/5] Initializing database schema and verifying WAL mode...")
    await init_db()

    async with AsyncSessionLocal() as session:
        # Check if tables already have data and prompt/clear cleanly
        existing_hospitals = (await session.execute(select(Hospital))).scalars().first()
        if existing_hospitals:
            print("  Existing data detected. Purging previous records for clean re-seed...")
            await session.execute(delete(AuditLog))
            await session.execute(delete(TimeLog))
            await session.execute(delete(ShiftRequest))
            await session.execute(delete(Shift))
            await session.execute(delete(Workplace))
            await session.execute(delete(User))
            await session.execute(delete(Hospital))
            await session.commit()
            print("  ✔ Existing records cleared.")

        # =====================================================================
        # 2. SEED HOSPITALS
        # =====================================================================
        print("\n[2/5] Seeding 3 Hospitals...")
        hospitals_data = [
            Hospital(
                code="HOSP-1",
                name="Hospital One",
                address="100 Medical Center Blvd, Metro General",
                requires_24h_icu_coverage=True,
                is_active=True,
            ),
            Hospital(
                code="HOSP-3",
                name="Hospital Three",
                address="300 University Pavilion Way, North Campus",
                requires_24h_icu_coverage=True,
                is_active=True,
            ),
            Hospital(
                code="HOSP-4",
                name="Hospital Four",
                address="400 St. Jude Specialty Trauma Center, West Wing",
                requires_24h_icu_coverage=True,
                is_active=True,
            ),
        ]
        session.add_all(hospitals_data)
        await session.flush()

        hosp1, hosp3, hosp4 = hospitals_data[0], hospitals_data[1], hospitals_data[2]
        print(f"  ✔ Created {len(hospitals_data)} Hospitals: {hosp1.name} (ID {hosp1.id}), {hosp3.name} (ID {hosp3.id}), {hosp4.name} (ID {hosp4.id})")

        # =====================================================================
        # 3. SEED WORKPLACES (5 ORs + 1 ICU Station per hospital = 18 total)
        # =====================================================================
        print("\n[3/5] Seeding 18 Workplaces (5 ORs + 1 ICU block per hospital)...")
        all_workplaces: List[Workplace] = []

        specialty_tags = [
            ["GENERAL_OR", "LAPAROSCOPY"],
            ["CARDIAC_ANESTHESIA", "VASCULAR"],
            ["NEURO_OR", "SPINE"],
            ["PEDIATRIC_ANESTHESIA", "ENT"],
            ["TRAUMA_OR", "ORTHOPEDIC"],
        ]

        for hosp in [hosp1, hosp3, hosp4]:
            # 5 Operating Rooms
            for i in range(1, 6):
                wp = Workplace(
                    hospital_id=hosp.id,
                    code=f"{hosp.code}-OR-{i}",
                    name=f"Operating Room #{i}",
                    workplace_type=WorkplaceType.OR,
                    required_skills=specialty_tags[i - 1],
                    is_active=True,
                )
                all_workplaces.append(wp)

            # 1 ICU 24-Hour Duty Station
            icu_wp = Workplace(
                hospital_id=hosp.id,
                code=f"{hosp.code}-ICU-MAIN",
                name="Main Intensive Care Unit (ICU)",
                workplace_type=WorkplaceType.ICU,
                required_skills=["ICU_24H", "CRITICAL_CARE"],
                is_active=True,
            )
            all_workplaces.append(icu_wp)

        session.add_all(all_workplaces)
        await session.flush()
        print(f"  ✔ Created {len(all_workplaces)} Workplaces across 3 hospitals (15 Surgical ORs + 3 ICUs).")

        # Index workplaces by (hospital_id, workplace_type, index)
        hosp_workplaces = {
            hosp.id: {
                "ICU": [w for w in all_workplaces if w.hospital_id == hosp.id and w.workplace_type == WorkplaceType.ICU][0],
                "ORs": [w for w in all_workplaces if w.hospital_id == hosp.id and w.workplace_type == WorkplaceType.OR],
            }
            for hosp in [hosp1, hosp3, hosp4]
        }

        # =====================================================================
        # 4. SEED 30 PHYSICIANS / USERS
        #    - 1 HEAD_OF_DEPT
        #    - 3 SENIOR_RESIDENTS (1 assigned per hospital)
        #    - 26 DOCTORS
        # =====================================================================
        print("\n[4/5] Seeding 30 Physicians (1 Head of Dept, 3 Senior Residents, 26 Doctors)...")
        shared_password_hash = get_password_hash("password123")
        users_to_create: List[User] = []

        # 1 HEAD_OF_DEPT
        head_of_dept = User(
            email="admin@chronomed.org",
            full_name="Dr. Alexander Vance",
            hashed_password=shared_password_hash,
            role=UserRole.HEAD_OF_DEPT,
            assigned_hospital_id=None,
            skills=["ICU_24H", "CARDIAC_ANESTHESIA", "TRAUMA_OR", "NEURO_OR", "PEDIATRIC_ANESTHESIA"],
            contract_weekly_hours=48,
            is_active=True,
        )
        users_to_create.append(head_of_dept)

        # 3 SENIOR RESIDENTS (1 assigned to each hospital)
        sr1 = User(
            email="senior.hosp1@hospital.org",
            full_name="Dr. Sarah Chen",
            hashed_password=shared_password_hash,
            role=UserRole.SENIOR_RESIDENT,
            assigned_hospital_id=hosp1.id,
            skills=["ICU_24H", "GENERAL_OR", "CARDIAC_ANESTHESIA"],
            contract_weekly_hours=44,
            is_active=True,
        )
        sr3 = User(
            email="senior.hosp3@hospital.org",
            full_name="Dr. Marcus Thorne",
            hashed_password=shared_password_hash,
            role=UserRole.SENIOR_RESIDENT,
            assigned_hospital_id=hosp3.id,
            skills=["ICU_24H", "NEURO_OR", "TRAUMA_OR"],
            contract_weekly_hours=44,
            is_active=True,
        )
        sr4 = User(
            email="senior.hosp4@hospital.org",
            full_name="Dr. Elena Rostova",
            hashed_password=shared_password_hash,
            role=UserRole.SENIOR_RESIDENT,
            assigned_hospital_id=hosp4.id,
            skills=["ICU_24H", "PEDIATRIC_ANESTHESIA", "REGIONAL_ANESTHESIA"],
            contract_weekly_hours=44,
            is_active=True,
        )
        users_to_create.extend([sr1, sr3, sr4])

        # 26 STAFF DOCTORS
        doctor_names = [
            "Dr. David Kim", "Dr. Maya Patel", "Dr. James Wilson", "Dr. Olivia Zhao",
            "Dr. Carlos Mendez", "Dr. Hannah Schmidt", "Dr. Liam O'Connor", "Dr. Sofia Rossi",
            "Dr. Lucas Dubois", "Dr. Aisha Al-Mansoor", "Dr. Noah Takahashi", "Dr. Chloe Martin",
            "Dr. Ethan Walker", "Dr. Isabella Silva", "Dr. Benjamin Brooks", "Dr. Mia Kowalski",
            "Dr. Samuel Adebayo", "Dr. Ava Lindqvist", "Dr. Gabriel Santos", "Dr. Harper Lee",
            "Dr. Julian Romero", "Dr. Zoe Fischer", "Dr. Mason Taylor", "Dr. Evelyn Murphy",
            "Dr. Daniel Goldberg", "Dr. Victoria Sinclair",
        ]

        skill_pools = [
            ["ICU_24H", "GENERAL_OR"],
            ["ICU_24H", "CARDIAC_ANESTHESIA"],
            ["ICU_24H", "NEURO_OR"],
            ["GENERAL_OR", "REGIONAL_ANESTHESIA"],
            ["GENERAL_OR", "PEDIATRIC_ANESTHESIA"],
            ["ICU_24H", "TRAUMA_OR"],
            ["GENERAL_OR", "OB_ANESTHESIA"],
        ]

        for idx, name in enumerate(doctor_names, start=1):
            doc = User(
                email=f"doctor{idx}@hospital.org",
                full_name=name,
                hashed_password=shared_password_hash,
                role=UserRole.DOCTOR,
                assigned_hospital_id=None,
                skills=skill_pools[idx % len(skill_pools)],
                contract_weekly_hours=40,
                is_active=True,
            )
            users_to_create.append(doc)

        session.add_all(users_to_create)
        await session.flush()
        print(f"  ✔ Created {len(users_to_create)} Physicians (all passwords set to 'password123'):")
        print(f"     • 1 Head of Dept:    {head_of_dept.email} ({head_of_dept.full_name})")
        print(f"     • 3 Senior Residents: {sr1.email} (HOSP-1), {sr3.email} (HOSP-3), {sr4.email} (HOSP-4)")
        print(f"     • 26 Staff Doctors:  doctor1@hospital.org ... doctor26@hospital.org")

        # Split doctors for scheduling
        all_icu_doctors = [u for u in users_to_create if "ICU_24H" in u.skills]
        all_general_doctors = [u for u in users_to_create if u.role == UserRole.DOCTOR]

        # =====================================================================
        # 5. SEED 1 MONTH OF SCHEDULES (30 DAYS)
        #    - 2 Coverage Gaps:
        #      * Gap 1: Day 7 at Hospital Three (HOSP-3) -> Missing 24h ICU duty doctor
        #      * Gap 2: Day 19 at Hospital Four (HOSP-4) -> Missing 24h ICU duty doctor
        #    - 3 Over-24h Continuous Shifts (32h duty -> FATIGUE_RISK_HIGH):
        #      * Fatigue 1: Day 3 at Hospital One (Doctor #5 works 24h ICU + 8h Daytime OR)
        #      * Fatigue 2: Day 12 at Hospital Three (Doctor #8 works 24h ICU + 8h Daytime OR)
        #      * Fatigue 3: Day 22 at Hospital Four (Doctor #14 works 24h ICU + 8h Daytime OR)
        # =====================================================================
        print("\n[5/5] Generating 30 days of shift schedules across all 3 hospitals...")
        print("  • Inserting 2 ICU Coverage Gaps (Day 7 @ HOSP-3, Day 19 @ HOSP-4)")
        print("  • Inserting 3 Over-24h (32h) Fatigue Triggers (Day 3, Day 12, Day 22)")

        # Anchor schedule at current day 00:00:00 UTC
        now_dt = datetime.now(timezone.utc)
        base_epoch = int(datetime(now_dt.year, now_dt.month, now_dt.day, 0, 0, 0, tzinfo=timezone.utc).timestamp())

        shifts_to_add: List[Shift] = []
        SEC_HOUR = 3600
        SEC_DAY = 86400

        # Map of intentional coverage gaps: (day_index, hospital_id)
        intentional_coverage_gaps = {
            (7, hosp3.id),   # Gap 1: Day 7 at Hospital Three
            (19, hosp4.id),  # Gap 2: Day 19 at Hospital Four
        }

        # Map of intentional 32-hour continuous fatigue shifts:
        # day_index -> (hospital_id, doctor)
        fatigue_triggers = {
            3: (hosp1.id, all_general_doctors[4]),    # Dr. Carlos Mendez
            12: (hosp3.id, all_general_doctors[7]),   # Dr. Sofia Rossi
            22: (hosp4.id, all_general_doctors[13]),  # Dr. Isabella Silva
        }

        icu_doc_cursor = 0
        or_doc_cursor = 0

        for day in range(30):
            day_start_ts = base_epoch + (day * SEC_DAY)
            shift_start_ts = day_start_ts + (8 * SEC_HOUR)  # 08:00 AM UTC
            shift_24h_end_ts = shift_start_ts + SEC_DAY     # 08:00 AM UTC next day

            for hosp in [hosp1, hosp3, hosp4]:
                icu_station = hosp_workplaces[hosp.id]["ICU"]
                or_rooms = hosp_workplaces[hosp.id]["ORs"]

                # -------------------------------------------------------------
                # A. 24h ICU BASE SHIFT (unless intentionally omitted as a gap)
                # -------------------------------------------------------------
                is_gap = (day, hosp.id) in intentional_coverage_gaps
                if not is_gap:
                    # Check if this day is a designated 32h fatigue trigger
                    if day in fatigue_triggers and fatigue_triggers[day][0] == hosp.id:
                        assigned_icu_doc = fatigue_triggers[day][1]
                    else:
                        assigned_icu_doc = all_icu_doctors[icu_doc_cursor % len(all_icu_doctors)]
                        icu_doc_cursor += 1

                    icu_base_shift = Shift(
                        shift_type=ShiftType.BASE_SHIFT,
                        parent_shift_id=None,
                        doctor_id=assigned_icu_doc.id,
                        hospital_id=hosp.id,
                        workplace_id=icu_station.id,
                        start_ts=shift_start_ts,
                        end_ts=shift_24h_end_ts,
                        status=ShiftStatus.SCHEDULED,
                        is_24h_icu_duty=True,
                        is_fatigue_risk=False,
                        continuous_hours_at_end=24.0,
                        fatigue_risk_hours=0.0,
                        notes=f"24h ICU Primary Coverage · {hosp.code}",
                        created_by_id=head_of_dept.id,
                        created_at=utc_now_ts(),
                        updated_at=utc_now_ts(),
                    )
                    shifts_to_add.append(icu_base_shift)

                    # Add child OR sub-task (09:00 - 15:00) inside the parent 24h ICU shift
                    # for the same doctor in the same hospital
                    child_task = Shift(
                        shift_type=ShiftType.OPERATIONAL_TASK,
                        parent_shift=icu_base_shift,
                        doctor_id=assigned_icu_doc.id,
                        hospital_id=hosp.id,
                        workplace_id=or_rooms[0].id,
                        start_ts=shift_start_ts + (1 * SEC_HOUR),  # 09:00 UTC
                        end_ts=shift_start_ts + (7 * SEC_HOUR),    # 15:00 UTC
                        status=ShiftStatus.SCHEDULED,
                        is_24h_icu_duty=False,
                        is_fatigue_risk=False,
                        continuous_hours_at_end=6.0,
                        fatigue_risk_hours=0.0,
                        notes=f"Scheduled Surgical Anesthesia Block (OR #1) during ICU duty",
                        created_by_id=head_of_dept.id,
                        created_at=utc_now_ts(),
                        updated_at=utc_now_ts(),
                    )
                    shifts_to_add.append(child_task)

                # -------------------------------------------------------------
                # B. DAYTIME OPERATING ROOM SHIFTS (08:00 - 16:00, 8 hours)
                # -------------------------------------------------------------
                # Schedule 2 daytime surgical suites per hospital
                for room_idx in [1, 2]:
                    or_doc = all_general_doctors[or_doc_cursor % len(all_general_doctors)]
                    or_doc_cursor += 1

                    daytime_or_shift = Shift(
                        shift_type=ShiftType.BASE_SHIFT,
                        parent_shift_id=None,
                        doctor_id=or_doc.id,
                        hospital_id=hosp.id,
                        workplace_id=or_rooms[room_idx].id,
                        start_ts=shift_start_ts,
                        end_ts=shift_start_ts + (8 * SEC_HOUR),  # 08:00 - 16:00
                        status=ShiftStatus.SCHEDULED,
                        is_24h_icu_duty=False,
                        is_fatigue_risk=False,
                        continuous_hours_at_end=8.0,
                        fatigue_risk_hours=0.0,
                        notes=f"Elective Surgery List ({or_rooms[room_idx].name})",
                        created_by_id=head_of_dept.id,
                        created_at=utc_now_ts(),
                        updated_at=utc_now_ts(),
                    )
                    shifts_to_add.append(daytime_or_shift)

            # -----------------------------------------------------------------
            # C. INJECT 32-HOUR CONTINUOUS WORK (FATIGUE TRIGGER)
            # -----------------------------------------------------------------
            # If previous day was a fatigue trigger day, schedule an immediate
            # back-to-back 8h OR daytime shift (08:00 - 16:00) for the SAME doctor!
            prev_day = day - 1
            if prev_day in fatigue_triggers:
                fatigue_hosp_id, fatigue_doc = fatigue_triggers[prev_day]
                fatigue_or_room = hosp_workplaces[fatigue_hosp_id]["ORs"][3]

                # Consecutive shift: starts exactly when 24h ICU shift ended (shift_start_ts = day_start_ts + 8h)
                fatigue_shift = Shift(
                    shift_type=ShiftType.BASE_SHIFT,
                    parent_shift_id=None,
                    doctor_id=fatigue_doc.id,
                    hospital_id=fatigue_hosp_id,
                    workplace_id=fatigue_or_room.id,
                    start_ts=shift_start_ts,                   # 08:00 UTC (exact continuation)
                    end_ts=shift_start_ts + (8 * SEC_HOUR),    # 16:00 UTC (+8h)
                    status=ShiftStatus.SCHEDULED,
                    is_24h_icu_duty=False,
                    is_fatigue_risk=True,                      # Flagged > 24h!
                    continuous_hours_at_end=32.0,              # 24h ICU + 8h OR = 32.0h
                    fatigue_risk_hours=8.0,                    # 8 hours exceeding 24h threshold
                    notes=f"WARNING: 32h Continuous Shift (24h ICU + 8h {fatigue_or_room.name}) — FATIGUE_RISK_HIGH",
                    created_by_id=head_of_dept.id,
                    created_at=utc_now_ts(),
                    updated_at=utc_now_ts(),
                )
                shifts_to_add.append(fatigue_shift)

        session.add_all(shifts_to_add)
        await session.flush()
        print(f"  ✔ Created {len(shifts_to_add)} Shifts across 30 days.")

        # =====================================================================
        # 6. SEED REALISTIC SHIFT SWAP REQUESTS (FSM) & TIMELOG ATTENDANCE
        # =====================================================================
        print("\n[Bonus] Seeding live FSM Shift Swap proposals & PWA TimeLogs...")

        # 1 Incoming Swap in PEER_ACCEPTED state (ready for Senior Resident approval)
        eligible_swaps = [s for s in shifts_to_add if s.shift_type == ShiftType.BASE_SHIFT and not s.is_fatigue_risk][:4]
        if len(eligible_swaps) >= 2:
            swap_req1 = ShiftRequest(
                request_type=RequestType.SHIFT_SWAP,
                status=RequestStatus.PEER_ACCEPTED,
                hospital_id=eligible_swaps[0].hospital_id,
                requester_id=eligible_swaps[0].doctor_id,
                target_doctor_id=eligible_swaps[1].doctor_id,
                source_shift_id=eligible_swaps[0].id,
                target_shift_id=eligible_swaps[1].id,
                reason="Family wedding on weekend duty; peer agreed to swap reciprocal shift.",
                peer_response_note="Accepted swap via mobile PWA companion.",
                peer_responded_at=utc_now_ts() - 3600,
                created_at=utc_now_ts() - 7200,
                updated_at=utc_now_ts() - 3600,
            )
            # 1 Swap in PROPOSED state (waiting for peer doctor response)
            swap_req2 = ShiftRequest(
                request_type=RequestType.SHIFT_SWAP,
                status=RequestStatus.PROPOSED,
                hospital_id=eligible_swaps[2].hospital_id,
                requester_id=eligible_swaps[2].doctor_id,
                target_doctor_id=eligible_swaps[3].doctor_id,
                source_shift_id=eligible_swaps[2].id,
                target_shift_id=None,
                reason="Medical conference attendance; requesting coverage.",
                created_at=utc_now_ts() - 1800,
                updated_at=utc_now_ts() - 1800,
            )
            session.add_all([swap_req1, swap_req2])

        # 2 Verified PWA TimeLogs
        if len(shifts_to_add) > 0:
            first_shift = shifts_to_add[0]
            log1 = TimeLog(
                shift_id=first_shift.id,
                doctor_id=first_shift.doctor_id,
                check_in_ts=first_shift.start_ts,
                check_out_ts=first_shift.start_ts + (8 * SEC_HOUR),
                recorded_duration_seconds=8 * SEC_HOUR,
                is_fatigue_flagged=False,
                pwa_client_metadata="PWA-iOS Safari 17.5 (Hospital Wi-Fi Verified)",
                notes="Shift completed smoothly; 4 surgical cases staffed.",
                created_at=utc_now_ts() - SEC_DAY,
            )
            session.add(log1)

        await session.commit()
        print("  ✔ Seeded sample FSM Swap Requests (PEER_ACCEPTED & PROPOSED) and PWA TimeLogs.")

    print("\n" + "=" * 70)
    print("  SEEDING COMPLETED SUCCESSFULLY!")
    print("=" * 70)
    print("\nDefault Login Credentials (all passwords: 'password123'):")
    print(f"  • Head of Department:  admin@chronomed.org")
    print(f"  • Senior Resident 1:    senior.hosp1@hospital.org (Hospital One)")
    print(f"  • Senior Resident 3:    senior.hosp3@hospital.org (Hospital Three)")
    print(f"  • Senior Resident 4:    senior.hosp4@hospital.org (Hospital Four)")
    print(f"  • Staff Doctor:         doctor1@hospital.org (up to doctor26@hospital.org)\n")
    print("Test Scenarios Ready to Validate:")
    print("  1. Coverage Gaps: Day 7 (HOSP-3) and Day 19 (HOSP-4) highlight CRITICAL_GAP alerts.")
    print("  2. Fatigue Risks: Days 3, 12, and 22 feature 32h continuous shifts (FATIGUE_RISK_HIGH).")
    print("  3. Peer Swap Inbox: Incoming PEER_ACCEPTED swap ready for manager approval.\n")


if __name__ == "__main__":
    asyncio.run(seed())
