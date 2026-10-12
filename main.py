from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from database import engine
from app.routes import accounts, transactions, connections

app = FastAPI(
    title="BFAMS-DeFi API",
    description="Backend API for financial data and analytics",
    version="0.1.0",
)

app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(connections.router)


@app.get("/")
def root():
    return {
        "message": "BFAMS-DeFi backend is running",
        "status": "ok",
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/health/db")
def database_health_check():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        return {
            "status": "healthy",
            "database": "connected",
        }
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database connection failed",
        )