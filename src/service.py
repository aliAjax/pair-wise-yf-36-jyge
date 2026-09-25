from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        kind = self.rules.normalize_kind(kind)
        if kind == "location":
            # 规则层只问“这个格位有没有在库样本”，落盘细节不暴露给规则层
            if field == "slot" and isinstance(value, tuple) and len(value) == 2:
                occupant = self.repository.get_slot_occupant(value[0], value[1])
                if occupant:
                    # 确认占用样本仍处于在库状态，避免脏占用行挡住合法入库
                    sample = self.repository.get_entity(occupant["sample_id"])
                    if sample and sample["status"] == "stored":
                        return [occupant]
            return []
        return self.repository.find_entities(kind, field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)

        kind = self.rules.normalize_kind(entity["kind"])
        effect = self.rules.location_effect(kind, action)
        if effect is None:
            updated = self.repository.update_entity(entity_id, expected, next_status, merged)
            self.audit.record(
                entity_id, actor, action, entity["status"], updated["status"], {"patch": patch}
            )
            return updated

        # 库位相关动作：实体版本、旧位释放、新位占用、审计一起提交，一起回滚
        with self.repository.transaction() as connection:
            updated = self.repository.update_entity_conn(
                connection, entity_id, expected, next_status, merged
            )
            detail = {"patch": patch}
            if effect == "occupy":
                occupancy = self.repository.occupy_location_conn(
                    connection,
                    entity_id,
                    patch["freezer"],
                    patch["position"],
                )
                detail["location"] = {"to": self._location_point(occupancy)}
            elif effect == "relocate":
                move = self.repository.relocate_location_conn(
                    connection,
                    entity_id,
                    patch["freezer"],
                    patch["position"],
                )
                detail["location"] = move
            elif effect == "release":
                released = self.repository.release_location_conn(connection, entity_id)
                if released:
                    detail["location"] = {"from": self._location_point(released, released["released_at"])}
            self.repository.append_audit_conn(
                connection,
                entity_id,
                actor.user_id,
                actor.role,
                action,
                entity["status"],
                updated["status"],
                detail,
            )
        return updated

    @staticmethod
    def _location_point(occupancy, released_at=None):
        point = {
            "freezer": occupancy["freezer"],
            "position": occupancy["position"],
            "occupied_at": occupancy["occupied_at"],
        }
        if released_at:
            point["released_at"] = released_at
        return point

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    # ---- 库位视图（接口/页面读取）-----------------------------------------

    def list_locations(self):
        """当前库位：每个在占格位附样本当前信息。"""
        items = []
        for occupancy in self.repository.list_active_locations():
            sample = self.repository.get_entity(occupancy["sample_id"])
            items.append(
                {
                    "freezer": occupancy["freezer"],
                    "position": occupancy["position"],
                    "sample_id": occupancy["sample_id"],
                    "sample_code": sample["data"].get("sample_code") if sample else None,
                    "sample_status": sample["status"] if sample else None,
                    "occupied_at": occupancy["occupied_at"],
                }
            )
        return items

    def location_history(self, sample_id):
        """历次变化：占用、释放时间与前后位置都留在记录里。"""
        if not self.repository.get_entity(sample_id):
            raise NotFoundError("entity not found: " + sample_id)
        return self.repository.list_location_history(sample_id)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
