from fastapi import FastAPI
from app.config import settings
from app.database import engine, Base, get_db
from app.routers import materials, vehicles, suppliers, purchase, alternatives, statistics
from app.routers import supplier_confirmations
from app.data.seed import seed_all

Base.metadata.create_all(bind=engine)

def _run_lightweight_migrations():
    """为已存在的SQLite库补充新增列（新表由create_all自动创建）"""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    new_columns = {
        "supplier_confirmations": {"current_version_id": "INTEGER"},
        "supplier_shortage_impacts": {
            "version_id": "INTEGER",
            "calc_status": "VARCHAR(20) DEFAULT 'current'"
        },
        "purchase_orders": {"purchase_suggestion_id": "INTEGER"},
    }
    with engine.begin() as conn:
        for table, columns in new_columns.items():
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for column, column_type in columns.items():
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))

_run_lightweight_migrations()

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="国产自行车零部件供应协同系统 - 从一颗滚珠到整套飞轮，零部件供应协同平台",
    version="1.0.0"
)

app.include_router(materials.router, prefix=settings.API_V1_STR)
app.include_router(vehicles.router, prefix=settings.API_V1_STR)
app.include_router(suppliers.router, prefix=settings.API_V1_STR)
app.include_router(purchase.router, prefix=settings.API_V1_STR)
app.include_router(alternatives.router, prefix=settings.API_V1_STR)
app.include_router(statistics.router, prefix=settings.API_V1_STR)
app.include_router(supplier_confirmations.router, prefix=settings.API_V1_STR)

@app.on_event("startup")
def startup_event():
    db = next(get_db())
    try:
        seed_all(db)
    finally:
        db.close()

@app.get("/")
def root():
    return {
        "message": "欢迎使用国产自行车零部件供应协同系统",
        "version": "1.0.0",
        "docs_url": "/docs",
        "api_prefix": settings.API_V1_STR
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
