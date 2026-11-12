"""多维下钻业务规则：路径完整保存、层级证据校验、聚合指标契约与状态流转。

几条必须守住的边界：

* 登记/修改时把用户提交的全部字段原样（仅做类型归一）落库，不能只留必填三项；
* ``层级维度列表`` 缺失与显式空列表含义不同：缺失表示存量记录没有填写过，
  空列表表示用户确认没有层级。两者都不能启用，但拒绝原因要分开，且不允许
  用起点维度或层数去“补造”层级；
* 启用前先向聚合计算模块解析并确认聚合方案仍然有效，任何一项失败都整体拒绝
  本次启用，状态、版本号都不推进；
* 修改层级采用乐观并发控制：携带 ``expected_version`` 时，过期版本直接拒绝，
  整份层级要么全部写入、要么全部不写。
"""
from __future__ import annotations

import re
import threading
from typing import Any

from app.services.aggregate import parse_indicator, resolve_indicator
from app.store import store

MODULE = "drill"
REQUIRED_FIELDS = ["路径编号", "起点维度", "下钻层数"]
OPTIONAL_FIELDS = ["层级维度列表", "聚合指标", "默认展开层", "排序方向", "最多展示行数"]
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用路径": "已启用", "停用路径": "已停用"}
ACTION_MESSAGES = {"提交校验": "下钻路径已提交校验", "启用路径": "下钻路径已启用", "停用路径": "下钻路径已停用"}
NEGATIVE_ACTIONS = ["停用路径"]

# 同一进程内所有写操作串行化，保证“比较版本 + 校验 + 落库”是一个原子动作。
_WRITE_LOCK = threading.RLock()

DIRECTION_ALIASES = {
    "升序": "升序",
    "正序": "升序",
    "asc": "升序",
    "ascending": "升序",
    "降序": "降序",
    "倒序": "降序",
    "desc": "降序",
    "descending": "降序",
}

_LEVEL_SPLIT_RE = re.compile(r"[,，、;；\n\r]+")
_NAME_KEYS = ("维度", "维度名称", "名称", "name", "dimension", "field", "指标")
_ID_KEYS = ("维度编号", "编号", "id", "code", "dimension_id")


def _issue(field: str, code: str, message: str) -> dict[str, str]:
    return {"field": field, "code": code, "message": message}


def _to_int(value: Any) -> tuple[int | None, str | None]:
    """把数字字段归一成 int；空值不算错误，由必填校验单独处理。"""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "必须是整数"
    if isinstance(value, int):
        return value, None
    if isinstance(value, float):
        if value.is_integer():
            return int(value), None
        return None, "必须是整数"
    text = str(value).strip()
    if not text:
        return None, None
    try:
        number = int(text, 10)
    except ValueError:
        return None, "必须是整数"
    return number, None


def _normalize_level_item(item: Any) -> tuple[str | None, str | None, str | None]:
    """返回 (展示名称, 身份键, 错误)。展示名称用于回显，身份键用于查重。"""
    if item is None:
        return None, None, "层级维度不能为空"
    if isinstance(item, dict):
        name = ""
        for key in _NAME_KEYS:
            if item.get(key) not in (None, ""):
                name = str(item[key]).strip()
                break
        identity = ""
        for key in _ID_KEYS:
            if item.get(key) not in (None, ""):
                identity = str(item[key]).strip()
                break
        if not name and identity:
            name = identity
        if not name:
            return None, None, "层级维度对象里没有可用的维度名称或维度编号"
        return name, identity or name, None
    if isinstance(item, str):
        name = item.strip()
        if not name:
            return None, None, "层级维度不能为空"
        return name, name, None
    if isinstance(item, (int, float)) and not isinstance(item, bool):
        return str(item), str(item), None
    return None, None, "层级维度必须是文本或包含维度名称的对象"


def _normalize_levels(raw: Any) -> tuple[list[str] | None, str, list[dict[str, str]]]:
    """归一化层级维度列表。

    返回的第二个值区分三种语义：``missing``（字段未提供）、``empty``（显式空列表）
    与 ``provided``（提供了具体层级）。
    """
    errors: list[dict[str, str]] = []
    if raw is None:
        return None, "missing", errors
    if isinstance(raw, str):
        if not raw.strip():
            return [], "empty", errors
        items: list[Any] = [part for part in _LEVEL_SPLIT_RE.split(raw)]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
        if not items:
            return [], "empty", errors
    else:
        return None, "invalid", [
            _issue("层级维度列表", "levels_type", "层级维度列表必须是数组或文本")
        ]

    levels: list[str] = []
    identities: set[str] = set()
    for index, item in enumerate(items):
        name, identity, error = _normalize_level_item(item)
        if error:
            errors.append(_issue(
                "层级维度列表",
                "level_item",
                f"第 {index + 1} 层{error}",
            ))
            continue
        assert name is not None and identity is not None
        key = identity.casefold()
        if key in identities:
            errors.append(_issue(
                "层级维度列表",
                "level_duplicate",
                f"层级维度「{name}」重复出现，同一维度身份只能下钻一次",
            ))
            continue
        identities.add(key)
        levels.append(name)
    return levels, "provided", errors


def _normalize_direction(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    canonical = DIRECTION_ALIASES.get(text.casefold(), DIRECTION_ALIASES.get(text))
    if canonical is None:
        return None, f"排序方向「{text}」不在允许范围（升序/降序）"
    return canonical, None


def _normalize_indicator(value: Any) -> tuple[Any, dict[str, Any] | None, list[dict[str, str]]]:
    """保留聚合指标原文，同时缓存一次结构解析结果；判断有效性留到启用时。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return value, None, []
    parsed = parse_indicator(value)
    return value, parsed, []


def current_version(entry: dict[str, Any]) -> int:
    number, _ = _to_int(entry.get("version"))
    return number or 0


def _required_errors(values: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field in REQUIRED_FIELDS:
        value = values.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)
    return missing


def _validate_path(
    entry: dict[str, Any],
    *,
    for_enable: bool,
    context: str,
) -> dict[str, Any]:
    """按条目当前（或候选）字段计算校验结果，不修改条目。

    ``error_count`` 只统计真正阻断的错误；``enable_blockers`` 是启用时额外要求
    的层级/指标证据（草稿阶段以警告呈现，启用阶段升级为错误）。
    """
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    enable_blockers: list[dict[str, str]] = []

    def block(item: dict[str, str]) -> None:
        if for_enable:
            errors.append(item)
        else:
            warnings.append(item)
            enable_blockers.append(dict(item))

    # 必填字段：存量记录里缺失的字段要明确指出，不能拿默认值补齐。
    for field in REQUIRED_FIELDS:
        value = entry.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(_issue(field, "missing", f"缺少必填字段：{field}"))

    path_no = entry.get("路径编号")
    if path_no is not None and not str(path_no).strip():
        errors.append(_issue("路径编号", "path_empty", "路径编号不能为空"))
    start_dimension = entry.get("起点维度")
    if start_dimension is not None and not str(start_dimension).strip():
        errors.append(_issue("起点维度", "dimension_empty", "起点维度不能为空"))

    layer_count, count_error = _to_int(entry.get("下钻层数"))
    if count_error:
        errors.append(_issue("下钻层数", "layer_count_invalid", f"下钻层数{count_error}"))
    elif layer_count is not None and layer_count < 0:
        errors.append(_issue("下钻层数", "layer_count_invalid", "下钻层数不能为负数"))

    levels_present = "层级维度列表" in entry and entry.get("层级维度列表") is not None
    levels, levels_kind, level_errors = _normalize_levels(entry.get("层级维度列表"))
    errors.extend(level_errors)
    if not levels_present:
        block(_issue(
            "层级维度列表",
            "levels_missing",
            "缺少层级维度列表：没有层级证据的路径不能启用（存量记录需要先补全层级）",
        ))
    elif levels_kind == "empty":
        block(_issue(
            "层级维度列表",
            "levels_empty",
            "层级维度列表为空：已确认没有层级，不能作为可执行的下钻路径",
        ))
    elif levels_kind == "invalid":
        block(_issue("层级维度列表", "levels_type", "层级维度列表格式不正确"))

    if levels is not None and layer_count is not None and levels_kind in ("provided", "empty"):
        if len(levels) != layer_count:
            errors.append(_issue(
                "层级维度列表",
                "levels_count_mismatch",
                f"层级维度列表包含 {len(levels)} 个维度，与下钻层数 {layer_count} 不一致；"
                "请补齐完整层级后再保存，不能只保存其中一部分",
            ))

    default_layer, default_error = _to_int(entry.get("默认展开层"))
    if default_error:
        errors.append(_issue("默认展开层", "default_layer_invalid", f"默认展开层{default_error}"))
    elif default_layer is not None:
        if default_layer < 0:
            errors.append(_issue("默认展开层", "default_layer_invalid", "默认展开层不能为负数"))
        elif levels is not None and default_layer > len(levels):
            errors.append(_issue(
                "默认展开层",
                "default_layer_exceeds_levels",
                f"默认展开层 {default_layer} 不得超过已确认层级 {len(levels)} 层",
            ))
        elif levels is None:
            warnings.append(_issue(
                "默认展开层",
                "default_layer_unverified",
                "层级维度列表缺失，默认展开层暂无法与已确认层级比对",
            ))

    direction, direction_error = _normalize_direction(entry.get("排序方向"))
    if direction_error:
        errors.append(_issue("排序方向", "direction_invalid", direction_error))

    max_rows, rows_error = _to_int(entry.get("最多展示行数"))
    if rows_error:
        errors.append(_issue("最多展示行数", "max_rows_invalid", f"最多展示行数{rows_error}"))
    elif max_rows is not None and max_rows < 1:
        errors.append(_issue("最多展示行数", "max_rows_invalid", "最多展示行数必须大于 0"))

    indicator = entry.get("聚合指标")
    indicator_parsed: dict[str, Any] | None = None
    if indicator is None or (isinstance(indicator, str) and not indicator.strip()):
        block(_issue(
            "聚合指标",
            "indicator_missing",
            "缺少聚合指标：启用前必须绑定一个有效的聚合方案",
        ))
    else:
        indicator_parsed = parse_indicator(indicator)
        if not indicator_parsed.get("ok"):
            block(_issue(
                "聚合指标",
                "indicator_unparsed",
                str(indicator_parsed.get("reason") or "聚合指标无法解析"),
            ))
        else:
            # 草稿/提交校验阶段只判断能否解析；方案是否仍启用留到启用时复查，
            # 避免把“现在暂未启用、稍后启用”的草稿直接判死。
            resolved = resolve_indicator(indicator)
            indicator_parsed = resolved
            if not resolved.get("ok"):
                block(_issue(
                    "聚合指标",
                    "indicator_unresolved",
                    str(resolved.get("reason") or "聚合指标在聚合计算模块中无法解析"),
                ))
            elif not resolved.get("active"):
                block(_issue(
                    "聚合指标",
                    "indicator_inactive",
                    str(resolved.get("reason") or "聚合指标绑定的聚合方案未启用"),
                ))

    executable = (
        not errors
        and levels_kind == "provided"
        and bool(levels)
        and indicator_parsed is not None
        and bool(indicator_parsed.get("ok"))
        and bool(indicator_parsed.get("active"))
        and entry.get("status") == "已启用"
    )

    return {
        "ok": not errors,
        "executable": executable,
        "enable_ready": not errors and not enable_blockers,
        "missing_fields": [item["field"] for item in errors if item["code"] == "missing"],
        "levels": levels_kind,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "enable_blockers": enable_blockers,
        "context": context,
        "聚合指标解析": indicator_parsed,
    }


def _rejection_message(action: str, validation: dict[str, Any]) -> str:
    items = list(validation.get("errors", [])) or list(validation.get("enable_blockers", []))
    reasons = [str(item.get("message") or "") for item in items]
    return f"{action}被拒绝：" + "；".join(reason for reason in reasons if reason)


def _normalize_submitted(values: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[dict[str, str]]]:
    """归一化本次提交的可编辑字段，供登记/修改共用。

    只处理确实提交了值的字段：字段缺失不会被补造，显式空列表会被保留成 ``[]``。
    """
    normalized: dict[str, Any] = {}
    missing: list[str] = []
    errors: list[dict[str, str]] = []

    for field in REQUIRED_FIELDS:
        if field not in values:
            missing.append(field)
            continue
        raw = values.get(field)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            errors.append(_issue(field, "missing", f"{field}不能为空"))
            continue
        if field == "下钻层数":
            number, error = _to_int(raw)
            if error or number is None:
                errors.append(_issue(field, "layer_count_invalid", f"下钻层数{error or '必须是整数'}"))
                continue
            normalized[field] = number
        else:
            normalized[field] = str(raw).strip()

    if "层级维度列表" in values and values.get("层级维度列表") is not None:
        levels, kind, level_errors = _normalize_levels(values.get("层级维度列表"))
        errors.extend(level_errors)
        if kind == "invalid" and levels is None:
            pass
        elif levels is not None:
            normalized["层级维度列表"] = levels

    if "聚合指标" in values and values.get("聚合指标") is not None:
        raw, _parsed, _ = _normalize_indicator(values.get("聚合指标"))
        # 只保存原文；解析结果每次读取时按聚合模块的当前数据重新计算，避免缓存过期结论。
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            normalized["聚合指标"] = ""
        else:
            normalized["聚合指标"] = raw
    elif "聚合指标" in values and values.get("聚合指标") is None:
        # 显式 null 不删除已有指标，避免调用方把“没传”误当成“清空”。
        pass

    if "默认展开层" in values and values.get("默认展开层") is not None:
        number, error = _to_int(values.get("默认展开层"))
        if error:
            errors.append(_issue("默认展开层", "default_layer_invalid", f"默认展开层{error}"))
        elif number is not None:
            normalized["默认展开层"] = number

    if "排序方向" in values and values.get("排序方向") is not None:
        direction, error = _normalize_direction(values.get("排序方向"))
        if error:
            errors.append(_issue("排序方向", "direction_invalid", error))
        elif direction is not None:
            normalized["排序方向"] = direction

    if "最多展示行数" in values and values.get("最多展示行数") is not None:
        number, error = _to_int(values.get("最多展示行数"))
        if error:
            errors.append(_issue("最多展示行数", "max_rows_invalid", f"最多展示行数{error}"))
        elif number is not None:
            normalized["最多展示行数"] = number

    return normalized, missing, errors


class DrillService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        start_dimension: str | None = None,
        layer_count: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("路径编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if start_dimension:
            rows = [
                row for row in rows
                if start_dimension in str(row.get("起点维度", ""))
            ]
        if layer_count is not None:
            rows = [
                row for row in rows
                if _to_int(row.get("下钻层数"))[0] == layer_count
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = rows[start:start + size]
        items = [self.describe_entry(row, full_validation=False) for row in page_rows]
        return items, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self.describe_entry(entry, full_validation=True)

    def describe_entry(
        self,
        entry: dict[str, Any],
        *,
        full_validation: bool,
    ) -> dict[str, Any]:
        """返回条目快照：实际落库值 + 当前校验结果 + 最近一次动作结果。"""
        snapshot = dict(entry)
        snapshot["version"] = current_version(entry)
        validation = _validate_path(
            entry,
            for_enable=False,
            context="read",
        )
        if not full_validation:
            validation = {
                "ok": validation["ok"],
                "executable": validation["executable"],
                "enable_ready": validation["enable_ready"],
                "missing_fields": validation["missing_fields"],
                "levels": validation["levels"],
                "error_count": validation["error_count"],
                "warning_count": validation["warning_count"],
                "errors": validation["errors"],
                "warnings": validation["warnings"],
                "enable_blockers": validation["enable_blockers"],
            }
        snapshot["validation"] = validation
        snapshot["executable"] = validation["executable"]
        return snapshot

    def create_entry(self, values: dict[str, Any]) -> tuple[
        dict[str, Any] | None, list[str], list[dict[str, str]]
    ]:
        """登记一条下钻路径：三个必填字段之外，提交了什么就完整保存什么。"""
        normalized, missing, errors = _normalize_submitted(values)
        if missing or errors:
            return None, missing, errors

        candidate: dict[str, Any] = dict(normalized)
        errors.extend(self._cross_field_errors(candidate, create=True))
        if errors:
            return None, [], errors

        with _WRITE_LOCK:
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            }
            entry.update(candidate)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["version"] = 1
            entry["last_action"] = {
                "action": "登记",
                "ok": True,
                "message": "下钻路径已登记",
            }
            rows.append(entry)
        return entry, [], []

    def _cross_field_errors(
        self,
        candidate: dict[str, Any],
        *,
        create: bool,
    ) -> list[dict[str, str]]:
        """跨字段一致性：层数、层级列表、默认展开层必须互相覆盖。

        create 时只把明确的结构错误拦下；缺失/空层级仅作为可保存的草稿状态。
        """
        errors: list[dict[str, str]] = []
        levels_present = "层级维度列表" in candidate and candidate.get("层级维度列表") is not None
        levels = candidate.get("层级维度列表")
        layer_count, count_error = _to_int(candidate.get("下钻层数"))
        if count_error:
            errors.append(_issue("下钻层数", "layer_count_invalid", f"下钻层数{count_error}"))
        elif layer_count is not None and layer_count < 0:
            errors.append(_issue("下钻层数", "layer_count_invalid", "下钻层数不能为负数"))

        if levels_present:
            if not isinstance(levels, list):
                errors.append(_issue("层级维度列表", "levels_type", "层级维度列表格式不正确"))
            elif layer_count is not None and len(levels) != layer_count:
                errors.append(_issue(
                    "层级维度列表",
                    "levels_count_mismatch",
                    f"层级维度列表包含 {len(levels)} 个维度，与下钻层数 {layer_count} 不一致；"
                    "请补齐完整层级后再保存，不能只保存其中一部分",
                ))
            default_layer, default_error = _to_int(candidate.get("默认展开层"))
            if default_error:
                errors.append(_issue("默认展开层", "default_layer_invalid", f"默认展开层{default_error}"))
            elif default_layer is not None and default_layer > len(levels):
                errors.append(_issue(
                    "默认展开层",
                    "default_layer_exceeds_levels",
                    f"默认展开层 {default_layer} 不得超过已确认层级 {len(levels)} 层",
                ))
            elif default_layer is not None and default_layer < 0:
                errors.append(_issue("默认展开层", "default_layer_invalid", "默认展开层不能为负数"))
        elif "默认展开层" in candidate and candidate.get("默认展开层") is not None:
            default_layer, default_error = _to_int(candidate.get("默认展开层"))
            if default_error:
                errors.append(_issue("默认展开层", "default_layer_invalid", f"默认展开层{default_error}"))
            elif default_layer is not None and default_layer < 0:
                errors.append(_issue("默认展开层", "default_layer_invalid", "默认展开层不能为负数"))
            elif default_layer is not None:
                # 没有已确认层级时，默认展开层没有可比对的依据，不能先落库再“以后再说”。
                errors.append(_issue(
                    "默认展开层",
                    "default_layer_unverified",
                    "层级维度列表尚未确认，不能设置默认展开层；请先补齐层级",
                ))
        return errors

    def update_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any] | None, bool, str, list[dict[str, str]]]:
        """修改路径字段（包括层级维度列表），整份提交要么全部生效要么全部拒绝。"""
        with _WRITE_LOCK:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, False, f"下钻路径 {entry_id} 不存在或已归档", []

            version = current_version(entry)
            if expected_version is not None and expected_version != version:
                message = (
                    f"下钻路径 {entry_id} 版本已过期：提交版本 {expected_version}，"
                    f"当前版本 {version}；请刷新后重试，本次未写入任何层级"
                )
                entry["last_action"] = {
                    "action": "修改路径",
                    "ok": False,
                    "message": message,
                }
                return entry, False, message, [
                    _issue("version", "version_conflict", message),
                ]

            normalized, missing, errors = _normalize_submitted(values)
            if missing and not any(field in entry for field in missing):
                message = f"缺少必填字段：{'、'.join(missing)}"
                return entry, False, message, [
                    _issue(field, "missing", f"缺少必填字段：{field}") for field in missing
                ]
            if errors:
                message = "修改路径被拒绝：" + "；".join(
                    str(item.get("message") or "") for item in errors
                )
                entry["last_action"] = {
                    "action": "修改路径",
                    "ok": False,
                    "message": message,
                }
                return entry, False, message, errors

            candidate = dict(entry)
            candidate.update(normalized)
            for field in REQUIRED_FIELDS:
                if field not in candidate or candidate.get(field) in (None, ""):
                    missing_field = field
                    message = f"修改路径被拒绝：缺少必填字段：{missing_field}"
                    entry["last_action"] = {
                        "action": "修改路径",
                        "ok": False,
                        "message": message,
                    }
                    return entry, False, message, [
                        _issue(missing_field, "missing", message)
                    ]

            errors = self._cross_field_errors(candidate, create=False)
            if errors:
                message = "修改路径被拒绝：" + "；".join(
                    str(item.get("message") or "") for item in errors
                )
                entry["last_action"] = {
                    "action": "修改路径",
                    "ok": False,
                    "message": message,
                }
                return entry, False, message, errors

            if entry.get("status") in ("待校验", "已停用"):
                validation = _validate_path(candidate, for_enable=False, context="update")
                if not validation["ok"]:
                    message = _rejection_message("修改路径", validation)
                    entry["last_action"] = {
                        "action": "修改路径",
                        "ok": False,
                        "message": message,
                    }
                    return entry, False, message, list(validation["errors"])

            if entry.get("status") == "已启用":
                # 已启用路径每次修改都按启用标准重新校验；层级改空或指标被清掉时
                # 整份拒绝，不允许把一个刚变成“不可执行”的路径静默保存。
                validation = _validate_path(candidate, for_enable=True, context="update")
                if not validation["ok"]:
                    message = _rejection_message("修改路径", validation)
                    entry["last_action"] = {
                        "action": "修改路径",
                        "ok": False,
                        "message": message,
                    }
                    return entry, False, message, list(validation["errors"])

            # 修改后仍需满足层数与层级的一致性；即使草稿也不能只留下半份维度。
            validation = _validate_path(candidate, for_enable=False, context="update")
            if not validation["ok"]:
                message = _rejection_message("修改路径", validation)
                entry["last_action"] = {
                    "action": "修改路径",
                    "ok": False,
                    "message": message,
                }
                return entry, False, message, list(validation["errors"])

            # 校验全部通过后才一次性写入，绝不出现“只写了一半维度”的中间态。
            entry.update(normalized)
            entry["version"] = version + 1
            message = "下钻路径已更新"
            entry["last_action"] = {
                "action": "修改路径",
                "ok": True,
                "message": message,
            }
            return entry, True, message, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any] | None, bool, str, list[dict[str, str]]]:
        """执行提交校验/启用/停用。返回 (条目, 是否生效, 说明, 拒绝原因)。"""
        with _WRITE_LOCK:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, False, f"下钻路径 {entry_id} 不存在或已归档", []
            if action not in ACTION_RULES:
                return entry, False, f"动作「{action}」不属于多维下钻可执行范围", []

            version = current_version(entry)
            if expected_version is not None and expected_version != version:
                message = (
                    f"下钻路径 {entry_id} 版本已过期：提交版本 {expected_version}，"
                    f"当前版本 {version}；本次动作未生效，请刷新后重试"
                )
                entry["last_action"] = {
                    "action": action,
                    "ok": False,
                    "message": message,
                }
                return entry, False, message, [
                    _issue("version", "version_conflict", message),
                ]

            target = ACTION_RULES[action]

            if action == "提交校验":
                validation = _validate_path(entry, for_enable=False, context="submit")
                if not validation["ok"]:
                    message = _rejection_message(action, validation)
                    entry["last_action"] = {
                        "action": action,
                        "ok": False,
                        "message": message,
                    }
                    return entry, False, message, list(validation["errors"])
                if entry.get("status") == "已启用":
                    message = "提交校验被拒绝：路径已启用，如需重新校验请先停用"
                    entry["last_action"] = {
                        "action": action,
                        "ok": False,
                        "message": message,
                    }
                    return entry, False, message, [
                        _issue("status", "status_conflict", message)
                    ]
                if entry.get("status") == target:
                    return entry, True, "路径已处于「待校验」，重复确认未重复生效", []
                entry["status"] = target
                entry["pending"] = True
                entry["abnormal"] = False
                entry["version"] = version + 1
                message = ACTION_MESSAGES[action]
                entry["last_action"] = {
                    "action": action,
                    "ok": True,
                    "message": message,
                }
                return entry, True, message, []

            if action == "启用路径":
                # 先完整校验（含跨模块聚合指标），全部通过前不碰 status/version。
                validation = _validate_path(entry, for_enable=True, context="enable")
                if not validation["ok"]:
                    message = _rejection_message(action, validation)
                    entry["last_action"] = {
                        "action": action,
                        "ok": False,
                        "message": message,
                    }
                    return entry, False, message, list(validation["errors"])
                if entry.get("status") == target:
                    return entry, True, "路径已处于「已启用」，重复确认未重复生效", []
                entry["status"] = target
                entry["pending"] = False
                entry["abnormal"] = False
                entry["version"] = version + 1
                message = ACTION_MESSAGES[action]
                entry["last_action"] = {
                    "action": action,
                    "ok": True,
                    "message": message,
                }
                return entry, True, message, []

            # 停用路径
            if entry.get("status") == target:
                return entry, True, "路径已处于「已停用」，重复确认未重复生效", []
            entry["status"] = target
            entry["pending"] = False
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            entry["version"] = version + 1
            message = ACTION_MESSAGES[action]
            entry["last_action"] = {
                "action": action,
                "ok": True,
                "message": message,
            }
            return entry, True, message, []
