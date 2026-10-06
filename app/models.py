from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text, Date, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base

class Material(Base):
    __tablename__ = "materials"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    category = Column(String(50), nullable=False)
    spec = Column(String(200))
    unit = Column(String(20), nullable=False)
    safety_stock = Column(Integer, default=0)
    is_critical = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    bom_items = relationship("BOMItem", back_populates="material")
    supply_capacities = relationship("SupplyCapacity", back_populates="material")
    purchase_suggestions = relationship("PurchaseSuggestion", back_populates="material")
    purchase_orders = relationship("PurchaseOrder", back_populates="material")
    deliveries = relationship("Delivery", back_populates="material")
    inventory_batches = relationship("InventoryBatch", back_populates="material")
    alternative_materials = relationship("AlternativeMaterial", 
                                         foreign_keys="AlternativeMaterial.material_id", 
                                         back_populates="material")
    alternative_for = relationship("AlternativeMaterial",
                                   foreign_keys="AlternativeMaterial.alternative_material_id",
                                   back_populates="alternative_material")

class VehicleModel(Base):
    __tablename__ = "vehicle_models"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    priority = Column(Integer, default=5)
    description = Column(Text)
    status = Column(String(20), default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    bom_items = relationship("BOMItem", back_populates="vehicle_model")
    production_batches = relationship("ProductionBatch", back_populates="vehicle_model")
    alternative_restrictions = relationship("AlternativeMaterialRestriction", back_populates="vehicle_model")

class BOMItem(Base):
    __tablename__ = "bom_items"
    id = Column(Integer, primary_key=True, index=True)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    remark = Column(String(200))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vehicle_model = relationship("VehicleModel", back_populates="bom_items")
    material = relationship("Material", back_populates="bom_items")

class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    contact = Column(String(50))
    phone = Column(String(30))
    address = Column(String(300))
    rating = Column(Float, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    supply_capacities = relationship("SupplyCapacity", back_populates="supplier")
    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")
    deliveries = relationship("Delivery", back_populates="supplier")

class SupplyCapacity(Base):
    __tablename__ = "supply_capacities"
    id = Column(Integer, primary_key=True, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    daily_capacity = Column(Integer, nullable=False)
    delivery_days = Column(Integer, nullable=False)
    pass_rate = Column(Float, nullable=False)
    current_stock = Column(Integer, default=0)
    unit_price = Column(Float, default=0)
    is_preferred = Column(Boolean, default=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    supplier = relationship("Supplier", back_populates="supply_capacities")
    material = relationship("Material", back_populates="supply_capacities")

class PurchaseSuggestion(Base):
    __tablename__ = "purchase_suggestions"
    id = Column(Integer, primary_key=True, index=True)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    suggested_quantity = Column(Integer, nullable=False)
    reason = Column(String(300))
    priority = Column(Integer, default=5)
    suggested_supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    expected_delivery_date = Column(Date)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    material = relationship("Material", back_populates="purchase_suggestions")

class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String(50), unique=True, index=True, nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    expected_date = Column(Date, nullable=False)
    actual_date = Column(Date)
    status = Column(String(20), default="ordered")
    remark = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    supplier = relationship("Supplier", back_populates="purchase_orders")
    material = relationship("Material", back_populates="purchase_orders")
    deliveries = relationship("Delivery", back_populates="purchase_order")
    delay_impacts = relationship("DelayImpact", back_populates="purchase_order")
    commitment_versions = relationship("CommitmentVersion", back_populates="purchase_order")

class Delivery(Base):
    __tablename__ = "deliveries"
    id = Column(Integer, primary_key=True, index=True)
    delivery_no = Column(String(50), unique=True, index=True, nullable=False)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    delivery_date = Column(Date, nullable=False)
    batch_no = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    purchase_order = relationship("PurchaseOrder", back_populates="deliveries")
    supplier = relationship("Supplier", back_populates="deliveries")
    material = relationship("Material", back_populates="deliveries")
    inspection = relationship("Inspection", back_populates="delivery", uselist=False)
    inventory_batch = relationship("InventoryBatch", back_populates="delivery", uselist=False)

class Inspection(Base):
    __tablename__ = "inspections"
    id = Column(Integer, primary_key=True, index=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=False)
    sample_size = Column(Integer, nullable=False)
    defective_count = Column(Integer, default=0)
    pass_rate = Column(Float, nullable=False)
    result = Column(String(20), nullable=False)
    inspector = Column(String(50))
    inspection_date = Column(Date, nullable=False)
    remark = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    delivery = relationship("Delivery", back_populates="inspection")

class InventoryBatch(Base):
    __tablename__ = "inventory_batches"
    id = Column(Integer, primary_key=True, index=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    available_quantity = Column(Integer, nullable=False)
    is_quarantined = Column(Boolean, default=False)
    quarantine_reason = Column(String(300))
    location = Column(String(100))
    expire_date = Column(Date)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    delivery = relationship("Delivery", back_populates="inventory_batch")
    material = relationship("Material", back_populates="inventory_batches")

class AlternativeMaterial(Base):
    __tablename__ = "alternative_materials"
    id = Column(Integer, primary_key=True, index=True)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    alternative_material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    priority = Column(Integer, default=1)
    is_active = Column(Boolean, default=True)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    material = relationship("Material", foreign_keys=[material_id], back_populates="alternative_materials")
    alternative_material = relationship("Material", foreign_keys=[alternative_material_id], back_populates="alternative_for")
    restrictions = relationship("AlternativeMaterialRestriction", back_populates="alternative")

class AlternativeMaterialRestriction(Base):
    __tablename__ = "alternative_restrictions"
    id = Column(Integer, primary_key=True, index=True)
    alternative_id = Column(Integer, ForeignKey("alternative_materials.id"), nullable=False)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    is_allowed = Column(Boolean, default=False)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    alternative = relationship("AlternativeMaterial", back_populates="restrictions")
    vehicle_model = relationship("VehicleModel", back_populates="alternative_restrictions")

class ProductionBatch(Base):
    __tablename__ = "production_batches"
    id = Column(Integer, primary_key=True, index=True)
    batch_no = Column(String(50), unique=True, index=True, nullable=False)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    plan_date = Column(Date, nullable=False)
    status = Column(String(20), default="planned")
    remark = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vehicle_model = relationship("VehicleModel", back_populates="production_batches")
    delay_impacts = relationship("DelayImpact", back_populates="production_batch")

class DelayImpact(Base):
    __tablename__ = "delay_impacts"
    id = Column(Integer, primary_key=True, index=True)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    production_batch_id = Column(Integer, ForeignKey("production_batches.id"), nullable=False)
    impact_level = Column(String(20), nullable=False)
    estimated_delay_days = Column(Integer, default=0)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    purchase_order = relationship("PurchaseOrder", back_populates="delay_impacts")
    production_batch = relationship("ProductionBatch", back_populates="delay_impacts")

class SupplierConfirmation(Base):
    __tablename__ = "supplier_confirmations"
    id = Column(Integer, primary_key=True, index=True)
    confirmation_no = Column(String(50), unique=True, index=True, nullable=False)
    purchase_suggestion_id = Column(Integer, ForeignKey("purchase_suggestions.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    requested_quantity = Column(Integer, nullable=False)
    committed_quantity = Column(Integer, nullable=False)
    committed_delivery_date = Column(Date)
    shortage_quantity = Column(Integer, default=0)
    status = Column(String(20), default="pending")
    confirmation_note = Column(Text)
    confirmed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    supplier = relationship("Supplier")
    material = relationship("Material")
    purchase_suggestion = relationship("PurchaseSuggestion")
    batches = relationship("SupplierConfirmationBatch", back_populates="confirmation", cascade="all, delete-orphan")
    shortage_impacts = relationship("SupplierShortageImpact", back_populates="confirmation", cascade="all, delete-orphan")

class SupplierConfirmationBatch(Base):
    __tablename__ = "supplier_confirmation_batches"
    id = Column(Integer, primary_key=True, index=True)
    confirmation_id = Column(Integer, ForeignKey("supplier_confirmations.id"), nullable=False)
    batch_no = Column(String(50), nullable=False)
    quantity = Column(Integer, nullable=False)
    planned_date = Column(Date, nullable=False)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    confirmation = relationship("SupplierConfirmation", back_populates="batches")

class SupplierShortageImpact(Base):
    __tablename__ = "supplier_shortage_impacts"
    id = Column(Integer, primary_key=True, index=True)
    confirmation_id = Column(Integer, ForeignKey("supplier_confirmations.id"), nullable=False)
    production_batch_id = Column(Integer, ForeignKey("production_batches.id"), nullable=False)
    affected_vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    shortage_material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    shortage_quantity = Column(Integer, nullable=False)
    impact_level = Column(String(20), nullable=False)
    estimated_delay_days = Column(Integer, default=0)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    confirmation = relationship("SupplierConfirmation", back_populates="shortage_impacts")
    production_batch = relationship("ProductionBatch")
    vehicle_model = relationship("VehicleModel")
    material = relationship("Material")

class CommitmentVersion(Base):
    """供应商承诺版本：每次承诺不可覆盖，含签署依据与分批到货计划"""
    __tablename__ = "commitment_versions"
    id = Column(Integer, primary_key=True, index=True)
    version_no = Column(String(60), unique=True, index=True, nullable=False)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    version_number = Column(Integer, nullable=False)
    committed_quantity = Column(Integer, nullable=False)
    committed_delivery_date = Column(Date)
    delivered_quantity = Column(Integer, default=0)
    adjustable_quantity = Column(Integer, default=0)
    status = Column(String(20), default="published")
    signed_by = Column(String(50), nullable=False)
    signature_doc = Column(String(200), nullable=False)
    signed_at = Column(DateTime(timezone=True))
    note = Column(Text)
    published_at = Column(DateTime(timezone=True))
    withdrawn_at = Column(DateTime(timezone=True))
    withdraw_reason = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    purchase_order = relationship("PurchaseOrder", back_populates="commitment_versions")
    batches = relationship("CommitmentVersionBatch", back_populates="version",
                           cascade="all, delete-orphan",
                           order_by="CommitmentVersionBatch.planned_date")
    impacts = relationship("CommitmentImpact", back_populates="version",
                           cascade="all, delete-orphan",
                           order_by="CommitmentImpact.production_batch_id")

    __table_args__ = (
        UniqueConstraint("purchase_order_id", "version_number", name="uq_commitment_version_order_number"),
    )

class CommitmentVersionBatch(Base):
    """承诺版本的分批到货计划，随版本固化不可修改"""
    __tablename__ = "commitment_version_batches"
    id = Column(Integer, primary_key=True, index=True)
    version_id = Column(Integer, ForeignKey("commitment_versions.id"), nullable=False)
    batch_no = Column(String(50), nullable=False)
    quantity = Column(Integer, nullable=False)
    planned_date = Column(Date, nullable=False)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    version = relationship("CommitmentVersion", back_populates="batches")

class CommitmentImpact(Base):
    """承诺版本生效时重算出的物料缺口与受影响生产批次快照"""
    __tablename__ = "commitment_impacts"
    id = Column(Integer, primary_key=True, index=True)
    version_id = Column(Integer, ForeignKey("commitment_versions.id"), nullable=False)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    production_batch_id = Column(Integer, ForeignKey("production_batches.id"), nullable=False)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    required_quantity = Column(Integer, nullable=False)
    covered_quantity = Column(Integer, nullable=False)
    shortage_quantity = Column(Integer, nullable=False)
    available_date = Column(Date)
    delay_days = Column(Integer)
    impact_level = Column(String(20), nullable=False)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    version = relationship("CommitmentVersion", back_populates="impacts")
    production_batch = relationship("ProductionBatch")
    vehicle_model = relationship("VehicleModel")
    material = relationship("Material")

    __table_args__ = (
        UniqueConstraint("version_id", "production_batch_id", name="uq_commitment_impact_version_batch"),
    )
