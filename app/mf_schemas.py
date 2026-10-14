"""
app/mf_schemas.py
=================
Pydantic schemas for Mutual Fund Analytics API endpoints.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from pydantic import BaseModel, Field


class SchemeSummary(BaseModel):
    id: str
    scheme_code: str
    scheme_name: str
    isin_primary: str | None = None
    isin_reinvestment: str | None = None
    plan: str | None = None
    option_name: str | None = None
    amc_name: str
    scheme_type: str
    scheme_category: str


class SchemeSearchResponse(BaseModel):
    total: int
    limit: int
    offset: int
    schemes: list[SchemeSummary]


class NavPointResponse(BaseModel):
    nav_date: date
    nav: Decimal


class NavHistoryResponse(BaseModel):
    scheme_code: str
    scheme_name: str
    total_observations: int
    navs: list[NavPointResponse]


class ReturnsResponse(BaseModel):
    scheme_code: str
    scheme_name: str
    start_date_requested: date
    start_date_actual: date
    start_nav: Decimal
    end_date_requested: date
    end_date_actual: date
    end_nav: Decimal
    elapsed_days: int
    absolute_return_pct: Decimal
    cagr_pct: Decimal | None = None
    annualized: bool


class RollingReturnPointResponse(BaseModel):
    start_date: date
    end_date: date
    start_nav: Decimal
    end_nav: Decimal
    return_pct: Decimal


class RollingReturnsResponse(BaseModel):
    scheme_code: str
    scheme_name: str
    window_days: int
    step_days: int
    total_periods: int
    annualized: bool
    mean_pct: Decimal
    median_pct: Decimal
    min_pct: Decimal
    max_pct: Decimal
    std_dev_pct: Decimal
    observations: list[RollingReturnPointResponse]


class DrawdownResponse(BaseModel):
    scheme_code: str
    scheme_name: str
    max_drawdown_pct: Decimal
    peak_date: date
    peak_nav: Decimal
    trough_date: date
    trough_nav: Decimal
    recovery_date: date | None = None
    is_recovered: bool
    total_observations: int


class CompareRequest(BaseModel):
    scheme_codes: list[str] = Field(min_length=1, max_length=50)
    start_date: date
    end_date: date
    max_staleness_days: int = Field(default=7, ge=0, le=90)


class CompareResponse(BaseModel):
    start_date_requested: str
    end_date_requested: str
    schemes_evaluated: int
    results: list[dict[str, Any]]
