"""Define and inspect a closed, typed bioinformatics skill."""

import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.skill import (
    InputReference,
    SkillDefinition,
    StepOutputReference,
    ToolReference,
    ToolStep,
    register_skill,
)


class FastqInspectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    max_reads: int = Field(default=1000, ge=1, le=100_000)


class FastqInspectionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_path: str
    reads_scanned: int = Field(ge=0)


FASTQ_INSPECTION = register_skill(
    SkillDefinition.from_models(
        id="bio.fastq.inspection",
        version="1.0.0",
        title="Bounded FASTQ inspection",
        description="Inspect a bounded FASTQ prefix with the approved remote tool.",
        status="approved",
        input_model=FastqInspectionInput,
        output_model=FastqInspectionOutput,
        steps=(
            ToolStep(
                id="inspect",
                tool=ToolReference(
                    provider="bioinfo-runner",
                    name="bio.fastq.inspect",
                    version="1.0.0",
                ),
                arguments={
                    "path": InputReference(path="path"),
                    "max_reads": InputReference(path="max_reads"),
                },
            ),
        ),
        outputs={
            "report_path": StepOutputReference(step="inspect", path="report_path"),
            "reads_scanned": StepOutputReference(step="inspect", path="reads_scanned"),
        },
        permissions=frozenset({"dataset:fastq:read"}),
        tags=frozenset({"bioinformatics", "fastq", "qc"}),
    )
)


if __name__ == "__main__":
    FASTQ_INSPECTION.validate_input({"path": "sample.fastq", "max_reads": 1000})
    print(FASTQ_INSPECTION.model_dump_json(indent=2))
