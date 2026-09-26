"""临床试验分层区组随机分配与盲法服务。"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "randomization.db"
MAX_ARM_LENGTH = 40


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request", details=None):
        super().__init__(message)
        self.message, self.status, self.code, self.details = message, status, code, details


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RandomizationStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('site','coordinator','monitor')),
                    site_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS trials(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                    protocol_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','running','stopped')),
                    arms_json TEXT NOT NULL, strata_factors_json TEXT NOT NULL,
                    block_size INTEGER NOT NULL CHECK(block_size >= 2),
                    seed TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, started_at TEXT,
                    enrollment_held INTEGER NOT NULL DEFAULT 0 CHECK(enrollment_held IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS strata(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    stratum_key TEXT NOT NULL, factors_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(trial_id,stratum_key)
                );
                CREATE TABLE IF NOT EXISTS allocations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    stratum_id INTEGER NOT NULL REFERENCES strata(id),
                    sequence INTEGER NOT NULL, block_no INTEGER NOT NULL,
                    arm TEXT NOT NULL, used_by INTEGER, used_at TEXT,
                    UNIQUE(stratum_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS participants(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    site_id TEXT NOT NULL, external_id TEXT NOT NULL,
                    stratum_id INTEGER NOT NULL REFERENCES strata(id),
                    allocation_id INTEGER NOT NULL UNIQUE REFERENCES allocations(id),
                    allocation_code TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'enrolled'
                        CHECK(status IN ('enrolled','withdrawn','completed')),
                    enrolled_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(trial_id,external_id)
                );
                CREATE TABLE IF NOT EXISTS unblinding_requests(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    participant_id INTEGER NOT NULL REFERENCES participants(id),
                    requester_id TEXT NOT NULL REFERENCES users(id), reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
                    first_approver TEXT REFERENCES users(id), second_approver TEXT REFERENCES users(id),
                    decided_at TEXT, created_at TEXT NOT NULL,
                    blocking_application_id INTEGER REFERENCES version_applications(id),
                    materials_note TEXT
                );
                CREATE TABLE IF NOT EXISTS version_applications(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    requested_by TEXT NOT NULL REFERENCES users(id),
                    protocol_version TEXT NOT NULL, change_reason TEXT NOT NULL,
                    arms_json TEXT NOT NULL, strata_factors_json TEXT NOT NULL,
                    block_size INTEGER NOT NULL, seed TEXT NOT NULL,
                    enrolled_roster_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('reviewing','returned','approved','cancelled')),
                    incompatibility_note TEXT,
                    reviewed_by TEXT REFERENCES users(id), review_note TEXT,
                    decided_at TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_version_applications_trial ON version_applications(trial_id);
                CREATE INDEX IF NOT EXISTS idx_unblinding_participant ON unblinding_requests(participant_id);
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trial_id INTEGER REFERENCES trials(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )
            # 旧库补列（SQLite 无法直接改 CHECK，新增状态通过新列/关联表表达）
            trial_cols = {r["name"] for r in conn.execute("PRAGMA table_info(trials)")}
            if "enrollment_held" not in trial_cols:
                conn.execute("ALTER TABLE trials ADD COLUMN enrollment_held INTEGER NOT NULL DEFAULT 0")
            ub_cols = {r["name"] for r in conn.execute("PRAGMA table_info(unblinding_requests)")}
            if "blocking_application_id" not in ub_cols:
                conn.execute("ALTER TABLE unblinding_requests ADD COLUMN blocking_application_id INTEGER")
            if "materials_note" not in ub_cols:
                conn.execute("ALTER TABLE unblinding_requests ADD COLUMN materials_note TEXT")

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,site_id) VALUES(?,?,?,?)",
                [
                    ("site1", "中心一协调员", "site", "S001"),
                    ("site2", "中心二协调员", "site", "S002"),
                    ("coord", "项目协调员", "coordinator", "CENTER"),
                    ("monitor1", "独立监查员甲", "monitor", "CENTER"),
                    ("monitor2", "独立监查员乙", "monitor", "CENTER"),
                ],
            )

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在或已停用", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _trial(self, conn, trial_id):
        row = conn.execute("SELECT * FROM trials WHERE id=?", (trial_id,)).fetchone()
        if not row:
            raise BusinessError("试验不存在", 404, "not_found")
        return row

    def _audit(self, conn, trial_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(trial_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (trial_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def create_trial(self, user_id, name, protocol_version, arms, strata_factors, block_size, seed):
        name = name.strip()
        if len(name) < 3 or not protocol_version.strip() or len(seed.strip()) < 8:
            raise BusinessError("试验名称、方案版本和至少 8 位随机种子不能为空", 422, "invalid_trial")
        if not isinstance(arms, list) or len(arms) < 2:
            raise BusinessError("至少需要两个试验组", 422, "invalid_arms")
        arms = [str(a).strip() for a in arms]
        if any(not a or len(a) > MAX_ARM_LENGTH for a in arms) or len(set(arms)) != len(arms):
            raise BusinessError("试验组名称必须非空、唯一且不过长", 422, "invalid_arms")
        if not isinstance(strata_factors, list) or any(not str(x).strip() for x in strata_factors) or len(set(strata_factors)) != len(strata_factors):
            raise BusinessError("分层因素必须是非空且不重复的数组", 422, "invalid_strata")
        if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size < len(arms) or block_size % len(arms) != 0:
            raise BusinessError("区组长度必须为试验组数的正整数倍", 422, "invalid_block_size")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"coordinator"})
            try:
                cur = conn.execute(
                    """INSERT INTO trials(name,protocol_version,arms_json,strata_factors_json,block_size,seed,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (name, protocol_version.strip(), json.dumps(arms), json.dumps([str(x).strip() for x in strata_factors]), block_size, seed.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("试验名称已存在", 409, "trial_exists")
            trial_id = cur.lastrowid
            self._audit(conn, trial_id, user_id, "trial.create", {"protocol_version": protocol_version, "arms": len(arms), "block_size": block_size})
            return {"id": trial_id, "name": name, "status": "draft", "arms": arms, "strata_factors": strata_factors, "block_size": block_size}

    def update_protocol(self, user_id, trial_id, protocol_version, arms=None, strata_factors=None, block_size=None, seed=None):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"coordinator"})
            trial = self._trial(conn, trial_id)
            enrolled = conn.execute("SELECT COUNT(*) FROM participants WHERE trial_id=?", (trial_id,)).fetchone()[0]
            if enrolled or trial["status"] != "draft":
                raise BusinessError(
                    "入组开始后方案锁定，不能直接修改；请提交方案版本变更申请（原因+已入组名单+分层核对）",
                    409, "protocol_locked",
                )
            new_arms = arms if arms is not None else json.loads(trial["arms_json"])
            new_strata = strata_factors if strata_factors is not None else json.loads(trial["strata_factors_json"])
            new_block = block_size if block_size is not None else trial["block_size"]
            new_seed = str(seed) if seed is not None else trial["seed"]
            self.create_trial_validation_only(new_arms, new_strata, new_block, new_seed)
            conn.execute(
                """UPDATE trials SET protocol_version=?,arms_json=?,strata_factors_json=?,block_size=?,seed=? WHERE id=?""",
                (protocol_version.strip(), json.dumps(new_arms), json.dumps(new_strata), new_block, new_seed, trial_id),
            )
            self._audit(conn, trial_id, user_id, "protocol.update", {"protocol_version": protocol_version})
            return {"id": trial_id, "protocol_version": protocol_version, "arms": new_arms, "block_size": new_block}

    @staticmethod
    def application_number(application_id):
        return f"VA-{int(application_id):04d}"

    def _reviewing_application(self, conn, trial_id):
        return conn.execute(
            "SELECT * FROM version_applications WHERE trial_id=? AND status='reviewing' ORDER BY id DESC LIMIT 1",
            (trial_id,),
        ).fetchone()

    def _enrolled_roster(self, conn, trial_id):
        rows = conn.execute(
            """SELECT p.id, p.external_id, p.site_id, p.allocation_code, p.status, p.created_at, s.factors_json
               FROM participants p JOIN strata s ON s.id=p.stratum_id
               WHERE p.trial_id=? ORDER BY p.id""",
            (trial_id,),
        ).fetchall()
        roster = []
        for row in rows:
            factors = json.loads(row["factors_json"])
            factors.pop("site_id", None)
            roster.append({
                "participant_id": row["id"], "external_id": row["external_id"],
                "site_id": row["site_id"], "allocation_code": row["allocation_code"],
                "status": row["status"], "enrolled_at": row["created_at"], "factors": factors,
            })
        return roster

    @staticmethod
    def _strata_compatibility(old_factors, new_factors, roster):
        """核对旧分层能否沿用到新方案：分层因素集合必须完全一致，旧受试者才能按原分层继续纳入。"""
        old_set, new_set = set(old_factors), set(new_factors)
        if old_set == new_set:
            return True, ""
        removed = sorted(old_set - new_set)
        added = sorted(new_set - old_set)
        parts = []
        if removed:
            parts.append(f"旧受试者使用的分层因素 {', '.join(removed)} 在新版本中被删除，无法沿用")
        if added:
            parts.append(f"新增分层因素 {', '.join(added)}，已入组的 {len(roster)} 名受试者无对应取值，无法回补")
        return False, "；".join(parts)

    def submit_version_application(
        self, user_id, trial_id, protocol_version, change_reason, arms=None,
        strata_factors=None, block_size=None, seed=None,
    ):
        protocol_version = str(protocol_version).strip()
        change_reason = str(change_reason).strip()
        if not protocol_version:
            raise BusinessError("新版本号不能为空", 422, "invalid_version")
        if len(change_reason) < 10:
            raise BusinessError("请填写版本变更原因（至少 10 字）", 422, "reason_required")
        with self.connect() as conn:
            self._user(conn, user_id, {"coordinator"})
            trial = self._trial(conn, trial_id)
            if trial["status"] != "running":
                raise BusinessError("只有入组进行中的试验可以提交版本变更申请", 409, "trial_not_running")
            if trial["enrollment_held"] or self._reviewing_application(conn, trial_id):
                raise BusinessError("已有版本申请在审核中，不能重复提交", 409, "application_in_review")
            new_arms = arms if arms is not None else json.loads(trial["arms_json"])
            new_strata = strata_factors if strata_factors is not None else json.loads(trial["strata_factors_json"])
            new_block = block_size if block_size is not None else trial["block_size"]
            new_seed = str(seed).strip() if seed is not None else trial["seed"]
            self.create_trial_validation_only(new_arms, new_strata, new_block, new_seed)
            # 已入组名单由系统归档（不信任前端传入），用于审核与事后追溯
            roster = self._enrolled_roster(conn, trial_id)
            old_strata = json.loads(trial["strata_factors_json"])
            compatible, note = self._strata_compatibility(old_strata, new_strata, roster)
            status = "reviewing" if compatible else "returned"
            cur = conn.execute(
                """INSERT INTO version_applications(trial_id,requested_by,protocol_version,change_reason,
                       arms_json,strata_factors_json,block_size,seed,enrolled_roster_json,
                       status,incompatibility_note,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trial_id, user_id, protocol_version, change_reason,
                 json.dumps(new_arms, ensure_ascii=False),
                 json.dumps(new_strata, ensure_ascii=False), new_block, new_seed,
                 json.dumps(roster, ensure_ascii=False), status, note, now()),
            )
            application_id = cur.lastrowid
            if compatible:
                # 审核期间暂停新入组
                conn.execute("UPDATE trials SET enrollment_held=1 WHERE id=?", (trial_id,))
            detail = {
                "application_id": application_id, "number": self.application_number(application_id),
                "protocol_version": protocol_version, "enrolled_count": len(roster),
                "compatible": compatible, "incompatibility_note": note,
            }
            self._audit(conn, trial_id, user_id, "version.submit", detail)
            return self._version_application_payload(conn,
                    conn.execute("SELECT * FROM version_applications WHERE id=?", (application_id,)).fetchone())

    def review_version_application(self, user_id, application_id, decision, review_note=""):
        decision = str(decision)
        if decision not in ("approve", "return"):
            raise BusinessError("审核结论必须是 approve 或 return", 422, "invalid_decision")
        review_note = str(review_note).strip()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"monitor"})
                if len(review_note) < 4:
                    raise BusinessError("请填写审核意见（至少 4 字）", 422, "review_note_required")
                application = conn.execute(
                    "SELECT * FROM version_applications WHERE id=?", (application_id,)
                ).fetchone()
                if not application:
                    raise BusinessError("版本申请不存在", 404, "not_found")
                if application["status"] != "reviewing":
                    raise BusinessError("该版本申请已处理", 409, "already_decided")
                new_status = "approved" if decision == "approve" else "returned"
                conn.execute(
                    "UPDATE version_applications SET status=?,reviewed_by=?,review_note=?,decided_at=? WHERE id=?",
                    (new_status, user_id, review_note, now(), application_id),
                )
                # 审核结束，恢复新入组
                conn.execute("UPDATE trials SET enrollment_held=0 WHERE id=?", (application["trial_id"],))
                if decision == "approve":
                    conn.execute(
                        """UPDATE trials SET protocol_version=?,arms_json=?,strata_factors_json=?,block_size=?,seed=?
                           WHERE id=?""",
                        (application["protocol_version"], application["arms_json"],
                         application["strata_factors_json"], application["block_size"],
                         application["seed"], application["trial_id"]),
                    )
                self._audit(conn, application["trial_id"], user_id, f"version.{new_status}", {
                    "application_id": application_id, "number": self.application_number(application_id),
                    "protocol_version": application["protocol_version"], "review_note": review_note,
                })
                return self._version_application_payload(conn,
                        conn.execute("SELECT * FROM version_applications WHERE id=?", (application_id,)).fetchone())
            except Exception:
                conn.rollback()
                raise

    def cancel_version_application(self, user_id, application_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"coordinator"})
            application = conn.execute(
                "SELECT * FROM version_applications WHERE id=?", (application_id,)
            ).fetchone()
            if not application:
                raise BusinessError("版本申请不存在", 404, "not_found")
            if application["requested_by"] != user_id:
                raise BusinessError("只能撤回自己提交的版本申请", 403, "forbidden")
            if application["status"] not in ("reviewing", "returned"):
                raise BusinessError("已批准的版本申请不能撤回", 409, "already_decided")
            conn.execute(
                "UPDATE version_applications SET status='cancelled',decided_at=? WHERE id=?",
                (now(), application_id),
            )
            conn.execute("UPDATE trials SET enrollment_held=0 WHERE id=?", (application["trial_id"],))
            self._audit(conn, application["trial_id"], user_id, "version.cancel", {
                "application_id": application_id, "number": self.application_number(application_id),
            })
            return self._version_application_payload(conn,
                    conn.execute("SELECT * FROM version_applications WHERE id=?", (application_id,)).fetchone())

    def list_version_applications(self, user_id, trial_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"site", "coordinator", "monitor"})
            self._trial(conn, trial_id)
            rows = conn.execute(
                "SELECT * FROM version_applications WHERE trial_id=? ORDER BY id DESC", (trial_id,)
            ).fetchall()
            return {"items": [self._version_application_payload(conn, row) for row in rows]}

    def get_version_application(self, user_id, application_id, include_roster=False):
        with self.connect() as conn:
            self._user(conn, user_id, {"site", "coordinator", "monitor"})
            row = conn.execute("SELECT * FROM version_applications WHERE id=?", (application_id,)).fetchone()
            if not row:
                raise BusinessError("版本申请不存在", 404, "not_found")
            return self._version_application_payload(conn, row, include_roster=include_roster)

    def _version_application_payload(self, conn, row, include_roster=False):
        payload = {
            "id": row["id"], "number": self.application_number(row["id"]),
            "trial_id": row["trial_id"], "requested_by": row["requested_by"],
            "protocol_version": row["protocol_version"], "change_reason": row["change_reason"],
            "arms": json.loads(row["arms_json"]),
            "strata_factors": json.loads(row["strata_factors_json"]),
            "block_size": row["block_size"], "seed": row["seed"],
            "enrolled_count": len(json.loads(row["enrolled_roster_json"])),
            "status": row["status"], "incompatibility_note": row["incompatibility_note"],
            "reviewed_by": row["reviewed_by"], "review_note": row["review_note"],
            "created_at": row["created_at"], "decided_at": row["decided_at"],
        }
        if include_roster:
            payload["enrolled_roster"] = json.loads(row["enrolled_roster_json"])
        return payload

    @staticmethod
    def create_trial_validation_only(arms, strata_factors, block_size, seed):
        if not isinstance(arms, list) or len(arms) < 2 or len(set(arms)) != len(arms):
            raise BusinessError("试验组配置无效", 422, "invalid_arms")
        if not isinstance(strata_factors, list) or not strata_factors or len(set(strata_factors)) != len(strata_factors):
            raise BusinessError("分层因素配置无效", 422, "invalid_strata")
        if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size < len(arms) or block_size % len(arms):
            raise BusinessError("区组长度无效", 422, "invalid_block_size")
        if len(str(seed)) < 8:
            raise BusinessError("随机种子至少 8 位", 422, "invalid_seed")

    def start_trial(self, user_id, trial_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"coordinator"})
            trial = self._trial(conn, trial_id)
            if trial["status"] != "draft":
                raise BusinessError("只有草稿试验可以开始", 409, "invalid_status")
            conn.execute("UPDATE trials SET status='running',started_at=? WHERE id=?", (now(), trial_id))
            self._audit(conn, trial_id, user_id, "trial.start", {})
            return {"id": trial_id, "status": "running"}

    def _stratum(self, conn, trial, factors, site_id):
        expected = json.loads(trial["strata_factors_json"])
        if set(factors) != set(expected):
            raise BusinessError(f"必须提供分层因素: {', '.join(expected)}", 422, "invalid_factors")
        normalized = {k: str(factors[k]).strip() for k in sorted(expected)}
        if any(not v for v in normalized.values()):
            raise BusinessError("分层因素值不能为空", 422, "invalid_factors")
        key = f"{site_id}|" + json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        row = conn.execute("SELECT * FROM strata WHERE trial_id=? AND stratum_key=?", (trial["id"], key)).fetchone()
        if row:
            return row
        cur = conn.execute(
            "INSERT INTO strata(trial_id,stratum_key,factors_json,created_at) VALUES(?,?,?,?)",
            (trial["id"], key, json.dumps({"site_id": site_id, **normalized}, ensure_ascii=False, sort_keys=True), now()),
        )
        return conn.execute("SELECT * FROM strata WHERE id=?", (cur.lastrowid,)).fetchone()

    def _next_allocation(self, conn, trial, stratum):
        for block_no in range(1, 101):
            count = conn.execute(
                "SELECT COUNT(*) FROM allocations WHERE stratum_id=? AND block_no=?", (stratum["id"], block_no)
            ).fetchone()[0]
            if count == 0:
                rng = random.Random(f"{trial['seed']}:{stratum['stratum_key']}:{block_no}")
                arms = json.loads(trial["arms_json"])
                plan = []
                blocks = len(arms) if trial["block_size"] > len(arms) else 1
                for _ in range(blocks * (trial["block_size"] // len(arms))):
                    plan.extend(arms)
                rng.shuffle(plan)
                start = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM allocations WHERE stratum_id=?", (stratum["id"],)
                ).fetchone()[0]
                for offset, arm in enumerate(plan, 1):
                    conn.execute(
                        "INSERT INTO allocations(trial_id,stratum_id,sequence,block_no,arm) VALUES(?,?,?,?,?)",
                        (trial["id"], stratum["id"], start + offset, block_no, arm),
                    )
            free = conn.execute(
                "SELECT * FROM allocations WHERE stratum_id=? AND used_by IS NULL ORDER BY sequence LIMIT 1", (stratum["id"],)
            ).fetchone()
            if free:
                return free
        raise BusinessError("随机分配表已耗尽，请由统计人员扩展方案", 409, "allocation_exhausted")

    def enroll(self, user_id, trial_id, external_id, factors):
        external_id = str(external_id).strip()
        if not external_id:
            raise BusinessError("外部受试者编号不能为空", 422, "invalid_external_id")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                trial = self._trial(conn, trial_id)
                if trial["status"] != "running":
                    raise BusinessError("试验尚未开始或已经停止", 409, "trial_not_running")
                if trial["enrollment_held"]:
                    review = self._reviewing_application(conn, trial_id)
                    note = {"application_id": self.application_number(review["id"])} if review else None
                    raise BusinessError(
                        "方案版本审核期间暂停新入组，请等待审核结束", 409, "enrollment_paused", note
                    )
                existing = conn.execute(
                    "SELECT * FROM participants WHERE trial_id=? AND external_id=?", (trial_id, external_id)
                ).fetchone()
                if existing:
                    if existing["site_id"] != actor["site_id"]:
                        raise BusinessError("不能在当前中心查看其他中心的受试者", 403, "site_isolation")
                    conn.commit()
                    return self._blinded_participant(conn, existing, actor, allow_arm=False, idempotent=True)
                stratum = self._stratum(conn, trial, factors, actor["site_id"])
                allocation = self._next_allocation(conn, trial, stratum)
                allocation_code = hashlib.sha256(f"{trial_id}:{external_id}".encode()).hexdigest()[:12].upper()
                cur = conn.execute(
                    """INSERT INTO participants(trial_id,site_id,external_id,stratum_id,allocation_id,allocation_code,enrolled_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (trial_id, actor["site_id"], external_id, stratum["id"], allocation["id"], allocation_code, user_id, now()),
                )
                participant_id = cur.lastrowid
                conn.execute("UPDATE allocations SET used_by=?,used_at=? WHERE id=?", (participant_id, now(), allocation["id"]))
                self._audit(conn, trial_id, user_id, "participant.enroll", {"participant_id": participant_id, "external_id": external_id, "allocation_id": allocation["id"], "site_id": actor["site_id"]})
                participant = conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
                return self._blinded_participant(conn, participant, actor, allow_arm=False, idempotent=False)
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if "participants.trial_id, participants.external_id" in str(exc):
                    with self.connect() as retry:
                        row = retry.execute("SELECT * FROM participants WHERE trial_id=? AND external_id=?", (trial_id, external_id)).fetchone()
                        if row and row["site_id"] == actor["site_id"]:
                            return self._blinded_participant(retry, row, actor, False, True)
                raise BusinessError("并发入组冲突，请重新提交", 409, "enrollment_conflict")
            except Exception:
                conn.rollback()
                raise

    def _blinded_participant(self, conn, participant, viewer, allow_arm=False, idempotent=False):
        result = {
            "id": participant["id"], "trial_id": participant["trial_id"],
            "external_id": participant["external_id"], "site_id": participant["site_id"],
            "allocation_code": participant["allocation_code"], "status": participant["status"],
            "created_at": participant["created_at"], "idempotent": idempotent,
        }
        if allow_arm:
            result["arm"] = conn.execute("SELECT arm FROM allocations WHERE id=?", (participant["allocation_id"],)).fetchone()["arm"]
        return result

    def list_participants(self, user_id, trial_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            self._trial(conn, trial_id)
            if actor["role"] == "site":
                rows = conn.execute("SELECT * FROM participants WHERE trial_id=? AND site_id=? ORDER BY id", (trial_id, actor["site_id"])).fetchall()
            else:
                rows = conn.execute("SELECT * FROM participants WHERE trial_id=? ORDER BY id", (trial_id,)).fetchall()
            return [self._blinded_participant(conn, row, actor) for row in rows]

    def get_participant(self, user_id, participant_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            row = conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
            if not row:
                raise BusinessError("受试者不存在", 404, "not_found")
            if actor["role"] == "site" and row["site_id"] != actor["site_id"]:
                raise BusinessError("只能查看本中心受试者", 403, "site_isolation")
            approved = conn.execute(
                "SELECT 1 FROM unblinding_requests WHERE participant_id=? AND status='approved'", (participant_id,)
            ).fetchone() is not None
            return self._blinded_participant(conn, row, actor, allow_arm=approved)

    def request_unblinding(self, user_id, participant_id, reason):
        if len(reason.strip()) < 8:
            raise BusinessError("揭盲原因至少 8 字", 422, "reason_required")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            participant = conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
            if not participant:
                raise BusinessError("受试者不存在", 404, "not_found")
            if actor["role"] == "site" and participant["site_id"] != actor["site_id"]:
                raise BusinessError("不能申请其他中心的揭盲", 403, "site_isolation")
            open_request = conn.execute(
                "SELECT id,status FROM unblinding_requests WHERE participant_id=? AND status='pending'",
                (participant_id,),
            ).fetchone()
            if open_request:
                raise BusinessError("该受试者已有待审批的揭盲申请", 409, "request_exists")
            # 有未完成的版本申请（审核中）时，揭盲停在“待补材料”，并展示版本申请编号
            review = self._reviewing_application(conn, participant["trial_id"])
            blocking_id = review["id"] if review else None
            cur = conn.execute(
                "INSERT INTO unblinding_requests(participant_id,requester_id,reason,blocking_application_id,created_at) VALUES(?,?,?,?,?)",
                (participant_id, user_id, reason.strip(), blocking_id, now()),
            )
            request_id = cur.lastrowid
            detail = {"request_id": request_id, "participant_id": participant_id}
            if blocking_id:
                detail["blocking_application"] = self.application_number(blocking_id)
                detail["state"] = "awaiting_materials"
            self._audit(conn, participant["trial_id"], user_id, "unblinding.request", detail)
            return self._unblinding_payload(conn,
                    conn.execute("SELECT * FROM unblinding_requests WHERE id=?", (request_id,)).fetchone())

    def supplement_unblinding(self, user_id, request_id, materials_note):
        materials_note = str(materials_note).strip()
        if len(materials_note) < 4:
            raise BusinessError("请填写补充材料说明（至少 4 字）", 422, "materials_note_required")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            request = conn.execute("SELECT * FROM unblinding_requests WHERE id=?", (request_id,)).fetchone()
            if not request:
                raise BusinessError("揭盲申请不存在", 404, "not_found")
            if request["blocking_application_id"] is None:
                raise BusinessError("该揭盲申请不处于待补材料状态", 409, "not_awaiting_materials")
            if actor["role"] == "site":
                participant = conn.execute("SELECT * FROM participants WHERE id=?", (request["participant_id"],)).fetchone()
                if participant["site_id"] != actor["site_id"]:
                    raise BusinessError("不能补交其他中心揭盲申请的材料", 403, "site_isolation")
            review = self._reviewing_application(
                conn,
                conn.execute("SELECT trial_id FROM participants WHERE id=?", (request["participant_id"],)).fetchone()["trial_id"],
            )
            if review:
                raise BusinessError(
                    f"版本申请 {self.application_number(review['id'])} 尚未完成，材料仍无法提交",
                    409, "application_in_review",
                    {"application_id": self.application_number(review["id"])},
                )
            conn.execute(
                "UPDATE unblinding_requests SET blocking_application_id=NULL,materials_note=? WHERE id=?",
                (materials_note, request_id),
            )
            participant = conn.execute("SELECT * FROM participants WHERE id=?", (request["participant_id"],)).fetchone()
            self._audit(conn, participant["trial_id"], user_id, "unblinding.supplement", {
                "request_id": request_id, "materials_note": materials_note,
            })
            return self._unblinding_payload(conn,
                    conn.execute("SELECT * FROM unblinding_requests WHERE id=?", (request_id,)).fetchone())

    def list_unblinding_requests(self, user_id, trial_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            self._trial(conn, trial_id)
            sql = (
                "SELECT ur.* FROM unblinding_requests ur JOIN participants p ON p.id=ur.participant_id "
                "WHERE p.trial_id=?"
            )
            params = [trial_id]
            if actor["role"] == "site":
                sql += " AND p.site_id=?"
                params.append(actor["site_id"])
            sql += " ORDER BY ur.id DESC"
            rows = conn.execute(sql, params).fetchall()
            return {"items": [
                self._unblinding_payload(conn, row, include_arm=row["status"] == "approved") for row in rows
            ]}

    def _unblinding_payload(self, conn, request, include_arm=False):
        participant = conn.execute(
            "SELECT external_id,site_id,trial_id FROM participants WHERE id=?", (request["participant_id"],)
        ).fetchone()
        blocking_number = None
        if request["blocking_application_id"] is not None:
            blocking_number = self.application_number(request["blocking_application_id"])
        payload = {
            "id": request["id"], "participant_id": request["participant_id"],
            "external_id": participant["external_id"], "site_id": participant["site_id"],
            "trial_id": participant["trial_id"], "requester_id": request["requester_id"],
            "reason": request["reason"],
            "state": "awaiting_materials" if request["blocking_application_id"] is not None else request["status"],
            "status": request["status"],
            "blocking_application_id": blocking_number,
            "materials_note": request["materials_note"],
            "first_approver": request["first_approver"], "second_approver": request["second_approver"],
            "created_at": request["created_at"], "decided_at": request["decided_at"],
        }
        if include_arm:
            arm = conn.execute(
                "SELECT a.arm FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.id=?",
                (request["participant_id"],),
            ).fetchone()["arm"]
            payload["arm"] = arm
        return payload

    def approve_unblinding(self, user_id, request_id):
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                approver = self._user(conn, user_id, {"monitor", "coordinator"})
                request = conn.execute("SELECT * FROM unblinding_requests WHERE id=?", (request_id,)).fetchone()
                if not request:
                    raise BusinessError("揭盲申请不存在", 404, "not_found")
                if request["blocking_application_id"] is not None:
                    raise BusinessError(
                        f"请先补交材料：版本申请 {self.application_number(request['blocking_application_id'])} 未完成",
                        409, "awaiting_materials",
                        {"application_id": self.application_number(request["blocking_application_id"])},
                    )
                if request["status"] != "pending":
                    raise BusinessError("揭盲申请已经完成", 409, "already_decided")
                if request["requester_id"] == user_id:
                    raise BusinessError("揭盲审批人不能与申请人为同一人", 409, "requester_cannot_approve")
                if request["first_approver"] is None:
                    conn.execute("UPDATE unblinding_requests SET first_approver=? WHERE id=?", (user_id, request_id))
                    participant = conn.execute("SELECT * FROM participants WHERE id=?", (request["participant_id"],)).fetchone()
                    self._audit(conn, participant["trial_id"], user_id, "unblinding.approve.first", {"request_id": request_id})
                    return {"id": request_id, "status": "pending", "state": "pending", "first_approver": user_id, "second_approval_required": True}
                if request["first_approver"] == user_id:
                    raise BusinessError("两次揭盲审批必须由不同人员完成", 409, "distinct_approver_required")
                conn.execute(
                    "UPDATE unblinding_requests SET second_approver=?,status='approved',decided_at=? WHERE id=?",
                    (user_id, now(), request_id),
                )
                participant = conn.execute("SELECT * FROM participants WHERE id=?", (request["participant_id"],)).fetchone()
                arm = conn.execute("SELECT arm FROM allocations WHERE id=?", (participant["allocation_id"],)).fetchone()["arm"]
                self._audit(conn, participant["trial_id"], user_id, "unblinding.approve.second", {"request_id": request_id, "participant_id": participant["id"]})
                return {"id": request_id, "status": "approved", "state": "approved", "first_approver": request["first_approver"], "second_approver": user_id, "arm": arm}
            except Exception:
                conn.rollback()
                raise

    def trial_summary(self, user_id, trial_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            trial = self._trial(conn, trial_id)
            where, params = "", [trial_id]
            if actor["role"] == "site":
                where, params = " AND site_id=?", [trial_id, actor["site_id"]]
            total = conn.execute(f"SELECT COUNT(*) FROM participants WHERE trial_id=?" + where, params).fetchone()[0]
            by_site = conn.execute(
                f"SELECT site_id,COUNT(*) AS count FROM participants WHERE trial_id=?" + where + " GROUP BY site_id", params
            ).fetchall()
            audit = conn.execute("SELECT * FROM audit_log WHERE trial_id=? ORDER BY id", (trial_id,)).fetchall()
            reviewing = self._reviewing_application(conn, trial_id)
            applications = conn.execute(
                "SELECT * FROM version_applications WHERE trial_id=? ORDER BY id DESC", (trial_id,)
            ).fetchall()
            return {
                "trial": {
                    "id": trial["id"], "name": trial["name"],
                    "protocol_version": trial["protocol_version"], "status": trial["status"],
                    "enrollment_held": bool(trial["enrollment_held"]),
                    "effective_status": "reviewing" if trial["enrollment_held"] else trial["status"],
                    "arms": json.loads(trial["arms_json"]),
                    "strata_factors": json.loads(trial["strata_factors_json"]),
                    "block_size": trial["block_size"], "seed": trial["seed"],
                },
                "reviewing_application": (
                    {"id": reviewing["id"], "number": self.application_number(reviewing["id"]),
                     "protocol_version": reviewing["protocol_version"]} if reviewing else None
                ),
                "version_applications": [self._version_application_payload(conn, row) for row in applications],
                "participants_visible": total, "by_site": [dict(x) for x in by_site],
                "audit": [dict(x) | {"detail": json.loads(x["detail"])} for x in audit],
            }


class Handler(BaseHTTPRequestHandler):
    server_version = "Randomization/1.0"
    STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                    ".html": "text/html; charset=utf-8"}
    def _store(self): return self.server.store  # type: ignore[attr-defined]
    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try: data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError): raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict): raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data
    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _static(self, path):
        target = BASE_DIR / "web" / "index.html" if path == "/" else (BASE_DIR / "web" / path.lstrip("/")).resolve()
        web_root = (BASE_DIR / "web").resolve()
        if not str(target).startswith(str(web_root)) or not target.is_file():
            raise BusinessError("页面不存在", 404, "not_found")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", self.STATIC_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _dispatch(self, method):
        path = urlparse(self.path).path.rstrip("/") or "/"; parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", ""); store = self._store()
        if method == "GET" and (path == "/" or path.startswith("/static/")):
            return self._static(path)
        if method == "GET" and path == "/health": return self._send(200, {"ok": True})
        if parts == ["api", "trials"] and method == "POST":
            d=self._body(); return self._send(201, store.create_trial(user,d.get("name",""),d.get("protocol_version",""),d.get("arms"),d.get("strata_factors"),d.get("block_size"),d.get("seed","")))
        if len(parts) >= 3 and parts[:2] == ["api", "trials"]:
            trial_id=int(parts[2])
            if len(parts)==4 and parts[3]=="protocol" and method=="POST":
                d=self._body(); return self._send(200, store.update_protocol(user,trial_id,d.get("protocol_version",""),d.get("arms"),d.get("strata_factors"),d.get("block_size"),d.get("seed")))
            if len(parts)==4 and parts[3]=="start" and method=="POST": return self._send(200, store.start_trial(user,trial_id))
            if len(parts)==4 and parts[3]=="participants" and method=="GET": return self._send(200, {"items": store.list_participants(user,trial_id)})
            if len(parts)==4 and parts[3]=="enroll" and method=="POST":
                d=self._body(); return self._send(201, store.enroll(user,trial_id,d.get("external_id",""),d.get("factors",{})))
            if len(parts)==4 and parts[3]=="summary" and method=="GET": return self._send(200, store.trial_summary(user,trial_id))
            if len(parts)==4 and parts[3]=="version-applications" and method=="POST":
                d=self._body(); return self._send(201, store.submit_version_application(user,trial_id,d.get("protocol_version",""),d.get("change_reason",""),d.get("arms"),d.get("strata_factors"),d.get("block_size"),d.get("seed")))
            if len(parts)==4 and parts[3]=="version-applications" and method=="GET":
                return self._send(200, store.list_version_applications(user,trial_id))
            if len(parts)==4 and parts[3]=="unblinding-requests" and method=="GET":
                return self._send(200, store.list_unblinding_requests(user,trial_id))
        if len(parts)==3 and parts[:2]==["api","version-applications"] and method=="GET":
            return self._send(200, store.get_version_application(user,int(parts[2]),include_roster=True))
        if len(parts)==4 and parts[:2]==["api","version-applications"] and parts[3]=="review" and method=="POST":
            d=self._body(); return self._send(200, store.review_version_application(user,int(parts[2]),d.get("decision",""),d.get("review_note","")))
        if len(parts)==4 and parts[:2]==["api","version-applications"] and parts[3]=="cancel" and method=="POST":
            return self._send(200, store.cancel_version_application(user,int(parts[2])))
        if len(parts)==3 and parts[:2]==["api","participants"] and method=="GET": return self._send(200, store.get_participant(user,int(parts[2])))
        if len(parts)==4 and parts[:2]==["api","participants"] and parts[3]=="unblinding-requests" and method=="POST":
            d=self._body(); return self._send(201, store.request_unblinding(user,int(parts[2]),d.get("reason","")))
        if len(parts)==4 and parts[:2]==["api","unblinding-requests"] and parts[3]=="approve" and method=="POST":
            return self._send(200, store.approve_unblinding(user,int(parts[2])))
        if len(parts)==4 and parts[:2]==["api","unblinding-requests"] and parts[3]=="supplement" and method=="POST":
            d=self._body(); return self._send(200, store.supplement_unblinding(user,int(parts[2]),d.get("materials_note","")))
        raise BusinessError("接口不存在",404,"not_found")
    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc:
            payload={"error":{"code":exc.code,"message":exc.message}}
            if exc.details: payload["error"]["details"]=exc.details
            self._send(exc.status,payload)
        except (ValueError,TypeError): self._send(400,{"error":{"code":"invalid_path","message":"路径参数格式错误"}})
        except Exception as exc: self._send(500,{"error":{"code":"internal_error","message":str(exc)}})
    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class RandomizationServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store): self.store=store; super().__init__(address,Handler)


def main():
    parser=argparse.ArgumentParser(description="临床试验随机分配与盲法服务")
    parser.add_argument("--db",default=str(DEFAULT_DB)); parser.add_argument("--port",type=int,default=8104)
    parser.add_argument("--init",action="store_true"); parser.add_argument("--seed",action="store_true")
    args=parser.parse_args(); store=RandomizationStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server=RandomizationServer(("127.0.0.1",args.port),store); print(f"随机化服务运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=="__main__": main()
