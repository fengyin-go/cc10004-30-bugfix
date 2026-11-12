"""聚合计算业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

import copy
import re
from typing import Any

from app.store import store

MODULE = "aggregate"
REQUIRED_FIELDS = ["方案编号", "聚合函数", "目标字段"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用方案": "已启用", "停用方案": "已停用"}
NEGATIVE_ACTIONS = ["停用方案"]

# ---------------------------------------------------------------------------
# 聚合指标解析（跨模块契约）
#
# 多维下钻的「聚合指标」不自己解释指标文本，而是统一走这里：drill 只保存原文
# 与解析结果，启用时再通过 resolve_indicator 判断引用的聚合方案是否存在、是否
# 已启用。这样聚合模块和下游模块对「方案编号 / 聚合函数 / 目标字段」的理解只有
# 一份，不会各写一套解析规则。
# ---------------------------------------------------------------------------

FUNCTION_ALIASES: dict[str, str] = {
    "sum": "SUM",
    "求和": "SUM",
    "合计": "SUM",
    "总计": "SUM",
    "avg": "AVG",
    "average": "AVG",
    "mean": "AVG",
    "平均": "AVG",
    "均值": "AVG",
    "count": "COUNT",
    "计数": "COUNT",
    "数量": "COUNT",
    "count_distinct": "COUNT_DISTINCT",
    "distinct_count": "COUNT_DISTINCT",
    "countdistinct": "COUNT_DISTINCT",
    "去重计数": "COUNT_DISTINCT",
    "max": "MAX",
    "最大": "MAX",
    "最大值": "MAX",
    "min": "MIN",
    "最小": "MIN",
    "最小值": "MIN",
}

_SCHEME_FIELD_SEPARATORS = ("::", "：", ":", "/", "|", ".")
_EXPRESSION_RE = re.compile(r"^(?P<func>[^()（）]+?)\s*[（(]\s*(?P<field>[^()（）]+?)\s*[)）]\s*$")


def normalize_function(value: Any) -> str:
    """把聚合函数统一成可比对的形式；未知函数保留原文但做大小写归一。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return FUNCTION_ALIASES.get(text.casefold(), FUNCTION_ALIASES.get(text, text.upper()))


def _scheme_number(row: dict[str, Any]) -> str:
    return str(row.get("方案编号") or "").strip()


def _scheme_function(row: dict[str, Any]) -> str:
    return normalize_function(row.get("聚合函数"))


def _scheme_target(row: dict[str, Any]) -> str:
    return str(row.get("目标字段") or "").strip()


def _blank_parse(raw: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "",
        "原文": copy.deepcopy(raw),
        "形式": None,
        "方案编号": None,
        "聚合函数": None,
        "目标字段": None,
        "状态": "未解析",
    }


def parse_indicator(value: Any) -> dict[str, Any]:
    """解析聚合指标原文，只做结构归一，不判断方案是否存在或启用。

    支持的形式（按顺序识别）：

    * 对象：``{"方案编号": "AGGR-0003", "目标字段": "销售额"}``
    * 表达式：``SUM(销售额)`` / ``求和（销售额）``
    * 方案 + 字段：``AGGR-0003::销售额``（也接受 ``:``、``/``、``|``、``.``）
    * 方案引用：``AGGR-0003``
    * 字段引用：``销售额``（后续用聚合模块里唯一匹配的目标字段解析）
    """
    result = _blank_parse(value)

    if value is None:
        result["reason"] = "聚合指标缺失"
        return result
    if isinstance(value, dict):
        scheme = ""
        for key in ("方案编号", "方案", "指标方案", "scheme", "scheme_no", "plan"):
            if value.get(key) not in (None, ""):
                scheme = str(value[key]).strip()
                break
        function = ""
        for key in ("聚合函数", "函数", "function", "func"):
            if value.get(key) not in (None, ""):
                function = normalize_function(value[key])
                break
        target = ""
        for key in ("目标字段", "字段", "指标字段", "field", "target"):
            if value.get(key) not in (None, ""):
                target = str(value[key]).strip()
                break
        if not any((scheme, function, target)):
            result["reason"] = "聚合指标对象里没有可解析的方案编号、聚合函数或目标字段"
            return result
        result.update({
            "ok": True,
            "形式": "指标对象",
            "方案编号": scheme or None,
            "聚合函数": function or None,
            "目标字段": target or None,
            "状态": "已解析",
        })
        return result

    if not isinstance(value, str):
        result["reason"] = "聚合指标必须是文本、指标对象或方案引用"
        return result

    text = value.strip()
    if not text:
        result["reason"] = "聚合指标为空"
        return result

    match = _EXPRESSION_RE.match(text)
    if match:
        function = normalize_function(match.group("func"))
        target = match.group("field").strip()
        if not function or not target:
            result["reason"] = f"聚合指标「{text}」的聚合函数或目标字段为空"
            return result
        result.update({
            "ok": True,
            "形式": "表达式",
            "聚合函数": function,
            "目标字段": target,
            "状态": "已解析",
        })
        return result

    for separator in _SCHEME_FIELD_SEPARATORS:
        if separator in text:
            left, right = text.split(separator, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                result.update({
                    "ok": True,
                    "形式": "方案字段引用",
                    "方案编号": left,
                    "目标字段": right,
                    "状态": "已解析",
                })
                return result

    result.update({
        "ok": True,
        "形式": "方案或字段引用",
        "标识": text,
        "状态": "已解析",
    })
    return result


def _find_scheme(rows: list[dict[str, Any]], number: str) -> dict[str, Any] | None:
    target = number.strip().casefold()
    for row in rows:
        if _scheme_number(row).casefold() == target:
            return row
    return None


def _scheme_candidates(
    rows: list[dict[str, Any]],
    *,
    function: str | None,
    target: str | None,
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for row in rows:
        if function and _scheme_function(row) != function:
            continue
        if target and _scheme_target(row).casefold() != target.casefold():
            continue
        matched.append(row)
    return matched


def resolve_indicator(value: Any) -> dict[str, Any]:
    """把聚合指标解析并落到具体聚合方案。

    返回字段中 ``ok`` 表示“能否解析到方案”，``active`` 表示“该方案是否已启用”。
    下钻启用必须 ``ok and active`` 同时成立；仅解析成功但方案未启用同样视为指标失效。
    """
    parsed = parse_indicator(value)
    parsed.setdefault("active", False)
    parsed.setdefault("方案状态", None)
    if not parsed.get("ok"):
        return parsed

    rows = store.rows(MODULE)
    scheme = parsed.get("方案编号")
    target = parsed.get("目标字段")
    function = parsed.get("聚合函数")

    if scheme:
        row = _find_scheme(rows, scheme)
        if row is None:
            parsed.update({
                "ok": False,
                "状态": "方案不存在",
                "reason": f"聚合指标「{_indicator_text(value)}」引用的聚合方案 {scheme} 不存在",
            })
            return parsed
        if target and _scheme_target(row) and _scheme_target(row).casefold() != target.casefold():
            parsed.update({
                "ok": False,
                "状态": "字段不一致",
                "reason": (
                    f"聚合指标的目标字段「{target}」与聚合方案 {scheme} 的目标字段"
                    f"「{_scheme_target(row)}」不一致"
                ),
            })
            return parsed
        if function and _scheme_function(row) and _scheme_function(row) != function:
            parsed.update({
                "ok": False,
                "状态": "函数不一致",
                "reason": (
                    f"聚合指标的聚合函数「{function}」与聚合方案 {scheme} 的聚合函数"
                    f"「{_scheme_function(row)}」不一致"
                ),
            })
            return parsed
        parsed["方案编号"] = _scheme_number(row)
        parsed["聚合函数"] = _scheme_function(row) or function
        parsed["目标字段"] = _scheme_target(row) or target
    else:
        identifier = parsed.get("标识")
        explicit_identity = bool(identifier)
        if identifier:
            row = _find_scheme(rows, identifier)
            if row is not None:
                parsed.update({
                    "形式": "方案引用",
                    "方案编号": _scheme_number(row),
                    "聚合函数": _scheme_function(row),
                    "目标字段": _scheme_target(row),
                })
                status = str(row.get("status") or "")
                parsed["方案状态"] = status
                parsed["active"] = status == "已启用"
                parsed["状态"] = "已启用" if parsed["active"] else "未启用"
                parsed["reason"] = "" if parsed["active"] else (
                    f"聚合指标失效：聚合方案 {_scheme_number(row)} 当前状态为「{status or '未知'}」，"
                    "只有已启用的聚合方案才能用于下钻"
                )
                return parsed
            if explicit_identity:
                # 明确写了一个标识，但既不是方案编号也不是已登记的字段时，
                # 不能退回“按函数模糊匹配”，否则会把用户写错的编号悄悄换成别的方案。
                parsed.update({
                    "ok": False,
                    "状态": "未匹配方案",
                    "reason": (
                        f"聚合指标「{_indicator_text(value)}」在聚合计算模块中没有匹配的方案；"
                        "请填写方案编号、方案编号::目标字段，或 SUM(目标字段) 形式"
                    ),
                })
                return parsed
        candidates = _scheme_candidates(rows, function=function, target=target)
        if not candidates:
            parsed.update({
                "ok": False,
                "状态": "未匹配方案",
                "reason": (
                    f"聚合指标「{_indicator_text(value)}」在聚合计算模块中没有匹配的方案；"
                    "请填写方案编号、方案编号::目标字段，或 SUM(目标字段) 形式"
                ),
            })
            return parsed
        active_candidates = [row for row in candidates if row.get("status") == "已启用"]
        chosen_rows = active_candidates or candidates
        if len(chosen_rows) > 1:
            parsed.update({
                "ok": False,
                "状态": "匹配不唯一",
                "reason": (
                    f"聚合指标「{_indicator_text(value)}」匹配到多个聚合方案"
                    f"（{'、'.join(_scheme_number(row) for row in chosen_rows)}），"
                    "请写明方案编号"
                ),
            })
            return parsed
        row = chosen_rows[0]
        parsed.update({
            "方案编号": _scheme_number(row),
            "聚合函数": _scheme_function(row) or function,
            "目标字段": _scheme_target(row) or target,
        })

    status = str(row.get("status") or "")
    parsed["方案状态"] = status
    parsed["active"] = status == "已启用"
    parsed["状态"] = "已启用" if parsed["active"] else "未启用"
    if not parsed["active"]:
        parsed["reason"] = (
            f"聚合指标失效：聚合方案 {_scheme_number(row)} 当前状态为「{status or '未知'}」，"
            "只有已启用的聚合方案才能用于下钻"
        )
    else:
        parsed["reason"] = ""
    return parsed


def _indicator_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


class AggregateService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("方案编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚合方案 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于聚合计算可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"聚合方案已{action}"
