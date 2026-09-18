"""危险品配载交接审核：草稿—命令—确认的版本化存储。

设计要点：

- ``POST /reviews`` 以 (舱位, 货项, commandId) 建立 ``revision=1`` 的草稿，
  保存规范化请求（货项按编号排序）与复用规则引擎得到的裁决；
- ``POST /reviews/{id}/commands`` 支持 ``REPLACE_ITEMS``（替换货项并把
  版本号加一）与 ``CONFIRM``（冻结当前快照）；
- 每个成功命令先按 ``commandId`` 全局判重：同标识同内容原样重放首次成功
  响应（字节一致），同标识不同内容返回 ``COMMAND_ID_REUSED``；失败命令
  （404/409/400）不写入任何索引，因而冲突命令可用同一 commandId 携带新的
  ``expectedRevision`` 重试；
- 判重先于请求校验：``replay_create`` / ``replay_command`` 在完整校验之
  前用原始请求体尽力规范化判重键，已占用标识直接给出重放或 409；未占用
  时才进入校验，非法内容按 400 拒绝且不占用标识；
- 判重检查、状态检查与写入在同一把进程内锁内一次完成，争用同一版本的
  多个命令只有一个原子成功，其余得到 409 且不留下部分状态。

存储为纯内存实现，服务重启即清空；本服务不依赖外部数据库。
"""

from __future__ import annotations

import json
import threading
import uuid

from app.rules import assess
from app.validation import ACTION_CONFIRM, ACTION_REPLACE_ITEMS, ApiError

DRAFT = "DRAFT"
CONFIRMED = "CONFIRMED"


def _canonical_items(items: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """规范化货项：按编号排序，使录入次序不影响判重与快照。"""
    return tuple(sorted(items, key=lambda item: item[0]))


def _reused_error(command_id: str) -> ApiError:
    return ApiError(
        "COMMAND_ID_REUSED",
        f"Command id '{command_id}' was already used with a "
        "different request.",
        status=409,
    )


def _lenient_items_key(raw_items: object) -> tuple[tuple[str, str], ...] | None:
    """尽力把原始 ``items`` 规范化为判重键。

    成功请求的货项一定是 ``(字符串编号, 字符串类别)``；形状偏离即不可能
    与任何已占用标识同内容，返回 ``None`` 表示“无法同内容”。
    """
    if not isinstance(raw_items, list):
        return None
    items: list[tuple[str, str]] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            return None
        item_id = entry.get("id")
        category = entry.get("category")
        if not isinstance(item_id, str) or not isinstance(category, str):
            return None
        items.append((item_id, category))
    return tuple(sorted(items))


def _create_key_from_payload(payload: object) -> tuple | None:
    """从原始请求体提取建草稿判重键，格式与 ``create_review`` 的 canonical 一致。"""
    if not isinstance(payload, dict):
        return None
    items_key = _lenient_items_key(payload.get("items"))
    if items_key is None:
        return None
    return ("CREATE", payload.get("hold"), items_key)


def _command_key_from_payload(review_id: str, payload: object) -> tuple | None:
    """从原始请求体提取命令判重键，格式与 ``apply_command`` 的 canonical 一致。"""
    if not isinstance(payload, dict):
        return None
    expected_revision = payload.get("expectedRevision")
    # bool 不是合法版本号，已占用标识的成功记录里也不会出现。
    if isinstance(expected_revision, bool) or not isinstance(
        expected_revision, int
    ):
        return None
    action = payload.get("action")
    if action == ACTION_REPLACE_ITEMS:
        items_key = _lenient_items_key(payload.get("items"))
        if items_key is None:
            return None
        return (ACTION_REPLACE_ITEMS, review_id, expected_revision, items_key)
    if action == ACTION_CONFIRM:
        return (ACTION_CONFIRM, review_id, expected_revision)
    return None


def _render_body(content: dict) -> bytes:
    """按 ``JSONResponse`` 相同的设置渲染响应体，保证重放字节一致。"""
    return json.dumps(
        content, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


class _Revision:
    """某一版本的规范化请求与裁决快照。"""

    def __init__(
        self,
        revision: int,
        hold: str,
        items: tuple[tuple[str, str], ...],
        command_id: str,
        action: str,
    ) -> None:
        self.revision = revision
        self.hold = hold
        self.items = items
        self.command_id = command_id
        self.action = action
        self.verdict = assess(list(items))


class _Review:
    def __init__(
        self,
        review_id: str,
        hold: str,
        items: tuple[tuple[str, str], ...],
        command_id: str,
    ) -> None:
        self.review_id = review_id
        self.hold = hold
        self.status = DRAFT
        self.revisions: list[_Revision] = [
            _Revision(1, hold, items, command_id, "CREATE")
        ]

    @property
    def revision(self) -> int:
        return self.revisions[-1].revision

    @property
    def current(self) -> _Revision:
        return self.revisions[-1]


class _CommandRecord:
    """一次成功命令的判重记录：规范化内容 + 首次响应字节。"""

    def __init__(
        self, canonical: tuple, review_id: str, status_code: int, body: bytes
    ) -> None:
        self.canonical = canonical
        self.review_id = review_id
        self.status_code = status_code
        self.body = body


class ReviewStore:
    """线程安全的审核存储；所有公开方法均为原子操作。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reviews: dict[str, _Review] = {}
        self._commands: dict[str, _CommandRecord] = {}

    # ---- 响应渲染 -------------------------------------------------------

    @staticmethod
    def _body_for(review: _Review, command_id: str) -> dict:
        current = review.current
        return {
            "reviewId": review.review_id,
            "revision": current.revision,
            "status": review.status,
            "commandId": command_id,
            "hold": current.hold,
            "items": [
                {"id": item_id, "category": category}
                for item_id, category in current.items
            ],
            "conclusion": current.verdict["conclusion"],
            "evidence": current.verdict["evidence"],
        }

    def _success(
        self,
        command_id: str,
        canonical: tuple,
        review: _Review,
        status_code: int,
    ) -> tuple[int, bytes]:
        body = _render_body(self._body_for(review, command_id))
        # 命令记录与状态变更在同一临界区内落盘，保证“仅一个原子成功”。
        self._commands[command_id] = _CommandRecord(
            canonical, review.review_id, status_code, body
        )
        return status_code, body

    # ---- 判重前置（先于请求校验）----------------------------------------

    def replay_create(
        self, command_id: str, payload: object
    ) -> tuple[int, bytes] | None:
        """建草稿的判重前置检查，语义见 ``_replay``。"""
        return self._replay(command_id, _create_key_from_payload(payload))

    def replay_command(
        self, review_id: str, command_id: str, payload: object
    ) -> tuple[int, bytes] | None:
        """下命令的判重前置检查，语义见 ``_replay``。"""
        return self._replay(
            command_id, _command_key_from_payload(review_id, payload)
        )

    def _replay(
        self, command_id: str, canonical: tuple | None
    ) -> tuple[int, bytes] | None:
        """已占用标识直接定结果：同内容重放首次响应，异内容（含无法规范化
        的非法内容）抛 ``COMMAND_ID_REUSED``；未占用返回 ``None``，由调用方
        继续完整校验。"""
        with self._lock:
            existing = self._commands.get(command_id)
            if existing is None:
                return None
            if canonical is None or existing.canonical != canonical:
                raise _reused_error(command_id)
            return existing.status_code, existing.body

    # ---- 建草稿 ---------------------------------------------------------

    def create_review(
        self, hold: str, items: list[tuple[str, str]], command_id: str
    ) -> tuple[int, bytes]:
        canonical_items = _canonical_items(items)
        # CREATE 标记确保同一 commandId 不能跨建草稿与下命令混用。
        canonical = ("CREATE", hold, canonical_items)
        with self._lock:
            existing = self._commands.get(command_id)
            if existing is not None:
                if existing.canonical != canonical:
                    raise _reused_error(command_id)
                return existing.status_code, existing.body

            review_id = uuid.uuid4().hex
            review = _Review(review_id, hold, canonical_items, command_id)
            self._reviews[review_id] = review
            return self._success(command_id, canonical, review, 201)

    # ---- 下命令 ---------------------------------------------------------

    def apply_command(
        self,
        review_id: str,
        command_id: str,
        action: str,
        expected_revision: int,
        items: list[tuple[str, str]] | None,
    ) -> tuple[int, bytes]:
        if action == ACTION_REPLACE_ITEMS:
            assert items is not None
            canonical = (
                "REPLACE_ITEMS",
                review_id,
                expected_revision,
                _canonical_items(items),
            )
        else:
            canonical = ("CONFIRM", review_id, expected_revision)

        with self._lock:
            existing = self._commands.get(command_id)
            if existing is not None:
                if existing.canonical != canonical:
                    raise _reused_error(command_id)
                return existing.status_code, existing.body

            # 新命令的固定报错顺序：
            # REVIEW_NOT_FOUND → REVISION_CONFLICT → REVIEW_FINALIZED。
            review = self._reviews.get(review_id)
            if review is None:
                raise ApiError(
                    "REVIEW_NOT_FOUND",
                    f"Review '{review_id}' does not exist.",
                    status=404,
                )
            if expected_revision != review.revision:
                raise ApiError(
                    "REVISION_CONFLICT",
                    f"Expected revision {expected_revision} but review "
                    f"'{review_id}' is at revision {review.revision}.",
                    status=409,
                )
            if review.status == CONFIRMED:
                raise ApiError(
                    "REVIEW_FINALIZED",
                    f"Review '{review_id}' is already confirmed and frozen.",
                    status=409,
                )

            if action == ACTION_REPLACE_ITEMS:
                assert items is not None
                canonical_items = _canonical_items(items)
                review.revisions.append(
                    _Revision(
                        review.revision + 1,
                        review.hold,
                        canonical_items,
                        command_id,
                        ACTION_REPLACE_ITEMS,
                    )
                )
            else:
                # 确认不推进版本号，只冻结当前快照。
                review.status = CONFIRMED

            return self._success(command_id, canonical, review, 200)


# 进程级单例存储。
store = ReviewStore()
