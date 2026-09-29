<!--
  components/ScheduleMatrix.vue — Interactive Resource-Timeline Matrix for Vue 3.
  Features:
  - X-Axis: Calendar Days + Top ICU Coverage Gap Alert Bar (`CRITICAL_GAP` in high-contrast red).
  - Y-Axis: Physicians grouped by Hospital (HOSP-A, HOSP-B, HOSP-C) and Primary Specialty.
  - Cells: 24h Parent Base Shifts as primary cards + nested Child OR Tasks (OR #1..#3) inside them.
  - Badges: `FATIGUE_RISK_HIGH` (32h continuous work), Status color coding (Draft, Confirmed, Pending Swap).
  - RBAC Enforcement: Standard Doctors have read-only cells + Swap Proposal trigger; Senior Residents
    can edit ONLY their `assignedHospitalId` rows; Head of Dept has global edit & approval access.
-->
<script setup>
import { ref, computed } from 'vue';
import { useScheduleStore } from '../stores/schedule';

const store = useScheduleStore();

// Quick-Edit & Swap Modal State
const isModalOpen = ref(false);
const modalContext = ref(null);
const modalError = ref(null);

// Form fields inside the Quick-Edit Modal
const formMode = ref('EDIT_BASE'); // 'EDIT_BASE' | 'ADD_CHILD_OR' | 'SWAP_FSM'
const formWorkplaceId = ref(null);
const formDurationHours = ref(24);
const formStatus = ref('CONFIRMED');
const formIs24hIcu = ref(true);
const formChildWorkplaceId = ref(null);
const formChildDurationHours = ref(8);
const formSwapTargetDoctorId = ref(null);
const formSwapReason = ref('');

// Role Switcher Presets to test RBAC enforcement in UI
const rolePresets = [
  {
    label: 'Dr. Elena Vance (DOCTOR · Read-Only + Swap)',
    id: 103,
    fullName: 'Dr. Elena Vance',
    role: 'DOCTOR',
    assignedHospitalId: null,
  },
  {
    label: 'Dr. Henrik Lindqvist (SENIOR_RESIDENT · HOSP-A Only)',
    id: 102,
    fullName: 'Dr. Henrik Lindqvist',
    role: 'SENIOR_RESIDENT',
    assignedHospitalId: 1,
  },
  {
    label: 'Dr. Clara Johansson (HEAD_OF_DEPT · All 3 Hospitals)',
    id: 101,
    fullName: 'Dr. Clara Johansson',
    role: 'HEAD_OF_DEPT',
    assignedHospitalId: null,
  },
];

function applyRolePreset(preset) {
  store.currentUser = {
    id: preset.id,
    fullName: preset.fullName,
    email: `${preset.id}@chronomed.org`,
    role: preset.role,
    assignedHospitalId: preset.assignedHospitalId,
  };
}

function getWorkplaceCode(workplaceId) {
  const wp = store.workplaces.find((w) => w.id === workplaceId);
  return wp ? wp.code.replace(/^HOSP-[A-C]-/, '') : `WP-${workplaceId}`;
}

function getWorkplaceName(workplaceId) {
  const wp = store.workplaces.find((w) => w.id === workplaceId);
  return wp ? wp.name : `Workplace #${workplaceId}`;
}

function statusBadgeClasses(status) {
  switch (status) {
    case 'DRAFT':
      return 'bg-slate-900/90 border-dashed border-slate-600 text-slate-300';
    case 'PENDING_SWAP':
      return 'bg-violet-950/70 border-violet-500/70 text-violet-100';
    case 'CONFIRMED':
    default:
      return 'bg-sky-950/60 border-sky-500/50 text-sky-100';
  }
}

const availableHospitalWorkplaces = computed(() => {
  if (!modalContext.value) return [];
  return store.workplaces.filter((w) => w.hospitalId === modalContext.value.hospital.id);
});

const availableOrWorkplaces = computed(() =>
  availableHospitalWorkplaces.value.filter((w) => w.type === 'OR')
);

const peerDoctorsForSwap = computed(() => {
  if (!modalContext.value) return [];
  return store.doctors.filter((d) => d.id !== modalContext.value.doctor.id);
});

function openCellModal(doctor, hospital, day) {
  modalError.value = null;
  const allocations = store.getCellAllocations(doctor.id, day);
  const primaryShift = allocations.baseShifts[0] || null;
  const canEdit = store.canEditHospital(hospital.id);

  modalContext.value = {
    doctor,
    hospital,
    day,
    primaryShift,
    childTasks: allocations.childTasks,
    activeSwap: allocations.activeSwap,
    canEdit,
  };

  const hospWps = store.workplaces.filter((w) => w.hospitalId === hospital.id);
  const defaultIcu = hospWps.find((w) => w.type === 'ICU') || hospWps[0];
  const defaultOr = hospWps.find((w) => w.type === 'OR') || hospWps[0];

  if (primaryShift) {
    formWorkplaceId.value = primaryShift.workplaceId;
    formDurationHours.value = Math.round((primaryShift.endTs - primaryShift.startTs) / 3600);
    formStatus.value = primaryShift.status;
    formIs24hIcu.value = primaryShift.is24hIcuDuty;
  } else {
    formWorkplaceId.value = defaultIcu?.id || null;
    formDurationHours.value = 24;
    formStatus.value = 'CONFIRMED';
    formIs24hIcu.value = true;
  }

  formChildWorkplaceId.value = defaultOr?.id || null;
  formChildDurationHours.value = 8;
  formSwapTargetDoctorId.value = peerDoctorsForSwap.value[0]?.id || null;
  formSwapReason.value = '';

  formMode.value = canEdit ? 'EDIT_BASE' : 'SWAP_FSM';
  isModalOpen.value = true;
}

async function handleSaveBaseShift() {
  modalError.value = null;
  try {
    const { doctor, hospital, day, primaryShift } = modalContext.value;
    const startTs = day.dutyWindowStartTs;
    const endTs = startTs + Number(formDurationHours.value) * 3600;

    await store.saveShiftAllocation({
      id: primaryShift?.id || null,
      shiftType: 'BASE_SHIFT',
      parentShiftId: null,
      doctorId: doctor.id,
      hospitalId: hospital.id,
      workplaceId: Number(formWorkplaceId.value),
      startTs,
      endTs,
      status: formStatus.value,
      is24hIcuDuty: Number(formDurationHours.value) === 24 && formIs24hIcu.value,
      notes:
        Number(formDurationHours.value) === 24
          ? `24h ${hospital.code} ICU Duty`
          : `${formDurationHours.value}h Daytime Shift`,
    });
    isModalOpen.value = false;
  } catch (err) {
    modalError.value = err.message || String(err);
  }
}

async function handleAddChildOrTask() {
  modalError.value = null;
  try {
    const { doctor, hospital, day, primaryShift } = modalContext.value;
    if (!primaryShift) {
      throw new Error('Assign a Parent Base Shift first before nesting a Child OR Task.');
    }
    const startTs = day.dutyWindowStartTs;
    const endTs = startTs + Number(formChildDurationHours.value) * 3600;

    await store.saveShiftAllocation({
      id: null,
      shiftType: 'OPERATIONAL_TASK',
      parentShiftId: primaryShift.id,
      doctorId: doctor.id,
      hospitalId: hospital.id,
      workplaceId: Number(formChildWorkplaceId.value),
      startTs,
      endTs,
      status: 'CONFIRMED',
      is24hIcuDuty: false,
      notes: 'Nested Child OR Task',
    });
    isModalOpen.value = false;
  } catch (err) {
    modalError.value = err.message || String(err);
  }
}

async function handleTriggerSwapAction(targetStatus) {
  modalError.value = null;
  try {
    const { primaryShift, activeSwap } = modalContext.value;
    await store.transitionSwapFsm({
      requestId: activeSwap?.id || null,
      targetStatus,
      sourceShiftId: primaryShift?.id,
      targetDoctorId: formSwapTargetDoctorId.value,
      reason: formSwapReason.value,
    });
    isModalOpen.value = false;
  } catch (err) {
    modalError.value = err.message || String(err);
  }
}
</script>

<template>
  <div class="bg-slate-950 text-slate-100 space-y-5">
    <!-- 1. TOP CONTROL & FILTER BAR -->
    <div class="border border-slate-800 rounded-lg bg-slate-900/60 p-4 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <!-- Hospital Segmented Filter (All 3 vs Specific Hospital) -->
        <div class="flex flex-wrap items-center gap-1 p-1 bg-slate-950 border border-slate-800 rounded-lg">
          <button
            type="button"
            @click="store.selectedHospitalId = 'ALL'"
            :class="[
              'px-3 py-1.5 text-xs font-medium rounded-md transition-colors',
              store.selectedHospitalId === 'ALL'
                ? 'bg-sky-500 text-slate-950 font-semibold'
                : 'text-slate-400 hover:text-slate-200',
            ]"
          >
            All 3 Hospitals
          </button>
          <button
            v-for="hosp in store.hospitals"
            :key="hosp.id"
            type="button"
            @click="store.selectedHospitalId = hosp.id"
            :class="[
              'px-3 py-1.5 text-xs font-mono font-medium rounded-md transition-colors',
              store.selectedHospitalId === hosp.id
                ? 'bg-sky-500 text-slate-950 font-semibold'
                : 'text-slate-400 hover:text-slate-200',
            ]"
          >
            {{ hosp.code }}
          </button>
        </div>

        <!-- Doctor Search + Toggle Child OR Tasks + Week/Month View -->
        <div class="flex flex-wrap items-center gap-3">
          <input
            v-model="store.doctorSearchQuery"
            type="text"
            placeholder="Filter by doctor or skill..."
            class="px-3 py-1.5 text-xs bg-slate-950 border border-slate-800 rounded-md text-slate-100 placeholder-slate-500 focus:outline-none focus:border-sky-400"
          />

          <label class="flex items-center gap-2 text-xs text-slate-300 cursor-pointer select-none">
            <input
              v-model="store.showOrTasks"
              type="checkbox"
              class="accent-sky-400 rounded"
            />
            <span>Nested OR Tasks</span>
          </label>

          <div class="flex items-center bg-slate-950 border border-slate-800 rounded-md p-0.5 text-xs">
            <button
              type="button"
              @click="store.viewMode = 'week'"
              :class="[
                'px-2.5 py-1 rounded',
                store.viewMode === 'week' ? 'bg-slate-800 text-white' : 'text-slate-400',
              ]"
            >
              Week
            </button>
            <button
              type="button"
              @click="store.viewMode = 'month'"
              :class="[
                'px-2.5 py-1 rounded',
                store.viewMode === 'month' ? 'bg-slate-800 text-white' : 'text-slate-400',
              ]"
            >
              10-Day
            </button>
          </div>
        </div>
      </div>

      <!-- RBAC Active Role Bar & Legend -->
      <div class="pt-3 border-t border-slate-800/80 flex flex-wrap items-center justify-between gap-4 text-xs">
        <div class="flex items-center gap-2">
          <span class="text-slate-400">Active RBAC Session:</span>
          <select
            :value="store.currentUser.id"
            @change="
              (e) =>
                applyRolePreset(
                  rolePresets.find((r) => r.id === Number(e.target.value)) || rolePresets[0]
                )
            "
            class="px-2.5 py-1 bg-slate-950 border border-slate-800 rounded text-sky-300 font-mono"
          >
            <option v-for="preset in rolePresets" :key="preset.id" :value="preset.id">
              {{ preset.label }}
            </option>
          </select>
        </div>

        <div class="flex flex-wrap items-center gap-4 text-slate-400">
          <span class="flex items-center gap-1.5">
            <span class="w-2.5 h-2.5 rounded-xs bg-sky-500/60 border border-sky-400"></span>
            Confirmed
          </span>
          <span class="flex items-center gap-1.5">
            <span class="w-2.5 h-2.5 rounded-xs bg-slate-800 border border-dashed border-slate-500"></span>
            Draft
          </span>
          <span class="flex items-center gap-1.5">
            <span class="w-2.5 h-2.5 rounded-xs bg-violet-500/60 border border-violet-400"></span>
            Pending Swap
          </span>
          <span class="flex items-center gap-1.5 text-amber-300 font-mono">
            FATIGUE_RISK_HIGH (&gt;24h)
          </span>
        </div>
      </div>
    </div>

    <!-- Non-blocking 32h Fatigue Warning Banner -->
    <div
      v-if="store.lastWarningBanner"
      class="p-3 rounded-lg bg-amber-950/50 border border-amber-600/60 text-xs text-amber-200 font-mono flex items-center justify-between"
    >
      <span>{{ store.lastWarningBanner }}</span>
      <button
        type="button"
        @click="store.lastWarningBanner = null"
        class="text-amber-400 hover:text-white ml-4"
      >
        Dismiss
      </button>
    </div>

    <!-- 2. RESOURCE-TIMELINE MATRIX GRID -->
    <div class="border border-slate-800 rounded-lg bg-slate-900/40 overflow-x-auto">
      <table class="w-full border-collapse text-left min-w-[920px]">
        <thead>
          <!-- Top Timeline Bar: Calendar Days + CRITICAL_GAP Red Warnings -->
          <tr class="border-b border-slate-800 bg-slate-950/90">
            <th class="w-64 p-3.5 text-xs font-semibold text-slate-300 border-r border-slate-800">
              Physician / Hospital &amp; Skill
            </th>
            <th
              v-for="day in store.calendarDays"
              :key="day.dayIso"
              class="p-2.5 border-r border-slate-800 last:border-r-0 align-top"
            >
              <div class="flex items-center justify-between">
                <span class="text-xs font-semibold text-white">{{ day.weekdayShort }}</span>
                <span class="text-xs font-mono text-slate-400 tabular-nums">{{ day.monthDay }}</span>
              </div>

              <!-- Coverage Engine Status Strip per Day -->
              <div
                v-if="store.coverageGapsByDay[day.dayIso]?.hasCriticalGap"
                class="mt-2 px-2 py-1 rounded bg-rose-950/90 border border-rose-500 text-rose-200 text-[11px] font-mono leading-tight"
              >
                <div class="font-semibold text-rose-300">CRITICAL_GAP · No 24h ICU</div>
                <div>
                  Missing:
                  {{
                    store.coverageGapsByDay[day.dayIso].missingHospitals
                      .map((h) => h.code)
                      .join(', ')
                  }}
                </div>
              </div>
              <div
                v-else
                class="mt-2 px-2 py-1 rounded bg-emerald-950/40 border border-emerald-800/50 text-emerald-300 text-[11px] font-mono"
              >
                24h ICU Covered
              </div>
            </th>
          </tr>
        </thead>

        <tbody class="divide-y divide-slate-800">
          <template v-for="group in store.groupedHospitalRows" :key="group.hospital.id">
            <!-- Hospital Group Header Row -->
            <tr class="bg-slate-950/80">
              <td
                :colspan="store.calendarDays.length + 1"
                class="px-4 py-2 text-xs font-mono flex items-center justify-between"
              >
                <div class="flex items-center gap-2">
                  <span class="font-semibold text-sky-400">{{ group.hospital.code }}</span>
                  <span class="text-slate-300">{{ group.hospital.name }}</span>
                  <span class="text-slate-500">·</span>
                  <span
                    :class="group.canEdit ? 'text-emerald-400' : 'text-slate-500'"
                  >
                    {{
                      group.canEdit
                        ? 'RBAC: Write & Approval Enabled'
                        : 'RBAC: Read-Only Scope'
                    }}
                  </span>
                </div>
              </td>
            </tr>

            <!-- Doctor Timeline Rows -->
            <tr
              v-for="doctor in group.doctors"
              :key="doctor.id"
              class="hover:bg-slate-900/40 transition-colors"
            >
              <!-- Y-Axis Doctor Info Cell -->
              <td class="p-3.5 border-r border-slate-800 align-top">
                <div class="text-xs font-semibold text-white">{{ doctor.fullName }}</div>
                <div class="text-[11px] text-slate-400 mt-0.5">
                  {{ doctor.primarySkill }} · <span class="font-mono">{{ doctor.role }}</span>
                </div>
              </td>

              <!-- Interactive Timeline Cells -->
              <td
                v-for="day in store.calendarDays"
                :key="`${doctor.id}-${day.dayIso}`"
                @click="openCellModal(doctor, group.hospital, day)"
                class="p-2 border-r border-slate-800 last:border-r-0 align-top cursor-pointer hover:bg-slate-800/40 transition-colors min-h-[88px]"
              >
                <template
                  v-for="alloc in [store.getCellAllocations(doctor.id, day)]"
                  :key="day.dayIso"
                >
                  <!-- Base Shift Block -->
                  <div
                    v-for="base in alloc.baseShifts"
                    :key="base.id"
                    :class="[
                      'p-2 rounded border text-xs space-y-1.5',
                      statusBadgeClasses(base.status),
                    ]"
                  >
                    <div class="flex items-center justify-between gap-1 font-mono">
                      <span class="font-semibold truncate">
                        {{ getWorkplaceCode(base.workplaceId) }}
                      </span>
                      <span class="text-[11px] opacity-80 tabular-nums">
                        {{ Math.round((base.endTs - base.startTs) / 3600) }}h
                      </span>
                    </div>

                    <!-- 32h Fatigue Warning Badge -->
                    <div
                      v-if="base.isFatigueRisk"
                      class="px-1.5 py-0.5 rounded bg-amber-500/20 border border-amber-400/70 text-amber-200 text-[10px] font-mono font-semibold"
                    >
                      FATIGUE_RISK_HIGH ({{ base.continuousHoursAtEnd }}h)
                    </div>

                    <!-- Nested Child OR Tasks inside the 24h Base Shift -->
                    <div v-if="alloc.childTasks.length > 0" class="space-y-1 pt-1 border-t border-slate-700/60">
                      <div
                        v-for="task in alloc.childTasks"
                        :key="task.id"
                        class="px-1.5 py-1 rounded bg-slate-950/90 border border-slate-700 text-[11px] font-mono text-slate-200 flex items-center justify-between"
                      >
                        <span>↳ {{ getWorkplaceCode(task.workplaceId) }}</span>
                        <span class="text-slate-400 tabular-nums">
                          {{ Math.round((task.endTs - task.startTs) / 3600) }}h
                        </span>
                      </div>
                    </div>
                  </div>

                  <!-- Empty Cell Hint (RBAC-aware) -->
                  <div
                    v-if="alloc.baseShifts.length === 0"
                    class="h-16 rounded border border-transparent hover:border-slate-800 flex items-center justify-center text-[11px] text-slate-600 font-mono"
                  >
                    {{ group.canEdit ? '+ Assign' : '—' }}
                  </div>
                </template>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>

    <!-- 3. CONTEXTUAL QUICK-EDIT & SHIFT SWAP FSM MODAL -->
    <div
      v-if="isModalOpen && modalContext"
      class="fixed inset-0 z-50 bg-slate-950/80 flex items-center justify-center p-4"
    >
      <div class="w-full max-w-lg border border-slate-800 rounded-lg bg-slate-900 p-5 space-y-4">
        <div class="flex items-start justify-between border-b border-slate-800 pb-3">
          <div>
            <h3 class="text-sm font-semibold text-white">
              {{ modalContext.doctor.fullName }} · {{ modalContext.hospital.code }}
            </h3>
            <p class="text-xs text-slate-400 font-mono mt-0.5">
              {{ modalContext.day.weekdayShort }} {{ modalContext.day.monthDay }} ({{
                modalContext.day.dayIso
              }})
            </p>
          </div>
          <button
            type="button"
            @click="isModalOpen = false"
            class="text-xs text-slate-400 hover:text-white"
          >
            Close
          </button>
        </div>

        <!-- Mode Tabs (RBAC Gated: Hide Edit Tabs for Standard Doctors or Out-of-Scope SRs) -->
        <div class="flex items-center gap-1 p-1 bg-slate-950 border border-slate-800 rounded-md text-xs">
          <button
            v-if="modalContext.canEdit"
            type="button"
            @click="formMode = 'EDIT_BASE'"
            :class="[
              'flex-1 py-1.5 rounded font-medium',
              formMode === 'EDIT_BASE' ? 'bg-slate-800 text-white' : 'text-slate-400',
            ]"
          >
            Assign Base Shift
          </button>
          <button
            v-if="modalContext.canEdit && modalContext.primaryShift"
            type="button"
            @click="formMode = 'ADD_CHILD_OR'"
            :class="[
              'flex-1 py-1.5 rounded font-medium',
              formMode === 'ADD_CHILD_OR' ? 'bg-slate-800 text-white' : 'text-slate-400',
            ]"
          >
            + Child OR Task
          </button>
          <button
            type="button"
            @click="formMode = 'SWAP_FSM'"
            :class="[
              'flex-1 py-1.5 rounded font-medium',
              formMode === 'SWAP_FSM' ? 'bg-slate-800 text-white' : 'text-slate-400',
            ]"
          >
            Shift Swap FSM
          </button>
        </div>

        <div
          v-if="modalError"
          class="p-2.5 rounded bg-rose-950/60 border border-rose-700 text-xs text-rose-200 font-mono"
        >
          {{ modalError }}
        </div>

        <!-- TAB A: Assign / Update Base Shift (Managers Only) -->
        <div v-if="formMode === 'EDIT_BASE' && modalContext.canEdit" class="space-y-3 text-xs">
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-slate-400 mb-1">Workplace ({{ modalContext.hospital.code }})</label>
              <select
                v-model="formWorkplaceId"
                class="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-800 rounded text-white font-mono"
              >
                <option v-for="wp in availableHospitalWorkplaces" :key="wp.id" :value="wp.id">
                  {{ wp.name }}
                </option>
              </select>
            </div>
            <div>
              <label class="block text-slate-400 mb-1">Shift Duration</label>
              <select
                v-model="formDurationHours"
                class="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-800 rounded text-white font-mono"
              >
                <option :value="24">24h ICU / Primary Duty</option>
                <option :value="16">16h Extended Duty</option>
                <option :value="8">8h Daytime OR Block</option>
              </select>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-3 items-center">
            <div>
              <label class="block text-slate-400 mb-1">Schedule Status</label>
              <select
                v-model="formStatus"
                class="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-800 rounded text-white font-mono"
              >
                <option value="CONFIRMED">CONFIRMED</option>
                <option value="DRAFT">DRAFT</option>
                <option value="PENDING_SWAP">PENDING_SWAP</option>
              </select>
            </div>
            <label class="flex items-center gap-2 pt-4 text-slate-200 cursor-pointer">
              <input v-model="formIs24hIcu" type="checkbox" class="accent-sky-400" />
              <span>Counts as 24h ICU Duty Coverage</span>
            </label>
          </div>

          <div class="pt-2 flex justify-end gap-2">
            <button
              type="button"
              @click="handleSaveBaseShift"
              class="px-4 py-1.5 rounded bg-sky-400 text-slate-950 font-semibold hover:bg-sky-300"
            >
              Validate &amp; Save Base Shift
            </button>
          </div>
        </div>

        <!-- TAB B: Attach Nested Child OR Task (Same Hospital Enforced) -->
        <div v-if="formMode === 'ADD_CHILD_OR' && modalContext.canEdit" class="space-y-3 text-xs">
          <p class="text-slate-400">
            Attaches a nested <code class="text-slate-200">OPERATIONAL_TASK</code> inside Parent Base
            Shift #{{ modalContext.primaryShift?.id }} at
            <strong class="text-white">{{ modalContext.hospital.code }}</strong>.
          </p>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-slate-400 mb-1">Operating Room</label>
              <select
                v-model="formChildWorkplaceId"
                class="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-800 rounded text-white font-mono"
              >
                <option v-for="wp in availableOrWorkplaces" :key="wp.id" :value="wp.id">
                  {{ wp.name }}
                </option>
              </select>
            </div>
            <div>
              <label class="block text-slate-400 mb-1">OR Task Duration (Hours)</label>
              <select
                v-model="formChildDurationHours"
                class="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-800 rounded text-white font-mono"
              >
                <option :value="4">4h Morning Block</option>
                <option :value="6">6h Surgical Block</option>
                <option :value="8">8h Full OR Day</option>
              </select>
            </div>
          </div>

          <div class="pt-2 flex justify-end">
            <button
              type="button"
              @click="handleAddChildOrTask"
              class="px-4 py-1.5 rounded bg-sky-400 text-slate-950 font-semibold hover:bg-sky-300"
            >
              Attach Child OR Task
            </button>
          </div>
        </div>

        <!-- TAB C: Shift Swap FSM (Available to Doctors & Managers) -->
        <div v-if="formMode === 'SWAP_FSM'" class="space-y-3 text-xs">
          <div v-if="!modalContext.primaryShift" class="text-slate-400 py-2">
            No active Base Shift in this cell to swap.
          </div>
          <template v-else>
            <div
              v-if="modalContext.activeSwap"
              class="p-3 rounded bg-slate-950 border border-slate-800 space-y-2"
            >
              <div class="flex items-center justify-between font-mono">
                <span class="text-slate-400">Active Request #{{ modalContext.activeSwap.id }}</span>
                <span class="text-violet-300 font-semibold">{{ modalContext.activeSwap.status }}</span>
              </div>
              <p class="text-slate-300">{{ modalContext.activeSwap.reason }}</p>

              <div class="flex flex-wrap gap-2 pt-2">
                <button
                  v-if="modalContext.activeSwap.status === 'PROPOSED'"
                  type="button"
                  @click="handleTriggerSwapAction('PEER_ACCEPTED')"
                  class="px-3 py-1.5 rounded bg-sky-500 text-slate-950 font-semibold"
                >
                  Peer Accept (Doctor B)
                </button>
                <button
                  v-if="
                    modalContext.activeSwap.status === 'PEER_ACCEPTED' && modalContext.canEdit
                  "
                  type="button"
                  @click="handleTriggerSwapAction('APPROVED')"
                  class="px-3 py-1.5 rounded bg-emerald-400 text-slate-950 font-semibold"
                >
                  Manager Approve (Atomic Swap)
                </button>
              </div>
            </div>

            <div v-else class="space-y-3">
              <div>
                <label class="block text-slate-400 mb-1">Target Peer Physician</label>
                <select
                  v-model="formSwapTargetDoctorId"
                  class="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-800 rounded text-white font-mono"
                >
                  <option v-for="peer in peerDoctorsForSwap" :key="peer.id" :value="peer.id">
                    {{ peer.fullName }} ({{ peer.primarySkill }})
                  </option>
                </select>
              </div>
              <div>
                <label class="block text-slate-400 mb-1">Swap Reason</label>
                <input
                  v-model="formSwapReason"
                  type="text"
                  placeholder="e.g., Requesting peer coverage for Wednesday ICU duty"
                  class="w-full px-2.5 py-1.5 bg-slate-950 border border-slate-800 rounded text-white"
                />
              </div>
              <div class="flex justify-end">
                <button
                  type="button"
                  @click="handleTriggerSwapAction('PROPOSED')"
                  class="px-4 py-1.5 rounded bg-violet-400 text-slate-950 font-semibold hover:bg-violet-300"
                >
                  Propose Shift Swap
                </button>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>
