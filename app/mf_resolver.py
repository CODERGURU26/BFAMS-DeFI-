import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import MfScheme


class ResolutionStatus(str, Enum):
    RESOLVED_SCHEME_CODE = "resolved_scheme_code"
    RESOLVED_ISIN = "resolved_isin"
    AMBIGUOUS_ISIN = "ambiguous_isin"
    UNRESOLVED = "unresolved"
    INVALID_IDENTIFIER = "invalid_identifier"


@dataclass
class SchemeResolutionResult:
    status: ResolutionStatus
    scheme_id: Optional[uuid.UUID] = None
    scheme_code: Optional[str] = None
    scheme_name: Optional[str] = None
    matched_by: Optional[str] = None  # "scheme_code" or "isin"
    reason: Optional[str] = None
    candidate_scheme_codes: list[str] = None

    def __post_init__(self):
        if self.candidate_scheme_codes is None:
            self.candidate_scheme_codes = []

    @property
    def is_resolved(self) -> bool:
        return self.status in (
            ResolutionStatus.RESOLVED_SCHEME_CODE,
            ResolutionStatus.RESOLVED_ISIN,
        )


def _looks_like_isin(val: str) -> bool:
    """Check if value matches standard ISIN format (12 alphanumeric, starting with 2 letter country code)."""
    return bool(re.match(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$", val))


def _looks_like_scheme_code(val: str) -> bool:
    """AMFI scheme codes are typically numeric digits (e.g., '119551')."""
    return val.isdigit()


def resolve_mf_scheme(
    session: Session,
    identifier: Optional[str] = None,
    *,
    scheme_code: Optional[str] = None,
    isin: Optional[str] = None,
    asset_name: Optional[str] = None,
) -> SchemeResolutionResult:
    """
    Deterministic mutual fund scheme resolver.
    
    Resolution priority:
    1. Exact AMFI scheme code match.
    2. Exact ISIN match (only if strictly unambiguous - single matching scheme).
    3. Return unresolved with reason (no fuzzy name matching).
    """
    cleaned_identifier = identifier.strip() if identifier else None
    cleaned_scheme_code = str(scheme_code).strip() if scheme_code else None
    cleaned_isin = isin.strip().upper() if isin else None

    # Determine explicit candidates
    target_code = cleaned_scheme_code
    target_isin = cleaned_isin

    if cleaned_identifier:
        if _looks_like_scheme_code(cleaned_identifier) and not target_code:
            target_code = cleaned_identifier
        elif _looks_like_isin(cleaned_identifier) and not target_isin:
            target_isin = cleaned_identifier
        elif not target_code and not target_isin:
            # Check if identifier matches a scheme_code directly
            target_code = cleaned_identifier

    # Priority 1: Exact AMFI scheme code match
    if target_code:
        scheme = session.scalars(
            select(MfScheme).where(MfScheme.scheme_code == target_code)
        ).first()
        if scheme:
            return SchemeResolutionResult(
                status=ResolutionStatus.RESOLVED_SCHEME_CODE,
                scheme_id=scheme.id,
                scheme_code=scheme.scheme_code,
                scheme_name=scheme.scheme_name,
                matched_by="scheme_code",
                reason=f"Matched exact AMFI scheme code '{target_code}'",
            )
        # If target_code didn't match and identifier didn't look like ISIN,
        # check if identifier might have been an ISIN after all
        if not target_isin and cleaned_identifier and not _looks_like_scheme_code(cleaned_identifier):
            target_isin = cleaned_identifier.upper()

    # Priority 2: Exact ISIN match
    if target_isin:
        matching_schemes = session.scalars(
            select(MfScheme).where(
                or_(
                    MfScheme.isin_primary == target_isin,
                    MfScheme.isin_reinvestment == target_isin,
                )
            )
        ).all()

        if len(matching_schemes) == 1:
            scheme = matching_schemes[0]
            return SchemeResolutionResult(
                status=ResolutionStatus.RESOLVED_ISIN,
                scheme_id=scheme.id,
                scheme_code=scheme.scheme_code,
                scheme_name=scheme.scheme_name,
                matched_by="isin",
                reason=f"Unambiguously matched ISIN '{target_isin}'",
                candidate_scheme_codes=[scheme.scheme_code],
            )
        elif len(matching_schemes) > 1:
            codes = [s.scheme_code for s in matching_schemes]
            return SchemeResolutionResult(
                status=ResolutionStatus.AMBIGUOUS_ISIN,
                scheme_id=None,
                scheme_code=None,
                scheme_name=None,
                matched_by="isin",
                reason=f"Ambiguous ISIN '{target_isin}' matches {len(matching_schemes)} schemes ({', '.join(codes[:5])}). Manual mapping required.",
                candidate_scheme_codes=codes,
            )
        else:
            return SchemeResolutionResult(
                status=ResolutionStatus.UNRESOLVED,
                scheme_id=None,
                reason=f"ISIN '{target_isin}' not found in mutual fund database. Manual mapping required.",
            )

    # If nothing matched or no valid identifier was supplied
    if not cleaned_identifier and not target_code and not target_isin:
        return SchemeResolutionResult(
            status=ResolutionStatus.INVALID_IDENTIFIER,
            reason="No identifier, scheme_code, or ISIN provided.",
        )

    return SchemeResolutionResult(
        status=ResolutionStatus.UNRESOLVED,
        reason=f"Could not resolve identifier '{cleaned_identifier}'. Fuzzy scheme name matching is disabled for financial safety.",
    )
