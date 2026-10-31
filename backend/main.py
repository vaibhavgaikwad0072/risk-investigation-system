import logging
from collections.abc import Generator
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from database import SessionLocal, init_db
from models import Customer, Transaction
from risk_data_loader import AssessmentContextTooLarge, StoredDataIntegrityError
from risk_schemas import AssessmentRequest, AssessmentResponse, serialize_assessment
from risk_service import (
    DEFAULT_ASSESSMENT_LIMITS,
    AssessmentLimits,
    AssessmentRangeTooLarge,
    run_assessment,
)
from synthetic_data import GeneratorConfig, ScenarioSpec, generate_dataset, persist_dataset

logger = logging.getLogger(__name__)

app = FastAPI(
    title="YottaFlex.ai API",
    description="Financial transaction risk investigation platform",
    version="0.1.0",
)


def seed_demo_data() -> None:
    with SessionLocal() as session:
        if session.query(Customer).count() > 0 or session.query(Transaction).count() > 0:
            return

        config = GeneratorConfig(
            seed=2468,
            run_id="DEMO",
            customer_count=8,
            account_count=12,
            transaction_count=40,
            start_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scenarios=(
                ScenarioSpec("rapid", "rapid_transfers", 3, interval_seconds=10),
                ScenarioSpec("repeat", "repeated_transfers", 3),
                ScenarioSpec("large", "large_amount", 1, 2_000_000),
                ScenarioSpec("network", "multi_account_movement", 2),
            ),
        )
        dataset = generate_dataset(config)
        persist_dataset(dataset, session)
        session.commit()


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_demo_data()


@app.get("/")
def home():
    return {
        "message": "Welcome to YottaFlex.ai",
        "status": "running",
    }


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "yottaflex-api",
    }


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_assessment_limits() -> AssessmentLimits:
    return DEFAULT_ASSESSMENT_LIMITS


@app.post("/risk/assessments", response_model=AssessmentResponse)
def create_risk_assessment(
    request: AssessmentRequest,
    session: Session = Depends(get_db),
    limits: AssessmentLimits = Depends(get_assessment_limits),
) -> AssessmentResponse:
    try:
        result = run_assessment(
            session=session,
            start_at=request.normalized_start(),
            end_at=request.normalized_end(),
            limits=limits,
        )
    except (AssessmentRangeTooLarge, AssessmentContextTooLarge) as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except StoredDataIntegrityError as exc:
        logger.error("Stored data failed risk assessment validation: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Stored transaction data could not be assessed",
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("Database failure while loading risk assessment data")
        raise HTTPException(
            status_code=503,
            detail="Risk assessment data is temporarily unavailable",
        ) from exc

    target_transactions = {
        transaction.transaction_ref: transaction
        for transaction in result.target_transactions
    }
    assessments = tuple(
        serialize_assessment(
            assessment,
            target_transactions[assessment.transaction_ref],
        )
        for assessment in result.assessments
    )
    return AssessmentResponse(
        start_at=result.start_at,
        end_at=result.end_at,
        assessment_count=len(assessments),
        assessments=assessments,
    )