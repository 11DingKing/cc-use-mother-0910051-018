from sqlalchemy.orm import Session
from typing import List, Optional, Dict
from app.crud.base import CRUDBase
from app.models import CommitmentVersion, CommitmentVersionBatch, CommitmentImpact
from app.schemas import CommitmentVersionPublish, CommitmentVersionBatchCreate

class CRUDCommitmentVersion(CRUDBase[CommitmentVersion, CommitmentVersionPublish, dict]):
    def get_by_version_no(self, db: Session, version_no: str) -> Optional[CommitmentVersion]:
        return db.query(CommitmentVersion).filter(CommitmentVersion.version_no == version_no).first()

    def get_by_order(self, db: Session, purchase_order_id: int) -> List[CommitmentVersion]:
        return db.query(CommitmentVersion).filter(
            CommitmentVersion.purchase_order_id == purchase_order_id
        ).order_by(CommitmentVersion.version_number).all()

    def get_effective_by_order(self, db: Session, purchase_order_id: int) -> Optional[CommitmentVersion]:
        return db.query(CommitmentVersion).filter(
            CommitmentVersion.purchase_order_id == purchase_order_id,
            CommitmentVersion.status == "published"
        ).order_by(CommitmentVersion.version_number.desc()).first()

    def get_max_version_number(self, db: Session, purchase_order_id: int) -> int:
        versions = db.query(CommitmentVersion).filter(
            CommitmentVersion.purchase_order_id == purchase_order_id
        ).all()
        return max((v.version_number for v in versions), default=0)

    def get_latest_superseded(self, db: Session, purchase_order_id: int) -> Optional[CommitmentVersion]:
        return db.query(CommitmentVersion).filter(
            CommitmentVersion.purchase_order_id == purchase_order_id,
            CommitmentVersion.status == "superseded"
        ).order_by(CommitmentVersion.version_number.desc()).first()

    def get_previous_version(self, db: Session, version: CommitmentVersion) -> Optional[CommitmentVersion]:
        return db.query(CommitmentVersion).filter(
            CommitmentVersion.purchase_order_id == version.purchase_order_id,
            CommitmentVersion.version_number < version.version_number
        ).order_by(CommitmentVersion.version_number.desc()).first()

crud_commitment_version = CRUDCommitmentVersion(CommitmentVersion)

class CRUDCommitmentVersionBatch(CRUDBase[CommitmentVersionBatch, CommitmentVersionBatchCreate, dict]):
    def get_by_version(self, db: Session, version_id: int) -> List[CommitmentVersionBatch]:
        return db.query(CommitmentVersionBatch).filter(
            CommitmentVersionBatch.version_id == version_id
        ).order_by(CommitmentVersionBatch.planned_date, CommitmentVersionBatch.id).all()

crud_commitment_version_batch = CRUDCommitmentVersionBatch(CommitmentVersionBatch)

class CRUDCommitmentImpact(CRUDBase[CommitmentImpact, dict, dict]):
    def get_by_version(self, db: Session, version_id: int) -> List[CommitmentImpact]:
        return db.query(CommitmentImpact).filter(
            CommitmentImpact.version_id == version_id
        ).order_by(CommitmentImpact.production_batch_id).all()

    def get_by_version_map(self, db: Session, version_id: int) -> Dict[int, CommitmentImpact]:
        return {imp.production_batch_id: imp for imp in self.get_by_version(db, version_id)}

    def get_by_production_batch(self, db: Session, production_batch_id: int) -> List[CommitmentImpact]:
        return db.query(CommitmentImpact).filter(
            CommitmentImpact.production_batch_id == production_batch_id
        ).all()

crud_commitment_impact = CRUDCommitmentImpact(CommitmentImpact)
