<!--
  components/SwapRequests.vue — Peer Shift Swap Inbox & Swap Proposal Modal for Doctors.
  Features:
  - Inbox-style feed displaying incoming peer swap proposals with clear clinical context:
    "Dr. Marcus Thorne wants to swap their 24h Duty at HOSP-A on [Date] with your shift..."
  - Touch-friendly "Accept" (`PEER_ACCEPTED`) and "Reject" (`PEER_REJECTED`) buttons wired
    to the Pinia store (`store.transitionSwapFsm`) and `/api/v1/requests/{id}/peer-response`.
  - Modal flow allowing the doctor to initiate a new swap request (select own shift ->
    select target peer doctor and their reciprocal shift).
-->
<script setup>
import { ref, computed } from 'vue';
import { useScheduleStore } from '../stores/schedule';

const store = useScheduleStore();

const isNewSwapModalOpen = ref(false);
const selectedOwnShiftId = ref(null);
const selectedPeerDoctorId = ref(null);
const selectedPeerShiftId = ref(null);
const swapReason = ref('');
const feedbackBanner = ref(null);

function formatShiftDescription(shiftId) {
  const shift = store.shifts.find((s) => s.id === shiftId);
  if (!shift) return `Shift #${shiftId}`;
  const hosp = store.hospitals.find((h) => h.id === shift.hospitalId);
  const wp = store.workplaces.find((w) => w.id === shift.workplaceId);
  const dateStr = new Date(shift.startTs * 1000).toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: '2-digit',
    timeZone: 'UTC',
  });
  const hours = Math.round((shift.endTs - shift.startTs) / 3600);
  return `${hours}h ${wp ? wp.name : 'Duty'} at ${hosp ? hosp.code : 'Hospital'} on ${dateStr}`;
}

function getDoctorName(doctorId) {
  const doc = store.doctors.find((d) => d.id === doctorId);
  return doc ? doc.fullName : `Doctor #${doctorId}`;
}

// Incoming swap requests where current doctor is the invited peer (or all swap requests in demo)
const incomingSwapProposals = computed(() =>
  store.swapRequests.map((req) => ({
    ...req,
    requesterName: getDoctorName(req.requesterId),
    targetDoctorName: getDoctorName(req.targetDoctorId),
    sourceShiftSummary: formatShiftDescription(req.sourceShiftId),
    targetShiftSummary: req.targetShiftId
      ? formatShiftDescription(req.targetShiftId)
      : 'Open Coverage / Reciprocal Duty TBD',
  }))
);

const myEligibleShifts = computed(() =>
  store.shifts.filter(
    (s) =>
      s.doctorId === store.currentUser.id &&
      s.shiftType === 'BASE_SHIFT' &&
      s.status !== 'CANCELLED'
  )
);

const peerDoctors = computed(() =>
  store.doctors.filter((d) => d.id !== store.currentUser.id)
);

const selectedPeerShifts = computed(() => {
  if (!selectedPeerDoctorId.value) return [];
  return store.shifts.filter(
    (s) =>
      s.doctorId === Number(selectedPeerDoctorId.value) &&
      s.shiftType === 'BASE_SHIFT' &&
      s.status !== 'CANCELLED'
  );
});

function openInitiateSwapModal() {
  selectedOwnShiftId.value = myEligibleShifts.value[0]?.id || null;
  selectedPeerDoctorId.value = peerDoctors.value[0]?.id || null;
  selectedPeerShiftId.value = null;
  swapReason.value = '';
  isNewSwapModalOpen.value = true;
}

async function handlePeerDecision(requestId, accept) {
  const nextStatus = accept ? 'PEER_ACCEPTED' : 'PEER_REJECTED';
  await store.transitionSwapFsm({
    requestId,
    targetStatus: nextStatus,
  });
  feedbackBanner.value = accept
    ? `Request #${requestId} marked PEER_ACCEPTED — routed to Senior Resident / Head of Dept for final approval.`
    : `Request #${requestId} marked PEER_REJECTED.`;
}

async function handleSubmitNewSwap() {
  if (!selectedOwnShiftId.value || !selectedPeerDoctorId.value) return;
  const created = await store.transitionSwapFsm({
    requestId: null,
    targetStatus: 'PROPOSED',
    sourceShiftId: Number(selectedOwnShiftId.value),
    targetDoctorId: Number(selectedPeerDoctorId.value),
    reason:
      swapReason.value ||
      `Swap proposed with reciprocal shift #${selectedPeerShiftId.value || 'TBD'}`,
  });
  isNewSwapModalOpen.value = false;
  feedbackBanner.value = `Created Swap Proposal #${created.id} (status: PROPOSED).`;
}
</script>

<template>
  <section class="space-y-4">
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-sm font-semibold text-white">Peer Shift Swap Inbox</h2>
        <p class="text-xs text-slate-400">
          Accepting a peer proposal transitions state to <code class="font-mono">PEER_ACCEPTED</code>.
        </p>
      </div>

      <button
        type="button"
        @click="openInitiateSwapModal"
        class="px-3 py-1.5 rounded-lg bg-sky-500 text-slate-950 text-xs font-semibold hover:bg-sky-400"
      >
        + New Swap
      </button>
    </div>

    <div
      v-if="feedbackBanner"
      class="p-3 rounded-xl bg-sky-950/60 border border-sky-500/50 text-xs text-sky-200 font-mono"
    >
      {{ feedbackBanner }}
    </div>

    <!-- Inbox Cards -->
    <div class="space-y-3">
      <article
        v-for="req in incomingSwapProposals"
        :key="req.id"
        class="rounded-xl border border-slate-800 bg-slate-900/60 p-4 space-y-3"
      >
        <div class="flex items-center justify-between text-xs font-mono">
          <span class="text-slate-400">Request #{{ req.id }} · {{ req.requestType }}</span>
          <span
            :class="[
              'px-2 py-0.5 rounded font-semibold',
              req.status === 'PEER_ACCEPTED'
                ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-600/50'
                : req.status === 'PEER_REJECTED'
                ? 'bg-rose-950/80 text-rose-300 border border-rose-600/50'
                : 'bg-violet-950/80 text-violet-200 border border-violet-500/50',
            ]"
          >
            {{ req.status }}
          </span>
        </div>

        <!-- Explicit Peer Swap Sentence -->
        <p class="text-xs text-slate-200 leading-relaxed">
          <strong class="text-white">{{ req.requesterName }}</strong> wants to swap their
          <span class="text-sky-300 font-medium">{{ req.sourceShiftSummary }}</span>
          with your
          <span class="text-emerald-300 font-medium">{{ req.targetShiftSummary }}</span>.
        </p>

        <p v-if="req.reason" class="text-xs text-slate-400 italic">
          "{{ req.reason }}"
        </p>

        <!-- Peer Accept / Reject Touch Actions -->
        <div class="grid grid-cols-2 gap-2.5 pt-2">
          <button
            type="button"
            @click="handlePeerDecision(req.id, true)"
            class="py-2.5 rounded-lg bg-emerald-400 text-slate-950 text-xs font-semibold hover:bg-emerald-300 active:scale-98 transition"
          >
            Accept (PEER_ACCEPTED)
          </button>
          <button
            type="button"
            @click="handlePeerDecision(req.id, false)"
            class="py-2.5 rounded-lg bg-slate-950 border border-rose-700/70 text-rose-300 text-xs font-semibold hover:bg-rose-950/50 active:scale-98 transition"
          >
            Reject (PEER_REJECTED)
          </button>
        </div>
      </article>
    </div>

    <!-- Modal to Initiate a New Swap Request -->
    <div
      v-if="isNewSwapModalOpen"
      class="fixed inset-0 z-50 bg-slate-950/80 flex items-center justify-center p-4"
    >
      <div class="w-full max-w-md rounded-2xl bg-slate-900 border border-slate-800 p-5 space-y-4 text-xs">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3">
          <h3 class="text-sm font-semibold text-white">Initiate Peer Shift Swap</h3>
          <button
            type="button"
            @click="isNewSwapModalOpen = false"
            class="text-slate-400 hover:text-white"
          >
            Close
          </button>
        </div>

        <div class="space-y-3">
          <div>
            <label class="block text-slate-400 mb-1">1. Select Your Shift to Swap</label>
            <select
              v-model="selectedOwnShiftId"
              class="w-full px-3 py-2 bg-slate-950 border border-slate-800 rounded-lg text-white font-mono"
            >
              <option v-for="s in myEligibleShifts" :key="s.id" :value="s.id">
                {{ formatShiftDescription(s.id) }}
              </option>
            </select>
          </div>

          <div>
            <label class="block text-slate-400 mb-1">2. Select Target Peer Physician</label>
            <select
              v-model="selectedPeerDoctorId"
              class="w-full px-3 py-2 bg-slate-950 border border-slate-800 rounded-lg text-white font-mono"
            >
              <option v-for="doc in peerDoctors" :key="doc.id" :value="doc.id">
                {{ doc.fullName }} ({{ doc.primarySkill }})
              </option>
            </select>
          </div>

          <div>
            <label class="block text-slate-400 mb-1">
              3. Select Peer's Reciprocal Shift (Optional)
            </label>
            <select
              v-model="selectedPeerShiftId"
              class="w-full px-3 py-2 bg-slate-950 border border-slate-800 rounded-lg text-white font-mono"
            >
              <option :value="null">One-Way Coverage (No Reciprocal Shift)</option>
              <option v-for="ps in selectedPeerShifts" :key="ps.id" :value="ps.id">
                {{ formatShiftDescription(ps.id) }}
              </option>
            </select>
          </div>

          <div>
            <label class="block text-slate-400 mb-1">Reason / Clinical Handover Note</label>
            <input
              v-model="swapReason"
              type="text"
              placeholder="e.g., Swapping 24h ICU duty for Thursday duty"
              class="w-full px-3 py-2 bg-slate-950 border border-slate-800 rounded-lg text-white"
            />
          </div>
        </div>

        <div class="flex justify-end gap-2 pt-2">
          <button
            type="button"
            @click="isNewSwapModalOpen = false"
            class="px-3 py-2 rounded-lg bg-slate-800 text-slate-300"
          >
            Cancel
          </button>
          <button
            type="button"
            @click="handleSubmitNewSwap"
            class="px-4 py-2 rounded-lg bg-sky-400 text-slate-950 font-semibold"
          >
            Submit Swap Proposal
          </button>
        </div>
      </div>
    </div>
  </section>
</template>
