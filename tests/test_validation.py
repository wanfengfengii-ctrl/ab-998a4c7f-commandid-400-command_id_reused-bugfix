"""判重预解析判据：完整校验前宽松构造的指纹与存储指纹必须一致/安全。

核心不变量：

- 对“合法但录入次序不同”的请求，宽松指纹必须与严格构造的存储指纹逐字节
  相同，从而走字节级重放；
- 对任何非法取值，宽松指纹都不得与既有成功命令的合法指纹相等（否则会把
  非法内容误判成重放），即稳定落到 COMMAND_ID_REUSED；
- 提取不出可用 commandId 时返回 ``None``，交回严格校验报 400。
"""

from __future__ import annotations

from app.validation import (
    build_command_canonical,
    build_create_canonical,
    canonical_items,
    preparse_command_dedup,
    preparse_create_dedup,
)

VALID_ITEMS = [
    {"id": "C330", "category": "WET"},
    {"id": "C101", "category": "FLAM"},
    {"id": "C205", "category": "OXID"},
]
VALID_ITEM_PAIRS = [
    ("C330", "WET"),
    ("C101", "FLAM"),
    ("C205", "OXID"),
]


def test_valid_create_preparse_fingerprint_matches_store_canonical():
    payload = {"hold": "HOLD-3", "commandId": "c1", "items": VALID_ITEMS}
    command_id, canonical = preparse_create_dedup(payload)
    assert command_id == "c1"
    assert canonical == build_create_canonical("HOLD-3", VALID_ITEM_PAIRS)
    # 录入次序不影响指纹。
    reordered = dict(payload)
    reordered["items"] = list(reversed(VALID_ITEMS))
    assert preparse_create_dedup(reordered)[1] == canonical


def test_valid_command_preparse_fingerprint_matches_store_canonical():
    payload = {
        "commandId": "c2",
        "action": "REPLACE_ITEMS",
        "expectedRevision": 1,
        "items": VALID_ITEMS,
    }
    command_id, canonical = preparse_command_dedup(payload, "review-1")
    assert command_id == "c2"
    assert canonical == build_command_canonical(
        "review-1", "REPLACE_ITEMS", 1, VALID_ITEM_PAIRS
    )

    confirm = {"commandId": "c3", "action": "CONFIRM", "expectedRevision": 2}
    cid, confirm_canonical = preparse_command_dedup(confirm, "review-1")
    assert cid == "c3"
    assert confirm_canonical == build_command_canonical(
        "review-1", "CONFIRM", 2, None
    )


def _valid_stored_create():
    return build_create_canonical("HOLD-3", VALID_ITEM_PAIRS)


def test_invalid_create_payloads_never_collide_with_stored_fingerprint():
    stored = _valid_stored_create()
    invalid_payloads = [
        {"hold": "   ", "commandId": "x", "items": VALID_ITEMS},
        {"hold": 7, "commandId": "x", "items": VALID_ITEMS},
        {"hold": None, "commandId": "x", "items": VALID_ITEMS},
        {"hold": "HOLD-3", "commandId": "x", "items": "not-a-list"},
        {"hold": "HOLD-3", "commandId": "x", "items": []},
        {"hold": "HOLD-3", "commandId": "x", "items": [
            {"id": "A1", "category": "FLAM"}]},
        {"hold": "HOLD-3", "commandId": "x", "items": [
            {"id": "A1", "category": "FLAM"}, "broken"]},
        {"hold": "HOLD-3", "commandId": "x", "items": [
            {"id": "A1", "category": "FLAM"},
            {"id": "B2", "category": "RADIO"}]},
        {"hold": "HOLD-3", "commandId": "x", "items": [
            {"id": "A1", "category": "FLAM"},
            {"id": "A1", "category": "GAS"}]},
        {"hold": "HOLD-3", "commandId": "x", "items": [
            {"id": None, "category": "FLAM"},
            {"id": "B2", "category": "GAS"}]},
        {"hold": "HOLD-3", "commandId": "x", "items": [
            {"id": "A1", "category": 9},
            {"id": "B2", "category": "GAS"}]},
    ]
    for payload in invalid_payloads:
        _, canonical = preparse_create_dedup(payload)
        assert canonical != stored, payload


def test_invalid_command_payloads_never_collide_with_stored_fingerprints():
    replace_stored = build_command_canonical(
        "review-1", "REPLACE_ITEMS", 1, [("A1", "GAS"), ("B2", "WET")]
    )
    confirm_stored = build_command_canonical("review-1", "CONFIRM", 1, None)

    invalid_commands = [
        # 非法 revision：0、负数、布尔、字符串。
        {"commandId": "x", "action": "CONFIRM", "expectedRevision": 0},
        {"commandId": "x", "action": "CONFIRM", "expectedRevision": -3},
        {"commandId": "x", "action": "CONFIRM", "expectedRevision": True},
        {"commandId": "x", "action": "CONFIRM", "expectedRevision": "1"},
        # 非法 action。
        {"commandId": "x", "action": "FREEZE", "expectedRevision": 1},
        # 替换货项非法。
        {"commandId": "x", "action": "REPLACE_ITEMS", "expectedRevision": 1,
         "items": [{"id": "A1", "category": "FLAM"},
                   {"id": "B2", "category": "RADIO"}]},
        {"commandId": "x", "action": "REPLACE_ITEMS", "expectedRevision": 1,
         "items": [{"id": "A1", "category": "FLAM"}]},
    ]
    for payload in invalid_commands:
        _, canonical = preparse_command_dedup(payload, "review-1")
        assert canonical != replace_stored, payload
        assert canonical != confirm_stored, payload


def test_bool_revision_is_not_treated_as_one():
    # True 不得被当作版本号 1 而与合法 CONFIRM 指纹相等。
    _, bool_canonical = preparse_command_dedup(
        {"commandId": "x", "action": "CONFIRM", "expectedRevision": True},
        "review-1",
    )
    assert bool_canonical != build_command_canonical("review-1", "CONFIRM", 1, None)


def test_different_review_id_yields_different_fingerprint():
    payload = {"commandId": "x", "action": "CONFIRM", "expectedRevision": 1}
    assert preparse_command_dedup(payload, "review-a")[1] != preparse_command_dedup(
        payload, "review-b"
    )[1]


def test_unusable_command_id_returns_none():
    assert preparse_create_dedup("not-an-object") is None
    assert preparse_create_dedup({"hold": "H", "items": []}) is None
    assert preparse_create_dedup(
        {"hold": "H", "commandId": "  ", "items": []}
    ) is None
    assert preparse_create_dedup({"hold": "H", "commandId": 9, "items": []}) is None

    assert preparse_command_dedup("not-an-object", "review-1") is None
    assert preparse_command_dedup({}, "review-1") is None
    assert preparse_command_dedup({"commandId": ""}, "review-1") is None


def test_canonical_items_sorts_by_id():
    assert canonical_items(VALID_ITEM_PAIRS) == (
        ("C101", "FLAM"),
        ("C205", "OXID"),
        ("C330", "WET"),
    )
