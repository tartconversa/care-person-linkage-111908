"""SQLite 模式：事件日志、当前来源状态、版本化仓表。

三层结构：
- events：追加式证据事件日志（接收、确认、撤销、重新启用、裁定），重放幂等；
- 当前来源状态表：由接收事务维护，是重建的输入；
- 版本化仓表（dim_/fact_/partner_day/settlement_/person_change/pending_item/
  candidate_pair）：每次重建整体写入一个版本，指针原子切换。
"""
from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  payload TEXT NOT NULL,
  received_at TEXT NOT NULL,
  UNIQUE(event_type, content_hash)
);
CREATE TABLE IF NOT EXISTS identities(
  identity_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  local_id TEXT NOT NULL,
  from_date TEXT NOT NULL,
  until_date TEXT,
  name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proofs(
  proof_id TEXT PRIMARY KEY,
  left_id TEXT NOT NULL,
  right_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  from_date TEXT NOT NULL,
  until_date TEXT,
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmations(
  confirmation_id TEXT PRIMARY KEY,
  left_id TEXT NOT NULL,
  right_id TEXT NOT NULL,
  from_date TEXT NOT NULL,
  until_date TEXT,
  submitted_by TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revocations(
  revocation_id TEXT PRIMARY KEY,
  target_kind TEXT NOT NULL,
  target_id TEXT NOT NULL,
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reactivations(
  reactivation_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  local_id TEXT NOT NULL,
  identity_id TEXT NOT NULL,
  from_date TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rulings(
  ruling_id TEXT PRIMARY KEY,
  pending_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  left_id TEXT NOT NULL,
  right_id TEXT NOT NULL,
  from_date TEXT NOT NULL,
  until_date TEXT,
  overrides TEXT NOT NULL,
  rejects TEXT NOT NULL,
  submitted_by TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS services(
  service_id TEXT PRIMARY KEY,
  identity_id TEXT NOT NULL,
  date TEXT NOT NULL,
  independent_identity TEXT,
  household_claim TEXT
);
CREATE TABLE IF NOT EXISTS households(
  evidence_id TEXT PRIMARY KEY,
  household_id TEXT NOT NULL,
  member TEXT NOT NULL,
  confirmed INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries(
  delivery_id TEXT PRIMARY KEY,
  service_id TEXT NOT NULL,
  old_person_ref TEXT NOT NULL,
  partner TEXT NOT NULL,
  baseline_person TEXT,
  baseline_version INTEGER
);
CREATE TABLE IF NOT EXISTS permissions(
  permission_id TEXT PRIMARY KEY,
  identity_id TEXT NOT NULL,
  partner TEXT NOT NULL,
  fields TEXT NOT NULL,
  from_date TEXT NOT NULL,
  until_date TEXT
);
CREATE TABLE IF NOT EXISTS versions(
  version INTEGER PRIMARY KEY,
  content_hash TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dim_person(
  version INTEGER NOT NULL,
  person_id TEXT NOT NULL,
  names TEXT NOT NULL,
  sources TEXT NOT NULL,
  members TEXT NOT NULL,
  PRIMARY KEY(version, person_id)
);
CREATE TABLE IF NOT EXISTS dim_person_member(
  version INTEGER NOT NULL,
  person_id TEXT NOT NULL,
  identity_id TEXT NOT NULL,
  link_basis TEXT NOT NULL,
  covered_segments TEXT NOT NULL,
  inst_from TEXT NOT NULL,
  inst_until TEXT,
  PRIMARY KEY(version, person_id, identity_id)
);
CREATE TABLE IF NOT EXISTS fact_service(
  version INTEGER NOT NULL,
  service_id TEXT NOT NULL,
  date TEXT NOT NULL,
  source_identity_id TEXT NOT NULL,
  effective_identity_id TEXT NOT NULL,
  person_id TEXT,
  basis TEXT NOT NULL,
  ambiguous INTEGER NOT NULL,
  reasons TEXT NOT NULL,
  candidates TEXT NOT NULL,
  dispute_id TEXT,
  household_claim TEXT,
  PRIMARY KEY(version, service_id)
);
CREATE TABLE IF NOT EXISTS fact_household(
  version INTEGER NOT NULL,
  evidence_id TEXT NOT NULL,
  household_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  confirmed INTEGER NOT NULL,
  from_date TEXT NOT NULL,
  until_date TEXT,
  via_instance TEXT NOT NULL,
  PRIMARY KEY(version, evidence_id)
);
CREATE TABLE IF NOT EXISTS partner_day(
  version INTEGER NOT NULL,
  partner TEXT NOT NULL,
  identity_id TEXT NOT NULL,
  service_id TEXT NOT NULL,
  date TEXT NOT NULL,
  visible TEXT NOT NULL,
  permission_id TEXT NOT NULL,
  PRIMARY KEY(version, partner, identity_id, service_id)
);
CREATE TABLE IF NOT EXISTS settlement_delivery(
  version INTEGER NOT NULL,
  delivery_id TEXT NOT NULL,
  service_id TEXT NOT NULL,
  old_person_ref TEXT NOT NULL,
  partner TEXT NOT NULL,
  current_person_id TEXT,
  baseline_person_id TEXT,
  status TEXT NOT NULL,
  PRIMARY KEY(version, delivery_id)
);
CREATE TABLE IF NOT EXISTS settlement_replacement(
  version INTEGER NOT NULL,
  replacement_id TEXT NOT NULL,
  delivery_id TEXT NOT NULL,
  old_person_ref TEXT NOT NULL,
  baseline_person_id TEXT,
  current_person_id TEXT,
  reason TEXT NOT NULL,
  PRIMARY KEY(version, replacement_id)
);
CREATE TABLE IF NOT EXISTS person_change(
  version INTEGER NOT NULL,
  person_id TEXT NOT NULL,
  change_kind TEXT NOT NULL,
  predecessors TEXT NOT NULL,
  successors TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(version, person_id)
);
CREATE TABLE IF NOT EXISTS pending_item(
  version INTEGER NOT NULL,
  pending_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  left_id TEXT NOT NULL,
  right_id TEXT NOT NULL,
  evidence TEXT NOT NULL,
  status TEXT NOT NULL,
  resolved_by TEXT,
  PRIMARY KEY(version, pending_id, status)
);
CREATE TABLE IF NOT EXISTS candidate_pair(
  version INTEGER NOT NULL,
  left_id TEXT NOT NULL,
  right_id TEXT NOT NULL,
  clue TEXT NOT NULL,
  detail TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(version, left_id, right_id, clue)
);
"""

# 版本化仓表的列（不含 version 列），建行与发布的唯一事实来源。
VERSIONED_COLUMNS = {
    'dim_person': ['person_id', 'names', 'sources', 'members'],
    'dim_person_member': ['person_id', 'identity_id', 'link_basis', 'covered_segments', 'inst_from', 'inst_until'],
    'fact_service': ['service_id', 'date', 'source_identity_id', 'effective_identity_id', 'person_id',
                     'basis', 'ambiguous', 'reasons', 'candidates', 'dispute_id', 'household_claim'],
    'fact_household': ['evidence_id', 'household_id', 'person_id', 'confirmed', 'from_date', 'until_date', 'via_instance'],
    'partner_day': ['partner', 'identity_id', 'service_id', 'date', 'visible', 'permission_id'],
    'settlement_delivery': ['delivery_id', 'service_id', 'old_person_ref', 'partner',
                            'current_person_id', 'baseline_person_id', 'status'],
    'settlement_replacement': ['replacement_id', 'delivery_id', 'old_person_ref',
                               'baseline_person_id', 'current_person_id', 'reason'],
    'person_change': ['person_id', 'change_kind', 'predecessors', 'successors', 'note'],
    'pending_item': ['pending_id', 'kind', 'left_id', 'right_id', 'evidence', 'status', 'resolved_by'],
    'candidate_pair': ['left_id', 'right_id', 'clue', 'detail', 'status'],
}
