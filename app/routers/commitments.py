from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from app.database import get_db
from app.crud.commitment import crud_commitment_version, crud_commitment_impact
from app.schemas import (
    CommitmentVersion, CommitmentVersionDetail, CommitmentVersionPublish,
    CommitmentVersionWithdraw, CommitmentImpact, CommitmentVersionDiff
)
from app.services.commitment import CommitmentService

router = APIRouter(prefix="/commitments", tags=["供应商承诺版本"])


def _get_version_or_404(db: Session, version_id: int):
    version = crud_commitment_version.get(db, version_id)
    if not version:
        raise HTTPException(status_code=404, detail="承诺版本不存在")
    return version


@router.post("/orders/{order_id}/versions", response_model=CommitmentVersionDetail)
def publish_version(
    order_id: int,
    publish_in: CommitmentVersionPublish,
    db: Session = Depends(get_db)
):
    """发布新的承诺版本：旧版本自动转为已取代，并按新版本重算物料缺口与受影响生产批次"""
    try:
        return CommitmentService.publish_version(db, order_id, publish_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders/{order_id}/versions", response_model=List[CommitmentVersion])
def list_versions(order_id: int, db: Session = Depends(get_db)):
    """采购单的承诺版本历史（含已取代、已撤回），版本内容不可覆盖"""
    return crud_commitment_version.get_by_order(db, order_id)


@router.get("/orders/{order_id}/current", response_model=CommitmentVersionDetail)
def get_current_version(order_id: int, db: Session = Depends(get_db)):
    """当前生效的承诺版本及其影响快照"""
    version = CommitmentService.get_current_version(db, order_id)
    if not version:
        raise HTTPException(status_code=404, detail="该采购订单当前无生效承诺版本")
    return version


@router.post("/orders/{order_id}/recalculate", response_model=List[CommitmentImpact])
def recalculate_order(order_id: int, db: Session = Depends(get_db)):
    """对当前生效版本重新计算影响快照，重复执行结果稳定一致"""
    try:
        return CommitmentService.recalculate_order(db, order_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders/{order_id}/impact-diff", response_model=CommitmentVersionDiff)
def diff_versions(
    order_id: int,
    from_version_id: Optional[int] = None,
    to_version_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """对比新旧两版承诺在可用日期、数量与受影响批次上的差异，默认对比当前版与上一版"""
    try:
        return CommitmentService.diff_versions(db, order_id, from_version_id, to_version_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/versions/{version_id}", response_model=CommitmentVersionDetail)
def get_version(version_id: int, db: Session = Depends(get_db)):
    return _get_version_or_404(db, version_id)


@router.get("/versions/{version_id}/impacts", response_model=List[CommitmentImpact])
def get_version_impacts(version_id: int, db: Session = Depends(get_db)):
    _get_version_or_404(db, version_id)
    return crud_commitment_impact.get_by_version(db, version_id)


@router.post("/versions/{version_id}/withdraw", response_model=CommitmentVersionDetail)
def withdraw_version(
    version_id: int,
    withdraw_in: CommitmentVersionWithdraw,
    db: Session = Depends(get_db)
):
    """撤回当前生效版本：自动回落到上一版并重算其影响"""
    try:
        return CommitmentService.withdraw_version(db, version_id, withdraw_in.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
