import React, { useState, useMemo } from 'react';
import {
  Check,
  Copy,
  Database,
  Download,
  FileCode,
  GitBranch,
  RotateCcw,
  Search,
  ShieldAlert,
  ShieldCheck,
  Sliders,
} from 'lucide-react';
import { MODELS_PY_CODE, DATABASE_PY_CODE } from './data/step1SourceCode';
import { VALIDATION_PY_CODE, FSM_PY_CODE } from './data/step2SourceCode';

type ActiveSection = 'code' | 'schema' | 'validator' | 'fsm';
type ActiveFile = 'services/validation.py' | 'services/fsm.py' | 'models.py' | 'database.py';

type RequestStatus =
  | 'DRAFT'
  | 'PROPOSED'
  | 'PEER_ACCEPTED'
  | 'PEER_REJECTED'
  | 'APPROVED'
  | 'REJECTED_BY_MANAGER'
  | 'REVISION_REQUESTED'
  | 'CANCELLED';

type UserRole = 'DOCTOR' | 'SENIOR_RESIDENT' | 'HEAD_OF_DEPT';

const ALLOWED_FSM_TRANSITIONS: Record<RequestStatus, RequestStatus[]> = {
  DRAFT: ['PROPOSED', 'CANCELLED'],
  PROPOSED: ['PEER_ACCEPTED', 'PEER_REJECTED', 'REVISION_REQUESTED', 'CANCELLED'],
  PEER_ACCEPTED: ['APPROVED', 'REJECTED_BY_MANAGER', 'REVISION_REQUESTED', 'CANCELLED'],
  PEER_REJECTED: ['REVISION_REQUESTED', 'CANCELLED'],
  REVISION_REQUESTED: ['PROPOSED', 'CANCELLED'],
  REJECTED_BY_MANAGER: ['REVISION_REQUESTED', 'CANCELLED'],
  APPROVED: [],
  CANCELLED: [],
};

interface TableSpec {
  tableName: string;
  modelName: string;
  purpose: string;
  indexes: string[];
  constraints: string[];
  columns: {
    name: string;
    type: string;
    key: string;
    notes: string;
  }[];
}

const SCHEMA_TABLES: TableSpec[] = [
  {
    tableName: 'hospitals',
    modelName: 'Hospital',
    purpose: 'Represents the 4 clinical hospitals, each with ~5 ORs and 1 mandatory 24-hour ICU duty post.',
    indexes: ['ix_hospitals_code (UNIQUE)', 'ix_hospitals_is_active'],
    constraints: ['PRIMARY KEY (id)', 'UNIQUE (code)'],
    columns: [
      { name: 'id', type: 'INTEGER', key: 'PK', notes: 'Auto-increment primary key' },
      { name: 'code', type: 'VARCHAR(16)', key: 'UQ', notes: 'Short identifier e.g. HOSP-A, HOSP-B, HOSP-C, HOSP-D' },
      { name: 'name', type: 'VARCHAR(120)', key: '—', notes: 'Full hospital facility name' },
      { name: 'requires_24h_icu_coverage', type: 'BOOLEAN', key: '—', notes: 'Flags missing 24h ICU Base Shift on any calendar day' },
      { name: 'is_active', type: 'BOOLEAN', key: 'IDX', notes: 'Active status in scheduling engine' },
      { name: 'created_at', type: 'INTEGER', key: '—', notes: 'UTC Unix epoch timestamp (seconds)' },
    ],
  },
  {
    tableName: 'workplaces',
    modelName: 'Workplace',
    purpose: 'Operating Rooms (OR #1..#5) and 24h ICU stations physically bound to a specific hospital.',
    indexes: ['ix_workplaces_hospital_type (hospital_id, workplace_type)', 'ix_workplaces_code'],
    constraints: ['FOREIGN KEY (hospital_id) REFERENCES hospitals(id)'],
    columns: [
      { name: 'id', type: 'INTEGER', key: 'PK', notes: 'Auto-increment primary key' },
      { name: 'hospital_id', type: 'INTEGER', key: 'FK', notes: 'References hospitals.id; enforces physical restriction' },
      { name: 'code', type: 'VARCHAR(32)', key: 'IDX', notes: 'Room code e.g. HOSP-A-OR-2, HOSP-A-ICU-1' },
      { name: 'name', type: 'VARCHAR(100)', key: '—', notes: 'Operating Room #2 or Main ICU Duty' },
      { name: 'workplace_type', type: 'ENUM(OR, ICU)', key: 'IDX', notes: 'Clinical unit classification' },
      { name: 'required_skills', type: 'JSON', key: '—', notes: 'Required specialty qualifications array' },
    ],
  },
  {
    tableName: 'users',
    modelName: 'User',
    purpose: '30 Anesthesiology & ICU physicians with RBAC roles and Senior Resident hospital scoping.',
    indexes: ['ix_users_email (UNIQUE)', 'ix_users_role', 'ix_users_assigned_hospital_id'],
    constraints: [
      'FOREIGN KEY (assigned_hospital_id) REFERENCES hospitals(id)',
      "CHECK ((role != 'SENIOR_RESIDENT') OR (assigned_hospital_id IS NOT NULL))",
    ],
    columns: [
      { name: 'id', type: 'INTEGER', key: 'PK', notes: 'Auto-increment primary key' },
      { name: 'email', type: 'VARCHAR(160)', key: 'UQ', notes: 'Unique login identifier' },
      { name: 'full_name', type: 'VARCHAR(120)', key: '—', notes: 'Physician full name' },
      { name: 'role', type: 'ENUM(UserRole)', key: 'IDX', notes: 'DOCTOR · SENIOR_RESIDENT · HEAD_OF_DEPT' },
      { name: 'assigned_hospital_id', type: 'INTEGER NULL', key: 'FK', notes: 'Mandatory scope for SENIOR_RESIDENT' },
      { name: 'skills', type: 'JSON', key: '—', notes: 'Specialty tags e.g. ["ICU_24H", "CARDIAC_OR"]' },
      { name: 'contract_weekly_hours', type: 'INTEGER', key: '—', notes: 'Baseline weekly hours (default 40)' },
    ],
  },
  {
    tableName: 'shifts',
    modelName: 'Shift',
    purpose: 'Unified self-referential table storing Base Shifts (Parent) and Operational Tasks (Child) with UTC Unix timestamps.',
    indexes: [
      'ix_shifts_doctor_interval (doctor_id, start_ts, end_ts)',
      'ix_shifts_hospital_interval (hospital_id, shift_type, start_ts, end_ts)',
      'ix_shifts_parent_shift_id',
    ],
    constraints: [
      'CHECK (end_ts > start_ts)',
      "CHECK ((shift_type = 'BASE_SHIFT' AND parent_shift_id IS NULL) OR (shift_type = 'OPERATIONAL_TASK' AND parent_shift_id IS NOT NULL))",
      'FOREIGN KEY (parent_shift_id) REFERENCES shifts(id)',
    ],
    columns: [
      { name: 'id', type: 'INTEGER', key: 'PK', notes: 'Primary key' },
      { name: 'shift_type', type: 'ENUM(ShiftType)', key: 'IDX', notes: 'BASE_SHIFT (Parent) or OPERATIONAL_TASK (Child)' },
      { name: 'parent_shift_id', type: 'INTEGER NULL', key: 'FK', notes: 'Self-referential FK to parent Base Shift' },
      { name: 'doctor_id', type: 'INTEGER', key: 'FK', notes: 'Assigned physician (users.id)' },
      { name: 'hospital_id', type: 'INTEGER', key: 'FK', notes: 'Physical hospital; Child must match Parent hospital_id' },
      { name: 'workplace_id', type: 'INTEGER', key: 'FK', notes: 'Assigned OR room or ICU duty station' },
      { name: 'start_ts', type: 'INTEGER', key: 'IDX', notes: 'UTC Unix epoch start (StartA < EndB AND EndA > StartB)' },
      { name: 'end_ts', type: 'INTEGER', key: 'IDX', notes: 'UTC Unix epoch end' },
      { name: 'is_24h_icu_duty', type: 'BOOLEAN', key: 'IDX', notes: 'Satisfies daily 24h ICU hospital coverage requirement' },
      { name: 'is_fatigue_risk', type: 'BOOLEAN', key: 'IDX', notes: 'Tagged True when continuous work > 24h (up to 32h)' },
      { name: 'continuous_hours_at_end', type: 'FLOAT', key: '—', notes: 'Cumulative continuous work hours at shift end' },
      { name: 'fatigue_risk_hours', type: 'FLOAT', key: '—', notes: 'Hours exceeding the 24h threshold (e.g. 8.0h on a 32h run)' },
    ],
  },
  {
    tableName: 'requests',
    modelName: 'ShiftRequest',
    purpose: 'Finite State Machine (FSM) for peer shift swaps, vacation requests, and schedule revisions.',
    indexes: [
      'ix_requests_hospital_status (hospital_id, status)',
      'ix_requests_requester_status (requester_id, status)',
    ],
    constraints: [
      'FOREIGN KEY (source_shift_id) REFERENCES shifts(id)',
      'FOREIGN KEY (target_shift_id) REFERENCES shifts(id)',
    ],
    columns: [
      { name: 'id', type: 'INTEGER', key: 'PK', notes: 'Primary key' },
      { name: 'request_type', type: 'ENUM(RequestType)', key: 'IDX', notes: 'SHIFT_SWAP · VACATION · REVISION' },
      { name: 'status', type: 'ENUM(RequestStatus)', key: 'IDX', notes: 'DRAFT -> PROPOSED -> PEER_ACCEPTED -> APPROVED ...' },
      { name: 'hospital_id', type: 'INTEGER', key: 'FK', notes: 'Scopes approval rights for Senior Residents' },
      { name: 'requester_id', type: 'INTEGER', key: 'FK', notes: 'Initiating physician (users.id)' },
      { name: 'target_doctor_id', type: 'INTEGER NULL', key: 'FK', notes: 'Counterparty physician for SHIFT_SWAP' },
      { name: 'source_shift_id', type: 'INTEGER NULL', key: 'FK', notes: 'Shift to be transferred upon APPROVED state' },
      { name: 'target_shift_id', type: 'INTEGER NULL', key: 'FK', notes: 'Reciprocal shift in a two-way swap' },
      { name: 'reviewed_by_id', type: 'INTEGER NULL', key: 'FK', notes: 'Approving Senior Resident or Head of Dept' },
    ],
  },
  {
    tableName: 'time_logs',
    modelName: 'TimeLog',
    purpose: 'PWA check-in and check-out records for real-time attendance and >24h fatigue verification.',
    indexes: ['ix_timelogs_doctor_checkin (doctor_id, check_in_ts)'],
    constraints: ['CHECK (check_out_ts IS NULL OR check_out_ts >= check_in_ts)'],
    columns: [
      { name: 'id', type: 'INTEGER', key: 'PK', notes: 'Primary key' },
      { name: 'shift_id', type: 'INTEGER', key: 'FK', notes: 'Linked scheduled shift (shifts.id)' },
      { name: 'doctor_id', type: 'INTEGER', key: 'FK', notes: 'Physician checking in via PWA' },
      { name: 'check_in_ts', type: 'INTEGER', key: 'IDX', notes: 'UTC Unix timestamp of check-in' },
      { name: 'check_out_ts', type: 'INTEGER NULL', key: '—', notes: 'UTC Unix timestamp of check-out' },
      { name: 'recorded_duration_seconds', type: 'INTEGER NULL', key: '—', notes: 'Computed actual worked duration' },
      { name: 'is_fatigue_flagged', type: 'BOOLEAN', key: 'IDX', notes: 'True if actual continuous attendance > 24h' },
    ],
  },
  {
    tableName: 'audit_logs',
    modelName: 'AuditLog',
    purpose: 'Immutable forensic log capturing atomic shift swap commits, FSM transitions, and manager overrides.',
    indexes: [
      'ix_audit_entity_lookup (entity_type, entity_id, created_at)',
      'ix_audit_hospital_time (hospital_id, created_at)',
    ],
    constraints: ['PRIMARY KEY (id)'],
    columns: [
      { name: 'id', type: 'INTEGER', key: 'PK', notes: 'Primary key' },
      { name: 'actor_id', type: 'INTEGER NULL', key: 'FK', notes: 'Acting User ID' },
      { name: 'actor_role', type: 'ENUM(UserRole)', key: '—', notes: 'Role at time of action' },
      { name: 'entity_type', type: 'VARCHAR(64)', key: 'IDX', notes: 'Shift · ShiftRequest · TimeLog · User' },
      { name: 'entity_id', type: 'INTEGER', key: 'IDX', notes: 'Target record primary key' },
      { name: 'action', type: 'ENUM(AuditAction)', key: 'IDX', notes: 'CREATE · FSM_TRANSITION · ATOMIC_SWAP_COMMIT ...' },
      { name: 'previous_state', type: 'JSON NULL', key: '—', notes: 'Pre-mutation snapshot' },
      { name: 'new_state', type: 'JSON NULL', key: '—', notes: 'Post-mutation snapshot' },
      { name: 'created_at', type: 'INTEGER', key: 'IDX', notes: 'UTC Unix timestamp' },
    ],
  },
];

export default function App() {
  const [activeSection, setActiveSection] = useState<ActiveSection>('code');
  const [activeFile, setActiveFile] = useState<ActiveFile>('services/validation.py');
  const [copiedFile, setCopiedFile] = useState<string | null>(null);
  const [schemaSearch, setSchemaSearch] = useState('');
  const [selectedTable, setSelectedTable] = useState<string>('shifts');

  // Domain Rule Validator State
  const [parentHospital, setParentHospital] = useState<'HOSP-A' | 'HOSP-B' | 'HOSP-C'>('HOSP-A');
  const [parentStartHour, setParentStartHour] = useState<number>(8); // Day 1 08:00
  const [parentDurationHours, setParentDurationHours] = useState<number>(24); // 24h ICU Duty
  const [childHospital, setChildHospital] = useState<'HOSP-A' | 'HOSP-B' | 'HOSP-C'>('HOSP-A');
  const [childStartHour, setChildStartHour] = useState<number>(8);
  const [childDurationHours, setChildDurationHours] = useState<number>(8); // 8h OR #2 task inside 24h duty
  const [enableContinuationShift, setEnableContinuationShift] = useState<boolean>(true);
  const [continuationDurationHours, setContinuationDurationHours] = useState<number>(8); // +8h next day = 32h total

  // Shift Swap FSM Simulator State
  const [fsmState, setFsmState] = useState<RequestStatus>('DRAFT');
  const [activeRole, setActiveRole] = useState<UserRole>('DOCTOR');
  const [srAssignedHospital, setSrAssignedHospital] = useState<'HOSP-A' | 'HOSP-B'>('HOSP-A');
  const [requestHospital, setRequestHospital] = useState<'HOSP-A' | 'HOSP-B'>('HOSP-A');
  const [dbShiftOwner, setDbShiftOwner] = useState<string>('Dr. Elena Vance (Requester #104)');
  const [fsmAuditTrail, setFsmAuditTrail] = useState<
    { ts: number; actor: string; from: string; to: string; note: string }[]
  >([
    {
      ts: 1759132800,
      actor: 'Dr. Elena Vance (DOCTOR)',
      from: 'INIT',
      to: 'DRAFT',
      note: 'Created ShiftSwap request #402 for HOSP-A 24h ICU Base Shift; DB shift owner unchanged.',
    },
  ]);
  const [fsmError, setFsmError] = useState<string | null>(null);

  const getCodeForFile = (file: ActiveFile): string => {
    switch (file) {
      case 'services/validation.py':
        return VALIDATION_PY_CODE;
      case 'services/fsm.py':
        return FSM_PY_CODE;
      case 'models.py':
        return MODELS_PY_CODE;
      case 'database.py':
        return DATABASE_PY_CODE;
    }
  };

  const currentCode = getCodeForFile(activeFile);

  const handleCopyCode = async (file: ActiveFile) => {
    const text = getCodeForFile(file);
    await navigator.clipboard.writeText(text);
    setCopiedFile(file);
    setTimeout(() => setCopiedFile(null), 1800);
  };

  const handleDownloadCode = (file: ActiveFile) => {
    const text = getCodeForFile(file);
    const blob = new Blob([text], { type: 'text/x-python' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = file.replace('services/', '');
    a.click();
    URL.revokeObjectURL(url);
  };

  // Filtered tables for Schema Inspector
  const filteredTables = useMemo(() => {
    const q = schemaSearch.trim().toLowerCase();
    if (!q) return SCHEMA_TABLES;
    return SCHEMA_TABLES.filter(
      (t) =>
        t.tableName.toLowerCase().includes(q) ||
        t.modelName.toLowerCase().includes(q) ||
        t.purpose.toLowerCase().includes(q) ||
        t.columns.some((c) => c.name.toLowerCase().includes(q) || c.notes.toLowerCase().includes(q))
    );
  }, [schemaSearch]);

  const activeTableSpec = useMemo(
    () => SCHEMA_TABLES.find((t) => t.tableName === selectedTable) || SCHEMA_TABLES[3],
    [selectedTable]
  );

  // Domain Validation Calculations (Unix timestamps anchored to 2026-10-01 00:00:00 UTC = 1790812800)
  const validationResult = useMemo(() => {
    const baseEpoch = 1790812800;
    const parentStartTs = baseEpoch + parentStartHour * 3600;
    const parentEndTs = parentStartTs + parentDurationHours * 3600;

    const childStartTs = baseEpoch + childStartHour * 3600;
    const childEndTs = childStartTs + childDurationHours * 3600;

    const contStartTs = parentEndTs;
    const contEndTs = contStartTs + (enableContinuationShift ? continuationDurationHours * 3600 : 0);

    const hospitalMatches = parentHospital === childHospital;
    const temporallyContained = childStartTs >= parentStartTs && childEndTs <= parentEndTs;
    const totalContinuousHours =
      parentDurationHours + (enableContinuationShift ? continuationDurationHours : 0);
    const isFatigueRisk = totalContinuousHours > 24;
    const fatigueRiskHours = Math.max(0, totalContinuousHours - 24);

    // Overlap check between Parent Base Shift and Continuation Base Shift: StartA < EndB AND EndA > StartB
    const parentAndContOverlap =
      enableContinuationShift && parentStartTs < contEndTs && parentEndTs > contStartTs;

    return {
      parentStartTs,
      parentEndTs,
      childStartTs,
      childEndTs,
      contStartTs,
      contEndTs,
      hospitalMatches,
      temporallyContained,
      totalContinuousHours,
      isFatigueRisk,
      fatigueRiskHours,
      parentAndContOverlap,
      isValidChildTask: hospitalMatches && temporallyContained,
    };
  }, [
    parentHospital,
    parentStartHour,
    parentDurationHours,
    childHospital,
    childStartHour,
    childDurationHours,
    enableContinuationShift,
    continuationDurationHours,
  ]);

  // FSM Transition Handler with RBAC enforcement
  const handleFsmTransition = (targetState: RequestStatus) => {
    setFsmError(null);
    const allowed = ALLOWED_FSM_TRANSITIONS[fsmState];
    if (!allowed.includes(targetState)) {
      setFsmError(
        `Illegal FSM transition: Cannot move directly from ${fsmState} to ${targetState}.`
      );
      return;
    }

    // RBAC Check for Manager States
    if (targetState === 'APPROVED' || targetState === 'REJECTED_BY_MANAGER') {
      if (activeRole === 'DOCTOR') {
        setFsmError(
          `RBAC Permission Denied: Role DOCTOR cannot execute '${targetState}'. Requires SENIOR_RESIDENT (scoped to ${requestHospital}) or HEAD_OF_DEPT.`
        );
        return;
      }
      if (activeRole === 'SENIOR_RESIDENT' && srAssignedHospital !== requestHospital) {
        setFsmError(
          `Hospital Scope Violation: SENIOR_RESIDENT assigned to ${srAssignedHospital} has read-only access to ${requestHospital} and cannot approve/reject its requests.`
        );
        return;
      }
    }

    const nowTs = Math.floor(Date.now() / 1000);
    let note = `Transitioned request #402 from ${fsmState} to ${targetState}.`;

    if (targetState === 'APPROVED') {
      setDbShiftOwner('Dr. Marcus Thorne (Target Peer #118) — Atomically Committed');
      note = `ATOMIC_SWAP_COMMIT: Shift #881 & Child OR Task #882 reassigned to Dr. Marcus Thorne inside SQLite WAL transaction.`;
    }

    setFsmState(targetState);
    setFsmAuditTrail((prev) => [
      {
        ts: nowTs,
        actor:
          activeRole === 'SENIOR_RESIDENT'
            ? `Senior Resident (${srAssignedHospital})`
            : activeRole === 'HEAD_OF_DEPT'
            ? 'Head of Department (Global)'
            : 'Dr. Elena Vance / Peer (DOCTOR)',
        from: fsmState,
        to: targetState,
        note,
      },
      ...prev,
    ]);
  };

  const resetFsmSimulator = () => {
    setFsmState('DRAFT');
    setFsmError(null);
    setDbShiftOwner('Dr. Elena Vance (Requester #104)');
    setFsmAuditTrail([
      {
        ts: Math.floor(Date.now() / 1000),
        actor: 'Dr. Elena Vance (DOCTOR)',
        from: 'INIT',
        to: 'DRAFT',
        note: 'Reset ShiftSwap request #402 to DRAFT; DB shift owner remains Dr. Elena Vance.',
      },
    ]);
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col">
      {/* Top Bar Contract: Zone 1 (Single wordmark) — Zone 2 (4 nav links) — Zone 3 (Primary action) */}
      <header className="flex items-center justify-between px-6 py-3.5 border-b border-slate-800 bg-slate-950/95 sticky top-0 z-30">
        <a
          href="#top"
          onClick={(e) => {
            e.preventDefault();
            setActiveSection('code');
          }}
          className="text-base font-semibold tracking-tight text-white whitespace-nowrap"
        >
          ChronoMed ICU
        </a>

        <nav className="flex items-center gap-6 text-sm font-medium text-slate-400">
          <button
            type="button"
            onClick={() => setActiveSection('code')}
            className={`py-1 transition-colors whitespace-nowrap border-b-2 ${
              activeSection === 'code'
                ? 'text-white border-sky-400'
                : 'border-transparent hover:text-slate-200'
            }`}
          >
            Python Source
          </button>
          <button
            type="button"
            onClick={() => setActiveSection('schema')}
            className={`py-1 transition-colors whitespace-nowrap border-b-2 ${
              activeSection === 'schema'
                ? 'text-white border-sky-400'
                : 'border-transparent hover:text-slate-200'
            }`}
          >
            SQLModel Schema
          </button>
          <button
            type="button"
            onClick={() => setActiveSection('validator')}
            className={`py-1 transition-colors whitespace-nowrap border-b-2 ${
              activeSection === 'validator'
                ? 'text-white border-sky-400'
                : 'border-transparent hover:text-slate-200'
            }`}
          >
            32h Fatigue & Overlap Lab
          </button>
          <button
            type="button"
            onClick={() => setActiveSection('fsm')}
            className={`py-1 transition-colors whitespace-nowrap border-b-2 ${
              activeSection === 'fsm'
                ? 'text-white border-sky-400'
                : 'border-transparent hover:text-slate-200'
            }`}
          >
            Shift Swap FSM
          </button>
        </nav>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => handleCopyCode(activeFile)}
            className="px-3.5 py-1.5 text-xs font-medium text-slate-950 bg-sky-400 rounded-md hover:bg-sky-300 transition-colors whitespace-nowrap flex items-center gap-1.5"
          >
            {copiedFile === activeFile ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
            <span>{copiedFile === activeFile ? `Copied ${activeFile}` : `Copy ${activeFile}`}</span>
          </button>
        </div>
      </header>

      {/* Main Content Container */}
      <main className="flex-1 max-w-[1400px] w-full mx-auto px-6 py-8 space-y-8">
        {/* Step 1 Architecture Header */}
        <section className="border-b border-slate-800 pb-6 flex flex-col lg:flex-row lg:items-end justify-between gap-6">
          <div className="space-y-2 max-w-3xl">
            <div className="text-xs text-slate-400 flex items-center gap-2 font-mono tabular-nums">
              <span>Step 1 &amp; Step 2 Deliverables</span>
              <span aria-hidden="true">·</span>
              <span>Python 3.12 + FastAPI + SQLModel</span>
              <span aria-hidden="true">·</span>
              <span>Validation Engine + Atomic FSM</span>
            </div>
            <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight text-white">
              Core Business Logic, Shift Validation &amp; FSM Services
            </h1>
            <p className="text-sm text-slate-400 leading-relaxed">
              Production domain services (<code className="text-slate-200">services/validation.py</code>,{' '}
              <code className="text-slate-200">services/fsm.py</code>) and SQLite WAL schema (
              <code className="text-slate-200">models.py</code>, <code className="text-slate-200">database.py</code>)
              implementing interval overlap checks, parent-child hospital consistency, 24h ICU coverage gap analysis,
              32h continuous work <code className="text-slate-200">FATIGUE_RISK_HIGH</code> stitching, and atomic RBAC
              shift swaps.
            </p>
          </div>

          {/* Key Quantitative Metrics (Unboxed clean layout with hairline dividers) */}
          <div className="grid grid-cols-3 divide-x divide-slate-800 border border-slate-800 rounded-lg bg-slate-900/50">
            <div className="px-4 py-2.5">
              <div className="text-xs text-slate-400">Domain Scale</div>
              <div className="text-sm font-semibold text-white font-mono tabular-nums mt-0.5">
                30 Docs · 4 Hosps
              </div>
            </div>
            <div className="px-4 py-2.5">
              <div className="text-xs text-slate-400">Continuous Cap</div>
              <div className="text-sm font-semibold text-amber-400 font-mono tabular-nums mt-0.5">
                32h (&gt;24h Flag)
              </div>
            </div>
            <div className="px-4 py-2.5">
              <div className="text-xs text-slate-400">SQLite Engine</div>
              <div className="text-sm font-semibold text-emerald-400 font-mono tabular-nums mt-0.5">
                WAL + FK=ON
              </div>
            </div>
          </div>
        </section>

        {/* SECTION 1: PYTHON SOURCE CODE VIEWER (models.py & database.py) */}
        {activeSection === 'code' && (
          <section className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-4">
              {/* Segmented Interactive File Switcher */}
              <div className="flex flex-wrap items-center gap-1 p-1 bg-slate-900 border border-slate-800 rounded-lg">
                <button
                  type="button"
                  onClick={() => setActiveFile('services/validation.py')}
                  className={`px-3.5 py-1.5 text-xs font-mono font-medium rounded-md transition-colors flex items-center gap-2 whitespace-nowrap ${
                    activeFile === 'services/validation.py'
                      ? 'bg-slate-800 text-white'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  <ShieldCheck className="w-3.5 h-3.5 text-amber-400" />
                  <span>backend/services/validation.py</span>
                </button>
                <button
                  type="button"
                  onClick={() => setActiveFile('services/fsm.py')}
                  className={`px-3.5 py-1.5 text-xs font-mono font-medium rounded-md transition-colors flex items-center gap-2 whitespace-nowrap ${
                    activeFile === 'services/fsm.py'
                      ? 'bg-slate-800 text-white'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  <GitBranch className="w-3.5 h-3.5 text-sky-400" />
                  <span>backend/services/fsm.py</span>
                </button>
                <button
                  type="button"
                  onClick={() => setActiveFile('models.py')}
                  className={`px-3.5 py-1.5 text-xs font-mono font-medium rounded-md transition-colors flex items-center gap-2 whitespace-nowrap ${
                    activeFile === 'models.py'
                      ? 'bg-slate-800 text-white'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  <FileCode className="w-3.5 h-3.5 text-sky-400" />
                  <span>backend/models.py</span>
                </button>
                <button
                  type="button"
                  onClick={() => setActiveFile('database.py')}
                  className={`px-3.5 py-1.5 text-xs font-mono font-medium rounded-md transition-colors flex items-center gap-2 whitespace-nowrap ${
                    activeFile === 'database.py'
                      ? 'bg-slate-800 text-white'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  <Database className="w-3.5 h-3.5 text-emerald-400" />
                  <span>backend/database.py</span>
                </button>
              </div>

              <div className="flex items-center gap-2.5">
                <button
                  type="button"
                  onClick={() => handleCopyCode(activeFile)}
                  className="px-3 py-1.5 text-xs font-medium text-slate-200 bg-slate-900 border border-slate-800 rounded-md hover:bg-slate-800 transition-colors flex items-center gap-1.5 whitespace-nowrap"
                >
                  {copiedFile === activeFile ? (
                    <Check className="w-3.5 h-3.5 text-emerald-400" />
                  ) : (
                    <Copy className="w-3.5 h-3.5 text-slate-400" />
                  )}
                  <span>{copiedFile === activeFile ? 'Copied to Clipboard' : 'Copy File'}</span>
                </button>
                <button
                  type="button"
                  onClick={() => handleDownloadCode(activeFile)}
                  className="px-3 py-1.5 text-xs font-medium text-slate-200 bg-slate-900 border border-slate-800 rounded-md hover:bg-slate-800 transition-colors flex items-center gap-1.5 whitespace-nowrap"
                >
                  <Download className="w-3.5 h-3.5 text-slate-400" />
                  <span>Download {activeFile}</span>
                </button>
              </div>
            </div>

            {/* Architecture Summary Strip for Selected File */}
            <div className="border border-slate-800 rounded-lg bg-slate-900/40 p-4 grid grid-cols-1 md:grid-cols-3 gap-6">
              {activeFile === 'models.py' ? (
                <>
                  <div>
                    <div className="text-xs font-semibold text-white">
                      01. Parent-Child Shift Hierarchy
                    </div>
                    <p className="text-xs text-slate-400 mt-1 leading-relaxed">
                      Self-referential <code className="text-slate-200">Shift.parent_shift_id</code> links
                      8h OR <code className="text-slate-200">OPERATIONAL_TASK</code> allocations to a 24h ICU{' '}
                      <code className="text-slate-200">BASE_SHIFT</code> while enforcing identical{' '}
                      <code className="text-slate-200">hospital_id</code>.
                    </p>
                  </div>
                  <div>
                    <div className="text-xs font-semibold text-white">
                      02. Integer UTC Overlap Indexing
                    </div>
                    <p className="text-xs text-slate-400 mt-1 leading-relaxed">
                      Composite B-Tree index <code className="text-slate-200">(doctor_id, start_ts, end_ts)</code>{' '}
                      evaluates <code className="text-slate-200">StartA &lt; EndB AND EndA &gt; StartB</code> in
                      sub-millisecond integer arithmetic.
                    </p>
                  </div>
                  <div>
                    <div className="text-xs font-semibold text-white">
                      03. Shift Swap FSM & RBAC Scoping
                    </div>
                    <p className="text-xs text-slate-400 mt-1 leading-relaxed">
                      Enforces <code className="text-slate-200">ALLOWED_FSM_TRANSITIONS</code> graph and SQL{' '}
                      <code className="text-slate-200">CHECK</code> constraint requiring{' '}
                      <code className="text-slate-200">assigned_hospital_id</code> on Senior Residents.
                    </p>
                  </div>
                </>
              ) : (
                <>
                  <div>
                    <div className="text-xs font-semibold text-white">
                      01. Connection-Level WAL & Foreign Keys
                    </div>
                    <p className="text-xs text-slate-400 mt-1 leading-relaxed">
                      Hooks <code className="text-slate-200">@event.listens_for(engine.sync_engine, &quot;connect&quot;)</code>{' '}
                      to execute <code className="text-slate-200">PRAGMA journal_mode=WAL;</code> and{' '}
                      <code className="text-slate-200">PRAGMA foreign_keys=ON;</code> on every connection.
                    </p>
                  </div>
                  <div>
                    <div className="text-xs font-semibold text-white">
                      02. Concurrency & Lock Resilience
                    </div>
                    <p className="text-xs text-slate-400 mt-1 leading-relaxed">
                      Configures <code className="text-slate-200">PRAGMA busy_timeout=5000;</code> and{' '}
                      <code className="text-slate-200">synchronous=NORMAL</code> so 30 PWA check-ins never collide
                      with schedule edits.
                    </p>
                  </div>
                  <div>
                    <div className="text-xs font-semibold text-white">
                      03. Atomic Shift Swap Transaction Helper
                    </div>
                    <p className="text-xs text-slate-400 mt-1 leading-relaxed">
                      Provides <code className="text-slate-200">atomic_transaction(session)</code> context manager
                      guaranteeing that <code className="text-slate-200">APPROVED</code> swaps and{' '}
                      <code className="text-slate-200">AuditLog</code> writes commit or roll back together.
                    </p>
                  </div>
                </>
              )}
            </div>

            {/* Code Display Box */}
            <div className="border border-slate-800 rounded-lg bg-slate-900 overflow-hidden">
              <div className="px-4 py-2.5 border-b border-slate-800 flex items-center justify-between text-xs text-slate-400 font-mono tabular-nums">
                <span>
                  /backend/{activeFile} · {currentCode.split('\n').length} lines · UTF-8 Python 3.12
                </span>
                <span>SQLModel + SQLAlchemy 2.0 Async</span>
              </div>
              <pre className="p-5 text-xs font-mono leading-relaxed text-slate-200 overflow-x-auto max-h-[640px] overflow-y-auto">
                <code>{currentCode}</code>
              </pre>
            </div>
          </section>
        )}

        {/* SECTION 2: SQLMODEL SCHEMA & INDEX INSPECTOR */}
        {activeSection === 'schema' && (
          <section className="grid grid-cols-1 lg:grid-cols-12 gap-6">
            {/* Left Sidebar: Table List + Search */}
            <div className="lg:col-span-4 space-y-4">
              <div className="relative">
                <Search className="w-4 h-4 text-slate-500 absolute left-3 top-1/2 -translate-y-1/2" />
                <input
                  type="text"
                  value={schemaSearch}
                  onChange={(e) => setSchemaSearch(e.target.value)}
                  placeholder="Filter tables, columns, or constraints..."
                  className="w-full pl-9 pr-3 py-2 text-xs bg-slate-900 border border-slate-800 rounded-lg text-slate-100 placeholder-slate-500 focus:outline-none focus:border-sky-400"
                />
              </div>

              <div className="border border-slate-800 rounded-lg bg-slate-900/50 divide-y divide-slate-800">
                {filteredTables.map((table) => {
                  const isSelected = table.tableName === activeTableSpec.tableName;
                  return (
                    <button
                      key={table.tableName}
                      type="button"
                      onClick={() => setSelectedTable(table.tableName)}
                      className={`w-full text-left px-4 py-3 transition-colors flex items-center justify-between ${
                        isSelected ? 'bg-slate-800/90 text-white' : 'hover:bg-slate-900 text-slate-300'
                      }`}
                    >
                      <div>
                        <div className="text-xs font-mono font-semibold">{table.tableName}</div>
                        <div className="text-xs text-slate-400 mt-0.5">
                          Model: <span className="font-mono">{table.modelName}</span>
                        </div>
                      </div>
                      <span className="text-xs font-mono tabular-nums text-slate-400">
                        {table.columns.length} cols
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Right Viewport: Active Table Details */}
            <div className="lg:col-span-8 space-y-6">
              <div className="border border-slate-800 rounded-lg bg-slate-900/40 p-5 space-y-4">
                <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-800 pb-4">
                  <div>
                    <h2 className="text-lg font-semibold text-white font-mono">
                      class {activeTableSpec.modelName}(SQLModel, table=True)
                    </h2>
                    <p className="text-xs text-slate-400 mt-1">{activeTableSpec.purpose}</p>
                  </div>
                  <div className="text-xs font-mono text-slate-400">
                    __tablename__ = &quot;{activeTableSpec.tableName}&quot;
                  </div>
                </div>

                {/* High-Density Column Grid */}
                <div className="overflow-x-auto">
                  <table className="w-full text-left border-collapse">
                    <thead>
                      <tr className="border-b border-slate-800 text-xs text-slate-400">
                        <th className="py-2 pr-4 font-medium">Column</th>
                        <th className="py-2 px-4 font-medium">SQLite / SQLModel Type</th>
                        <th className="py-2 px-4 font-medium">Index / Key</th>
                        <th className="py-2 pl-4 font-medium">Domain Invariant & Purpose</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/70 text-xs">
                      {activeTableSpec.columns.map((col) => (
                        <tr key={col.name} className="hover:bg-slate-900/60">
                          <td className="py-2.5 pr-4 font-mono font-medium text-sky-300 whitespace-nowrap">
                            {col.name}
                          </td>
                          <td className="py-2.5 px-4 font-mono text-slate-300 whitespace-nowrap">
                            {col.type}
                          </td>
                          <td className="py-2.5 px-4 font-mono text-slate-400 whitespace-nowrap">
                            {col.key}
                          </td>
                          <td className="py-2.5 pl-4 text-slate-300">{col.notes}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {/* Indexes & SQL Check Constraints */}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-3 border-t border-slate-800 text-xs">
                  <div>
                    <div className="font-semibold text-slate-200 mb-1.5">Composite & B-Tree Indexes</div>
                    <ul className="space-y-1 font-mono text-slate-400">
                      {activeTableSpec.indexes.map((idx) => (
                        <li key={idx}>{idx}</li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <div className="font-semibold text-slate-200 mb-1.5">Relational & Check Constraints</div>
                    <ul className="space-y-1 font-mono text-slate-400">
                      {activeTableSpec.constraints.map((c) => (
                        <li key={c}>{c}</li>
                      ))}
                    </ul>
                  </div>
                </div>
              </div>
            </div>
          </section>
        )}

        {/* SECTION 3: INTERACTIVE PARENT-CHILD SHIFT & 32-HOUR FATIGUE VALIDATOR */}
        {activeSection === 'validator' && (
          <section className="grid grid-cols-1 lg:grid-cols-12 gap-6">
            <div className="lg:col-span-5 border border-slate-800 rounded-lg bg-slate-900/40 p-5 space-y-5">
              <div className="flex items-center justify-between border-b border-slate-800 pb-3">
                <h2 className="text-base font-semibold text-white flex items-center gap-2">
                  <Sliders className="w-4 h-4 text-sky-400" />
                  <span>Shift & Fatigue Constraint Parameters</span>
                </h2>
                <span className="text-xs font-mono text-slate-400">models.Shift</span>
              </div>

              {/* Parent Base Shift Config */}
              <div className="space-y-3">
                <div className="text-xs font-semibold text-slate-200">
                  1. Parent Base Shift (BASE_SHIFT · 24h ICU Duty)
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-slate-400 mb-1">Parent Hospital</label>
                    <select
                      value={parentHospital}
                      onChange={(e) => setParentHospital(e.target.value as 'HOSP-A' | 'HOSP-B' | 'HOSP-C')}
                      className="w-full px-3 py-1.5 text-xs bg-slate-900 border border-slate-800 rounded-md text-white font-mono"
                    >
                      <option value="HOSP-A">HOSP-A (Central ICU)</option>
                      <option value="HOSP-B">HOSP-B (North Surgical)</option>
                      <option value="HOSP-C">HOSP-C (Trauma Center)</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-slate-400 mb-1">
                      Duration: {parentDurationHours}h
                    </label>
                    <input
                      type="range"
                      min={8}
                      max={24}
                      step={4}
                      value={parentDurationHours}
                      onChange={(e) => setParentDurationHours(Number(e.target.value))}
                      className="w-full accent-sky-400"
                    />
                  </div>
                </div>
              </div>

              {/* Child Operational Task Config */}
              <div className="space-y-3 pt-3 border-t border-slate-800">
                <div className="text-xs font-semibold text-slate-200">
                  2. Child Operational Task (OPERATIONAL_TASK · OR #2 Sub-Allocation)
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-slate-400 mb-1">Child Task Hospital</label>
                    <select
                      value={childHospital}
                      onChange={(e) => setChildHospital(e.target.value as 'HOSP-A' | 'HOSP-B' | 'HOSP-C')}
                      className="w-full px-3 py-1.5 text-xs bg-slate-900 border border-slate-800 rounded-md text-white font-mono"
                    >
                      <option value="HOSP-A">HOSP-A (OR #2)</option>
                      <option value="HOSP-B">HOSP-B (OR #4)</option>
                      <option value="HOSP-C">HOSP-C (OR #1)</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-slate-400 mb-1">
                      Child Start Hour: {String(childStartHour).padStart(2, '0')}:00
                    </label>
                    <input
                      type="range"
                      min={4}
                      max={20}
                      step={2}
                      value={childStartHour}
                      onChange={(e) => setChildStartHour(Number(e.target.value))}
                      className="w-full accent-sky-400"
                    />
                  </div>
                </div>
                <div>
                  <label className="block text-xs text-slate-400 mb-1">
                    Child OR Task Duration: {childDurationHours} hours
                  </label>
                  <input
                    type="range"
                    min={4}
                    max={16}
                    step={2}
                    value={childDurationHours}
                    onChange={(e) => setChildDurationHours(Number(e.target.value))}
                    className="w-full accent-sky-400"
                  />
                </div>
              </div>

              {/* Next-Day Daytime Continuation (32h Continuous Work) */}
              <div className="space-y-3 pt-3 border-t border-slate-800">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-slate-200">
                    3. Post-24h Daytime OR Continuation Shift
                  </span>
                  <input
                    type="checkbox"
                    checked={enableContinuationShift}
                    onChange={(e) => setEnableContinuationShift(e.target.checked)}
                    className="accent-sky-400 w-4 h-4"
                  />
                </div>
                {enableContinuationShift && (
                  <div>
                    <label className="block text-xs text-slate-400 mb-1">
                      Adjacent Daytime Shift Duration: +{continuationDurationHours}h (Total:{' '}
                      {validationResult.totalContinuousHours}h)
                    </label>
                    <input
                      type="range"
                      min={4}
                      max={12}
                      step={2}
                      value={continuationDurationHours}
                      onChange={(e) => setContinuationDurationHours(Number(e.target.value))}
                      className="w-full accent-amber-400"
                    />
                  </div>
                )}
              </div>
            </div>

            {/* Right Output: Live SQLModel Validation Diagnostics */}
            <div className="lg:col-span-7 border border-slate-800 rounded-lg bg-slate-900/40 p-5 space-y-5">
              <div className="flex items-center justify-between border-b border-slate-800 pb-3">
                <h2 className="text-base font-semibold text-white">
                  SQLModel Validation Engine Evaluation
                </h2>
                <span className="text-xs font-mono tabular-nums text-slate-400">
                  StartA &lt; EndB AND EndA &gt; StartB
                </span>
              </div>

              {/* Diagnostic Rows */}
              <div className="divide-y divide-slate-800 border border-slate-800 rounded-lg bg-slate-950/60">
                <div className="p-4 flex items-start justify-between gap-4">
                  <div>
                    <div className="text-xs font-semibold text-white">
                      Physical Hospital Restriction (Parent vs. Child OR Task)
                    </div>
                    <p className="text-xs text-slate-400 mt-0.5 font-mono">
                      parent.hospital_id ({parentHospital}) == child.hospital_id ({childHospital})
                    </p>
                  </div>
                  <div className="flex items-center gap-1.5 text-xs font-mono whitespace-nowrap">
                    {validationResult.hospitalMatches ? (
                      <>
                        <ShieldCheck className="w-4 h-4 text-emerald-400" />
                        <span className="text-emerald-400">PASSED</span>
                      </>
                    ) : (
                      <>
                        <ShieldAlert className="w-4 h-4 text-rose-400" />
                        <span className="text-rose-400">REJECTED (CROSS-HOSPITAL)</span>
                      </>
                    )}
                  </div>
                </div>

                <div className="p-4 flex items-start justify-between gap-4">
                  <div>
                    <div className="text-xs font-semibold text-white">
                      Temporal Containment (Child Interval Inside Parent Window)
                    </div>
                    <p className="text-xs text-slate-400 mt-0.5 font-mono tabular-nums">
                      Parent [{validationResult.parentStartTs}, {validationResult.parentEndTs}] ⊇ Child [
                      {validationResult.childStartTs}, {validationResult.childEndTs}]
                    </p>
                  </div>
                  <div className="flex items-center gap-1.5 text-xs font-mono whitespace-nowrap">
                    {validationResult.temporallyContained ? (
                      <>
                        <ShieldCheck className="w-4 h-4 text-emerald-400" />
                        <span className="text-emerald-400">CONTAINED</span>
                      </>
                    ) : (
                      <>
                        <ShieldAlert className="w-4 h-4 text-rose-400" />
                        <span className="text-rose-400">OUT OF BOUNDS</span>
                      </>
                    )}
                  </div>
                </div>

                <div className="p-4 flex items-start justify-between gap-4">
                  <div>
                    <div className="text-xs font-semibold text-white">
                      32-Hour Continuous Work & Fatigue Risk Tagging
                    </div>
                    <p className="text-xs text-slate-400 mt-0.5 font-mono tabular-nums">
                      continuous_hours_at_end = {validationResult.totalContinuousHours.toFixed(1)}h ·
                      fatigue_risk_hours = {validationResult.fatigueRiskHours.toFixed(1)}h
                    </p>
                  </div>
                  <div className="flex items-center gap-1.5 text-xs font-mono whitespace-nowrap">
                    {validationResult.isFatigueRisk ? (
                      <>
                        <ShieldAlert className="w-4 h-4 text-amber-400" />
                        <span className="text-amber-400">
                          ALLOWED · TAGGED FATIGUE_RISK (+{validationResult.fatigueRiskHours}h)
                        </span>
                      </>
                    ) : (
                      <>
                        <ShieldCheck className="w-4 h-4 text-emerald-400" />
                        <span className="text-emerald-400">NOMINAL (&le;24h)</span>
                      </>
                    )}
                  </div>
                </div>
              </div>

              {/* Generated SQLModel Row Preview */}
              <div className="space-y-2">
                <div className="text-xs font-semibold text-slate-300">
                  Resulting SQLModel <code className="font-mono">Shift</code> Row Attributes
                </div>
                <pre className="p-4 rounded-lg bg-slate-950 border border-slate-800 text-xs font-mono text-slate-300 overflow-x-auto tabular-nums">
                  {`# Parent Base Shift (shifts.id = 881)
Shift(
    id=881,
    shift_type=ShiftType.BASE_SHIFT,
    parent_shift_id=None,
    hospital_id="${parentHospital}",
    start_ts=${validationResult.parentStartTs},
    end_ts=${validationResult.parentEndTs},
    is_24h_icu_duty=${parentDurationHours === 24 ? 'True' : 'False'},
    is_fatigue_risk=False,
    continuous_hours_at_end=${parentDurationHours.toFixed(1)},
)

# Child Operational Task (shifts.id = 882) -> ${
                    validationResult.isValidChildTask
                      ? 'VALIDATED'
                      : 'RAISES ValueError IN validate_as_child_of()'
                  }
Shift(
    id=882,
    shift_type=ShiftType.OPERATIONAL_TASK,
    parent_shift_id=881,
    hospital_id="${childHospital}",
    start_ts=${validationResult.childStartTs},
    end_ts=${validationResult.childEndTs},
)${
                    enableContinuationShift
                      ? `

# Next-Day Daytime Continuation Shift (shifts.id = 883)
Shift(
    id=883,
    shift_type=ShiftType.BASE_SHIFT,
    parent_shift_id=None,
    hospital_id="${parentHospital}",
    start_ts=${validationResult.contStartTs},  # EndA == StartB (No overlap conflict)
    end_ts=${validationResult.contEndTs},
    is_fatigue_risk=${validationResult.isFatigueRisk ? 'True' : 'False'},
    continuous_hours_at_end=${validationResult.totalContinuousHours.toFixed(1)},
    fatigue_risk_hours=${validationResult.fatigueRiskHours.toFixed(1)},
)`
                      : ''
                  }`}
                </pre>
              </div>
            </div>
          </section>
        )}

        {/* SECTION 4: SHIFT SWAP FINITE STATE MACHINE (FSM) & RBAC SIMULATOR */}
        {activeSection === 'fsm' && (
          <section className="grid grid-cols-1 lg:grid-cols-12 gap-6">
            <div className="lg:col-span-6 border border-slate-800 rounded-lg bg-slate-900/40 p-5 space-y-5">
              <div className="flex items-center justify-between border-b border-slate-800 pb-3">
                <h2 className="text-base font-semibold text-white flex items-center gap-2">
                  <GitBranch className="w-4 h-4 text-sky-400" />
                  <span>Shift Swap FSM & RBAC State Simulator</span>
                </h2>
                <button
                  type="button"
                  onClick={resetFsmSimulator}
                  className="px-2.5 py-1 text-xs font-medium text-slate-300 bg-slate-900 border border-slate-800 rounded hover:bg-slate-800 flex items-center gap-1"
                >
                  <RotateCcw className="w-3 h-3" />
                  <span>Reset FSM</span>
                </button>
              </div>

              {/* RBAC Role & Hospital Scope Selector */}
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                  <label className="block text-xs text-slate-400 mb-1">Active Actor Role</label>
                  <select
                    value={activeRole}
                    onChange={(e) => setActiveRole(e.target.value as UserRole)}
                    className="w-full px-2.5 py-1.5 text-xs bg-slate-900 border border-slate-800 rounded text-white font-mono"
                  >
                    <option value="DOCTOR">DOCTOR</option>
                    <option value="SENIOR_RESIDENT">SENIOR_RESIDENT</option>
                    <option value="HEAD_OF_DEPT">HEAD_OF_DEPT</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-slate-400 mb-1">Request Hospital</label>
                  <select
                    value={requestHospital}
                    onChange={(e) => setRequestHospital(e.target.value as 'HOSP-A' | 'HOSP-B')}
                    className="w-full px-2.5 py-1.5 text-xs bg-slate-900 border border-slate-800 rounded text-white font-mono"
                  >
                    <option value="HOSP-A">HOSP-A</option>
                    <option value="HOSP-B">HOSP-B</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-slate-400 mb-1">SR Assigned Scope</label>
                  <select
                    value={srAssignedHospital}
                    onChange={(e) => setSrAssignedHospital(e.target.value as 'HOSP-A' | 'HOSP-B')}
                    disabled={activeRole !== 'SENIOR_RESIDENT'}
                    className="w-full px-2.5 py-1.5 text-xs bg-slate-900 border border-slate-800 rounded text-white font-mono disabled:opacity-40"
                  >
                    <option value="HOSP-A">HOSP-A</option>
                    <option value="HOSP-B">HOSP-B</option>
                  </select>
                </div>
              </div>

              {/* Current State & Atomic DB Shift Ownership Readout */}
              <div className="p-4 rounded-lg bg-slate-950 border border-slate-800 space-y-2">
                <div className="flex items-center justify-between text-xs">
                  <span className="text-slate-400">Current FSM Status:</span>
                  <span className="font-mono font-semibold text-sky-400">{fsmState}</span>
                </div>
                <div className="flex items-center justify-between text-xs pt-2 border-t border-slate-900">
                  <span className="text-slate-400">SQLite Shift #881 Owner:</span>
                  <span
                    className={`font-mono font-medium ${
                      fsmState === 'APPROVED' ? 'text-emerald-400' : 'text-slate-200'
                    }`}
                  >
                    {dbShiftOwner}
                  </span>
                </div>
              </div>

              {fsmError && (
                <div className="p-3 rounded-lg bg-rose-950/50 border border-rose-800/70 text-xs text-rose-200 font-mono">
                  {fsmError}
                </div>
              )}

              {/* Transition Trigger Buttons */}
              <div className="space-y-2">
                <div className="text-xs font-medium text-slate-300">
                  Trigger Next FSM State Transition (Valid transitions from <code className="font-mono">{fsmState}</code>{' '}
                  highlighted):
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                  {(Object.keys(ALLOWED_FSM_TRANSITIONS) as RequestStatus[]).map((stateKey) => {
                    const isAllowed = ALLOWED_FSM_TRANSITIONS[fsmState].includes(stateKey);
                    const isCurrent = fsmState === stateKey;
                    return (
                      <button
                        key={stateKey}
                        type="button"
                        onClick={() => handleFsmTransition(stateKey)}
                        disabled={isCurrent}
                        className={`px-2.5 py-2 text-xs font-mono rounded border text-left transition-colors truncate ${
                          isCurrent
                            ? 'bg-sky-500/20 border-sky-400 text-sky-200 cursor-default'
                            : isAllowed
                            ? 'bg-slate-900 border-slate-700 text-white hover:border-sky-400'
                            : 'bg-slate-950 border-slate-900 text-slate-600 hover:border-rose-800/60'
                        }`}
                      >
                        {stateKey}
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>

            {/* Right Column: Live AuditLog Table */}
            <div className="lg:col-span-6 border border-slate-800 rounded-lg bg-slate-900/40 p-5 space-y-4">
              <div className="flex items-center justify-between border-b border-slate-800 pb-3">
                <h2 className="text-base font-semibold text-white">
                  Immutable AuditLog Stream (<code className="font-mono text-sm">audit_logs</code>)
                </h2>
                <span className="text-xs font-mono tabular-nums text-slate-400">
                  {fsmAuditTrail.length} entries
                </span>
              </div>

              <div className="divide-y divide-slate-800 border border-slate-800 rounded-lg bg-slate-950/60 max-h-[380px] overflow-y-auto">
                {fsmAuditTrail.map((entry, idx) => (
                  <div key={`${entry.ts}-${idx}`} className="p-3.5 space-y-1 text-xs">
                    <div className="flex items-center justify-between font-mono text-slate-400 tabular-nums">
                      <span>{entry.actor}</span>
                      <span>
                        {entry.from} &rarr; <strong className="text-white">{entry.to}</strong> · ts={entry.ts}
                      </span>
                    </div>
                    <p className="text-slate-300">{entry.note}</p>
                  </div>
                ))}
              </div>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
