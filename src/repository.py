import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError, ValidationError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS location_occupancy (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sample_id TEXT NOT NULL,
                    freezer TEXT NOT NULL,
                    position TEXT NOT NULL,
                    occupied_at TEXT NOT NULL,
                    released_at TEXT
                );
                /* 一个在库库位同一时刻只能有一个占用者：抢同一格时数据库只放行一个 */
                CREATE UNIQUE INDEX IF NOT EXISTS idx_location_active_slot
                    ON location_occupancy(freezer, position)
                    WHERE released_at IS NULL;
                /* 一个样本同一时刻只能有一条在占记录 */
                CREATE UNIQUE INDEX IF NOT EXISTS idx_location_active_sample
                    ON location_occupancy(sample_id)
                    WHERE released_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_location_sample_history
                    ON location_occupancy(sample_id, id);
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _occupancy_from_row(row):
        return {
            "id": row["id"],
            "sample_id": row["sample_id"],
            "freezer": row["freezer"],
            "position": row["position"],
            "occupied_at": row["occupied_at"],
            "released_at": row["released_at"],
            "active": row["released_at"] is None,
        }

    @contextmanager
    def transaction(self):
        """跨表原子操作：实体更新、库位释放/占用、审计在同一事务内提交或回滚。"""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            return self._get_entity_conn(connection, entity_id)

    def _get_entity_conn(self, connection, entity_id):
        row = connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        with self.transaction() as connection:
            return self.update_entity_conn(
                connection, entity_id, expected_version, status, data
            )

    def update_entity_conn(self, connection, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        row = connection.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        current_version = int(row["version"])
        if expected_version is not None and current_version != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, current_version)
            )
        connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (status, payload, now, entity_id, current_version),
        )
        return self._get_entity_conn(connection, entity_id)

    # ---- 库位占用（落盘层）-------------------------------------------------

    def _active_slot_conn(self, connection, freezer, position):
        row = connection.execute(
            "SELECT * FROM location_occupancy "
            "WHERE freezer = ? AND position = ? AND released_at IS NULL",
            (freezer, position),
        ).fetchone()
        return self._occupancy_from_row(row) if row else None

    def _active_sample_conn(self, connection, sample_id):
        row = connection.execute(
            "SELECT * FROM location_occupancy "
            "WHERE sample_id = ? AND released_at IS NULL",
            (sample_id,),
        ).fetchone()
        return self._occupancy_from_row(row) if row else None

    def occupy_location_conn(self, connection, sample_id, freezer, position):
        """占用空库位；已被他人占用时抛出冲突，调用方事务整体回滚。"""
        holder = self._active_slot_conn(connection, freezer, position)
        if holder and holder["sample_id"] != sample_id:
            raise ConflictError(
                "库位 %s/%s 已被在库样本 %s 占用"
                % (freezer, position, holder["sample_id"])
            )
        now = utcnow()
        try:
            cursor = connection.execute(
                "INSERT INTO location_occupancy"
                "(sample_id, freezer, position, occupied_at, released_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (sample_id, freezer, position, now),
            )
        except sqlite3.IntegrityError:
            # 并发下唯一索引兜底：两个请求抢同一格时只有一个能进来
            raise ConflictError(
                "库位 %s/%s 刚被其他样本占用，请刷新后重试" % (freezer, position)
            )
        row = connection.execute(
            "SELECT * FROM location_occupancy WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return self._occupancy_from_row(row)

    def release_location_conn(self, connection, sample_id):
        """释放样本当前库位，没有在占记录时返回 None。"""
        current = self._active_sample_conn(connection, sample_id)
        if not current:
            return None
        now = utcnow()
        connection.execute(
            "UPDATE location_occupancy SET released_at = ? WHERE id = ?",
            (now, current["id"]),
        )
        current["released_at"] = now
        current["active"] = False
        return current

    def relocate_location_conn(self, connection, sample_id, freezer, position):
        """旧位释放与新位占用在同一事务内完成，返回前后位置与时间。"""
        old = self._active_sample_conn(connection, sample_id)
        if not old:
            raise ConflictError("样本 %s 没有在库库位，无法移库" % sample_id)
        if old["freezer"] == freezer and old["position"] == position:
            raise ValidationError("target position is the same as current position")
        holder = self._active_slot_conn(connection, freezer, position)
        if holder and holder["sample_id"] != sample_id:
            raise ConflictError(
                "目标位置 %s/%s 已被在库样本 %s 占用；样本 %s 保留在原位置 %s/%s"
                % (
                    freezer,
                    position,
                    holder["sample_id"],
                    sample_id,
                    old["freezer"],
                    old["position"],
                )
            )
        now = utcnow()
        connection.execute(
            "UPDATE location_occupancy SET released_at = ? WHERE id = ?",
            (now, old["id"]),
        )
        try:
            cursor = connection.execute(
                "INSERT INTO location_occupancy"
                "(sample_id, freezer, position, occupied_at, released_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (sample_id, freezer, position, now),
            )
        except sqlite3.IntegrityError:
            # 与并发请求抢同一格落败：随事务回滚，旧位释放一并撤销，原位置保持
            raise ConflictError(
                "目标位置 %s/%s 刚被其他样本占用，样本 %s 保留在原位置 %s/%s"
                % (freezer, position, sample_id, old["freezer"], old["position"])
            )
        new_row = connection.execute(
            "SELECT * FROM location_occupancy WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        new = self._occupancy_from_row(new_row)
        return {
            "from": {
                "freezer": old["freezer"],
                "position": old["position"],
                "occupied_at": old["occupied_at"],
                "released_at": now,
            },
            "to": {
                "freezer": new["freezer"],
                "position": new["position"],
                "occupied_at": new["occupied_at"],
            },
        }

    def get_active_location(self, sample_id):
        with self._connect() as connection:
            return self._active_sample_conn(connection, sample_id)

    def get_slot_occupant(self, freezer, position):
        with self._connect() as connection:
            return self._active_slot_conn(connection, freezer, position)

    def list_active_locations(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM location_occupancy WHERE released_at IS NULL "
                "ORDER BY freezer, position, id"
            ).fetchall()
        return [self._occupancy_from_row(row) for row in rows]

    def list_location_history(self, sample_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM location_occupancy WHERE sample_id = ? ORDER BY id",
                (sample_id,),
            ).fetchall()
        return [self._occupancy_from_row(row) for row in rows]

    # ---- 审计 -------------------------------------------------------------

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            self.append_audit_conn(
                connection,
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                detail,
            )

    def append_audit_conn(self, connection, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
