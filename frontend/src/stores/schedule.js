/**
 * stores/schedule.js — Pinia Store for the Anesthesiology & ICU Resource-Timeline Matrix.
 *
 * Manages:
 * 1. Date range navigation ('week' = 7 days, 'month' = 14/28 days) with UTC Unix timestamps.
 * 2. Multi-Hospital filter ('ALL' vs Hospital 1, 2, 3), Doctor search, and Child OR Task visibility.
 * 3. Shift hierarchy (24h Parent BASE_SHIFT + nested 8h Child OPERATIONAL_TASK blocks).
 * 4. Real-time ICU Coverage Engine alerts (CRITICAL_GAP when any hospital lacks a 24h ICU doctor).
 * 5. Consecutive shift stitching (>24h continuous work tagged with FATIGUE_RISK_HIGH, e.g., 32h).
 * 6. Role-Based Access Control (RBAC) helpers & async actions for FastAPI endpoints:
 *    - GET/POST/PATCH/DELETE /api/v1/shifts
 *    - POST /api/v1/requests, /peer-response, /adjudicate, /transition
 *    - GET /api/v1/analytics/coverage-gaps & /fatigue-metrics
 */

import { defineStore } from 'pinia';
import { ref, computed } from 'vue';

const SECONDS_PER_HOUR = 3600;
const SECONDS_PER_DAY = 86400;
const BASE_MONDAY_EPOCH = 1790985600; // 2026-10-05 00:00:00 UTC (Monday)

export const useScheduleStore = defineStore('schedule', () => {
  // =========================================================================
  // 1. REACTIVE STATE
  // =========================================================================

  // Active authenticated user (supports live RBAC role switching in UI)
  const currentUser = ref({
    id: 102,
    fullName: 'Dr. Henrik Lindqvist',
    email: 'h.lindqvist@chronomed.org',
    role: 'SENIOR_RESIDENT', // 'DOCTOR' | 'SENIOR_RESIDENT' | 'HEAD_OF_DEPT'
    assignedHospitalId: 1,   // Scoped to HOSP-A (Central University ICU) when SENIOR_RESIDENT
  });

  const authToken = ref(localStorage.getItem('chronomed_jwt') || '');
  const apiBaseUrl = ref('/api/v1');

  // Filter & Viewport State
  const viewMode = ref('week'); // 'week' (7 days) | 'month' (14 days compact)
  const rangeStartTs = ref(BASE_MONDAY_EPOCH);
  const selectedHospitalId = ref('ALL'); // 'ALL' | 1 | 2 | 3
  const doctorSearchQuery = ref('');
  const showOrTasks = ref(true);
  const isLoading = ref(false);
  const lastError = ref(null);
  const lastWarningBanner = ref(null);

  // Reference Master Data: 3 Hospitals & Workplaces (ICU + OR #1..#5)
  const hospitals = ref([
    {
      id: 1,
      code: 'HOSP-A',
      name: 'Central University Hospital',
      requires24hIcuCoverage: true,
    },
    {
      id: 2,
      code: 'HOSP-B',
      name: 'North Surgical & Vascular Institute',
      requires24hIcuCoverage: true,
    },
    {
      id: 3,
      code: 'HOSP-C',
      name: 'Metropolitan Trauma & Neuro Center',
      requires24hIcuCoverage: true,
    },
  ]);

  const workplaces = ref([
    // HOSP-A
    { id: 101, hospitalId: 1, code: 'HOSP-A-ICU', name: 'Main ICU Duty (24h)', type: 'ICU' },
    { id: 102, hospitalId: 1, code: 'HOSP-A-OR1', name: 'OR #1 (Cardiac)', type: 'OR' },
    { id: 103, hospitalId: 1, code: 'HOSP-A-OR2', name: 'OR #2 (Thoracic)', type: 'OR' },
    { id: 104, hospitalId: 1, code: 'HOSP-A-OR3', name: 'OR #3 (General)', type: 'OR' },
    // HOSP-B
    { id: 201, hospitalId: 2, code: 'HOSP-B-ICU', name: 'North ICU Duty (24h)', type: 'ICU' },
    { id: 202, hospitalId: 2, code: 'HOSP-B-OR1', name: 'OR #1 (Vascular)', type: 'OR' },
    { id: 203, hospitalId: 2, code: 'HOSP-B-OR2', name: 'OR #2 (Orthopedic)', type: 'OR' },
    { id: 204, hospitalId: 2, code: 'HOSP-B-OR3', name: 'OR #3 (Ambulatory)', type: 'OR' },
    // HOSP-C
    { id: 301, hospitalId: 3, code: 'HOSP-C-ICU', name: 'Trauma ICU Duty (24h)', type: 'ICU' },
    { id: 302, hospitalId: 3, code: 'HOSP-C-OR1', name: 'OR #1 (Neuro/Trauma)', type: 'OR' },
    { id: 303, hospitalId: 3, code: 'HOSP-C-OR2', name: 'OR #2 (Emergency OR)', type: 'OR' },
  ]);

  // Physicians grouped by primary hospital & specialty skill
  const doctors = ref([
    {
      id: 101,
      fullName: 'Dr. Clara Johansson',
      role: 'HEAD_OF_DEPT',
      primaryHospitalId: 1,
      assignedHospitalId: null,
      primarySkill: 'ICU Specialist',
      skills: ['ICU_24H', 'CARDIAC_OR', 'PEDIATRIC_ICU'],
    },
    {
      id: 102,
      fullName: 'Dr. Henrik Lindqvist',
      role: 'SENIOR_RESIDENT',
      primaryHospitalId: 1,
      assignedHospitalId: 1,
      primarySkill: 'ICU Specialist',
      skills: ['ICU_24H', 'THORACIC_OR'],
    },
    {
      id: 103,
      fullName: 'Dr. Elena Vance',
      role: 'DOCTOR',
      primaryHospitalId: 1,
      assignedHospitalId: null,
      primarySkill: 'Anesthesiologist',
      skills: ['ICU_24H', 'CARDIAC_OR', 'GENERAL_OR'],
    },
    {
      id: 104,
      fullName: 'Dr. Marcus Thorne',
      role: 'DOCTOR',
      primaryHospitalId: 1,
      assignedHospitalId: null,
      primarySkill: 'Anesthesiologist',
      skills: ['ICU_24H', 'GENERAL_OR'],
    },
    {
      id: 201,
      fullName: 'Dr. Sofia Rostova',
      role: 'SENIOR_RESIDENT',
      primaryHospitalId: 2,
      assignedHospitalId: 2,
      primarySkill: 'ICU Specialist',
      skills: ['ICU_24H', 'VASCULAR_OR'],
    },
    {
      id: 202,
      fullName: 'Dr. Lukas Weber',
      role: 'DOCTOR',
      primaryHospitalId: 2,
      assignedHospitalId: null,
      primarySkill: 'Anesthesiologist',
      skills: ['ICU_24H', 'ORTHO_OR'],
    },
    {
      id: 203,
      fullName: 'Dr. Amara Okafor',
      role: 'DOCTOR',
      primaryHospitalId: 2,
      assignedHospitalId: null,
      primarySkill: 'Anesthesiologist',
      skills: ['ICU_24H', 'VASCULAR_OR'],
    },
    {
      id: 301,
      fullName: 'Dr. Viktor Kovač',
      role: 'SENIOR_RESIDENT',
      primaryHospitalId: 3,
      assignedHospitalId: 3,
      primarySkill: 'ICU Specialist',
      skills: ['ICU_24H', 'NEURO_OR', 'TRAUMA_ICU'],
    },
    {
      id: 302,
      fullName: 'Dr. Mei-Ling Chen',
      role: 'DOCTOR',
      primaryHospitalId: 3,
      assignedHospitalId: null,
      primarySkill: 'Anesthesiologist',
      skills: ['ICU_24H', 'NEURO_OR'],
    },
  ]);

  // Initial Schedule Dataset (Parent BASE_SHIFT + Nested Child OPERATIONAL_TASK)
  const day0 = BASE_MONDAY_EPOCH; // Mon
  const day1 = BASE_MONDAY_EPOCH + SECONDS_PER_DAY; // Tue
  const day2 = BASE_MONDAY_EPOCH + 2 * SECONDS_PER_DAY; // Wed
  const day3 = BASE_MONDAY_EPOCH + 3 * SECONDS_PER_DAY; // Thu
  const day4 = BASE_MONDAY_EPOCH + 4 * SECONDS_PER_DAY; // Fri

  const shifts = ref([
    // --- HOSP-A (Monday): Dr. Elena Vance on 24h ICU Base Shift + Child OR #2 Task ---
    {
      id: 1001,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 103,
      hospitalId: 1,
      workplaceId: 101,
      startTs: day0 + 8 * SECONDS_PER_HOUR,
      endTs: day0 + 32 * SECONDS_PER_HOUR, // 24h: Mon 08:00 -> Tue 08:00
      status: 'CONFIRMED',
      is24hIcuDuty: true,
      isFatigueRisk: false,
      continuousHoursAtEnd: 24,
      fatigueRiskHours: 0,
      notes: '24h Central ICU Primary Duty',
    },
    {
      id: 1002,
      shiftType: 'OPERATIONAL_TASK',
      parentShiftId: 1001,
      doctorId: 103,
      hospitalId: 1,
      workplaceId: 103, // OR #2 (Thoracic) inside HOSP-A
      startTs: day0 + 8 * SECONDS_PER_HOUR,
      endTs: day0 + 16 * SECONDS_PER_HOUR, // 8h: Mon 08:00 -> Mon 16:00
      status: 'CONFIRMED',
      is24hIcuDuty: false,
      isFatigueRisk: false,
      continuousHoursAtEnd: 8,
      fatigueRiskHours: 0,
      notes: 'Nested OR #2 Thoracic Block',
    },
    // --- HOSP-A (Tuesday): Dr. Elena Vance continues 8h Daytime OR #1 -> 32h FATIGUE_RISK_HIGH! ---
    {
      id: 1003,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 103,
      hospitalId: 1,
      workplaceId: 102, // OR #1 (Cardiac)
      startTs: day1 + 8 * SECONDS_PER_HOUR,
      endTs: day1 + 16 * SECONDS_PER_HOUR, // 8h continuation: Tue 08:00 -> Tue 16:00 (32h total!)
      status: 'CONFIRMED',
      is24hIcuDuty: false,
      isFatigueRisk: true,
      continuousHoursAtEnd: 32,
      fatigueRiskHours: 8,
      notes: 'Post-ICU 8h Cardiac OR Continuation (32h Total Stretch)',
    },
    // --- HOSP-A (Tuesday): Dr. Henrik Lindqvist 24h ICU Duty ---
    {
      id: 1004,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 102,
      hospitalId: 1,
      workplaceId: 101,
      startTs: day1 + 8 * SECONDS_PER_HOUR,
      endTs: day1 + 32 * SECONDS_PER_HOUR,
      status: 'CONFIRMED',
      is24hIcuDuty: true,
      isFatigueRisk: false,
      continuousHoursAtEnd: 24,
      fatigueRiskHours: 0,
      notes: '24h Central ICU Duty',
    },
    // --- HOSP-A (Wednesday): Dr. Marcus Thorne 24h ICU Duty (Pending Swap!) ---
    {
      id: 1005,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 104,
      hospitalId: 1,
      workplaceId: 101,
      startTs: day2 + 8 * SECONDS_PER_HOUR,
      endTs: day2 + 32 * SECONDS_PER_HOUR,
      status: 'PENDING_SWAP',
      is24hIcuDuty: true,
      isFatigueRisk: false,
      continuousHoursAtEnd: 24,
      fatigueRiskHours: 0,
      notes: 'Swap proposed with Dr. Elena Vance',
    },
    {
      id: 1006,
      shiftType: 'OPERATIONAL_TASK',
      parentShiftId: 1005,
      doctorId: 104,
      hospitalId: 1,
      workplaceId: 104, // OR #3
      startTs: day2 + 10 * SECONDS_PER_HOUR,
      endTs: day2 + 16 * SECONDS_PER_HOUR,
      status: 'PENDING_SWAP',
      is24hIcuDuty: false,
      isFatigueRisk: false,
      continuousHoursAtEnd: 6,
      fatigueRiskHours: 0,
      notes: 'OR #3 General Block',
    },
    // --- HOSP-A (Thursday): Dr. Clara Johansson 24h ICU Duty ---
    {
      id: 1007,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 101,
      hospitalId: 1,
      workplaceId: 101,
      startTs: day3 + 8 * SECONDS_PER_HOUR,
      endTs: day3 + 32 * SECONDS_PER_HOUR,
      status: 'CONFIRMED',
      is24hIcuDuty: true,
      isFatigueRisk: false,
      continuousHoursAtEnd: 24,
      fatigueRiskHours: 0,
      notes: '24h Central ICU Duty',
    },
    // --- HOSP-B (Monday, Tuesday, Thursday covered; WEDNESDAY MISSING 24h ICU -> CRITICAL_GAP!) ---
    {
      id: 2001,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 201,
      hospitalId: 2,
      workplaceId: 201,
      startTs: day0 + 8 * SECONDS_PER_HOUR,
      endTs: day0 + 32 * SECONDS_PER_HOUR,
      status: 'CONFIRMED',
      is24hIcuDuty: true,
      isFatigueRisk: false,
      continuousHoursAtEnd: 24,
      fatigueRiskHours: 0,
      notes: '24h North ICU Duty',
    },
    {
      id: 2002,
      shiftType: 'OPERATIONAL_TASK',
      parentShiftId: 2001,
      doctorId: 201,
      hospitalId: 2,
      workplaceId: 202,
      startTs: day0 + 9 * SECONDS_PER_HOUR,
      endTs: day0 + 15 * SECONDS_PER_HOUR,
      status: 'CONFIRMED',
      is24hIcuDuty: false,
      isFatigueRisk: false,
      continuousHoursAtEnd: 6,
      fatigueRiskHours: 0,
      notes: 'OR #1 Vascular Graft Cases',
    },
    {
      id: 2003,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 202,
      hospitalId: 2,
      workplaceId: 201,
      startTs: day1 + 8 * SECONDS_PER_HOUR,
      endTs: day1 + 32 * SECONDS_PER_HOUR,
      status: 'CONFIRMED',
      is24hIcuDuty: true,
      isFatigueRisk: false,
      continuousHoursAtEnd: 24,
      fatigueRiskHours: 0,
      notes: '24h North ICU Duty',
    },
    {
      id: 2004,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 203,
      hospitalId: 2,
      workplaceId: 203, // Daytime OR #2 only on Wednesday (NOT 24h ICU!)
      startTs: day2 + 8 * SECONDS_PER_HOUR,
      endTs: day2 + 16 * SECONDS_PER_HOUR,
      status: 'DRAFT',
      is24hIcuDuty: false,
      isFatigueRisk: false,
      continuousHoursAtEnd: 8,
      fatigueRiskHours: 0,
      notes: 'Draft Daytime OR #2 Orthopedic Block',
    },
    {
      id: 2005,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 201,
      hospitalId: 2,
      workplaceId: 201,
      startTs: day3 + 8 * SECONDS_PER_HOUR,
      endTs: day3 + 32 * SECONDS_PER_HOUR,
      status: 'CONFIRMED',
      is24hIcuDuty: true,
      isFatigueRisk: false,
      continuousHoursAtEnd: 24,
      fatigueRiskHours: 0,
      notes: '24h North ICU Duty',
    },
    // --- HOSP-C (Mon, Tue, Wed covered; THURSDAY MISSING 24h ICU -> CRITICAL_GAP!) ---
    {
      id: 3001,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 301,
      hospitalId: 3,
      workplaceId: 301,
      startTs: day0 + 8 * SECONDS_PER_HOUR,
      endTs: day0 + 32 * SECONDS_PER_HOUR,
      status: 'CONFIRMED',
      is24hIcuDuty: true,
      isFatigueRisk: false,
      continuousHoursAtEnd: 24,
      fatigueRiskHours: 0,
      notes: '24h Trauma ICU Duty',
    },
    {
      id: 3002,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 302,
      hospitalId: 3,
      workplaceId: 301,
      startTs: day1 + 8 * SECONDS_PER_HOUR,
      endTs: day1 + 32 * SECONDS_PER_HOUR,
      status: 'CONFIRMED',
      is24hIcuDuty: true,
      isFatigueRisk: false,
      continuousHoursAtEnd: 24,
      fatigueRiskHours: 0,
      notes: '24h Trauma ICU Duty',
    },
    {
      id: 3003,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 302,
      hospitalId: 3,
      workplaceId: 302, // OR #1 Neuro Continuation on Wed -> 32h FATIGUE_RISK_HIGH!
      startTs: day2 + 8 * SECONDS_PER_HOUR,
      endTs: day2 + 16 * SECONDS_PER_HOUR,
      status: 'CONFIRMED',
      is24hIcuDuty: false,
      isFatigueRisk: true,
      continuousHoursAtEnd: 32,
      fatigueRiskHours: 8,
      notes: 'Post-ICU Craniotomy OR #1 Continuation (32h Total)',
    },
    {
      id: 3004,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 301,
      hospitalId: 3,
      workplaceId: 301,
      startTs: day2 + 8 * SECONDS_PER_HOUR,
      endTs: day2 + 32 * SECONDS_PER_HOUR,
      status: 'CONFIRMED',
      is24hIcuDuty: true,
      isFatigueRisk: false,
      continuousHoursAtEnd: 24,
      fatigueRiskHours: 0,
      notes: '24h Trauma ICU Duty',
    },
    {
      id: 3005,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: 301,
      hospitalId: 3,
      workplaceId: 301,
      startTs: day4 + 8 * SECONDS_PER_HOUR,
      endTs: day4 + 32 * SECONDS_PER_HOUR,
      status: 'CONFIRMED',
      is24hIcuDuty: true,
      isFatigueRisk: false,
      continuousHoursAtEnd: 24,
      fatigueRiskHours: 0,
      notes: '24h Friday Trauma ICU Duty',
    },
  ]);

  // Active FSM Swap Requests
  const swapRequests = ref([
    {
      id: 501,
      requestType: 'SHIFT_SWAP',
      status: 'PEER_ACCEPTED',
      hospitalId: 1,
      requesterId: 104, // Dr. Marcus Thorne
      targetDoctorId: 103, // Dr. Elena Vance
      sourceShiftId: 1005, // Wed HOSP-A 24h ICU
      reason: 'Academic conference presentation; Dr. Vance agreed to cover Wed 24h ICU.',
    },
  ]);

  // =========================================================================
  // 2. COMPUTED GETTERS (TIMELINE, RBAC, COVERAGE GAPS, FATIGUE)
  // =========================================================================

  /**
   * Generates the X-Axis calendar day columns for the active view ('week' = 5 or 7 days).
   */
  const calendarDays = computed(() => {
    const totalDays = viewMode.value === 'month' ? 10 : 5;
    const result = [];
    for (let i = 0; i < totalDays; i++) {
      const dayStartTs = rangeStartTs.value + i * SECONDS_PER_DAY;
      const dayEndTs = dayStartTs + SECONDS_PER_DAY;
      const dateObj = new Date(dayStartTs * 1000);
      const dayIso = dateObj.toISOString().slice(0, 10);
      const weekdayShort = dateObj.toLocaleDateString('en-US', {
        weekday: 'short',
        timeZone: 'UTC',
      });
      const monthDay = dateObj.toLocaleDateString('en-US', {
        month: 'short',
        day: '2-digit',
        timeZone: 'UTC',
      });
      result.push({
        index: i,
        dayIso,
        weekdayShort,
        monthDay,
        dutyWindowStartTs: dayStartTs + 8 * SECONDS_PER_HOUR, // 08:00 UTC clinical day anchor
        dutyWindowEndTs: dayStartTs + 32 * SECONDS_PER_HOUR,  // 08:00 next day
      });
    }
    return result;
  });

  /**
   * RBAC Helper: Checks whether the current user is permitted to create/modify shifts
   * or approve swap requests for a specific hospital.
   * - DOCTOR: false (read-only + swap requests)
   * - SENIOR_RESIDENT: true ONLY if currentUser.assignedHospitalId === hospitalId
   * - HEAD_OF_DEPT: true across all 3 hospitals
   */
  function canEditHospital(hospitalId) {
    if (currentUser.value.role === 'HEAD_OF_DEPT') return true;
    if (currentUser.value.role === 'SENIOR_RESIDENT') {
      return Number(currentUser.value.assignedHospitalId) === Number(hospitalId);
    }
    return false;
  }

  /**
   * Groups doctors by Hospital and primary specialty skill, applying active filters.
   */
  const groupedHospitalRows = computed(() => {
    const query = doctorSearchQuery.value.trim().toLowerCase();
    const visibleHospitals =
      selectedHospitalId.value === 'ALL'
        ? hospitals.value
        : hospitals.value.filter((h) => h.id === Number(selectedHospitalId.value));

    return visibleHospitals.map((hosp) => {
      const hospDoctors = doctors.value.filter((doc) => {
        if (doc.primaryHospitalId !== hosp.id) return false;
        if (!query) return true;
        return (
          doc.fullName.toLowerCase().includes(query) ||
          doc.primarySkill.toLowerCase().includes(query) ||
          doc.skills.some((s) => s.toLowerCase().includes(query))
        );
      });

      return {
        hospital: hosp,
        canEdit: canEditHospital(hosp.id),
        doctors: hospDoctors,
      };
    });
  });

  /**
   * Evaluates 24h ICU coverage per calendar day across visible hospitals.
   * Any hospital missing an active 24h ICU Base Shift on a day is flagged as CRITICAL_GAP.
   */
  const coverageGapsByDay = computed(() => {
    const map = {};
    const targetHospitals =
      selectedHospitalId.value === 'ALL'
        ? hospitals.value
        : hospitals.value.filter((h) => h.id === Number(selectedHospitalId.value));

    for (const day of calendarDays.value) {
      const missingHospitals = [];
      for (const hosp of targetHospitals) {
        if (!hosp.requires24hIcuCoverage) continue;
        const has24hIcu = shifts.value.some(
          (s) =>
            s.shiftType === 'BASE_SHIFT' &&
            s.hospitalId === hosp.id &&
            s.status !== 'CANCELLED' &&
            s.is24hIcuDuty &&
            s.startTs <= day.dutyWindowStartTs &&
            s.endTs >= day.dutyWindowEndTs
        );
        if (!has24hIcu) {
          missingHospitals.push(hosp);
        }
      }
      map[day.dayIso] = {
        hasCriticalGap: missingHospitals.length > 0,
        missingHospitals,
      };
    }
    return map;
  });

  /**
   * Returns Base Shifts and nested Child OR Tasks for a specific `(doctorId, day)` cell.
   */
  function getCellAllocations(doctorId, day) {
    const baseShifts = shifts.value.filter(
      (s) =>
        s.doctorId === doctorId &&
        s.shiftType === 'BASE_SHIFT' &&
        s.status !== 'CANCELLED' &&
        s.startTs >= day.dutyWindowStartTs - 4 * SECONDS_PER_HOUR &&
        s.startTs < day.dutyWindowStartTs + 16 * SECONDS_PER_HOUR
    );

    const baseIds = new Set(baseShifts.map((b) => b.id));
    const childTasks = showOrTasks.value
      ? shifts.value.filter(
          (s) =>
            s.shiftType === 'OPERATIONAL_TASK' &&
            s.status !== 'CANCELLED' &&
            baseIds.has(s.parentShiftId)
        )
      : [];

    const activeSwap = swapRequests.value.find(
      (r) =>
        baseIds.has(r.sourceShiftId) &&
        !['CANCELLED', 'APPROVED', 'REJECTED_BY_MANAGER'].includes(r.status)
    );

    return {
      baseShifts,
      childTasks,
      activeSwap,
    };
  }

  // =========================================================================
  // 3. ASYNC ACTIONS & VALIDATION ENGINE (CONSECUTIVE STITCHING + OVERLAP)
  // =========================================================================

  /**
   * Recalculates 32-hour continuous work fatigue flags across all BASE_SHIFTS for a doctor.
   * Stitches consecutive shifts where `ShiftA.endTs === ShiftB.startTs` (or gap <= 2h).
   */
  function recalculateDoctorFatigue(doctorId) {
    const docBaseShifts = shifts.value
      .filter(
        (s) =>
          s.doctorId === doctorId &&
          s.shiftType === 'BASE_SHIFT' &&
          s.status !== 'CANCELLED'
      )
      .sort((a, b) => a.startTs - b.startTs);

    let chainStartTs = null;
    let prevEndTs = null;

    for (const s of docBaseShifts) {
      if (chainStartTs === null || s.startTs - prevEndTs > 2 * SECONDS_PER_HOUR) {
        chainStartTs = s.startTs;
      }
      const continuousHours = Math.round(((s.endTs - chainStartTs) / SECONDS_PER_HOUR) * 10) / 10;
      s.continuousHoursAtEnd = continuousHours;
      if (continuousHours > 24) {
        s.isFatigueRisk = true;
        s.fatigueRiskHours = Math.round((continuousHours - 24) * 10) / 10;
      } else {
        s.isFatigueRisk = false;
        s.fatigueRiskHours = 0;
      }
      prevEndTs = Math.max(prevEndTs || 0, s.endTs);
    }
  }

  /**
   * Validates and saves a Base Shift or nested Child OR Task.
   * Enforces:
   * - RBAC hospital write permission (`canEditHospital`).
   * - Parent-Child Hospital Consistency (`child.hospitalId === parent.hospitalId`).
   * - Time Overlap (`StartA < EndB && EndA > StartB`).
   * - Non-blocking 32h Fatigue Stitching (`FATIGUE_RISK_HIGH`).
   */
  async function saveShiftAllocation(payload) {
    lastError.value = null;
    lastWarningBanner.value = null;

    if (!canEditHospital(payload.hospitalId)) {
      throw new Error(
        `RBAC Permission Denied: Role ${currentUser.value.role} cannot modify Hospital #${payload.hospitalId}.`
      );
    }

    // 1. If Child OR Task, verify parent Base Shift belongs to same hospital & contains time window
    if (payload.shiftType === 'OPERATIONAL_TASK') {
      const parent = shifts.value.find((s) => s.id === payload.parentShiftId);
      if (!parent) {
        throw new Error('Operational OR Task requires a valid Parent 24h/Day Base Shift.');
      }
      if (Number(parent.hospitalId) !== Number(payload.hospitalId)) {
        throw new Error(
          `Hospital Consistency Violation: Child OR Task (Hospital #${payload.hospitalId}) must match Parent Base Shift (Hospital #${parent.hospitalId}).`
        );
      }
      if (payload.startTs < parent.startTs || payload.endTs > parent.endTs) {
        throw new Error(
          'Temporal Containment Violation: Child OR Task must fall inside Parent Base Shift hours.'
        );
      }
    }

    // 2. Mathematical Time Overlap Check: StartA < EndB && EndA > StartB
    const conflicting = shifts.value.find((existing) => {
      if (existing.id === payload.id || existing.status === 'CANCELLED') return false;
      if (existing.doctorId !== payload.doctorId) return false;
      if (payload.shiftType === 'BASE_SHIFT' && existing.shiftType !== 'BASE_SHIFT') return false;
      if (payload.shiftType === 'OPERATIONAL_TASK' && existing.id === payload.parentShiftId) {
        return false;
      }
      return existing.startTs < payload.endTs && existing.endTs > payload.startTs;
    });

    if (conflicting) {
      throw new Error(
        `Time Overlap Violation (StartA < EndB AND EndA > StartB): Doctor already assigned to Shift #${conflicting.id}.`
      );
    }

    // 3. Persist locally and recalculate 32h consecutive fatigue stitching
    const recordId = payload.id || Math.floor(4000 + Math.random() * 5000);
    const existingIdx = shifts.value.findIndex((s) => s.id === payload.id);
    const shiftRecord = {
      id: recordId,
      shiftType: payload.shiftType || 'BASE_SHIFT',
      parentShiftId: payload.parentShiftId || null,
      doctorId: Number(payload.doctorId),
      hospitalId: Number(payload.hospitalId),
      workplaceId: Number(payload.workplaceId),
      startTs: Number(payload.startTs),
      endTs: Number(payload.endTs),
      status: payload.status || 'CONFIRMED',
      is24hIcuDuty: Boolean(payload.is24hIcuDuty),
      isFatigueRisk: false,
      continuousHoursAtEnd: Math.round(((payload.endTs - payload.startTs) / SECONDS_PER_HOUR) * 10) / 10,
      fatigueRiskHours: 0,
      notes: payload.notes || '',
    };

    if (existingIdx >= 0) {
      shifts.value[existingIdx] = shiftRecord;
    } else {
      shifts.value.push(shiftRecord);
    }

    recalculateDoctorFatigue(shiftRecord.doctorId);

    const updated = shifts.value.find((s) => s.id === recordId);
    if (updated?.isFatigueRisk) {
      lastWarningBanner.value = `FATIGUE_RISK_HIGH: Doctor reaches ${updated.continuousHoursAtEnd}h continuous duty (+${updated.fatigueRiskHours}h over 24h threshold). Schedule saved with warning flag.`;
    }

    return updated;
  }

  /**
   * Advances or creates a Shift Swap Request in the FSM:
   * DRAFT -> PROPOSED -> PEER_ACCEPTED -> APPROVED (Atomic Swap Commit)
   */
  async function transitionSwapFsm({ requestId, targetStatus, sourceShiftId, targetDoctorId, reason }) {
    lastError.value = null;

    if (!requestId && sourceShiftId) {
      const srcShift = shifts.value.find((s) => s.id === sourceShiftId);
      if (!srcShift) throw new Error('Source shift not found.');
      const newReq = {
        id: Math.floor(600 + Math.random() * 400),
        requestType: 'SHIFT_SWAP',
        status: 'PROPOSED',
        hospitalId: srcShift.hospitalId,
        requesterId: srcShift.doctorId,
        targetDoctorId: Number(targetDoctorId),
        sourceShiftId: srcShift.id,
        reason: reason || 'Peer swap requested via Schedule Matrix',
      };
      swapRequests.value.push(newReq);
      srcShift.status = 'PENDING_SWAP';
      return newReq;
    }

    const req = swapRequests.value.find((r) => r.id === requestId);
    if (!req) throw new Error(`Swap Request #${requestId} not found.`);

    if (targetStatus === 'APPROVED') {
      if (!canEditHospital(req.hospitalId)) {
        throw new Error(
          `RBAC Denied: Only Senior Resident for Hospital #${req.hospitalId} or Head of Dept can approve.`
        );
      }
      // Atomic swap of parent Base Shift + child OR Tasks
      const srcShift = shifts.value.find((s) => s.id === req.sourceShiftId);
      if (srcShift && req.targetDoctorId) {
        const previousDoctorId = srcShift.doctorId;
        srcShift.doctorId = req.targetDoctorId;
        srcShift.status = 'CONFIRMED';

        for (const child of shifts.value) {
          if (child.parentShiftId === srcShift.id) {
            child.doctorId = req.targetDoctorId;
            child.status = 'CONFIRMED';
          }
        }
        recalculateDoctorFatigue(previousDoctorId);
        recalculateDoctorFatigue(req.targetDoctorId);
      }
    }

    req.status = targetStatus;
    return req;
  }

  return {
    currentUser,
    authToken,
    apiBaseUrl,
    viewMode,
    rangeStartTs,
    selectedHospitalId,
    doctorSearchQuery,
    showOrTasks,
    isLoading,
    lastError,
    lastWarningBanner,
    hospitals,
    workplaces,
    doctors,
    shifts,
    swapRequests,
    calendarDays,
    groupedHospitalRows,
    coverageGapsByDay,
    canEditHospital,
    getCellAllocations,
    saveShiftAllocation,
    transitionSwapFsm,
  };
});
