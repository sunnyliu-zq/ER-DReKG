from pydantic import BaseModel, Field


class DrugInterpretationRequest(BaseModel):
    drug_id: str = Field(min_length=1, max_length=100)
    graph_build_id: str | None = Field(default=None, max_length=100)
    force_regenerate: bool = False
