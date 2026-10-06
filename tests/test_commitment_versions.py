import pytest
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.services.commitment import CommitmentService
from app.services.inspection import InspectionService
from app.crud.commitment import crud_commitment_version, crud_commitment_impact
from app.schemas import (
    CommitmentVersionPublish, CommitmentVersionBatchCreate, InspectionCreate
)
from tests.test_data_factory import DataFactory


def _days(n):
    return date.today() + timedelta(days=n)


def _batch(no, qty, days):
    return CommitmentVersionBatchCreate(
        batch_no=no, quantity=qty, planned_date=_days(days)
    )


def _publish_in(committed, batches, **kwargs):
    return CommitmentVersionPublish(
        committed_quantity=committed,
        committed_delivery_date=kwargs.get("committed_delivery_date"),
        signed_by=kwargs.get("signed_by", "张经理"),
        signature_doc=kwargs.get("signature_doc", "HT-2026-001"),
        note=kwargs.get("note"),
        batches=batches
    )


def _impact_signature(db, version_id):
    """影响快照内容签名：用于断言重复复算结果稳定"""
    impacts = crud_commitment_impact.get_by_version(db, version_id)
    return [
        (i.production_batch_id, i.required_quantity, i.covered_quantity,
         i.shortage_quantity, i.available_date, i.delay_days, i.impact_level)
        for i in impacts
    ]


class TestCommitmentVersionLifecycle:
    """承诺版本不可覆盖，发布新版本只取代不修改"""

    def test_publish_creates_immutable_version_history(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        po = factory.create_purchase_order(
            "PO-VER-001", "TS001", "TM001", 130, _days(15)
        )

        v1 = CommitmentService.publish_version(
            db_session, po.id,
            _publish_in(130, [_batch("VB-1", 80, 12), _batch("VB-2", 50, 15)],
                        signed_by="张经理", signature_doc="HT-2026-001")
        )
        assert v1.version_number == 1
        assert v1.status == "published"
        assert v1.adjustable_quantity == 130
        assert v1.delivered_quantity == 0
        assert v1.signed_by == "张经理"
        assert v1.signature_doc == "HT-2026-001"
        assert v1.signed_at is not None
        assert len(v1.batches) == 2
        v1_batch_ids = [b.id for b in v1.batches]

        v2 = CommitmentService.publish_version(
            db_session, po.id,
            _publish_in(100, [_batch("VB-3", 100, 20)],
                        signed_by="李经理", signature_doc="HT-2026-002")
        )
        assert v2.version_number == 2
        assert v2.status == "published"
        assert v2.version_no != v1.version_no

        # 旧版本被取代但内容不可覆盖：数量、分批、签署依据全部保持原样
        db_session.refresh(v1)
        assert v1.status == "superseded"
        assert v1.committed_quantity == 130
        assert v1.adjustable_quantity == 130
        assert v1.signed_by == "张经理"
        assert v1.signature_doc == "HT-2026-001"
        assert [b.id for b in v1.batches] == v1_batch_ids
        assert sorted(b.quantity for b in v1.batches) == [50, 80]

        versions = crud_commitment_version.get_by_order(db_session, po.id)
        assert [v.version_number for v in versions] == [1, 2]

    def test_publish_validates_against_completed_deliveries(self, db_session):
        """已完成到货从可调整范围中扣除：分批计划只能覆盖未到货部分"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        po = factory.create_purchase_order(
            "PO-VER-002", "TS002", "TM002", 200, _days(10)
        )
        factory.create_delivery(
            "DEL-VER-002", "PO-VER-002", "TS002", "TM002",
            quantity=80, delivery_date=date.today()
        )

        # 承诺数量低于已到货数量 → 拒绝
        with pytest.raises(ValueError, match="不能低于已到货数量"):
            CommitmentService.publish_version(
                db_session, po.id, _publish_in(70, [_batch("VB-1", 70, 10)])
            )

        # 分批合计未扣除已到货 → 拒绝
        with pytest.raises(ValueError, match="已从可调整范围中扣除"):
            CommitmentService.publish_version(
                db_session, po.id, _publish_in(200, [_batch("VB-1", 200, 10)])
            )

        # 可调整数量大于0但不提供分批计划 → 拒绝
        with pytest.raises(ValueError, match="必须提供分批到货计划"):
            CommitmentService.publish_version(db_session, po.id, _publish_in(200, []))

        # 分批合计 = 承诺200 - 已到货80 = 120 → 接受
        version = CommitmentService.publish_version(
            db_session, po.id, _publish_in(200, [_batch("VB-1", 120, 10)])
        )
        assert version.delivered_quantity == 80
        assert version.adjustable_quantity == 120
        assert sum(b.quantity for b in version.batches) == 120

    def test_withdraw_falls_back_to_previous_version(self, db_session):
        """撤回当前版本后自动回落上一版并重算影响，可安全撤回错误确认"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        po = factory.create_purchase_order(
            "PO-VER-003", "TS001", "TM001", 80, _days(15)
        )
        tb001 = factory.production_batches["TB001"]

        v1 = CommitmentService.publish_version(
            db_session, po.id, _publish_in(80, [_batch("VB-1", 80, 12)])
        )
        v2 = CommitmentService.publish_version(
            db_session, po.id, _publish_in(80, [_batch("VB-2", 80, 25)])
        )
        # v2 到货更晚，TB001 延期更严重
        v2_impacts = crud_commitment_impact.get_by_version(db_session, v2.id)
        assert len(v2_impacts) == 1
        assert v2_impacts[0].production_batch_id == tb001.id
        assert v2_impacts[0].delay_days == 15

        # 已被取代的版本不允许撤回
        with pytest.raises(ValueError, match="仅当前生效版本可撤回"):
            CommitmentService.withdraw_version(db_session, v1.id, "非生效版本")

        # 撤回 v2 → 回落 v1 并按当前数据重算 v1 的影响
        withdrawn = CommitmentService.withdraw_version(db_session, v2.id, "录入错误，撤回")
        assert withdrawn.status == "withdrawn"
        assert withdrawn.withdraw_reason == "录入错误，撤回"
        assert withdrawn.withdrawn_at is not None

        db_session.refresh(v1)
        assert v1.status == "published"
        v1_impacts = crud_commitment_impact.get_by_version(db_session, v1.id)
        assert len(v1_impacts) == 1
        assert v1_impacts[0].production_batch_id == tb001.id
        assert v1_impacts[0].delay_days == 2

        # 再撤回 v1 → 无生效版本
        CommitmentService.withdraw_version(db_session, v1.id)
        assert CommitmentService.get_current_version(db_session, po.id) is None
        with pytest.raises(ValueError, match="无生效承诺版本"):
            CommitmentService.recalculate_order(db_session, po.id)

        # 已撤回的版本不能再次撤回
        with pytest.raises(ValueError, match="仅当前生效版本可撤回"):
            CommitmentService.withdraw_version(db_session, v2.id)


class TestCommitmentRecalculation:
    """重算的确定性与幂等性"""

    def test_recalculate_is_idempotent(self, db_session):
        """复算任务重复执行必须得到稳定结果：行数、主键、内容完全一致"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        po = factory.create_purchase_order(
            "PO-CALC-001", "TS001", "TM001", 80, _days(15)
        )
        v1 = CommitmentService.publish_version(
            db_session, po.id, _publish_in(80, [_batch("VB-1", 80, 12)])
        )

        first = CommitmentService.recalculate_order(db_session, po.id)
        first_snapshot = [
            (i.id, i.production_batch_id, i.shortage_quantity,
             i.available_date, i.delay_days, i.impact_level)
            for i in first
        ]
        for _ in range(3):
            again = CommitmentService.recalculate_order(db_session, po.id)
            again_snapshot = [
                (i.id, i.production_batch_id, i.shortage_quantity,
                 i.available_date, i.delay_days, i.impact_level)
                for i in again
            ]
            assert again_snapshot == first_snapshot, "重复复算必须产生完全稳定的结果"

        # 没有产生重复影响记录
        all_impacts = crud_commitment_impact.get_by_version(db_session, v1.id)
        assert len(all_impacts) == len(first_snapshot)

    def test_multiple_orders_jointly_cover_one_gap(self, db_session):
        """多张采购单共同覆盖一个缺口，结果与处理顺序无关且重复执行稳定"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        tb004 = factory.production_batches["TB004"]

        po_a = factory.create_purchase_order(
            "PO-CALC-002A", "TS002", "TM002", 100, _days(6)
        )
        v_a = CommitmentService.publish_version(
            db_session, po_a.id, _publish_in(100, [_batch("VBA-1", 100, 6)])
        )
        # 只有 PO-A 时 TB004 只能覆盖 100，缺口 50
        impacts_a = {
            i.production_batch_id: i
            for i in crud_commitment_impact.get_by_version(db_session, v_a.id)
        }
        assert impacts_a[tb004.id].covered_quantity == 100
        assert impacts_a[tb004.id].shortage_quantity == 50

        po_b = factory.create_purchase_order(
            "PO-CALC-002B", "TS002", "TM002", 40, _days(8)
        )
        v_b = CommitmentService.publish_version(
            db_session, po_b.id, _publish_in(40, [_batch("VBB-1", 40, 8)])
        )
        # PO-A 与 PO-B 共同覆盖：TB004 覆盖 100+40=140，缺口降为 10
        impacts_b = {
            i.production_batch_id: i
            for i in crud_commitment_impact.get_by_version(db_session, v_b.id)
        }
        assert impacts_b[tb004.id].covered_quantity == 140
        assert impacts_b[tb004.id].shortage_quantity == 10

        # 对两张采购单分别重复复算，结果稳定
        stable_b = _impact_signature(db_session, v_b.id)
        for _ in range(2):
            CommitmentService.recalculate_order(db_session, po_b.id)
            assert _impact_signature(db_session, v_b.id) == stable_b
        # PO-A 复算后刷新为包含 PO-B 供应的最新图景，且重复复算稳定
        CommitmentService.recalculate_order(db_session, po_a.id)
        refreshed_a = {
            i.production_batch_id: i
            for i in crud_commitment_impact.get_by_version(db_session, v_a.id)
        }
        assert refreshed_a[tb004.id].covered_quantity == 140
        assert refreshed_a[tb004.id].shortage_quantity == 10
        stable_a = _impact_signature(db_session, v_a.id)
        CommitmentService.recalculate_order(db_session, po_a.id)
        assert _impact_signature(db_session, v_a.id) == stable_a

    def test_completed_arrivals_deducted_from_recalculation(self, db_session):
        """发布后完成的到货从分批计划中扣除，不与库存重复计数"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        tb002 = factory.production_batches["TB002"]

        po = factory.create_purchase_order(
            "PO-CALC-003", "TS001", "TM001", 60, _days(12)
        )
        v1 = CommitmentService.publish_version(
            db_session, po.id, _publish_in(60, [_batch("VB-1", 60, 12)])
        )

        # 发布后到货 50 件并检验合格入库
        delivery = factory.create_delivery(
            "DEL-CALC-003", "PO-CALC-003", "TS001", "TM001",
            quantity=50, delivery_date=date.today()
        )
        InspectionService.record_inspection_and_process(
            db_session,
            InspectionCreate(
                delivery_id=delivery.id, sample_size=10, defective_count=0,
                pass_rate=1.0, result="passed", inspector="测试员",
                inspection_date=date.today()
            )
        )

        # 剩余分批计划 = 60 - 50 = 10；库存 50 覆盖 TB001(50)，
        # TB002(30) 只能由剩余计划覆盖 10，缺口 20 且无法再齐套
        impacts = CommitmentService.recalculate_order(db_session, po.id)
        assert len(impacts) == 1
        impact = impacts[0]
        assert impact.production_batch_id == tb002.id
        assert impact.covered_quantity == 10
        assert impact.shortage_quantity == 20
        assert impact.available_date is None
        assert impact.impact_level == "high"

        # 若未扣除已到货，剩余计划会被误算为 60 从而掩盖缺口；
        # 再次复算结果必须稳定
        again = CommitmentService.recalculate_order(db_session, po.id)
        assert [(i.id, i.shortage_quantity) for i in again] == \
               [(impact.id, 20)]

    def test_version_regression_produces_stable_results(self, db_session):
        """承诺版本倒退（内容回到旧版）时，重算结果与旧版一致"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        po = factory.create_purchase_order(
            "PO-CALC-004", "TS001", "TM001", 80, _days(15)
        )

        v1 = CommitmentService.publish_version(
            db_session, po.id, _publish_in(80, [_batch("VB-1", 80, 12)])
        )
        signature_v1 = _impact_signature(db_session, v1.id)

        CommitmentService.publish_version(
            db_session, po.id, _publish_in(80, [_batch("VB-2", 80, 25)])
        )
        v3 = CommitmentService.publish_version(
            db_session, po.id, _publish_in(80, [_batch("VB-3", 80, 12)])
        )

        # v3 与 v1 内容相同，影响快照必须一致（主键除外）
        assert _impact_signature(db_session, v3.id) == signature_v1


class TestCommitmentImpactDiff:
    """影响查询展示新旧版本在可用日期、数量、受影响批次上的差异"""

    def _setup_two_versions(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        po = factory.create_purchase_order(
            "PO-DIFF-001", "TS001", "TM001", 80, _days(25)
        )
        v1 = CommitmentService.publish_version(
            db_session, po.id,
            _publish_in(80, [_batch("VB-1", 80, 25)],
                        committed_delivery_date=_days(25))
        )
        v2 = CommitmentService.publish_version(
            db_session, po.id,
            _publish_in(80, [_batch("VB-2", 50, 9), _batch("VB-3", 30, 12)])
        )
        return factory, po, v1, v2

    def test_diff_shows_date_quantity_and_batch_changes(self, db_session):
        factory, po, v1, v2 = self._setup_two_versions(db_session)
        tb001 = factory.production_batches["TB001"]

        diff = CommitmentService.diff_versions(db_session, po.id, v1.id, v2.id)

        assert diff.from_version_number == 1
        assert diff.to_version_number == 2
        assert diff.committed_quantity_delta == 0
        # 整体承诺交期从 +25 提前到 +12（取分批最晚日期）
        assert diff.delivery_date_shift_days == -13

        # 分批到货计划差异：+25 的 80 件移除，+9 新增 50，+12 新增 30
        changes = {(c.planned_date, c.change_type): c for c in diff.schedule_changes}
        assert changes[(_days(25), "removed")].old_quantity == 80
        assert changes[(_days(9), "added")].new_quantity == 50
        assert changes[(_days(12), "added")].new_quantity == 30

        # 受影响批次差异：v1 影响 TB001（延期15天），v2 无影响 → TB001 解除
        assert diff.old_affected_batches == 1
        assert diff.new_affected_batches == 0
        assert len(diff.impact_diffs) == 1
        impact_diff = diff.impact_diffs[0]
        assert impact_diff.production_batch_id == tb001.id
        assert impact_diff.change_type == "resolved"
        assert impact_diff.old_impact.shortage_quantity == 50
        assert impact_diff.old_impact.available_date == _days(25)
        assert impact_diff.old_impact.delay_days == 15
        assert impact_diff.new_impact is None
        assert impact_diff.shortage_delta == -50
        assert "解除1个" in diff.summary

    def test_diff_defaults_to_previous_and_current_version(self, db_session):
        _, po, v1, v2 = self._setup_two_versions(db_session)

        default_diff = CommitmentService.diff_versions(db_session, po.id)
        assert default_diff.from_version_id == v1.id
        assert default_diff.to_version_id == v2.id

        explicit_diff = CommitmentService.diff_versions(db_session, po.id, v1.id, v2.id)
        assert default_diff.model_dump() == explicit_diff.model_dump()

    def test_diff_worsened_and_newly_affected(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        tb001 = factory.production_batches["TB001"]
        tb002 = factory.production_batches["TB002"]
        po = factory.create_purchase_order(
            "PO-DIFF-002", "TS001", "TM001", 80, _days(15)
        )
        v1 = CommitmentService.publish_version(
            db_session, po.id, _publish_in(80, [_batch("VB-1", 80, 12)])
        )
        # 版本倒退：承诺数量缩减且到货大幅推迟
        v2 = CommitmentService.publish_version(
            db_session, po.id, _publish_in(50, [_batch("VB-2", 50, 32)])
        )

        diff = CommitmentService.diff_versions(db_session, po.id, v1.id, v2.id)
        assert diff.committed_quantity_delta == -30
        by_batch = {d.production_batch_id: d for d in diff.impact_diffs}
        # TB001：v1 缺口50延期2天 → v2 缺口50延期22天，恶化
        assert by_batch[tb001.id].change_type == "worsened"
        assert by_batch[tb001.id].old_impact.delay_days == 2
        assert by_batch[tb001.id].new_impact.delay_days == 22
        assert by_batch[tb001.id].available_date_shift_days == 20
        # TB002：v1 可覆盖 → v2 新增受影响
        assert by_batch[tb002.id].change_type == "newly_affected"
        assert by_batch[tb002.id].old_impact is None
        assert by_batch[tb002.id].new_impact.shortage_quantity == 30

    def test_diff_rejects_invalid_version_pairs(self, db_session):
        factory, po, v1, v2 = self._setup_two_versions(db_session)
        other = factory.create_purchase_order(
            "PO-DIFF-003", "TS001", "TM001", 10, _days(15)
        )
        other_v1 = CommitmentService.publish_version(
            db_session, other.id, _publish_in(10, [_batch("VBO-1", 10, 15)])
        )

        # 跨采购单对比 → 拒绝
        with pytest.raises(ValueError, match="不存在版本"):
            CommitmentService.diff_versions(db_session, po.id, v1.id, other_v1.id)
        # 新旧颠倒 → 拒绝
        with pytest.raises(ValueError, match="旧版本号必须小于新版本号"):
            CommitmentService.diff_versions(db_session, po.id, v2.id, v1.id)
        # 首个版本无旧版可比较
        with pytest.raises(ValueError, match="无旧版可比较"):
            CommitmentService.diff_versions(db_session, other.id, to_version_id=other_v1.id)


class TestCommitmentAPI:
    """接口层验收"""

    def test_api_publish_query_withdraw_and_diff(self, db_session, override_get_db):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        po = factory.create_purchase_order(
            "PO-API-001", "TS001", "TM001", 80, _days(15)
        )
        client = TestClient(app)
        base = f"/api/v1/commitments/orders/{po.id}"

        # 分批合计与承诺数量不一致 → 400
        bad = client.post(f"{base}/versions", json={
            "committed_quantity": 80,
            "signed_by": "张经理",
            "signature_doc": "HT-001",
            "batches": [{"batch_no": "B1", "quantity": 60,
                         "planned_date": _days(12).isoformat()}]
        })
        assert bad.status_code == 400

        # 缺少签署依据 → 422
        missing_signature = client.post(f"{base}/versions", json={
            "committed_quantity": 80,
            "batches": [{"batch_no": "B1", "quantity": 80,
                         "planned_date": _days(12).isoformat()}]
        })
        assert missing_signature.status_code == 422

        # 正常发布 v1、v2
        r1 = client.post(f"{base}/versions", json={
            "committed_quantity": 80,
            "signed_by": "张经理",
            "signature_doc": "HT-001",
            "batches": [{"batch_no": "B1", "quantity": 80,
                         "planned_date": _days(12).isoformat()}]
        })
        assert r1.status_code == 200
        v1 = r1.json()
        assert v1["version_number"] == 1
        assert v1["status"] == "published"
        assert len(v1["impacts"]) == 1

        r2 = client.post(f"{base}/versions", json={
            "committed_quantity": 80,
            "signed_by": "李经理",
            "signature_doc": "HT-002",
            "batches": [{"batch_no": "B2", "quantity": 80,
                         "planned_date": _days(25).isoformat()}]
        })
        assert r2.status_code == 200
        v2 = r2.json()

        # 版本历史包含两版，内容未被覆盖
        versions = client.get(f"{base}/versions").json()
        assert [v["version_number"] for v in versions] == [1, 2]
        assert versions[0]["committed_quantity"] == 80
        assert versions[0]["signature_doc"] == "HT-001"

        # 当前生效版本为 v2
        current = client.get(f"{base}/current").json()
        assert current["id"] == v2["id"]

        # 版本差异：v1→v2 到货推迟，TB001 恶化
        diff = client.get(f"{base}/impact-diff").json()
        assert diff["from_version_id"] == v1["id"]
        assert diff["to_version_id"] == v2["id"]
        assert diff["delivery_date_shift_days"] == 13
        assert diff["impact_diffs"][0]["change_type"] == "worsened"

        # 重复复算结果稳定
        recalc1 = client.post(f"{base}/recalculate").json()
        recalc2 = client.post(f"{base}/recalculate").json()
        assert recalc1 == recalc2

        # 撤回 v2 → 回落 v1
        withdrawn = client.post(
            f"/api/v1/commitments/versions/{v2['id']}/withdraw",
            json={"reason": "确认录入错误"}
        ).json()
        assert withdrawn["status"] == "withdrawn"
        current_after = client.get(f"{base}/current").json()
        assert current_after["id"] == v1["id"]

        # 版本不可覆盖：不提供修改入口
        put_resp = client.put(f"/api/v1/commitments/versions/{v1['id']}", json={})
        assert put_resp.status_code == 405
