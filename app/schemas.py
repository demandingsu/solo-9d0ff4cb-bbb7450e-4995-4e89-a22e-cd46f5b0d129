"""请求/响应模型(Pydantic v2)。所有尺寸均为整数微米。"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------- 批次操作


class UpsertItemOp(BaseModel):
    """新建或更新检验项目规范(标称值 + 上下限,整数微米)。"""

    op: Literal["upsert_item"]
    item_code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    nominal_um: int
    upper_um: int
    lower_um: int

    @model_validator(mode="after")
    def _check_limits(self):
        if not (self.lower_um <= self.nominal_um <= self.upper_um):
            raise ValueError("需满足 lower_um <= nominal_um <= upper_um")
        return self


class UpsertWorkstationOp(BaseModel):
    """登记/更新工位:终端代际 new(标称值+上下限)或 old(标称值+对称公差)。"""

    op: Literal["upsert_workstation"]
    workstation_code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    terminal_generation: Literal["new", "old"]


class ClaimItemOp(BaseModel):
    """工位领用检验项目。"""

    op: Literal["claim_item"]
    workstation_code: str = Field(min_length=1, max_length=64)
    item_code: str = Field(min_length=1, max_length=64)


class UnclaimItemOp(BaseModel):
    """工位取消领用检验项目。"""

    op: Literal["unclaim_item"]
    workstation_code: str = Field(min_length=1, max_length=64)
    item_code: str = Field(min_length=1, max_length=64)


AnyOp = Annotated[
    Union[UpsertItemOp, UpsertWorkstationOp, ClaimItemOp, UnclaimItemOp],
    Field(discriminator="op"),
]


class BatchCreate(BaseModel):
    title: str = ""
    ops: list[AnyOp] = []

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "title": "示例:登记工位/规范并领用",
                    "ops": [
                        {
                            "op": "upsert_item",
                            "item_code": "ITEM-DIA-40",
                            "name": "主轴孔直径",
                            "nominal_um": 40000,
                            "upper_um": 40008,
                            "lower_um": 39992,
                        },
                        {
                            "op": "upsert_workstation",
                            "workstation_code": "WS-01",
                            "name": "一号加工中心",
                            "terminal_generation": "new",
                        },
                        {
                            "op": "claim_item",
                            "workstation_code": "WS-01",
                            "item_code": "ITEM-DIA-40",
                        },
                    ],
                }
            ]
        }
    }


class OpsReplace(BaseModel):
    ops: list[AnyOp]


# ---------------------------------------------------------------- 输出模型


class Violation(BaseModel):
    workstation_code: str
    item_code: str
    nominal_um: int
    upper_um: int
    lower_um: int
    reason: str


class PreviewItem(BaseModel):
    item_code: str
    name: str
    nominal_um: int
    upper_um: int
    lower_um: int
    symmetric: bool


class WorkstationPreview(BaseModel):
    workstation_code: str
    terminal_generation: str
    items: list[PreviewItem]


class BatchOut(BaseModel):
    id: int
    title: str
    status: str
    ops: list[dict[str, Any]]
    violations: list[Violation] | None
    involved: dict[str, list[str]] | None  # 核对指纹记录的所涉实体
    replay_basis_available: bool  # 是否已保存回放依据(旧批次为 False,无法调查)
    created_at: datetime
    verified_at: datetime | None
    published_at: datetime | None


class VerifyResponse(BaseModel):
    batch_id: int
    status: str  # verified | rejected
    violations: list[Violation]
    preview: list[WorkstationPreview]
    verified_at: datetime


class PublishResponse(BaseModel):
    batch_id: int
    status: str
    published_at: datetime
    changes: list[dict[str, Any]]


class ItemOut(BaseModel):
    code: str
    name: str
    nominal_um: int
    upper_um: int
    lower_um: int
    symmetric: bool
    version: int
    claims_version: int


class ItemDetailOut(ItemOut):
    claimed_by: list[str]


class WorkstationOut(BaseModel):
    code: str
    name: str
    terminal_generation: str
    version: int
    claims_version: int
    claimed_items: list[str]


class SpecItemNew(BaseModel):
    """新终端下发格式:标称值 + 上下限。"""

    item_code: str
    name: str
    nominal_um: int
    upper_um: int
    lower_um: int


class SpecItemOld(BaseModel):
    """旧终端下发格式:标称值 + 对称公差。"""

    item_code: str
    name: str
    nominal_um: int
    tolerance_um: int


class WorkstationSpecOut(BaseModel):
    """按工位下发的一整份生效规范。"""

    workstation_code: str
    workstation_name: str
    terminal_generation: str
    revision: str
    generated_at: datetime
    items: list[SpecItemNew | SpecItemOld]


# ---------------------------------------------------------------- 调查(只读)


class SpecSnapshot(BaseModel):
    """一整份下发规范的对比快照:与下发格式一致,但不含生成时间(不参与比较)。"""

    workstation_code: str
    workstation_name: str
    terminal_generation: str
    items: list[SpecItemNew | SpecItemOld]


class WorkstationSpecDiff(BaseModel):
    """某工位两边的下发规范对比;工位只在一边存在时另一边为 null。"""

    workstation_code: str
    actual: SpecSnapshot | None  # 实际当前下发(含被调查批次)
    without_batch: SpecSnapshot | None  # 假设少了该批、重放后续批次后的下发


class ReplayFailure(BaseModel):
    """回放中无法应用、被整体丢弃的批次及原因。"""

    batch_id: int
    title: str
    published_at: datetime | None
    reason: str  # op_error | asymmetric_tolerance_on_old_terminal
    message: str
    violations: list[Violation] | None


class InvestigateResponse(BaseModel):
    """只读调查结果:假设少了该批,后续哪些批次会失败、各工位下发规范怎么变。"""

    batch_id: int
    title: str
    published_at: datetime
    replayed_batches: list[int]  # 参与回放的后续批次(按实际发布顺序)
    failed_batches: list[ReplayFailure]  # 回放失败、改动被丢弃的批次
    spec_diffs: list[WorkstationSpecDiff]  # 下发规范有变化的工位及两边内容
