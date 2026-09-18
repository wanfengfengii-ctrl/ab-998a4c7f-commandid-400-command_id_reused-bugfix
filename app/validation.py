"""请求体校验：任何非法输入整体拒绝，并返回稳定错误代码。"""

from __future__ import annotations

from app.rules import CATEGORIES

MIN_ITEMS = 2
MAX_ITEMS = 20

# 预审命令类型。
ACTION_REPLACE_ITEMS = "REPLACE_ITEMS"
ACTION_CONFIRM = "CONFIRM"
COMMAND_ACTIONS = frozenset({ACTION_REPLACE_ITEMS, ACTION_CONFIRM})


class ApiError(Exception):
    """携带稳定错误代码与 HTTP 状态码的校验错误。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _validate_hold(payload: dict) -> str:
    hold = payload.get("hold")
    if not isinstance(hold, str) or not hold.strip():
        raise ApiError("EMPTY_HOLD", "Field 'hold' must be a non-empty string.")
    return hold


def _validate_items(raw_items: object) -> list[tuple[str, str]]:
    """校验 ``items`` 数组，返回 ``[(编号, 类别), ...]``。

    校验顺序固定：数组类型 → 数量 → 逐项（编号 → 类别 → 编号唯一性），
    保证同类非法输入永远得到同一个错误代码。
    """
    if not isinstance(raw_items, list):
        raise ApiError("INVALID_REQUEST", "Field 'items' must be a JSON array.")

    count = len(raw_items)
    if not MIN_ITEMS <= count <= MAX_ITEMS:
        raise ApiError(
            "ITEM_COUNT_OUT_OF_RANGE",
            f"Field 'items' must contain between {MIN_ITEMS} and {MAX_ITEMS} "
            f"entries; got {count}.",
        )

    seen_ids: set[str] = set()
    items: list[tuple[str, str]] = []
    for index, entry in enumerate(raw_items):
        if not isinstance(entry, dict):
            raise ApiError(
                "INVALID_REQUEST", f"Item at index {index} must be a JSON object."
            )
        item_id = entry.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ApiError(
                "INVALID_ITEM_ID",
                f"Item at index {index} must have a non-empty string 'id'.",
            )
        category = entry.get("category")
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ApiError(
                "UNKNOWN_CATEGORY",
                f"Item '{item_id}' has unknown category {category!r}; "
                f"expected one of {sorted(CATEGORIES)}.",
            )
        if item_id in seen_ids:
            raise ApiError(
                "DUPLICATE_ITEM_ID", f"Duplicate item id '{item_id}'."
            )
        seen_ids.add(item_id)
        items.append((item_id, category))

    return items


def validate_payload(payload: object) -> tuple[str, list[tuple[str, str]]]:
    """校验并解析请求体，返回 ``(舱位, [(编号, 类别), ...])``。

    校验顺序固定：整体结构 → 舱位 → 货项数量 → 逐项（编号 → 类别 →
    编号唯一性），保证同类非法输入永远得到同一个错误代码。
    """
    if not isinstance(payload, dict):
        raise ApiError("INVALID_REQUEST", "Request body must be a JSON object.")

    hold = _validate_hold(payload)
    items = _validate_items(payload.get("items"))
    return hold, items


def _validate_command_id(payload: dict) -> str:
    command_id = payload.get("commandId")
    if not isinstance(command_id, str) or not command_id.strip():
        raise ApiError(
            "INVALID_COMMAND_ID",
            "Field 'commandId' must be a non-empty string.",
        )
    return command_id


def validate_review_create_payload(
    payload: object,
) -> tuple[str, list[tuple[str, str]], str]:
    """校验建草稿请求，返回 ``(舱位, [(编号, 类别), ...], commandId)``。

    复用 ``assess`` 的全部货项校验，非法输入整体拒绝、不产生草稿。
    """
    if not isinstance(payload, dict):
        raise ApiError("INVALID_REQUEST", "Request body must be a JSON object.")

    hold = _validate_hold(payload)
    items = _validate_items(payload.get("items"))
    command_id = _validate_command_id(payload)
    return hold, items, command_id


def validate_review_command_payload(
    payload: object,
) -> tuple[str, str, int, list[tuple[str, str]] | None]:
    """校验草稿命令，返回 ``(commandId, action, expectedRevision, items)``。

    ``items`` 仅在 ``REPLACE_ITEMS`` 时存在并经过与 ``assess`` 完全相同的
    校验；``CONFIRM`` 时为 ``None``。
    """
    if not isinstance(payload, dict):
        raise ApiError("INVALID_REQUEST", "Request body must be a JSON object.")

    command_id = _validate_command_id(payload)

    action = payload.get("action")
    if action not in COMMAND_ACTIONS:
        raise ApiError(
            "INVALID_ACTION",
            f"Field 'action' must be one of {sorted(COMMAND_ACTIONS)}; "
            f"got {action!r}.",
        )

    expected_revision = payload.get("expectedRevision")
    # bool 是 int 的子类，需显式排除（True/False 不是合法版本号）。
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise ApiError(
            "INVALID_REVISION",
            "Field 'expectedRevision' must be a positive integer.",
        )

    items: list[tuple[str, str]] | None = None
    if action == ACTION_REPLACE_ITEMS:
        items = _validate_items(payload.get("items"))

    return command_id, action, expected_revision, items


# ---- commandId 判重指纹 --------------------------------------------------
#
# 判重必须先于完整内容校验：已成功占用的 commandId 再次出现时，即使新请求体
# 非法，也要先得到 COMMAND_ID_REUSED，而不是 400。但构造指纹又不能触发严格
# 校验抛错，因此下面提供“宽松预解析”：只要求 commandId 本身可用作判重键，
# 其余字段按原值取样，并保证任何非法取值构造出的指纹都不可能与存储中成功
# 命令的合法指纹相等（类型/长度/取值域不同），从而稳定判为“不同内容”。

_INVALID = "<invalid>"


def canonical_items(items: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """规范化货项：按编号排序，使录入次序不影响判重与快照。"""
    return tuple(sorted(items, key=lambda item: item[0]))


def build_create_canonical(
    hold: str, items: list[tuple[str, str]]
) -> tuple:
    """建草稿请求的判重指纹（CREATE 标记防止与命令混用同一 commandId）。"""
    return ("CREATE", hold, canonical_items(items))


def build_command_canonical(
    review_id: str,
    action: str,
    expected_revision: int,
    items: list[tuple[str, str]] | None,
) -> tuple:
    """草稿命令的判重指纹。"""
    if action == ACTION_REPLACE_ITEMS:
        assert items is not None
        return (
            ACTION_REPLACE_ITEMS,
            review_id,
            expected_revision,
            canonical_items(items),
        )
    return (ACTION_CONFIRM, review_id, expected_revision)


def _lenient_hold(raw: object) -> object:
    # 合法指纹中 hold 必为非空白字符串；空白串/非字符串不可能与之相等。
    return raw if isinstance(raw, str) else (_INVALID, "hold", raw)


def _lenient_items(raw: object) -> object:
    """宽松取样 items，绝不抛错。

    全部条目都是字符串二元组时（类别可能非法、编号可能重复/为空、数量可能
    越界），与合法指纹一样按编号排序；这些非法之处本身就保证指纹不可能命
    中存储记录。出现非对象条目或非字符串字段时，混入长度不同的标记元组，
    结构上即与合法的 ``((str, str), ...)`` 不同，也免去混合类型排序问题。
    """
    if not isinstance(raw, list):
        return (_INVALID, "items", raw)

    pairs: list[tuple] = []
    all_string_pairs = True
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            pairs.append((_INVALID, "item", index, entry))
            all_string_pairs = False
            continue
        item_id = entry.get("id")
        category = entry.get("category")
        if not isinstance(item_id, str) or not isinstance(category, str):
            pairs.append((_INVALID, "field", item_id, category))
            all_string_pairs = False
        else:
            pairs.append((item_id, category))

    if all_string_pairs:
        return canonical_items(pairs)
    return tuple(pairs)


def _lenient_revision(raw: object) -> object:
    # bool 是 int 子类，必须先排除：True 不能被当作版本号 1 命中存储指纹。
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return (_INVALID, "revision", raw)
    return raw


def preparse_create_dedup(payload: object) -> tuple[str, tuple] | None:
    """建草稿请求的判重预解析，返回 ``(commandId, 指纹)``。

    仅在请求体为对象且 ``commandId`` 为非空白字符串时返回指纹；否则返回
    ``None``，调用方继续走严格校验以得到 INVALID_REQUEST /
    INVALID_COMMAND_ID 等 400 错误。
    """
    if not isinstance(payload, dict):
        return None
    command_id = payload.get("commandId")
    if not isinstance(command_id, str) or not command_id.strip():
        return None
    canonical = (
        "CREATE",
        _lenient_hold(payload.get("hold")),
        _lenient_items(payload.get("items")),
    )
    return command_id, canonical


def preparse_command_dedup(
    payload: object, review_id: str
) -> tuple[str, tuple] | None:
    """草稿命令的判重预解析，返回 ``(commandId, 指纹)``。

    ``review_id`` 取自路径，恒为字符串。其余字段宽松取样；任何非法取值都
    使指纹不可能等于存储中的成功命令指纹。
    """
    if not isinstance(payload, dict):
        return None
    command_id = payload.get("commandId")
    if not isinstance(command_id, str) or not command_id.strip():
        return None

    action = payload.get("action")
    revision = _lenient_revision(payload.get("expectedRevision"))
    if action == ACTION_REPLACE_ITEMS:
        canonical = (
            ACTION_REPLACE_ITEMS,
            review_id,
            revision,
            _lenient_items(payload.get("items")),
        )
    elif action == ACTION_CONFIRM:
        canonical = (ACTION_CONFIRM, review_id, revision)
    else:
        # 合法指纹首元素只能是 REPLACE_ITEMS / CONFIRM。
        canonical = (_INVALID, "action", action)
    return command_id, canonical
