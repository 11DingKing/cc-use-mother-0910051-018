import pytest
from datetime import date, timedelta
from tests.test_data_factory import DataFactory

from app.services.purchase import PurchaseService
from app.services.supplier_confirmation import SupplierConfirmationService
from app.crud.supplier_confirmation import (
    crud_supplier_commitment_version,
    crud_supplier_confirmation_batch,
    crud_supplier_shortage_impact
)
from app.schemas import (
    SupplierConfirmationConfirm, SupplierConfirmationBatchCreate
)


def _make_carbon_confirmation(db_session, factory, conf_no):
    """生成TM001（碳纤维车架）采购建议并创建供应商确认，需求=80生产+50安全库存=130"""
    suggestions = PurchaseService.generate_purchase_suggestions(db_session)
    carbon_suggestion = next(
        s for s in suggestions if s.material and s.material.code == "TM001"
    )
    confirmation = SupplierConfirmationService.create_confirmation_from_suggestion(
        db_session,
        suggestion_id=carbon_suggestion.id,
        confirmation_no=conf_no,
        supplier_id=factory.suppliers["TS001"].id
    )
    return confirmation


def _confirm(db_session, confirmation_id, committed_qty, committed_date,
             note=None, batches=None, signed_by=None, signature_basis=None):
    return SupplierConfirmationService.supplier_confirm(
        db_session,
        confirmation_id=confirmation_id,
        confirm_data=SupplierConfirmationConfirm(
            committed_quantity=committed_qty,
            committed_delivery_date=committed_date,
            confirmation_note=note,
            signed_by=signed_by,
            signature_basis=signature_basis,
            batches=batches or []
        )
    )


class TestImmutableCommitmentVersions:
    """每次承诺形成不可覆盖的版本"""

    def test_each_confirm_creates_new_version(self, db_session):
        """多次确认生成递增版本，旧版本被取代且内容不可覆盖"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-VER-001")

        confirmed_1 = _confirm(
            db_session, confirmation.id, 130,
            date.today() + timedelta(days=10),
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-001", quantity=130,
                planned_date=date.today() + timedelta(days=10)
            )]
        )
        version_1_id = confirmed_1.current_version_id
        assert version_1_id is not None

        # 第二次确认：供应商调整承诺数量与到货日期
        confirmed_2 = _confirm(
            db_session, confirmation.id, 100,
            date.today() + timedelta(days=15),
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-002", quantity=100,
                planned_date=date.today() + timedelta(days=15)
            )]
        )

        versions = crud_supplier_commitment_version.get_by_confirmation(
            db_session, confirmation.id
        )
        assert len(versions) == 2, "每次确认都应生成一个版本"
        v1, v2 = versions
        assert v1.version_no == 1
        assert v2.version_no == 2
        assert v1.id == version_1_id
        assert v1.status == "superseded", "旧版本应被标记为已被取代"
        assert v2.status == "active"
        # 旧版本内容不可覆盖
        assert v1.committed_quantity == 130
        assert v1.committed_delivery_date == date.today() + timedelta(days=10)
        assert len(v1.batches) == 1
        assert v1.batches[0].batch_no == "VB-001"
        assert v1.batches[0].quantity == 130
        # 确认单当前态指向新版本
        assert confirmed_2.current_version_id == v2.id
        assert confirmed_2.committed_quantity == 100
        assert confirmed_2.shortage_quantity == 30

    def test_version_contains_batches_and_signature(self, db_session):
        """版本包含分批到货计划与供应商签署依据"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-VER-002")

        confirmed = _confirm(
            db_session, confirmation.id, 100,
            date.today() + timedelta(days=20),
            note="产能紧张，分两批交付",
            signed_by="张经理",
            signature_basis="合同HT-2026-001+邮件确认",
            batches=[
                SupplierConfirmationBatchCreate(
                    batch_no="VB-SIGN-1", quantity=60,
                    planned_date=date.today() + timedelta(days=15), remark="首批"
                ),
                SupplierConfirmationBatchCreate(
                    batch_no="VB-SIGN-2", quantity=40,
                    planned_date=date.today() + timedelta(days=20), remark="二批"
                )
            ]
        )

        version = crud_supplier_commitment_version.get_active_by_confirmation(
            db_session, confirmation.id
        )
        assert version is not None
        assert version.id == confirmed.current_version_id
        assert version.signed_by == "张经理"
        assert version.signature_basis == "合同HT-2026-001+邮件确认"
        assert version.signed_at is not None
        assert len(version.batches) == 2
        assert sum(b.quantity for b in version.batches) == 100
        # 旧分批表同步为当前版本镜像，既有查询行为不变
        mirror_batches = crud_supplier_confirmation_batch.get_by_confirmation(
            db_session, confirmation.id
        )
        assert len(mirror_batches) == 2
        assert {b.batch_no for b in mirror_batches} == {"VB-SIGN-1", "VB-SIGN-2"}

    def test_old_version_snapshot_not_overwritten_by_reconfirm(self, db_session):
        """再次确认后旧版本的分批计划与签署依据保持原样"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-VER-003")

        _confirm(
            db_session, confirmation.id, 130,
            date.today() + timedelta(days=10),
            signed_by="张经理", signature_basis="合同HT-001",
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-KEEP-1", quantity=130,
                planned_date=date.today() + timedelta(days=10)
            )]
        )
        _confirm(
            db_session, confirmation.id, 90,
            date.today() + timedelta(days=18),
            signed_by="李厂长", signature_basis="合同HT-002",
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-KEEP-2", quantity=90,
                planned_date=date.today() + timedelta(days=18)
            )]
        )

        versions = crud_supplier_commitment_version.get_by_confirmation(
            db_session, confirmation.id
        )
        v1 = versions[0]
        assert v1.signed_by == "张经理"
        assert v1.signature_basis == "合同HT-001"
        assert v1.committed_quantity == 130
        assert [b.batch_no for b in v1.batches] == ["VB-KEEP-1"]
        v2 = versions[1]
        assert v2.signed_by == "李厂长"
        assert [b.batch_no for b in v2.batches] == ["VB-KEEP-2"]


class TestWithdrawCommitmentVersion:
    """撤回错误确认：回滚到上一版本并重算影响"""

    def test_withdraw_active_version_rolls_back(self, db_session):
        """撤回当前版本后回滚到上一版本，影响记录恢复且保持稳定"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-WD-001")

        # V1：承诺50，缺口80，产生短缺影响
        _confirm(
            db_session, confirmation.id, 50,
            date.today() + timedelta(days=15),
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-WD-1", quantity=50,
                planned_date=date.today() + timedelta(days=15)
            )]
        )
        impacts_v1 = crud_supplier_shortage_impact.get_by_confirmation(
            db_session, confirmation.id
        )
        assert len(impacts_v1) > 0
        impact_ids_v1 = sorted(i.id for i in impacts_v1)

        # V2：承诺全量，无缺口
        confirmed_2 = _confirm(
            db_session, confirmation.id, 130,
            date.today() + timedelta(days=10),
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-WD-2", quantity=130,
                planned_date=date.today() + timedelta(days=10)
            )]
        )
        assert confirmed_2.shortage_quantity == 0
        assert len(crud_supplier_shortage_impact.get_by_confirmation(
            db_session, confirmation.id
        )) == 0

        # 撤回V2（错误确认），应回滚到V1
        rolled_back = SupplierConfirmationService.withdraw_version(
            db_session, confirmation.id, reason="误操作，撤回重新确认"
        )

        assert rolled_back.committed_quantity == 50
        assert rolled_back.shortage_quantity == 80
        assert rolled_back.status == "shortage"
        versions = crud_supplier_commitment_version.get_by_confirmation(
            db_session, confirmation.id
        )
        v1, v2 = versions
        assert v1.status == "active", "撤回后上一版本应恢复生效"
        assert v2.status == "withdrawn"
        assert v2.withdraw_reason == "误操作，撤回重新确认"
        assert rolled_back.current_version_id == v1.id
        # 影响记录恢复到V1时的结果（记录id保持稳定）
        impacts_after = crud_supplier_shortage_impact.get_by_confirmation(
            db_session, confirmation.id
        )
        assert sorted(i.id for i in impacts_after) == impact_ids_v1
        # 分批镜像也恢复为V1的计划
        mirror_batches = crud_supplier_confirmation_batch.get_by_confirmation(
            db_session, confirmation.id
        )
        assert [b.batch_no for b in mirror_batches] == ["VB-WD-1"]

    def test_withdraw_only_version_returns_to_pending(self, db_session):
        """撤回唯一版本后确认单回到待确认，可再次确认"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-WD-002")

        _confirm(db_session, confirmation.id, 100, date.today() + timedelta(days=15))
        withdrawn = SupplierConfirmationService.withdraw_version(
            db_session, confirmation.id, reason="供应商撤回承诺"
        )

        assert withdrawn.status == "pending"
        assert withdrawn.committed_quantity == 0
        assert withdrawn.shortage_quantity == 0
        assert withdrawn.current_version_id is None
        versions = crud_supplier_commitment_version.get_by_confirmation(
            db_session, confirmation.id
        )
        assert len(versions) == 1
        assert versions[0].status == "withdrawn"

        # 回到待确认后可再次确认，版本号继续递增
        reconfirmed = _confirm(
            db_session, confirmation.id, 120, date.today() + timedelta(days=12)
        )
        assert reconfirmed.status == "shortage"
        versions = crud_supplier_commitment_version.get_by_confirmation(
            db_session, confirmation.id
        )
        assert len(versions) == 2
        assert versions[-1].version_no == 2
        assert versions[-1].status == "active"

    def test_withdraw_twice_rejected(self, db_session):
        """重复撤回同一版本应报错"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-WD-003")

        _confirm(db_session, confirmation.id, 100, date.today() + timedelta(days=15))
        SupplierConfirmationService.withdraw_version(db_session, confirmation.id)
        with pytest.raises(ValueError, match="已撤回"):
            SupplierConfirmationService.withdraw_version(
                db_session, confirmation.id, version_no=1
            )

    def test_withdraw_without_active_version_rejected(self, db_session):
        """没有生效版本时不允许撤回"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-WD-004")

        with pytest.raises(ValueError, match="可撤回"):
            SupplierConfirmationService.withdraw_version(db_session, confirmation.id)


class TestDeliveredQuantityLocked:
    """已完成到货从可调整范围中扣除"""

    def test_committed_quantity_cannot_below_delivered(self, db_session):
        """已到货部分不可调整：新承诺数量不得低于已到货量"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()

        suggestions = PurchaseService.generate_purchase_suggestions(db_session)
        carbon_suggestion = next(
            s for s in suggestions if s.material and s.material.code == "TM001"
        )
        # 先转单（建议需处于pending），再创建确认
        po = PurchaseService.convert_suggestion_to_order(
            db_session,
            suggestion_id=carbon_suggestion.id,
            order_no="TEST-PO-LOCK-001",
            quantity=130,
            expected_date=date.today() + timedelta(days=10)
        )
        assert po.purchase_suggestion_id == carbon_suggestion.id
        factory.purchase_orders["TEST-PO-LOCK-001"] = po

        confirmation = SupplierConfirmationService.create_confirmation_from_suggestion(
            db_session,
            suggestion_id=carbon_suggestion.id,
            confirmation_no="TEST-CONF-LOCK-001",
            supplier_id=factory.suppliers["TS001"].id
        )
        _confirm(
            db_session, confirmation.id, 130,
            date.today() + timedelta(days=10),
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-LOCK-1", quantity=130,
                planned_date=date.today() + timedelta(days=10)
            )]
        )

        # 供应商完成到货60件
        factory.create_delivery(
            "TEST-DEL-LOCK-001", "TEST-PO-LOCK-001", "TS001", "TM001",
            quantity=60, delivery_date=date.today()
        )

        # 新承诺低于已到货量：拒绝
        with pytest.raises(ValueError, match="不可调整"):
            _confirm(db_session, confirmation.id, 50, date.today() + timedelta(days=15))

        # 新承诺覆盖已到货量：允许，版本记录锁定的到货量
        confirmed = _confirm(
            db_session, confirmation.id, 100,
            date.today() + timedelta(days=15),
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-LOCK-2", quantity=100,
                planned_date=date.today() + timedelta(days=15)
            )]
        )
        active_version = crud_supplier_commitment_version.get_active_by_confirmation(
            db_session, confirmation.id
        )
        assert active_version.id == confirmed.current_version_id
        assert active_version.locked_delivered_quantity == 60


class TestStableRecalculation:
    """多采购单共同覆盖缺口、复算重复执行时必须得到稳定结果"""

    def test_multiple_orders_cover_gap_and_recalc_is_stable(self, db_session):
        """多张采购单共同覆盖缺口时，重复复算结果（含记录id）完全一致"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()

        # 两张在途采购单共同覆盖同一物料缺口
        factory.create_purchase_order(
            "TEST-PO-COVER-1", "TS001", "TM001",
            quantity=40, expected_date=date.today() + timedelta(days=8)
        )
        factory.create_purchase_order(
            "TEST-PO-COVER-2", "TS001", "TM001",
            quantity=40, expected_date=date.today() + timedelta(days=12)
        )

        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-STABLE-001")
        _confirm(
            db_session, confirmation.id, 30,
            date.today() + timedelta(days=20),
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-STABLE-1", quantity=30,
                planned_date=date.today() + timedelta(days=20)
            )]
        )

        impacts_1 = crud_supplier_shortage_impact.get_by_confirmation(
            db_session, confirmation.id
        )
        assert len(impacts_1) > 0
        snapshot_1 = sorted(
            (i.id, i.production_batch_id, i.shortage_quantity, i.impact_level)
            for i in impacts_1
        )

        # 重复执行复算任务，结果必须稳定（记录id与内容都不变）
        for _ in range(3):
            SupplierConfirmationService._recalculate_shortage_impact(
                db_session, confirmation.id
            )
            impacts_n = crud_supplier_shortage_impact.get_by_confirmation(
                db_session, confirmation.id
            )
            snapshot_n = sorted(
                (i.id, i.production_batch_id, i.shortage_quantity, i.impact_level)
                for i in impacts_n
            )
            assert snapshot_n == snapshot_1, "重复复算必须得到稳定结果"

        # 在途40+40与承诺30共同覆盖130需求：TB001(50台)缺口10由短缺承担
        tb001 = factory.production_batches["TB001"]
        impact_tb001 = next(
            (i for i in impacts_1 if i.production_batch_id == tb001.id), None
        )
        assert impact_tb001 is not None
        assert impact_tb001.shortage_quantity == 10

    def test_recalc_tracks_triggering_version(self, db_session):
        """短缺影响可追溯到触发它的承诺版本"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-TRACE-001")

        _confirm(db_session, confirmation.id, 130, date.today() + timedelta(days=10))
        confirmed_2 = _confirm(
            db_session, confirmation.id, 50, date.today() + timedelta(days=20)
        )

        impacts = crud_supplier_shortage_impact.get_by_confirmation(
            db_session, confirmation.id
        )
        assert len(impacts) > 0
        for impact in impacts:
            assert impact.version_id == confirmed_2.current_version_id, \
                "影响记录应关联触发它的承诺版本"
            assert impact.calc_status == "current"
        # 历史快照中无V1记录（V1无短缺），当前影响全部来自V2
        history = crud_supplier_shortage_impact.get_history_by_confirmation(
            db_session, confirmation.id
        )
        assert len(history) == len(impacts)


class TestVersionRegressionAndDiff:
    """承诺版本倒退与新旧版本差异查询"""

    def test_version_regression_recalculates_impacts(self, db_session):
        """版本倒退（承诺减少+日期延后）时重新计算受影响批次"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-DIFF-001")

        # V1：全额按时承诺，无影响
        _confirm(
            db_session, confirmation.id, 130,
            date.today() + timedelta(days=10),
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-DIFF-1", quantity=130,
                planned_date=date.today() + timedelta(days=10)
            )]
        )
        assert len(crud_supplier_shortage_impact.get_by_confirmation(
            db_session, confirmation.id
        )) == 0

        # V2：倒退为60件且延期10天
        _confirm(
            db_session, confirmation.id, 60,
            date.today() + timedelta(days=20),
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-DIFF-2", quantity=60,
                planned_date=date.today() + timedelta(days=20)
            )]
        )
        impacts = crud_supplier_shortage_impact.get_by_confirmation(
            db_session, confirmation.id
        )
        assert len(impacts) == 2, "缺口70应影响TB001(50)与TB002(20)"
        total_affected = sum(i.shortage_quantity for i in impacts)
        assert total_affected == 70

        diff = SupplierConfirmationService.get_version_diff(db_session, confirmation.id)
        assert diff.from_version.version_no == 1
        assert diff.to_version.version_no == 2
        assert diff.committed_quantity_change == -70
        assert diff.shortage_quantity_change == 70
        assert diff.delivery_date_change_days == 10
        # 分批计划差异：旧批次移除、新批次新增
        schedule = {c.batch_no: c for c in diff.batch_schedule_changes}
        assert schedule["VB-DIFF-1"].change_type == "removed"
        assert schedule["VB-DIFF-2"].change_type == "added"
        # 受影响批次差异：两个批次均为新增影响
        assert len(diff.affected_batches_added) == 2
        assert len(diff.affected_batches_removed) == 0
        assert len(diff.affected_batches_changed) == 0
        added_batch_nos = {b.production_batch_no for b in diff.affected_batches_added}
        assert added_batch_nos == {"TB001", "TB002"}

    def test_version_improvement_shows_removed_impacts(self, db_session):
        """版本改善（承诺增加）时差异展示移除的受影响批次"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-DIFF-002")

        # V1：承诺50，缺口80
        _confirm(
            db_session, confirmation.id, 50,
            date.today() + timedelta(days=15),
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-IMP-1", quantity=50,
                planned_date=date.today() + timedelta(days=15)
            )]
        )
        # V2：改善为130全额
        _confirm(
            db_session, confirmation.id, 130,
            date.today() + timedelta(days=10),
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-IMP-2", quantity=130,
                planned_date=date.today() + timedelta(days=10)
            )]
        )

        diff = SupplierConfirmationService.get_version_diff(db_session, confirmation.id)
        assert diff.committed_quantity_change == 80
        assert diff.shortage_quantity_change == -80
        assert diff.delivery_date_change_days == -5
        assert len(diff.affected_batches_removed) > 0
        assert len(diff.affected_batches_added) == 0
        removed = diff.affected_batches_removed[0]
        assert removed.old_shortage_quantity > 0
        assert removed.new_shortage_quantity == 0

    def test_diff_requires_two_versions(self, db_session):
        """只有一个版本时没有可对比的历史版本"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-DIFF-003")

        _confirm(db_session, confirmation.id, 130, date.today() + timedelta(days=10))
        with pytest.raises(ValueError, match="历史版本"):
            SupplierConfirmationService.get_version_diff(db_session, confirmation.id)


class TestVersionApiEndpoints:
    """版本查询、撤回与差异接口"""

    def test_version_endpoints(self, db_session, override_get_db):
        from fastapi.testclient import TestClient
        from app.main import app

        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        confirmation = _make_carbon_confirmation(db_session, factory, "TEST-CONF-API-001")
        _confirm(
            db_session, confirmation.id, 100,
            date.today() + timedelta(days=15),
            signed_by="张经理", signature_basis="合同HT-API-001",
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-API-1", quantity=100,
                planned_date=date.today() + timedelta(days=15)
            )]
        )
        _confirm(
            db_session, confirmation.id, 30,
            date.today() + timedelta(days=20),
            signed_by="李厂长", signature_basis="合同HT-API-002",
            batches=[SupplierConfirmationBatchCreate(
                batch_no="VB-API-2", quantity=30,
                planned_date=date.today() + timedelta(days=20)
            )]
        )

        client = TestClient(app)
        base = f"/api/v1/supplier-confirmations/{confirmation.id}"

        # 版本列表
        resp = client.get(f"{base}/versions")
        assert resp.status_code == 200
        versions = resp.json()
        assert len(versions) == 2
        assert versions[0]["status"] == "superseded"
        assert versions[1]["status"] == "active"
        assert versions[1]["signed_by"] == "李厂长"
        assert versions[1]["signature_basis"] == "合同HT-API-002"
        assert versions[1]["batches"][0]["batch_no"] == "VB-API-2"

        # 单个版本详情
        resp = client.get(f"{base}/versions/1")
        assert resp.status_code == 200
        assert resp.json()["committed_quantity"] == 100

        # 版本差异：V1缺口30（TB001影响30）→ V2缺口100（TB001影响50、TB002新增影响30）
        resp = client.get(f"{base}/version-diff")
        assert resp.status_code == 200
        diff = resp.json()
        assert diff["committed_quantity_change"] == -70
        assert diff["shortage_quantity_change"] == 70
        assert diff["delivery_date_change_days"] == 5
        assert len(diff["affected_batches_added"]) > 0
        assert diff["affected_batches_added"][0]["new_shortage_quantity"] > 0
        assert len(diff["affected_batches_changed"]) > 0
        changed = diff["affected_batches_changed"][0]
        assert changed["new_shortage_quantity"] > changed["old_shortage_quantity"]

        # 撤回当前版本
        resp = client.post(f"{base}/withdraw", json={"reason": "承诺录入错误"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["committed_quantity"] == 100
        assert body["status"] == "shortage"

        # 再次撤回（回滚后V1生效中）→ 撤回V1 → 回到待确认
        resp = client.post(f"{base}/versions/1/withdraw", json={"reason": "全部撤回"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"

        # 无生效版本时撤回报错
        resp = client.post(f"{base}/withdraw", json={})
        assert resp.status_code == 400

        # 不存在的版本
        resp = client.get(f"{base}/versions/99")
        assert resp.status_code == 404
