/**
 * src/stores/schedule.js — Pinia Store for the Anesthesiology & ICU Resource-Timeline Matrix.
 */

import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import api from '../services/api';

const SECONDS_PER_HOUR = 3600;
const SECONDS_PER_DAY = 86400;

export const useScheduleStore = defineStore('schedule', () => {
  const shifts = ref([]);
  const hospitals = ref([]);
  const workplaces = ref([]);
  const doctors = ref([]);
  const swapRequests = ref([]);
  const coverageGaps = ref([]);
  const fatigueAlerts = ref([]);

  const viewMode = ref('week');
  const rangeStartTs = ref(getTodayMidnightUtc());
  const selectedHospitalId = ref('ALL');
  const doctorSearchQuery = ref('');
  const showOrTasks = ref(true);

  const isLoading = ref(false);
  const lastError = ref(null);
  const lastWarningBanner = ref(null);

  const currentUser = ref({
    id: 1,
    fullName: 'Dr. Alexander Vance',
    email: 'admin@chronomed.org',
    role: 'HEAD_OF_DEPT',
    assignedHospitalId: null,
  });

  function getTodayMidnightUtc() {
    const now = new Date();
    return Math.floor(
      Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) / 1000
    );
  }

  const selectedDateRange = computed(() => {
    const daysCount = viewMode.value === 'month' ? 14 : 7;
    return {
      startTs: rangeStartTs.value,
      endTs: rangeStartTs.value + daysCount * SECONDS_PER_DAY,
    };
  });

  const calendarDays = computed(() => {
    const daysCount = viewMode.value === 'month' ? 14 : 7;
    const result = [];

    for (let i = 0; i < daysCount; i++) {
      const dayStartTs = rangeStartTs.value + i * SECONDS_PER_DAY;
      const dateObj = new Date(dayStartTs * 1000);
      const dayIso = dateObj.toISOString().slice(0, 10);
      const weekdayShort = dateObj.toLocaleDateString('en-US', {
        weekday: 'short',
        timeZone: 'UTC',
      });
      const monthDay = dateObj.toLocaleDateString('en-US', {
        month: 'numeric',
        day: 'numeric',
        timeZone: 'UTC',
      });

      result.push({
        index: i,
        dayIso,
        weekdayShort,
        monthDay,
        dutyWindowStartTs: dayStartTs + 8 * SECONDS_PER_HOUR,
        dutyWindowEndTs: dayStartTs + 32 * SECONDS_PER_HOUR,
      });
    }
    return result;
  });

  function canEditHospital(hospitalId) {
    if (currentUser.value?.role === 'HEAD_OF_DEPT') return true;
    if (currentUser.value?.role === 'SENIOR_RESIDENT') {
      return Number(currentUser.value.assignedHospitalId) === Number(hospitalId);
    }
    return false;
  }

  const groupedHospitalRows = computed(() => {
    const query = doctorSearchQuery.value.trim().toLowerCase();
    const visibleHospitals =
      selectedHospitalId.value === 'ALL'
        ? hospitals.value
        : hospitals.value.filter((h) => h.id === Number(selectedHospitalId.value));

    return visibleHospitals.map((hosp) => {
      const hospDoctors = doctors.value.filter((doc) => {
        if (doc.primaryHospitalId !== hosp.id && doc.assignedHospitalId !== hosp.id) {
          if (doc.primaryHospitalId && doc.primaryHospitalId !== hosp.id) return false;
        }
        if (!query) return true;
        return (
          doc.fullName.toLowerCase().includes(query) ||
          (doc.primarySkill && doc.primarySkill.toLowerCase().includes(query)) ||
          (Array.isArray(doc.skills) && doc.skills.some((s) => s.toLowerCase().includes(query)))
        );
      });

      return {
        hospital: hosp,
        canEdit: canEditHospital(hosp.id),
        doctors: hospDoctors,
      };
    });
  });

  const coverageGapsByDay = computed(() => {
    const map = {};
    const targetHospitals =
      selectedHospitalId.value === 'ALL'
        ? hospitals.value
        : hospitals.value.filter((h) => h.id === Number(selectedHospitalId.value));

    for (const day of calendarDays.value) {
      const missingHospitals = [];
      const backendGap = coverageGaps.value.find((g) => g.day_iso === day.dayIso);
      if (backendGap) {
        const matchingHosp = targetHospitals.find((h) => h.id === backendGap.hospital_id);
        if (matchingHosp && !missingHospitals.includes(matchingHosp)) {
          missingHospitals.push(matchingHosp);
        }
      }

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
        if (!has24hIcu && !missingHospitals.some((m) => m.id === hosp.id)) {
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

  async function fetchSchedule() {
    isLoading.value = true;
    lastError.value = null;

    try {
      const response = await api.get('/bootstrap');
      const data = response.data;

      if (Array.isArray(data.hospitals)) hospitals.value = data.hospitals;
      if (Array.isArray(data.workplaces)) workplaces.value = data.workplaces;
      if (Array.isArray(data.doctors)) {
        doctors.value = data.doctors;
        const defaultDoc =
          data.doctors.find((d) => d.role === 'HEAD_OF_DEPT') || data.doctors[0];
        if (defaultDoc && (!currentUser.value || currentUser.value.id === 1)) {
          currentUser.value = {
            id: defaultDoc.id,
            fullName: defaultDoc.fullName,
            email: defaultDoc.email,
            role: defaultDoc.role,
            assignedHospitalId: defaultDoc.assignedHospitalId,
          };
        }
      }
      if (Array.isArray(data.shifts)) {
        shifts.value = data.shifts;
        const validStartTimestamps = data.shifts
          .map((s) => s.startTs)
          .filter((ts) => Number.isFinite(ts) && ts > 0);
        if (validStartTimestamps.length > 0) {
          const minTs = Math.min(...validStartTimestamps);
          const shiftDate = new Date(minTs * 1000);
          rangeStartTs.value = Math.floor(
            Date.UTC(shiftDate.getUTCFullYear(), shiftDate.getUTCMonth(), shiftDate.getUTCDate()) / 1000
          );
        }
      }
      if (Array.isArray(data.swapRequests)) swapRequests.value = data.swapRequests;

      await fetchAnalytics(selectedDateRange.value.startTs, selectedDateRange.value.endTs);
      return data;
    } catch (err) {
      console.warn('[ScheduleStore] Live API bootstrap failed, falling back:', err);
      lastError.value = err.response?.data?.detail || err.message;
    } finally {
      isLoading.value = false;
    }
  }

  async function createShift(shiftData) {
    isLoading.value = true;
    lastError.value = null;
    lastWarningBanner.value = null;

    const payload = {
      shift_type: shiftData.shiftType || 'BASE_SHIFT',
      parent_shift_id: shiftData.parentShiftId || null,
      doctor_id: Number(shiftData.doctorId),
      hospital_id: Number(shiftData.hospitalId),
      workplace_id: Number(shiftData.workplaceId),
      start_ts: Number(shiftData.startTs),
      end_ts: Number(shiftData.endTs),
      is_24h_icu_duty: Boolean(shiftData.is24hIcuDuty),
      notes: shiftData.notes || '',
    };

    try {
      const response = await api.post('/shifts', payload);
      const created = response.data;

      const formattedShift = {
        id: created.id,
        shiftType: created.shift_type,
        parentShiftId: created.parent_shift_id,
        doctorId: created.doctor_id,
        hospitalId: created.hospital_id,
        workplaceId: created.workplace_id,
        startTs: created.start_ts,
        endTs: created.end_ts,
        durationHours: created.duration_hours,
        status: created.status,
        is24hIcuDuty: created.is_24h_icu_duty,
        isFatigueRisk: created.is_fatigue_risk,
        continuousHoursAtEnd: created.continuous_hours_at_end,
        fatigueRiskHours: created.fatigue_risk_hours,
        notes: created.notes,
      };

      shifts.value.push(formattedShift);

      if (created.is_fatigue_risk) {
        lastWarningBanner.value = `FATIGUE_RISK_HIGH: Shift creates ${created.continuous_hours_at_end}h continuous duty (+${created.fatigue_risk_hours}h over 24h threshold).`;
      }

      fetchAnalytics(selectedDateRange.value.startTs, selectedDateRange.value.endTs);
      return formattedShift;
    } catch (err) {
      const detail = err.response?.data?.detail;
      const errorMsg =
        typeof detail === 'object' ? detail.message || JSON.stringify(detail) : detail || err.message;
      lastError.value = errorMsg;
      throw new Error(errorMsg);
    } finally {
      isLoading.value = false;
    }
  }

  async function requestSwap(swapData) {
    isLoading.value = true;
    lastError.value = null;

    const payload = {
      request_type: 'SHIFT_SWAP',
      hospital_id: Number(swapData.hospitalId),
      source_shift_id: Number(swapData.sourceShiftId),
      target_doctor_id: Number(swapData.targetDoctorId),
      target_shift_id: swapData.targetShiftId ? Number(swapData.targetShiftId) : null,
      reason: swapData.reason || 'Peer shift swap proposed via Schedule Matrix',
      submit_immediately: true,
    };

    try {
      const response = await api.post('/requests', payload);
      const created = response.data;

      const formattedReq = {
        id: created.id,
        requestType: created.request_type,
        status: created.status,
        hospitalId: created.hospital_id,
        requesterId: created.requester_id,
        targetDoctorId: created.target_doctor_id,
        sourceShiftId: created.source_shift_id,
        targetShiftId: created.target_shift_id,
        reason: created.reason,
      };

      swapRequests.value.unshift(formattedReq);
      const srcShift = shifts.value.find((s) => s.id === swapData.sourceShiftId);
      if (srcShift) srcShift.status = 'PENDING_SWAP';

      return formattedReq;
    } catch (err) {
      const detail = err.response?.data?.detail;
      const errorMsg =
        typeof detail === 'object' ? detail.message || JSON.stringify(detail) : detail || err.message;
      lastError.value = errorMsg;
      throw new Error(errorMsg);
    } finally {
      isLoading.value = false;
    }
  }

  async function transitionSwapFsm({ requestId, targetStatus, sourceShiftId, targetDoctorId, reason }) {
    if (!requestId && sourceShiftId && targetDoctorId) {
      const src = shifts.value.find((s) => s.id === sourceShiftId);
      return requestSwap({
        hospitalId: src ? src.hospitalId : 1,
        sourceShiftId,
        targetDoctorId,
        reason,
      });
    }

    try {
      if (['APPROVED', 'REJECTED_BY_MANAGER', 'REVISION_REQUESTED'].includes(targetStatus)) {
        const res = await api.post(`/requests/${requestId}/adjudicate`, {
          decision: targetStatus,
          manager_notes: reason || null,
        });
        await fetchSchedule();
        return res.data;
      } else if (['PEER_ACCEPTED', 'PEER_REJECTED'].includes(targetStatus)) {
        const res = await api.post(`/requests/${requestId}/peer-response`, {
          accept: targetStatus === 'PEER_ACCEPTED',
          note: reason || null,
        });
        await fetchSchedule();
        return res.data;
      } else {
        const res = await api.post(`/requests/${requestId}/transition`, {
          target_status: targetStatus,
          notes: reason || null,
        });
        await fetchSchedule();
        return res.data;
      }
    } catch (err) {
      const detail = err.response?.data?.detail;
      const msg = typeof detail === 'object' ? detail.message || JSON.stringify(detail) : detail || err.message;
      lastError.value = msg;
      throw new Error(msg);
    }
  }

  async function fetchAnalytics(startTs, endTs, hospitalId = null) {
    const sTs = startTs || selectedDateRange.value.startTs;
    const eTs = endTs || selectedDateRange.value.endTs;

    try {
      const params = {
        range_start_ts: sTs,
        range_end_ts: eTs,
      };
      if (hospitalId && hospitalId !== 'ALL') {
        params.hospital_id = Number(hospitalId);
      }

      const [gapsRes, fatigueRes] = await Promise.allSettled([
        api.get('/analytics/coverage-gaps', { params }),
        api.get('/analytics/fatigue-metrics', { params }),
      ]);

      if (gapsRes.status === 'fulfilled' && gapsRes.value.data?.gaps) {
        coverageGaps.value = gapsRes.value.data.gaps;
      }

      if (fatigueRes.status === 'fulfilled' && fatigueRes.value.data?.doctors) {
        fatigueAlerts.value = fatigueRes.value.data.doctors;
      }
    } catch (err) {
      console.warn('[ScheduleStore] Analytics fetch warning:', err);
    }
  }

  fetchSchedule();

  return {
    shifts,
    hospitals,
    workplaces,
    doctors,
    swapRequests,
    coverageGaps,
    fatigueAlerts,
    viewMode,
    rangeStartTs,
    selectedDateRange,
    selectedHospitalId,
    doctorSearchQuery,
    showOrTasks,
    isLoading,
    lastError,
    lastWarningBanner,
    currentUser,
    calendarDays,
    groupedHospitalRows,
    coverageGapsByDay,
    canEditHospital,
    getCellAllocations,
    fetchSchedule,
    createShift,
    requestSwap,
    fetchAnalytics,
    transitionSwapFsm,
    saveShiftAllocation: createShift,
  };
});
