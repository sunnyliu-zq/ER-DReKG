"""AI-assisted drug interpretation router (DeepSeek Flash)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.interpretation import service
from app.interpretation.evidence import EvidenceError
from app.interpretation.provider import ProviderAuthError, ProviderConfigError, ProviderRequestError, ProviderResponseError
from app.interpretation.schemas import DrugInterpretationRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/v1/projects/{project_id}/drug-interpretation")
def create_drug_interpretation(project_id: str, payload: DrugInterpretationRequest, db: Session = Depends(get_db)):
    """Read existing project evidence and generate a grounded mechanism report."""
    try:
        return service.interpret_drug(
            db,
            project_id,
            payload.drug_id,
            payload.graph_build_id,
            force_regenerate=payload.force_regenerate,
        )
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except EvidenceError as error:
        logger.warning("Drug interpretation evidence error: %s", error)
        raise HTTPException(409, str(error)) from error
    except service.InterpretationBusyError as error:
        raise HTTPException(409, str(error)) from error
    except ProviderConfigError as error:
        logger.warning("Drug interpretation provider not configured: %s", type(error).__name__)
        raise HTTPException(503, "AI 解释服务未配置（后端缺少 DeepSeek API Key）") from error
    except ProviderAuthError as error:
        logger.warning("Drug interpretation provider authentication failed: %s", error)
        raise HTTPException(503, "AI 解释服务认证失败（请检查后端 DeepSeek API Key 与 deepseek-flash 模型权限）") from error
    except ProviderResponseError as error:
        logger.warning("Drug interpretation response invalid: %s", error)
        raise HTTPException(502, "AI 解释返回格式校验失败") from error
    except ProviderRequestError as error:
        logger.warning("Drug interpretation provider unavailable: %s", error)
        raise HTTPException(503, "AI 解释服务暂时不可用") from error
