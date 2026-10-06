from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date
from app.database import get_db
from app.crud.supplier_confirmation import (
    crud_supplier_confirmation,
    crud_supplier_confirmation_batch,
    crud_supplier_commitment_version,
    crud_supplier_shortage_impact
)
from app.schemas import (
    SupplierConfirmation, SupplierConfirmationCreate, SupplierConfirmationUpdate,
    SupplierConfirmationConfirm, SupplierConfirmationBatch,
    SupplierShortageImpact, SupplierConfirmationStatistics,
    SupplierBottleneckAnalysis, SupplierConfirmationWithDetail,
    SupplierCommitmentVersion, SupplierCommitmentVersionWithdraw,
    SupplierCommitmentVersionDiff
)
from app.services.supplier_confirmation import SupplierConfirmationService

router = APIRouter(prefix="/supplier-confirmations", tags=["供应商协同确认"])

@router.get("", response_model=List[SupplierConfirmation])
def get_confirmations(
    status: Optional[str] = None,
    supplier_id: Optional[int] = None,
    material_id: Optional[int] = None,
    purchase_suggestion_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    if status:
        return crud_supplier_confirmation.get_by_status(db, status)
    if supplier_id:
        return crud_supplier_confirmation.get_by_supplier(db, supplier_id)
    if material_id:
        return crud_supplier_confirmation.get_by_material(db, material_id)
    if purchase_suggestion_id:
        return crud_supplier_confirmation.get_by_purchase_suggestion(db, purchase_suggestion_id)
    return crud_supplier_confirmation.get_multi(db)

@router.get("/pending", response_model=List[SupplierConfirmation])
def get_pending_confirmations(
    supplier_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    return SupplierConfirmationService.get_pending_confirmations(db, supplier_id)

@router.get("/shortage", response_model=List[SupplierConfirmation])
def get_shortage_confirmations(
    supplier_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    return SupplierConfirmationService.get_shortage_confirmations(db, supplier_id)

@router.get("/statistics", response_model=SupplierConfirmationStatistics)
def get_confirmation_statistics(db: Session = Depends(get_db)):
    return SupplierConfirmationService.get_confirmation_statistics(db)

@router.get("/bottlenecks", response_model=List[SupplierBottleneckAnalysis])
def get_supplier_bottlenecks(db: Session = Depends(get_db)):
    return SupplierConfirmationService.get_supplier_bottlenecks(db)

@router.get("/{confirmation_id}", response_model=SupplierConfirmationWithDetail)
def get_confirmation(confirmation_id: int, db: Session = Depends(get_db)):
    conf = crud_supplier_confirmation.get(db, confirmation_id)
    if not conf:
        raise HTTPException(status_code=404, detail="供应商确认不存在")
    return conf

@router.get("/{confirmation_id}/batches", response_model=List[SupplierConfirmationBatch])
def get_confirmation_batches(confirmation_id: int, db: Session = Depends(get_db)):
    conf = crud_supplier_confirmation.get(db, confirmation_id)
    if not conf:
        raise HTTPException(status_code=404, detail="供应商确认不存在")
    return crud_supplier_confirmation_batch.get_by_confirmation(db, confirmation_id)

@router.get("/{confirmation_id}/shortage-impacts", response_model=List[SupplierShortageImpact])
def get_confirmation_shortage_impacts(confirmation_id: int, db: Session = Depends(get_db)):
    conf = crud_supplier_confirmation.get(db, confirmation_id)
    if not conf:
        raise HTTPException(status_code=404, detail="供应商确认不存在")
    return crud_supplier_shortage_impact.get_by_confirmation(db, confirmation_id)

@router.get("/{confirmation_id}/versions", response_model=List[SupplierCommitmentVersion])
def get_confirmation_versions(confirmation_id: int, db: Session = Depends(get_db)):
    """承诺版本列表：每次确认形成的不可覆盖版本（含分批计划与签署依据）"""
    conf = crud_supplier_confirmation.get(db, confirmation_id)
    if not conf:
        raise HTTPException(status_code=404, detail="供应商确认不存在")
    return crud_supplier_commitment_version.get_by_confirmation(db, confirmation_id)

@router.get("/{confirmation_id}/versions/{version_no}", response_model=SupplierCommitmentVersion)
def get_confirmation_version(confirmation_id: int, version_no: int, db: Session = Depends(get_db)):
    version = crud_supplier_commitment_version.get_by_version_no(db, confirmation_id, version_no)
    if not version:
        raise HTTPException(status_code=404, detail="承诺版本不存在")
    return version

@router.get("/{confirmation_id}/version-diff", response_model=SupplierCommitmentVersionDiff)
def get_version_diff(
    confirmation_id: int,
    from_version_no: Optional[int] = None,
    to_version_no: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """版本差异：旧版与新版在可用日期、数量和受影响批次上的对比"""
    try:
        return SupplierConfirmationService.get_version_diff(
            db, confirmation_id, from_version_no, to_version_no
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{confirmation_id}/withdraw", response_model=SupplierConfirmation)
def withdraw_active_version(
    confirmation_id: int,
    withdraw_in: SupplierCommitmentVersionWithdraw,
    db: Session = Depends(get_db)
):
    """撤回当前生效的承诺版本，回滚到上一版本（无则回到待确认）并重算影响"""
    try:
        return SupplierConfirmationService.withdraw_version(
            db, confirmation_id, version_no=None, reason=withdraw_in.reason
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{confirmation_id}/versions/{version_no}/withdraw", response_model=SupplierConfirmation)
def withdraw_version(
    confirmation_id: int,
    version_no: int,
    withdraw_in: SupplierCommitmentVersionWithdraw,
    db: Session = Depends(get_db)
):
    """撤回指定承诺版本"""
    try:
        return SupplierConfirmationService.withdraw_version(
            db, confirmation_id, version_no=version_no, reason=withdraw_in.reason
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("", response_model=SupplierConfirmation)
def create_confirmation(confirmation_in: SupplierConfirmationCreate, db: Session = Depends(get_db)):
    existing = crud_supplier_confirmation.get_by_confirmation_no(db, confirmation_in.confirmation_no)
    if existing:
        raise HTTPException(status_code=400, detail="确认单号已存在")
    return crud_supplier_confirmation.create_with_batches(db, obj_in=confirmation_in)

@router.post("/from-suggestion", response_model=SupplierConfirmation)
def create_from_suggestion(
    suggestion_id: int,
    confirmation_no: str,
    supplier_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    try:
        return SupplierConfirmationService.create_confirmation_from_suggestion(
            db, suggestion_id, confirmation_no, supplier_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{confirmation_id}/confirm", response_model=SupplierConfirmation)
def supplier_confirm(
    confirmation_id: int,
    confirm_data: SupplierConfirmationConfirm,
    db: Session = Depends(get_db)
):
    try:
        return SupplierConfirmationService.supplier_confirm(db, confirmation_id, confirm_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{confirmation_id}/recalculate-shortage", response_model=List[SupplierShortageImpact])
def recalculate_shortage_impact(confirmation_id: int, db: Session = Depends(get_db)):
    conf = crud_supplier_confirmation.get(db, confirmation_id)
    if not conf:
        raise HTTPException(status_code=404, detail="供应商确认不存在")
    from app.services.supplier_confirmation import SupplierConfirmationService
    return SupplierConfirmationService._recalculate_shortage_impact(db, confirmation_id)

@router.put("/{confirmation_id}", response_model=SupplierConfirmation)
def update_confirmation(
    confirmation_id: int,
    confirmation_in: SupplierConfirmationUpdate,
    db: Session = Depends(get_db)
):
    db_obj = crud_supplier_confirmation.get(db, confirmation_id)
    if not db_obj:
        raise HTTPException(status_code=404, detail="供应商确认不存在")
    return crud_supplier_confirmation.update(db, db_obj=db_obj, obj_in=confirmation_in)
