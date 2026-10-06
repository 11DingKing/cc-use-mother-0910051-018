from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Tuple
from datetime import date, datetime

from app.crud.purchase import crud_purchase_order, crud_delivery, crud_inventory_batch
from app.crud.vehicle import crud_vehicle, crud_production_batch
from app.crud.commitment import (
    crud_commitment_version,
    crud_commitment_version_batch,
    crud_commitment_impact
)
from app.schemas import (
    CommitmentVersionPublish, CommitmentVersionDiff, CommitmentScheduleChange,
    CommitmentImpactBatchDiff, CommitmentImpactSnapshot
)
from app.models import CommitmentVersion, CommitmentVersionBatch, CommitmentImpact, PurchaseOrder

# 承诺版本状态
STATUS_PUBLISHED = "published"    # 当前生效
STATUS_SUPERSEDED = "superseded"  # 已被更新版本取代
STATUS_WITHDRAWN = "withdrawn"    # 已撤回

# 影响快照中参与对比的字段，复算幂等性以这些字段内容一致为准
IMPACT_COMPARE_FIELDS = (
    "required_quantity", "covered_quantity", "shortage_quantity",
    "available_date", "delay_days", "impact_level", "remark"
)


class CommitmentService:
    """供应商承诺版本服务：版本不可覆盖，发布/撤回触发缺口与生产批次重算"""

    # ---------- 版本生命周期 ----------

    @staticmethod
    def publish_version(
        db: Session,
        purchase_order_id: int,
        data: CommitmentVersionPublish
    ) -> CommitmentVersion:
        order = crud_purchase_order.get(db, purchase_order_id)
        if not order:
            raise ValueError(f"采购订单不存在: {purchase_order_id}")
        if order.status not in ("ordered", "partial"):
            raise ValueError(f"采购订单状态为 {order.status}，不允许发布承诺版本")

        delivered_now = CommitmentService._get_delivered_quantity(db, order.id)
        if data.committed_quantity < delivered_now:
            raise ValueError(
                f"已完成到货{delivered_now}件，承诺数量({data.committed_quantity})不能低于已到货数量"
            )
        adjustable = data.committed_quantity - delivered_now
        batches_total = sum(b.quantity for b in data.batches)
        if adjustable > 0 and not data.batches:
            raise ValueError(f"可调整数量{adjustable}件，必须提供分批到货计划")
        if batches_total != adjustable:
            raise ValueError(
                f"分批到货数量合计({batches_total})与可调整数量({adjustable})不一致，"
                f"已完成到货{delivered_now}件已从可调整范围中扣除"
            )

        committed_date = data.committed_delivery_date
        if committed_date is None and data.batches:
            committed_date = max(b.planned_date for b in data.batches)

        version_number = crud_commitment_version.get_max_version_number(db, order.id) + 1
        previous = crud_commitment_version.get_effective_by_order(db, order.id)
        if previous:
            previous.status = STATUS_SUPERSEDED
            db.add(previous)

        version = CommitmentVersion(
            version_no=f"CV-{order.order_no}-V{version_number:02d}",
            purchase_order_id=order.id,
            version_number=version_number,
            committed_quantity=data.committed_quantity,
            committed_delivery_date=committed_date,
            delivered_quantity=delivered_now,
            adjustable_quantity=adjustable,
            status=STATUS_PUBLISHED,
            signed_by=data.signed_by,
            signature_doc=data.signature_doc,
            signed_at=data.signed_at or datetime.now(),
            note=data.note,
            published_at=datetime.now()
        )
        db.add(version)
        db.flush()
        for batch_data in data.batches:
            db.add(CommitmentVersionBatch(
                version_id=version.id,
                batch_no=batch_data.batch_no,
                quantity=batch_data.quantity,
                planned_date=batch_data.planned_date,
                remark=batch_data.remark
            ))
        db.commit()

        CommitmentService._recalculate_version_impacts(db, version.id)
        return crud_commitment_version.get(db, version.id)

    @staticmethod
    def withdraw_version(
        db: Session,
        version_id: int,
        reason: Optional[str] = None
    ) -> CommitmentVersion:
        version = crud_commitment_version.get(db, version_id)
        if not version:
            raise ValueError(f"承诺版本不存在: {version_id}")
        if version.status != STATUS_PUBLISHED:
            raise ValueError(f"仅当前生效版本可撤回，当前状态: {version.status}")

        version.status = STATUS_WITHDRAWN
        version.withdrawn_at = datetime.now()
        version.withdraw_reason = reason
        db.add(version)
        db.commit()

        # 回落到最近一个被取代的版本，恢复其生效状态并重算影响
        previous = crud_commitment_version.get_latest_superseded(db, version.purchase_order_id)
        if previous:
            previous.status = STATUS_PUBLISHED
            db.add(previous)
            db.commit()
            CommitmentService._recalculate_version_impacts(db, previous.id)
        return crud_commitment_version.get(db, version_id)

    @staticmethod
    def recalculate_order(db: Session, purchase_order_id: int) -> List[CommitmentImpact]:
        order = crud_purchase_order.get(db, purchase_order_id)
        if not order:
            raise ValueError(f"采购订单不存在: {purchase_order_id}")
        version = crud_commitment_version.get_effective_by_order(db, purchase_order_id)
        if not version:
            raise ValueError(f"采购订单 {order.order_no} 当前无生效承诺版本")
        return CommitmentService._recalculate_version_impacts(db, version.id)

    @staticmethod
    def get_current_version(db: Session, purchase_order_id: int) -> Optional[CommitmentVersion]:
        return crud_commitment_version.get_effective_by_order(db, purchase_order_id)

    # ---------- 影响重算（确定性、幂等） ----------

    @staticmethod
    def _get_delivered_quantity(db: Session, purchase_order_id: int) -> int:
        deliveries = crud_delivery.get_by_purchase_order(db, purchase_order_id)
        return sum(d.quantity for d in deliveries)

    @staticmethod
    def _recalculate_version_impacts(db: Session, version_id: int) -> List[CommitmentImpact]:
        version = crud_commitment_version.get(db, version_id)
        if not version:
            raise ValueError(f"承诺版本不存在: {version_id}")
        order = crud_purchase_order.get(db, version.purchase_order_id)
        impacts = CommitmentService._simulate_material_gaps(db, order.material_id)
        CommitmentService._apply_impacts(db, version, impacts)
        return crud_commitment_impact.get_by_version(db, version_id)

    @staticmethod
    def _simulate_material_gaps(db: Session, material_id: int) -> List[dict]:
        """
        物料级缺口模拟，输出只依赖当前数据状态，与执行次数、采购单处理顺序无关：
        - 供应 = 当前可用库存 + 各采购单生效版本的剩余分批计划
          （版本发布后新完成的到货从分批计划中扣除，避免与库存重复计数）
          + 无承诺版本采购单的剩余订货量（按订单交期）
        - 需求 = 计划状态生产批次的 BOM 用量，按 (计划日期, -车型优先级, 批次ID) 固定顺序消耗
        """
        stock = crud_inventory_batch.get_total_stock(db, material_id)

        supplies: List[dict] = []
        orders = db.query(PurchaseOrder).filter(
            PurchaseOrder.material_id == material_id,
            PurchaseOrder.status.in_(["ordered", "partial"])
        ).order_by(PurchaseOrder.id).all()
        for order in orders:
            delivered = CommitmentService._get_delivered_quantity(db, order.id)
            version = crud_commitment_version.get_effective_by_order(db, order.id)
            if version:
                # 已完成到货从可调整范围中扣除：冲减发布后的分批计划
                arrived_since_publish = max(0, delivered - (version.delivered_quantity or 0))
                batches = sorted(
                    version.batches,
                    key=lambda b: (b.planned_date, b.batch_no, b.id)
                )
                for batch in batches:
                    qty = batch.quantity
                    if arrived_since_publish > 0:
                        take = min(qty, arrived_since_publish)
                        qty -= take
                        arrived_since_publish -= take
                    if qty > 0:
                        supplies.append({
                            "date": batch.planned_date,
                            "quantity": qty,
                            "order_id": order.id,
                            "batch_no": batch.batch_no
                        })
            else:
                remaining = max(0, order.quantity - delivered)
                if remaining > 0:
                    supplies.append({
                        "date": order.expected_date,
                        "quantity": remaining,
                        "order_id": order.id,
                        "batch_no": ""
                    })
        supplies.sort(key=lambda s: (s["date"], s["order_id"], s["batch_no"]))
        for supply in supplies:
            supply["remaining"] = supply["quantity"]

        model_bom_quantity: Dict[int, int] = {}
        for vm in crud_vehicle.get_active_models(db):
            bom_qty = crud_production_batch.get_bom_quantity(db, vm.id, material_id)
            if bom_qty:
                model_bom_quantity[vm.id] = bom_qty
        if not model_bom_quantity:
            return []

        demand_batches = [
            b for b in crud_production_batch.get_planned_batches_sorted(db)
            if b.vehicle_model_id in model_bom_quantity
        ]
        demand_batches.sort(key=lambda b: (
            b.plan_date,
            -(b.vehicle_model.priority if b.vehicle_model else 0),
            b.id
        ))

        impacts: List[dict] = []
        pool_stock = stock
        for pb in demand_batches:
            bom_qty = model_bom_quantity[pb.vehicle_model_id]
            required = bom_qty * pb.quantity
            available = pool_stock + sum(
                s["remaining"] for s in supplies if s["date"] <= pb.plan_date
            )
            covered = min(required, available)

            need = covered
            from_stock = min(pool_stock, need)
            pool_stock -= from_stock
            need -= from_stock
            for supply in supplies:
                if need <= 0:
                    break
                if supply["date"] <= pb.plan_date and supply["remaining"] > 0:
                    take = min(need, supply["remaining"])
                    supply["remaining"] -= take
                    need -= take

            shortage = required - covered
            if shortage <= 0:
                continue

            # 缺口发生后，pool_stock 与计划日前的在途均已耗尽，
            # 按剩余供应的累计量找最早齐套日期
            cumulative = 0
            available_date: Optional[date] = None
            for supply in supplies:
                if supply["remaining"] <= 0:
                    continue
                cumulative += supply["remaining"]
                if cumulative >= shortage:
                    available_date = max(supply["date"], pb.plan_date)
                    break
            delay_days = (available_date - pb.plan_date).days if available_date else None
            impact_level = CommitmentService._assess_impact_level(delay_days)

            if available_date:
                remark = (
                    f"批次{pb.batch_no}计划{pb.plan_date.isoformat()}需求{required}件，"
                    f"缺口{shortage}件，预计{available_date.isoformat()}齐套，延期{delay_days}天"
                )
            else:
                remark = (
                    f"批次{pb.batch_no}计划{pb.plan_date.isoformat()}需求{required}件，"
                    f"缺口{shortage}件，库存与在途承诺均无法覆盖"
                )
            impacts.append({
                "production_batch_id": pb.id,
                "vehicle_model_id": pb.vehicle_model_id,
                "material_id": material_id,
                "required_quantity": required,
                "covered_quantity": covered,
                "shortage_quantity": shortage,
                "available_date": available_date,
                "delay_days": delay_days,
                "impact_level": impact_level,
                "remark": remark
            })
        return impacts

    @staticmethod
    def _assess_impact_level(delay_days: Optional[int]) -> str:
        if delay_days is None:
            return "high"
        if delay_days >= 8:
            return "high"
        if delay_days >= 4:
            return "medium"
        return "low"

    @staticmethod
    def _apply_impacts(
        db: Session,
        version: CommitmentVersion,
        impacts: List[dict]
    ) -> None:
        """按 (version_id, production_batch_id) 幂等 upsert：重复复算不产生任何变更"""
        existing = crud_commitment_impact.get_by_version_map(db, version.id)
        for impact_data in impacts:
            pb_id = impact_data["production_batch_id"]
            row = existing.pop(pb_id, None)
            if row is None:
                db.add(CommitmentImpact(
                    version_id=version.id,
                    purchase_order_id=version.purchase_order_id,
                    **impact_data
                ))
                continue
            changed = False
            for field in IMPACT_COMPARE_FIELDS:
                if getattr(row, field) != impact_data[field]:
                    setattr(row, field, impact_data[field])
                    changed = True
            if changed:
                db.add(row)
        for stale in existing.values():
            db.delete(stale)
        db.commit()

    # ---------- 版本差异对比 ----------

    @staticmethod
    def diff_versions(
        db: Session,
        purchase_order_id: int,
        from_version_id: Optional[int] = None,
        to_version_id: Optional[int] = None
    ) -> CommitmentVersionDiff:
        order = crud_purchase_order.get(db, purchase_order_id)
        if not order:
            raise ValueError(f"采购订单不存在: {purchase_order_id}")

        if to_version_id:
            to_version = crud_commitment_version.get(db, to_version_id)
            if not to_version or to_version.purchase_order_id != purchase_order_id:
                raise ValueError(f"采购订单 {order.order_no} 不存在版本: {to_version_id}")
        else:
            to_version = crud_commitment_version.get_effective_by_order(db, purchase_order_id)
            if not to_version:
                raise ValueError(f"采购订单 {order.order_no} 当前无生效承诺版本")

        if from_version_id:
            from_version = crud_commitment_version.get(db, from_version_id)
            if not from_version or from_version.purchase_order_id != purchase_order_id:
                raise ValueError(f"采购订单 {order.order_no} 不存在版本: {from_version_id}")
        else:
            from_version = crud_commitment_version.get_previous_version(db, to_version)
            if not from_version:
                raise ValueError(f"版本 {to_version.version_no} 是首个版本，无旧版可比较")
        if from_version.version_number >= to_version.version_number:
            raise ValueError("旧版本号必须小于新版本号")

        schedule_changes = CommitmentService._diff_schedules(from_version, to_version)
        impact_diffs, old_count, new_count = CommitmentService._diff_impacts(
            db, from_version, to_version
        )

        date_shift = None
        if from_version.committed_delivery_date and to_version.committed_delivery_date:
            date_shift = (
                to_version.committed_delivery_date - from_version.committed_delivery_date
            ).days

        newly = sum(1 for d in impact_diffs if d.change_type == "newly_affected")
        resolved = sum(1 for d in impact_diffs if d.change_type == "resolved")
        worsened = sum(1 for d in impact_diffs if d.change_type == "worsened")
        improved = sum(1 for d in impact_diffs if d.change_type == "improved")
        qty_delta = to_version.committed_quantity - from_version.committed_quantity
        summary = (
            f"承诺数量{from_version.committed_quantity}→{to_version.committed_quantity}（{qty_delta:+d}），"
            f"受影响批次{old_count}→{new_count}：新增{newly}个、解除{resolved}个、"
            f"恶化{worsened}个、改善{improved}个"
        )

        return CommitmentVersionDiff(
            purchase_order_id=order.id,
            order_no=order.order_no,
            material_id=order.material_id,
            from_version_id=from_version.id,
            from_version_number=from_version.version_number,
            to_version_id=to_version.id,
            to_version_number=to_version.version_number,
            committed_quantity_delta=qty_delta,
            adjustable_quantity_delta=to_version.adjustable_quantity - from_version.adjustable_quantity,
            delivery_date_shift_days=date_shift,
            schedule_changes=schedule_changes,
            impact_diffs=impact_diffs,
            old_affected_batches=old_count,
            new_affected_batches=new_count,
            summary=summary
        )

    @staticmethod
    def _diff_schedules(
        from_version: CommitmentVersion,
        to_version: CommitmentVersion
    ) -> List[CommitmentScheduleChange]:
        def aggregate(version: CommitmentVersion) -> Dict[date, int]:
            result: Dict[date, int] = {}
            for batch in version.batches:
                result[batch.planned_date] = result.get(batch.planned_date, 0) + batch.quantity
            return result

        old_schedule = aggregate(from_version)
        new_schedule = aggregate(to_version)
        changes: List[CommitmentScheduleChange] = []
        for planned_date in sorted(set(old_schedule) | set(new_schedule)):
            old_qty = old_schedule.get(planned_date, 0)
            new_qty = new_schedule.get(planned_date, 0)
            if old_qty == new_qty:
                continue
            if old_qty == 0:
                change_type = "added"
            elif new_qty == 0:
                change_type = "removed"
            else:
                change_type = "changed"
            changes.append(CommitmentScheduleChange(
                planned_date=planned_date,
                old_quantity=old_qty,
                new_quantity=new_qty,
                delta=new_qty - old_qty,
                change_type=change_type
            ))
        return changes

    @staticmethod
    def _diff_impacts(
        db: Session,
        from_version: CommitmentVersion,
        to_version: CommitmentVersion
    ) -> Tuple[List[CommitmentImpactBatchDiff], int, int]:
        old_map = crud_commitment_impact.get_by_version_map(db, from_version.id)
        new_map = crud_commitment_impact.get_by_version_map(db, to_version.id)

        batch_ids = set(old_map) | set(new_map)
        batch_meta: Dict[int, tuple] = {}
        for pb_id in batch_ids:
            pb = crud_production_batch.get(db, pb_id)
            batch_meta[pb_id] = (
                pb.plan_date if pb else date.min,
                pb.batch_no if pb else "",
                pb.vehicle_model_id if pb else 0
            )

        def to_snapshot(imp: CommitmentImpact) -> CommitmentImpactSnapshot:
            return CommitmentImpactSnapshot(
                required_quantity=imp.required_quantity,
                covered_quantity=imp.covered_quantity,
                shortage_quantity=imp.shortage_quantity,
                available_date=imp.available_date,
                delay_days=imp.delay_days,
                impact_level=imp.impact_level
            )

        diffs: List[CommitmentImpactBatchDiff] = []
        for pb_id in sorted(batch_ids, key=lambda i: (batch_meta[i][0], i)):
            old_imp = old_map.get(pb_id)
            new_imp = new_map.get(pb_id)
            plan_date, batch_no, vm_id = batch_meta[pb_id]
            if old_imp is None:
                change_type = "newly_affected"
                shortage_delta = new_imp.shortage_quantity
                date_shift = None
            elif new_imp is None:
                change_type = "resolved"
                shortage_delta = -old_imp.shortage_quantity
                date_shift = None
            else:
                shortage_delta = new_imp.shortage_quantity - old_imp.shortage_quantity
                date_shift = None
                if old_imp.available_date and new_imp.available_date:
                    date_shift = (new_imp.available_date - old_imp.available_date).days
                if shortage_delta > 0:
                    change_type = "worsened"
                elif shortage_delta < 0:
                    change_type = "improved"
                else:
                    # 缺口相同则比较齐套日期，无法齐套(None)视为最晚
                    old_date = old_imp.available_date or date.max
                    new_date = new_imp.available_date or date.max
                    if new_date > old_date:
                        change_type = "worsened"
                    elif new_date < old_date:
                        change_type = "improved"
                    else:
                        change_type = "unchanged"
            diffs.append(CommitmentImpactBatchDiff(
                production_batch_id=pb_id,
                production_batch_no=batch_no,
                vehicle_model_id=vm_id,
                plan_date=plan_date if plan_date != date.min else None,
                old_impact=to_snapshot(old_imp) if old_imp else None,
                new_impact=to_snapshot(new_imp) if new_imp else None,
                shortage_delta=shortage_delta,
                available_date_shift_days=date_shift,
                change_type=change_type
            ))
        return diffs, len(old_map), len(new_map)
