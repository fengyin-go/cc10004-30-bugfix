"""多维下钻接口：维护下钻路径，覆盖登记、修改、提交校验、启用、停用等动作。

请求体兼容两种形状：字段直接放在顶层，或包在 ``values`` 里。页面按顶层字段
提交，历史调用方用 ``values`` 也不会失效；``action`` 同样两处都认，避免出现
“点了启用但后端只收到空动作”的假成功。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, PageResult
from app.services.drill import (
    ALL_FIELDS,
    DrillService,
    _normalize_levels,
)

router = APIRouter(prefix="/api/drill", tags=["多维下钻"])

service = DrillService()

LIST_FIELDS = ["路径编号", "起点维度", "下钻层数", "层级维度列表", "聚合指标", "默认展开层", "排序方向", "最多展示行数"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


class DrillPayload(BaseModel):
    """登记/修改/动作请求：顶层字段与 values 合并，values 优先。"""

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None
    expected_version: int | None = None
    # 允许页面把字段直接放在顶层提交，避免只保存到 values 之外的字段被静默丢弃。
    model_config = {"extra": "allow"}

    def merged(self) -> dict[str, Any]:
        merged = {key: value for key, value in self.model_extra.items()} if self.model_extra else {}
        merged.update(self.values or {})
        return merged


def _collect_payload(payload: DrillPayload) -> dict[str, Any]:
    field_values = payload.merged()

    # 页面把 action 放在顶层；历史调用方把它放在 values 里。
    action = field_values.get("action")
    if action is None:
        action = payload.values.get("action") if payload.values else None
    if action is not None:
        field_values["action"] = str(action).strip()

    if payload.expected_version is not None:
        field_values["expected_version"] = payload.expected_version
    return field_values


def _payload_version(field_values: dict[str, Any]) -> int | None:
    raw = field_values.get("expected_version")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_version 必须是整数")


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按路径编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    start_dimension: str | None = Query(default=None, description="按起点维度检索"),
    layer_count: int | None = Query(default=None, description="按下钻层数筛选"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按路径编号、起点维度与状态过滤多维下钻列表；没有数据时返回空页，不报错。

    每条记录附带 ``validation`` 与 ``executable``：页面据此知道哪些路径真正可执行，
    而不是只看状态列。
    """
    if page < 1:
        raise HTTPException(status_code=400, detail="页码必须大于 0")
    if size < 1 or size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        start_dimension=start_dimension,
        layer_count=layer_count,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出多维下钻清单：返回当前过滤条件下的全量数据。

    必须声明在 ``/{entry_id}`` 之前，否则会被当成 entry_id 解析而报 422。
    """
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "drill", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条下钻路径明细；不存在时给出可读的错误说明。

    返回的是实际接纳值（落库字段）+ 版本 + 最近一次动作结果。重新进入页面时，
    页面直接渲染这份快照：被拒绝的字段不会被回填成“看起来已保存”的值。
    """
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: DrillPayload) -> ActionResult:
    """登记一条下钻路径，缺字段时说明原因而不是静默丢弃；其余字段完整保存。"""
    field_values = _collect_payload(payload)
    values = {key: value for key, value in field_values.items() if key in ALL_FIELDS}
    entry, missing, errors = service.create_entry(values)
    if missing:
        return ActionResult(
            ok=False,
            message=f"缺少必填字段：{'、'.join(missing)}",
            errors=[{"field": field, "code": "missing", "message": f"缺少必填字段：{field}"} for field in missing],
        )
    if errors:
        return ActionResult(
            ok=False,
            message="下钻路径登记被拒绝：" + "；".join(str(item.get("message") or "") for item in errors),
            errors=errors,
        )
    return ActionResult(
        ok=True,
        message="下钻路径已登记",
        entry=service.describe_entry(entry, full_validation=True),
    )


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: DrillPayload) -> ActionResult:
    """修改下钻路径字段（含层级维度列表）。

    采用乐观并发控制：必须带 ``expected_version``，与当前版本不一致时整份拒绝，
    并返回当前版本；校验失败时不写入任何字段，避免只写一半层级。
    """
    field_values = _collect_payload(payload)
    expected_version = _payload_version(field_values)
    if expected_version is None:
        raise HTTPException(status_code=400, detail="修改下钻路径必须携带 expected_version 以防并发覆盖")

    values = {
        key: value for key, value in field_values.items()
        if key in ALL_FIELDS
    }
    # 空 values 视为没有修改内容，直接回报实际接纳值，不推进版本。
    if not values:
        entry = service.get_entry(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
        return ActionResult(ok=True, message="未提交任何字段，未修改下钻路径", entry=entry)

    entry, ok, message, errors = service.update_entry(
        entry_id, values, expected_version=expected_version
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    result = ActionResult(
        ok=ok,
        message=message,
        entry=service.describe_entry(entry, full_validation=True),
        errors=[] if ok else errors,
    )
    return result


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: DrillPayload) -> ActionResult:
    """对单条下钻路径执行提交校验、启用路径、停用路径。

    不允许的动作、缺少层级证据或聚合指标失效时会被整体拦下并给出具体原因；
    重复确认同一状态是幂等的，不会重复推进版本。
    """
    field_values = _collect_payload(payload)
    action = str(field_values.get("action") or "").strip()
    if not action:
        return ActionResult(
            ok=False,
            message="缺少 action：请提交「提交校验」「启用路径」或「停用路径」",
            entry=service.get_entry(entry_id),
        )
    expected_version = _payload_version(field_values)
    entry, ok, message, errors = service.run_action(
        entry_id, action, expected_version=expected_version
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    result = ActionResult(
        ok=ok,
        message=message,
        entry=service.describe_entry(entry, full_validation=True),
        errors=[] if ok else errors,
    )
    return result


@router.get("/{entry_id}/levels")
def parse_levels(levels: str | None = Query(default=None, description="以逗号分隔的层级维度")) -> dict[str, Any]:
    """辅助接口：把页面输入的层级文本归一成层级维度列表，并回显解析结果。

    只做归一化，不落库；空字符串会被当作“显式空列表”，与字段缺失区分开。
    """
    if levels is None:
        return {"provided": False, "levels": None}
    parsed, kind, errors = _normalize_levels(levels)
    return {
        "provided": True,
        "levels": parsed,
        "kind": kind,
        "errors": errors,
    }
