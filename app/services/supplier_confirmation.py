from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Tuple
from datetime import date, datetime, timedelta
from app.crud.supplier_confirmation import (
    crud_supplier_confirmation,
    crud_supplier_confirmation_batch,
    crud_supplier_commitment_version,
    crud_supplier_commitment_version_batch,
    crud_supplier_shortage_impact
)
from app.crud.purchase import crud_purchase_suggestion, crud_inventory_batch, crud_purchase_order
from app.crud.vehicle import crud_vehicle, crud_production_batch
from app.crud.supplier import crud_supplier, crud_supply_capacity
from app.crud.material import crud_material
from app.schemas import (
    SupplierConfirmationCreate, SupplierConfirmationConfirm,
    SupplierConfirmationBatchCreate, SupplierShortageImpactCreate,
    SupplierConfirmationStatistics, SupplierBottleneckAnalysis,
    SupplierBottleneckBatch, Material,
    SupplierCommitmentVersionDiff, SupplierCommitmentBatchScheduleChange,
    SupplierCommitmentImpactDiff
)
from app.models import (
    SupplierConfirmation, SupplierShortageImpact,
    SupplierCommitmentVersion, SupplierCommitmentVersionBatch,
    SupplierConfirmationBatch
)

class SupplierConfirmationService:

    @staticmethod
    def create_confirmation_from_suggestion(
        db: Session,
        suggestion_id: int,
        confirmation_no: str,
        supplier_id: Optional[int] = None
    ) -> SupplierConfirmation:
        suggestion = crud_purchase_suggestion.get(db, suggestion_id)
        if not suggestion:
            raise ValueError(f"采购建议不存在: {suggestion_id}")
        existing = crud_supplier_confirmation.get_by_purchase_suggestion(db, suggestion_id)
        if existing:
            raise ValueError(f"该采购建议已存在供应商确认: {suggestion_id}")
        final_supplier_id = supplier_id or suggestion.suggested_supplier_id
        if not final_supplier_id:
            raise ValueError("必须指定供应商")
        confirmation_in = SupplierConfirmationCreate(
            confirmation_no=confirmation_no,
            purchase_suggestion_id=suggestion_id,
            supplier_id=final_supplier_id,
            material_id=suggestion.material_id,
            requested_quantity=suggestion.suggested_quantity,
            committed_quantity=0,
            committed_delivery_date=suggestion.expected_delivery_date,
            shortage_quantity=0,
            status="pending",
            confirmation_note=None,
            batches=[]
        )
        confirmation = crud_supplier_confirmation.create_with_batches(db, obj_in=confirmation_in)
        crud_purchase_suggestion.update(db, db_obj=suggestion, obj_in={"status": "sent_to_supplier"})
        return confirmation

    @staticmethod
    def supplier_confirm(
        db: Session,
        confirmation_id: int,
        confirm_data: SupplierConfirmationConfirm
    ) -> SupplierConfirmation:
        """供应商确认：每次确认生成一个不可覆盖的承诺版本，并重新计算短缺影响"""
        confirmation = crud_supplier_confirmation.get(db, confirmation_id)
        if not confirmation:
            raise ValueError(f"供应商确认不存在: {confirmation_id}")
        if confirmation.status not in ("pending", "revised", "confirmed", "shortage"):
            raise ValueError(f"当前状态不允许确认: {confirmation.status}")
        committed_qty = confirm_data.committed_quantity
        if committed_qty < 0:
            raise ValueError("承诺数量不能为负数")
        # 已完成到货从可调整范围中扣除：新承诺数量不得低于已到货量
        delivered_qty = SupplierConfirmationService._get_delivered_quantity(db, confirmation)
        if committed_qty < delivered_qty:
            raise ValueError(f"已完成到货{delivered_qty}件不可调整，承诺数量不能低于已到货量")
        shortage_qty = max(0, confirmation.requested_quantity - committed_qty)
        committed_date = confirm_data.committed_delivery_date
        status = "confirmed"
        if shortage_qty > 0:
            status = "shortage"
        batches_total = sum(b.quantity for b in confirm_data.batches) if confirm_data.batches else 0
        if confirm_data.batches and batches_total != committed_qty:
            raise ValueError(f"分批到货数量合计({batches_total})与承诺数量({committed_qty})不一致")

        # 旧生效版本标记为已被取代，历史版本内容保持不可覆盖
        active_version = crud_supplier_commitment_version.get_active_by_confirmation(db, confirmation_id)
        if active_version:
            active_version.status = "superseded"
            db.add(active_version)

        # 生成新版本：包含分批到货计划与供应商签署依据
        version = SupplierCommitmentVersion(
            confirmation_id=confirmation_id,
            version_no=crud_supplier_commitment_version.get_next_version_no(db, confirmation_id),
            committed_quantity=committed_qty,
            committed_delivery_date=committed_date,
            shortage_quantity=shortage_qty,
            status="active",
            confirmation_note=confirm_data.confirmation_note,
            signed_by=confirm_data.signed_by,
            signature_basis=confirm_data.signature_basis,
            signed_at=datetime.now(),
            locked_delivered_quantity=delivered_qty
        )
        db.add(version)
        db.flush()
        for batch_data in confirm_data.batches:
            db.add(SupplierCommitmentVersionBatch(
                version_id=version.id,
                batch_no=batch_data.batch_no,
                quantity=batch_data.quantity,
                planned_date=batch_data.planned_date,
                remark=batch_data.remark
            ))

        # 同步旧分批表为当前生效版本的镜像，保持既有查询行为
        SupplierConfirmationService._mirror_batches_from_data(db, confirmation_id, confirm_data.batches)

        crud_supplier_confirmation.update(
            db,
            db_obj=confirmation,
            obj_in={
                "committed_quantity": committed_qty,
                "committed_delivery_date": committed_date,
                "shortage_quantity": shortage_qty,
                "status": status,
                "confirmation_note": confirm_data.confirmation_note,
                "confirmed_at": datetime.now(),
                "current_version_id": version.id
            }
        )
        SupplierConfirmationService._recalculate_shortage_impact(db, confirmation_id)
        SupplierConfirmationService._sync_delay_impact_with_supplier(db, confirmation_id)
        return crud_supplier_confirmation.get(db, confirmation_id)

    @staticmethod
    def withdraw_version(
        db: Session,
        confirmation_id: int,
        version_no: Optional[int] = None,
        reason: Optional[str] = None
    ) -> SupplierConfirmation:
        """撤回承诺版本：撤回生效版本时回滚到上一可用版本（无则回到待确认），并重新计算影响"""
        confirmation = crud_supplier_confirmation.get(db, confirmation_id)
        if not confirmation:
            raise ValueError(f"供应商确认不存在: {confirmation_id}")
        if version_no is not None:
            version = crud_supplier_commitment_version.get_by_version_no(db, confirmation_id, version_no)
            if not version:
                raise ValueError(f"承诺版本不存在: v{version_no}")
        else:
            version = crud_supplier_commitment_version.get_active_by_confirmation(db, confirmation_id)
            if not version:
                raise ValueError("当前没有生效中的承诺版本可撤回")
        if version.status == "withdrawn":
            raise ValueError(f"版本v{version.version_no}已撤回，不可重复撤回")

        was_active = version.status == "active"
        version.status = "withdrawn"
        version.withdraw_reason = reason
        db.add(version)

        if was_active:
            candidates = [
                v for v in crud_supplier_commitment_version.get_by_confirmation(db, confirmation_id)
                if v.status == "superseded"
            ]
            rollback = max(candidates, key=lambda v: v.version_no) if candidates else None
            if rollback:
                rollback.status = "active"
                db.add(rollback)
                confirmation.committed_quantity = rollback.committed_quantity
                confirmation.committed_delivery_date = rollback.committed_delivery_date
                confirmation.shortage_quantity = rollback.shortage_quantity
                confirmation.status = "shortage" if rollback.shortage_quantity > 0 else "confirmed"
                confirmation.confirmation_note = rollback.confirmation_note
                confirmation.current_version_id = rollback.id
                SupplierConfirmationService._mirror_batches_from_version(db, confirmation_id, rollback)
            else:
                confirmation.committed_quantity = 0
                confirmation.committed_delivery_date = None
                confirmation.shortage_quantity = 0
                confirmation.status = "pending"
                confirmation.current_version_id = None
                SupplierConfirmationService._mirror_batches_from_data(db, confirmation_id, [])
            db.add(confirmation)
        db.commit()
        SupplierConfirmationService._recalculate_shortage_impact(db, confirmation_id)
        return crud_supplier_confirmation.get(db, confirmation_id)

    @staticmethod
    def _get_delivered_quantity(db: Session, confirmation: SupplierConfirmation) -> int:
        """已完成到货量：同一采购建议转出的采购单累计到货，该部分不可再调整"""
        from app.models import Delivery
        orders = crud_purchase_order.get_by_purchase_suggestion(db, confirmation.purchase_suggestion_id)
        if not orders:
            return 0
        order_ids = [o.id for o in orders]
        deliveries = db.query(Delivery).filter(Delivery.purchase_order_id.in_(order_ids)).all()
        return sum(d.quantity for d in deliveries)

    @staticmethod
    def _mirror_batches_from_data(db: Session, confirmation_id: int, batches_data) -> None:
        """将旧分批表刷新为给定分批数据的镜像"""
        existing_batches = crud_supplier_confirmation_batch.get_by_confirmation(db, confirmation_id)
        for eb in existing_batches:
            db.delete(eb)
        db.flush()
        for batch_data in batches_data:
            db.add(SupplierConfirmationBatch(
                confirmation_id=confirmation_id,
                batch_no=batch_data.batch_no,
                quantity=batch_data.quantity,
                planned_date=batch_data.planned_date,
                remark=batch_data.remark
            ))
        db.flush()
        # 使父对象已加载的分批集合失效，避免后续级联访问到已删除的镜像实例
        confirmation = db.get(SupplierConfirmation, confirmation_id)
        if confirmation is not None:
            db.expire(confirmation, ["batches"])

    @staticmethod
    def _mirror_batches_from_version(db: Session, confirmation_id: int, version: SupplierCommitmentVersion) -> None:
        """将旧分批表刷新为指定承诺版本分批计划的镜像"""
        version_batches = crud_supplier_commitment_version_batch.get_by_version(db, version.id)
        SupplierConfirmationService._mirror_batches_from_data(db, confirmation_id, version_batches)

    @staticmethod
    def _recalculate_shortage_impact(db: Session, confirmation_id: int) -> List[SupplierShortageImpact]:
        """重新计算短缺影响。

        计算结果是输入数据的纯函数，重复执行得到稳定结果：
        - 与当前生效记录内容一致时不做任何变更（记录id保持稳定）；
        - 版本切换时，旧版本记录冻结为历史快照（可追溯到触发版本）；
        - 回滚场景下当前版本已有的历史快照内容一致则直接恢复生效。
        """
        confirmation = crud_supplier_confirmation.get(db, confirmation_id)
        if not confirmation:
            return []
        computed: List[dict] = []
        if confirmation.shortage_quantity > 0:
            computed = SupplierConfirmationService._compute_shortage_impacts(db, confirmation)
        current_version_id = confirmation.current_version_id
        existing_current = crud_supplier_shortage_impact.get_by_confirmation(db, confirmation_id)

        def sig_of_dict(d: dict) -> Tuple:
            return (d["production_batch_id"], d["shortage_material_id"], d["shortage_quantity"],
                    d["impact_level"], d["estimated_delay_days"])

        def sig_of_obj(o: SupplierShortageImpact) -> Tuple:
            return (o.production_batch_id, o.shortage_material_id, o.shortage_quantity,
                    o.impact_level, o.estimated_delay_days)

        computed_sigs = sorted(sig_of_dict(c) for c in computed)
        existing_sigs = sorted(sig_of_obj(e) for e in existing_current)
        same_version = all(e.version_id == current_version_id for e in existing_current)
        if computed_sigs == existing_sigs and same_version:
            return existing_current

        # 同版本重算：替换旧记录；跨版本切换：旧记录冻结为历史快照
        for imp in existing_current:
            if imp.version_id == current_version_id:
                db.delete(imp)
            else:
                imp.calc_status = "superseded"
                db.add(imp)
        db.flush()

        # 当前版本已有内容一致的历史快照则恢复生效，保证回滚结果稳定
        reusable: List[SupplierShortageImpact] = []
        if current_version_id is not None:
            reusable = [
                i for i in crud_supplier_shortage_impact.get_by_version(db, current_version_id)
                if i.calc_status == "superseded"
            ]
        impacts: List[SupplierShortageImpact] = []
        remaining = list(computed)
        for comp in list(remaining):
            sig = sig_of_dict(comp)
            match = next((r for r in reusable if sig_of_obj(r) == sig), None)
            if match:
                match.calc_status = "current"
                db.add(match)
                reusable.remove(match)
                impacts.append(match)
                remaining.remove(comp)
        for comp in remaining:
            impact = SupplierShortageImpact(
                confirmation_id=confirmation_id,
                version_id=current_version_id,
                calc_status="current",
                **comp
            )
            db.add(impact)
            impacts.append(impact)
        db.commit()
        for imp in impacts:
            db.refresh(imp)
        return impacts

    @staticmethod
    def _compute_shortage_impacts(db: Session, confirmation: SupplierConfirmation) -> List[dict]:
        """纯计算当前缺口对生产批次的影响，排序确定，重复执行结果一致"""
        material_id = confirmation.material_id
        shortage_qty = confirmation.shortage_quantity
        if shortage_qty <= 0:
            return []

        current_stock = crud_inventory_batch.get_total_stock(db, material_id)
        in_transit_orders = crud_purchase_order.get_in_transit_by_material_ordered_by_date(db, material_id)

        # 本确认单同一采购建议转出的采购单不重复计入在途（其承诺量已体现在缺口中）
        own_order_ids = {
            o.id for o in crud_purchase_order.get_by_purchase_suggestion(db, confirmation.purchase_suggestion_id)
        }
        adjusted_in_transit = [o for o in in_transit_orders if o["order_id"] not in own_order_ids]

        class MaterialPool:
            def __init__(self, initial_stock: int, in_transit: List[dict]):
                self.stock = initial_stock
                self.in_transit = in_transit

            def get_available_by(self, check_date: date) -> int:
                available = self.stock
                for order in self.in_transit:
                    if order["expected_date"] <= check_date:
                        available += order["remaining_quantity"]
                return available

            def consume(self, amount: int, consume_date: date) -> Tuple[int, List[dict]]:
                consumed_details = []
                remaining = amount
                if self.stock >= remaining:
                    self.stock -= remaining
                    consumed_details.append({"source": "stock", "quantity": remaining})
                    return remaining, consumed_details
                consumed_from_stock = self.stock
                remaining -= self.stock
                self.stock = 0
                if consumed_from_stock > 0:
                    consumed_details.append({"source": "stock", "quantity": consumed_from_stock})
                for order in self.in_transit:
                    if remaining <= 0:
                        break
                    if order["expected_date"] <= consume_date and order["remaining_quantity"] > 0:
                        take = min(remaining, order["remaining_quantity"])
                        order["remaining_quantity"] -= take
                        remaining -= take
                        consumed_details.append({
                            "source": "in_transit",
                            "order_id": order["order_id"],
                            "quantity": take
                        })
                return amount - remaining, consumed_details

        pool = MaterialPool(current_stock, adjusted_in_transit)

        active_models = crud_vehicle.get_active_models(db)
        model_bom_quantity: Dict[int, int] = {}
        for vm in active_models:
            bom_items = crud_vehicle.get_bom_items(db, vm.id)
            uses_material = next((bi for bi in bom_items if bi.material_id == material_id), None)
            if uses_material:
                model_bom_quantity[vm.id] = uses_material.quantity

        if not model_bom_quantity:
            return []

        all_batches = crud_production_batch.get_planned_batches_sorted(db)
        relevant_batches = [
            b for b in all_batches
            if b.vehicle_model_id in model_bom_quantity
        ]
        relevant_batches.sort(key=lambda b: (
            b.plan_date,
            -(b.vehicle_model.priority if b.vehicle_model else 0),
            b.id
        ))

        from app.crud.alternative import crud_alternative_material, crud_alternative_restriction
        from app.crud.purchase import crud_inventory_batch as inv_batch
        alternatives_data: Dict[int, Dict] = {}
        for vm_id in model_bom_quantity:
            alts = crud_alternative_material.get_alternatives_for_material(db, material_id)
            available_alts = []
            total_alt_qty = 0
            for alt in alts:
                if crud_alternative_restriction.is_alternative_allowed(db, alt.id, vm_id):
                    alt_stock = inv_batch.get_total_stock(db, alt.alternative_material_id)
                    if alt_stock > 0:
                        available_alts.append({
                            "alternative_id": alt.id,
                            "material_id": alt.alternative_material_id,
                            "stock": alt_stock,
                            "priority": alt.priority
                        })
                        total_alt_qty += alt_stock
            alternatives_data[vm_id] = {
                "available": available_alts,
                "total_stock": total_alt_qty
            }

        computed: List[dict] = []
        remaining_shortage = shortage_qty

        for batch in relevant_batches:
            if remaining_shortage <= 0:
                break
            vm_id = batch.vehicle_model_id
            bom_qty = model_bom_quantity.get(vm_id, 0)
            required_qty = bom_qty * batch.quantity

            available_on_time = pool.get_available_by(batch.plan_date)

            if available_on_time >= required_qty:
                pool.consume(required_qty, batch.plan_date)
                continue

            alt_data = alternatives_data.get(vm_id, {})
            total_alt_stock = alt_data.get("total_stock", 0)
            shortfall = required_qty - available_on_time

            if total_alt_stock >= shortfall:
                pool.consume(available_on_time, batch.plan_date)
                continue

            shortage_for_batch = min(shortfall, remaining_shortage)
            actual_affected_qty = min(shortage_for_batch, required_qty - available_on_time)

            impact_level, delay_days, remark = SupplierConfirmationService._assess_impact(
                shortage_qty=actual_affected_qty,
                batch_qty=batch.quantity,
                per_unit=bom_qty,
                material=confirmation.material
            )

            max_vehicles = actual_affected_qty // bom_qty if bom_qty > 0 else 0
            remark = f"供应商缺料{shortage_qty}件，本批次缺口{actual_affected_qty}件，影响约{max_vehicles}台整车"

            computed.append({
                "production_batch_id": batch.id,
                "affected_vehicle_model_id": vm_id,
                "shortage_material_id": material_id,
                "shortage_quantity": actual_affected_qty,
                "impact_level": impact_level,
                "estimated_delay_days": delay_days,
                "remark": remark
            })
            remaining_shortage -= actual_affected_qty

            pool.consume(available_on_time, batch.plan_date)

        return computed

    @staticmethod
    def _assess_impact(
        shortage_qty: int,
        batch_qty: int,
        per_unit: int,
        material
    ) -> Tuple[str, int, str]:
        max_vehicles_affected = shortage_qty // per_unit if per_unit > 0 else 0
        affected_ratio = max_vehicles_affected / batch_qty if batch_qty > 0 else 0
        is_critical = material.is_critical if material else False
        if is_critical and (affected_ratio > 0.5 or max_vehicles_affected >= 50):
            impact_level = "high"
            delay_days = 15
            remark = f"关键物料缺料{shortage_qty}件，影响约{max_vehicles_affected}台整车，需紧急处理"
        elif affected_ratio > 0.3 or max_vehicles_affected >= 30:
            impact_level = "medium"
            delay_days = 7
            remark = f"物料缺料{shortage_qty}件，影响约{max_vehicles_affected}台整车，需关注"
        elif affected_ratio > 0 or max_vehicles_affected > 0:
            impact_level = "low"
            delay_days = 3
            remark = f"物料缺料{shortage_qty}件，影响约{max_vehicles_affected}台整车"
        else:
            impact_level = "none"
            delay_days = 0
            remark = "缺料在库存缓冲范围内，暂不影响"
        return impact_level, delay_days, remark

    @staticmethod
    def _sync_delay_impact_with_supplier(db: Session, confirmation_id: int):
        confirmation = crud_supplier_confirmation.get(db, confirmation_id)
        if not confirmation or not confirmation.committed_delivery_date:
            return
        suggestion = confirmation.purchase_suggestion
        if not suggestion or not suggestion.expected_delivery_date:
            return
        committed_date = confirmation.committed_delivery_date
        expected_date = suggestion.expected_delivery_date
        delay_days = (committed_date - expected_date).days
        if delay_days > 0:
            from app.services.delay_analysis import DelayAnalysisService
            pass

    @staticmethod
    def get_version_diff(
        db: Session,
        confirmation_id: int,
        from_version_no: Optional[int] = None,
        to_version_no: Optional[int] = None
    ) -> SupplierCommitmentVersionDiff:
        """对比两个承诺版本在可用日期、数量与受影响批次上的差异。

        默认对比最新版本与其前一版本；影响数据取各版本冻结的历史快照。
        """
        confirmation = crud_supplier_confirmation.get(db, confirmation_id)
        if not confirmation:
            raise ValueError(f"供应商确认不存在: {confirmation_id}")
        versions = crud_supplier_commitment_version.get_by_confirmation(db, confirmation_id)
        if not versions:
            raise ValueError("该确认单暂无承诺版本")
        if to_version_no is not None:
            to_version = crud_supplier_commitment_version.get_by_version_no(db, confirmation_id, to_version_no)
            if not to_version:
                raise ValueError(f"承诺版本不存在: v{to_version_no}")
        else:
            to_version = versions[-1]
        if from_version_no is not None:
            from_version = crud_supplier_commitment_version.get_by_version_no(db, confirmation_id, from_version_no)
            if not from_version:
                raise ValueError(f"承诺版本不存在: v{from_version_no}")
        else:
            earlier = [v for v in versions if v.version_no < to_version.version_no]
            if not earlier:
                raise ValueError("没有可对比的历史版本")
            from_version = earlier[-1]

        if from_version.committed_delivery_date and to_version.committed_delivery_date:
            date_change_days = (to_version.committed_delivery_date - from_version.committed_delivery_date).days
        else:
            date_change_days = None

        # 分批到货计划差异（按批次号匹配）
        from_batches = {b.batch_no: b for b in from_version.batches}
        to_batches = {b.batch_no: b for b in to_version.batches}
        schedule_changes: List[SupplierCommitmentBatchScheduleChange] = []
        for batch_no in sorted(set(from_batches) | set(to_batches)):
            fb = from_batches.get(batch_no)
            tb = to_batches.get(batch_no)
            if fb and not tb:
                schedule_changes.append(SupplierCommitmentBatchScheduleChange(
                    batch_no=batch_no, change_type="removed",
                    old_quantity=fb.quantity, old_planned_date=fb.planned_date
                ))
            elif tb and not fb:
                schedule_changes.append(SupplierCommitmentBatchScheduleChange(
                    batch_no=batch_no, change_type="added",
                    new_quantity=tb.quantity, new_planned_date=tb.planned_date
                ))
            elif fb.quantity != tb.quantity or fb.planned_date != tb.planned_date:
                schedule_changes.append(SupplierCommitmentBatchScheduleChange(
                    batch_no=batch_no, change_type="changed",
                    old_quantity=fb.quantity, new_quantity=tb.quantity,
                    old_planned_date=fb.planned_date, new_planned_date=tb.planned_date
                ))

        # 受影响生产批次差异（取各版本冻结的影响快照，按生产批次匹配）
        from_impacts = {i.production_batch_id: i for i in crud_supplier_shortage_impact.get_by_version(db, from_version.id)}
        to_impacts = {i.production_batch_id: i for i in crud_supplier_shortage_impact.get_by_version(db, to_version.id)}
        added: List[SupplierCommitmentImpactDiff] = []
        removed: List[SupplierCommitmentImpactDiff] = []
        changed: List[SupplierCommitmentImpactDiff] = []
        for pb_id in sorted(set(from_impacts) | set(to_impacts)):
            fi = from_impacts.get(pb_id)
            ti = to_impacts.get(pb_id)
            ref = ti or fi
            pb = ref.production_batch
            base = {
                "production_batch_id": pb_id,
                "production_batch_no": pb.batch_no if pb else "",
                "vehicle_model_name": ref.vehicle_model.name if ref.vehicle_model else "",
                "plan_date": pb.plan_date if pb else None
            }
            if ti and not fi:
                added.append(SupplierCommitmentImpactDiff(
                    **base, change_type="added",
                    new_shortage_quantity=ti.shortage_quantity,
                    new_impact_level=ti.impact_level,
                    new_estimated_delay_days=ti.estimated_delay_days
                ))
            elif fi and not ti:
                removed.append(SupplierCommitmentImpactDiff(
                    **base, change_type="removed",
                    old_shortage_quantity=fi.shortage_quantity,
                    old_impact_level=fi.impact_level,
                    old_estimated_delay_days=fi.estimated_delay_days
                ))
            elif (fi.shortage_quantity != ti.shortage_quantity or
                  fi.impact_level != ti.impact_level or
                  fi.estimated_delay_days != ti.estimated_delay_days):
                changed.append(SupplierCommitmentImpactDiff(
                    **base, change_type="changed",
                    old_shortage_quantity=fi.shortage_quantity,
                    new_shortage_quantity=ti.shortage_quantity,
                    old_impact_level=fi.impact_level,
                    new_impact_level=ti.impact_level,
                    old_estimated_delay_days=fi.estimated_delay_days,
                    new_estimated_delay_days=ti.estimated_delay_days
                ))

        return SupplierCommitmentVersionDiff(
            confirmation_id=confirmation_id,
            confirmation_no=confirmation.confirmation_no,
            from_version=from_version,
            to_version=to_version,
            committed_quantity_change=to_version.committed_quantity - from_version.committed_quantity,
            shortage_quantity_change=to_version.shortage_quantity - from_version.shortage_quantity,
            delivery_date_change_days=date_change_days,
            batch_schedule_changes=schedule_changes,
            affected_batches_added=added,
            affected_batches_removed=removed,
            affected_batches_changed=changed
        )

    @staticmethod
    def get_pending_confirmations(db: Session, supplier_id: Optional[int] = None) -> List[SupplierConfirmation]:
        if supplier_id:
            all_sup = crud_supplier_confirmation.get_by_supplier(db, supplier_id)
            return [c for c in all_sup if c.status in ("pending", "revised")]
        return crud_supplier_confirmation.get_by_status(db, "pending") + \
               crud_supplier_confirmation.get_by_status(db, "revised")

    @staticmethod
    def get_shortage_confirmations(db: Session, supplier_id: Optional[int] = None) -> List[SupplierConfirmation]:
        all_shortage = crud_supplier_confirmation.get_shortage_confirmations(db)
        if supplier_id:
            return [c for c in all_shortage if c.supplier_id == supplier_id]
        return all_shortage

    @staticmethod
    def get_confirmation_statistics(db: Session) -> SupplierConfirmationStatistics:
        all_confirmations = crud_supplier_confirmation.get_multi(db)
        total = len(all_confirmations)
        pending = sum(1 for c in all_confirmations if c.status == "pending")
        confirmed = sum(1 for c in all_confirmations if c.status == "confirmed")
        shortage = sum(1 for c in all_confirmations if c.status == "shortage")
        total_req = sum(c.requested_quantity for c in all_confirmations)
        total_com = sum(c.committed_quantity for c in all_confirmations)
        total_short = sum(c.shortage_quantity for c in all_confirmations)
        rate = (total_com / total_req) if total_req > 0 else 1.0
        return SupplierConfirmationStatistics(
            total_confirmations=total,
            pending_confirmations=pending,
            confirmed_confirmations=confirmed,
            shortage_confirmations=shortage,
            total_requested_qty=total_req,
            total_committed_qty=total_com,
            total_shortage_qty=total_short,
            commitment_rate=round(rate, 4)
        )

    @staticmethod
    def get_supplier_bottlenecks(db: Session) -> List[SupplierBottleneckAnalysis]:
        suppliers = crud_supplier.get_multi(db)
        result: List[SupplierBottleneckAnalysis] = []
        for supplier in suppliers:
            bottleneck_confs: List[SupplierConfirmation] = []
            batch_map: Dict[int, Dict] = {}
            confirmations = crud_supplier_confirmation.get_by_supplier(db, supplier.id)
            for conf in confirmations:
                is_bottleneck = False
                if conf.shortage_quantity > 0:
                    is_bottleneck = True
                if conf.committed_delivery_date and conf.purchase_suggestion and conf.purchase_suggestion.expected_delivery_date:
                    delay = (conf.committed_delivery_date - conf.purchase_suggestion.expected_delivery_date).days
                    if delay > 3:
                        is_bottleneck = True
                if is_bottleneck:
                    bottleneck_confs.append(conf)
                impacts = crud_supplier_shortage_impact.get_by_confirmation(db, conf.id)
                for impact in impacts:
                    batch_id = impact.production_batch_id
                    if batch_id not in batch_map:
                        pb = impact.production_batch
                        vm = impact.vehicle_model
                        batch_map[batch_id] = {
                            "production_batch_id": batch_id,
                            "production_batch_no": pb.batch_no if pb else "",
                            "vehicle_model_id": impact.affected_vehicle_model_id,
                            "vehicle_model_name": vm.name if vm else "",
                            "plan_date": pb.plan_date if pb else date.today(),
                            "quantity": pb.quantity if pb else 0,
                            "affected_materials": [],
                            "material_ids": set(),
                            "total_shortage_quantity": 0,
                            "max_delay": 0,
                            "has_high": False
                        }
                    entry = batch_map[batch_id]
                    mat_id = impact.shortage_material_id
                    if mat_id not in entry["material_ids"]:
                        mat = crud_material.get(db, mat_id)
                        if mat:
                            entry["affected_materials"].append(Material.model_validate(mat))
                            entry["material_ids"].add(mat_id)
                    entry["total_shortage_quantity"] += impact.shortage_quantity
                    entry["max_delay"] = max(entry["max_delay"], impact.estimated_delay_days)
                    if impact.impact_level == "high":
                        entry["has_high"] = True
            if not bottleneck_confs and not batch_map:
                continue
            affected_batches_list: List[SupplierBottleneckBatch] = []
            total_delay_sum = 0
            for entry in batch_map.values():
                impact_level = "high" if entry["has_high"] else ("medium" if entry["max_delay"] >= 7 else "low")
                total_delay_sum += entry["max_delay"]
                affected_batches_list.append(SupplierBottleneckBatch(
                    production_batch_id=entry["production_batch_id"],
                    production_batch_no=entry["production_batch_no"],
                    vehicle_model_id=entry["vehicle_model_id"],
                    vehicle_model_name=entry["vehicle_model_name"],
                    plan_date=entry["plan_date"],
                    quantity=entry["quantity"],
                    affected_materials=entry["affected_materials"],
                    total_shortage_quantity=entry["total_shortage_quantity"],
                    impact_level=impact_level
                ))
            total_short = sum(c.shortage_quantity for c in bottleneck_confs)
            avg_delay = (total_delay_sum / len(affected_batches_list)) if affected_batches_list else 0.0
            has_high = any(b.impact_level == "high" for b in affected_batches_list)
            overall = "high" if has_high else ("medium" if avg_delay >= 7 or total_short > 1000 else "low")
            result.append(SupplierBottleneckAnalysis(
                supplier_id=supplier.id,
                supplier_code=supplier.code,
                supplier_name=supplier.name,
                bottleneck_confirmations=bottleneck_confs,
                affected_batches=affected_batches_list,
                total_affected_batches=len(affected_batches_list),
                total_shortage_qty=total_short,
                average_delay_days=round(avg_delay, 2),
                overall_impact_level=overall
            ))
        result.sort(key=lambda x: (
            0 if x.overall_impact_level == "high" else (1 if x.overall_impact_level == "medium" else 2),
            -x.total_affected_batches,
            -x.total_shortage_qty
        ))
        return result
