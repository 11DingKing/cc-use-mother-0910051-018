from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date
from app.crud.base import CRUDBase
from app.models import (
    SupplierConfirmation, SupplierConfirmationBatch, SupplierShortageImpact,
    SupplierCommitmentVersion, SupplierCommitmentVersionBatch
)
from app.schemas import (
    SupplierConfirmationCreate, SupplierConfirmationUpdate,
    SupplierConfirmationBatchCreate, SupplierShortageImpactCreate
)

class CRUDSupplierConfirmation(CRUDBase[SupplierConfirmation, SupplierConfirmationCreate, SupplierConfirmationUpdate]):
    def get_by_confirmation_no(self, db: Session, confirmation_no: str) -> Optional[SupplierConfirmation]:
        return db.query(SupplierConfirmation).filter(SupplierConfirmation.confirmation_no == confirmation_no).first()

    def get_by_supplier(self, db: Session, supplier_id: int) -> List[SupplierConfirmation]:
        return db.query(SupplierConfirmation).filter(SupplierConfirmation.supplier_id == supplier_id).all()

    def get_by_material(self, db: Session, material_id: int) -> List[SupplierConfirmation]:
        return db.query(SupplierConfirmation).filter(SupplierConfirmation.material_id == material_id).all()

    def get_by_purchase_suggestion(self, db: Session, suggestion_id: int) -> List[SupplierConfirmation]:
        return db.query(SupplierConfirmation).filter(SupplierConfirmation.purchase_suggestion_id == suggestion_id).all()

    def get_by_status(self, db: Session, status: str) -> List[SupplierConfirmation]:
        return db.query(SupplierConfirmation).filter(SupplierConfirmation.status == status).all()

    def get_shortage_confirmations(self, db: Session) -> List[SupplierConfirmation]:
        return db.query(SupplierConfirmation).filter(SupplierConfirmation.shortage_quantity > 0).all()

    def create_with_batches(self, db: Session, *, obj_in: SupplierConfirmationCreate) -> SupplierConfirmation:
        obj_in_data = obj_in.model_dump()
        batches_data = obj_in_data.pop("batches", [])
        db_obj = SupplierConfirmation(**obj_in_data)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        for batch_data in batches_data:
            batch = SupplierConfirmationBatch(
                confirmation_id=db_obj.id,
                **batch_data
            )
            db.add(batch)
        if batches_data:
            db.commit()
            db.refresh(db_obj)
        return db_obj

crud_supplier_confirmation = CRUDSupplierConfirmation(SupplierConfirmation)

class CRUDSupplierConfirmationBatch(CRUDBase[SupplierConfirmationBatch, SupplierConfirmationBatchCreate, dict]):
    def get_by_confirmation(self, db: Session, confirmation_id: int) -> List[SupplierConfirmationBatch]:
        return db.query(SupplierConfirmationBatch).filter(SupplierConfirmationBatch.confirmation_id == confirmation_id).all()

    def get_by_batch_no(self, db: Session, batch_no: str) -> List[SupplierConfirmationBatch]:
        return db.query(SupplierConfirmationBatch).filter(SupplierConfirmationBatch.batch_no == batch_no).all()

crud_supplier_confirmation_batch = CRUDSupplierConfirmationBatch(SupplierConfirmationBatch)

class CRUDSupplierCommitmentVersion(CRUDBase[SupplierCommitmentVersion, dict, dict]):
    def get_by_confirmation(self, db: Session, confirmation_id: int) -> List[SupplierCommitmentVersion]:
        return db.query(SupplierCommitmentVersion).filter(
            SupplierCommitmentVersion.confirmation_id == confirmation_id
        ).order_by(SupplierCommitmentVersion.version_no).all()

    def get_active_by_confirmation(self, db: Session, confirmation_id: int) -> Optional[SupplierCommitmentVersion]:
        return db.query(SupplierCommitmentVersion).filter(
            SupplierCommitmentVersion.confirmation_id == confirmation_id,
            SupplierCommitmentVersion.status == "active"
        ).first()

    def get_by_version_no(self, db: Session, confirmation_id: int, version_no: int) -> Optional[SupplierCommitmentVersion]:
        return db.query(SupplierCommitmentVersion).filter(
            SupplierCommitmentVersion.confirmation_id == confirmation_id,
            SupplierCommitmentVersion.version_no == version_no
        ).first()

    def get_next_version_no(self, db: Session, confirmation_id: int) -> int:
        versions = self.get_by_confirmation(db, confirmation_id)
        return (versions[-1].version_no + 1) if versions else 1

crud_supplier_commitment_version = CRUDSupplierCommitmentVersion(SupplierCommitmentVersion)

class CRUDSupplierCommitmentVersionBatch(CRUDBase[SupplierCommitmentVersionBatch, dict, dict]):
    def get_by_version(self, db: Session, version_id: int) -> List[SupplierCommitmentVersionBatch]:
        return db.query(SupplierCommitmentVersionBatch).filter(
            SupplierCommitmentVersionBatch.version_id == version_id
        ).all()

crud_supplier_commitment_version_batch = CRUDSupplierCommitmentVersionBatch(SupplierCommitmentVersionBatch)

class CRUDSupplierShortageImpact(CRUDBase[SupplierShortageImpact, SupplierShortageImpactCreate, dict]):
    def get_by_confirmation(self, db: Session, confirmation_id: int) -> List[SupplierShortageImpact]:
        """当前生效的短缺影响（历史版本快照不返回）"""
        return db.query(SupplierShortageImpact).filter(
            SupplierShortageImpact.confirmation_id == confirmation_id,
            SupplierShortageImpact.calc_status == "current"
        ).all()

    def get_history_by_confirmation(self, db: Session, confirmation_id: int) -> List[SupplierShortageImpact]:
        """全部短缺影响（含历史版本快照），按版本与id排序保证稳定"""
        return db.query(SupplierShortageImpact).filter(
            SupplierShortageImpact.confirmation_id == confirmation_id
        ).order_by(SupplierShortageImpact.version_id, SupplierShortageImpact.id).all()

    def get_by_version(self, db: Session, version_id: int) -> List[SupplierShortageImpact]:
        return db.query(SupplierShortageImpact).filter(
            SupplierShortageImpact.version_id == version_id
        ).order_by(SupplierShortageImpact.id).all()

    def get_by_production_batch(self, db: Session, production_batch_id: int) -> List[SupplierShortageImpact]:
        return db.query(SupplierShortageImpact).filter(
            SupplierShortageImpact.production_batch_id == production_batch_id,
            SupplierShortageImpact.calc_status == "current"
        ).all()

    def get_by_supplier(self, db: Session, supplier_id: int) -> List[SupplierShortageImpact]:
        return db.query(SupplierShortageImpact).join(
            SupplierConfirmation, SupplierConfirmation.id == SupplierShortageImpact.confirmation_id
        ).filter(
            SupplierConfirmation.supplier_id == supplier_id,
            SupplierShortageImpact.calc_status == "current"
        ).all()

    def delete_by_confirmation(self, db: Session, confirmation_id: int) -> int:
        deleted = db.query(SupplierShortageImpact).filter(SupplierShortageImpact.confirmation_id == confirmation_id).delete()
        db.commit()
        return deleted

crud_supplier_shortage_impact = CRUDSupplierShortageImpact(SupplierShortageImpact)
