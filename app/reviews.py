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
- 判重先于请求体的完整内容校验：入口层先按宽松提取出的 commandId 查询，
  命中已占用标识即交由存储判重，使非法内容也得到 COMMAND_ID_REUSED；
  权威判重仍在临界区内与状态检查、写入一次完成，保证并发原子性；
- 判重检查、状态检查与写入在同一把进程内锁内一次完成，争用同一版本的
  多个命令只有一个原子成功，其余得到 409 且不留下部分状态。

存储为纯内存实现，服务重启即清空；本服务不依赖外部数据库。
"""

from __future__ import annotations

import json
import threading
import uuid

from app.rules import assess
from app.validation import (
    ACTION_CONFIRM,
    ACTION_REPLACE_ITEMS,
    ApiError,
    build_command_canonical,
    build_create_canonical,
    canonical_items,
)

DRAFT = "DRAFT"
CONFIRMED = "CONFIRMED"


def _reused_error(command_id: str) -> ApiError:
    return ApiError(
        "COMMAND_ID_REUSED",
        f"Command id '{command_id}' was already used with a different request.",
        status=409,
    )


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

    # ---- 判重查询 -------------------------------------------------------

    def lookup_command(
        self, command_id: str, canonical: tuple
    ) -> tuple[int, bytes] | None:
        """只读判重：在完整内容校验之前判定已占用的 commandId。

        - 标识未被成功命令占用 → 返回 ``None``，调用方继续严格校验与写入；
        - 标识已占用且指纹相同 → 返回首次成功响应，原样字节级重放；
        - 标识已占用但指纹不同 → 抛 ``COMMAND_ID_REUSED``（409）。

        本方法与权威写入判重使用同一把锁；写入路径在锁内会再次判定，因此
        查询与写入之间即使有并发命令抢先占用标识，结果仍以临界区内的权威
        判定为准，不会出现重放/重复写入。
        """
        with self._lock:
            existing = self._commands.get(command_id)
            if existing is None:
                return None
            if existing.canonical != canonical:
                raise _reused_error(command_id)
            return existing.status_code, existing.body

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

    # ---- 建草稿 ---------------------------------------------------------

    def create_review(
        self, hold: str, items: list[tuple[str, str]], command_id: str
    ) -> tuple[int, bytes]:
        canonical = build_create_canonical(hold, items)
        with self._lock:
            existing = self._commands.get(command_id)
            if existing is not None:
                if existing.canonical != canonical:
                    raise _reused_error(command_id)
                return existing.status_code, existing.body

            review_id = uuid.uuid4().hex
            review = _Review(review_id, hold, canonical_items(items), command_id)
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
        canonical = build_command_canonical(
            review_id, action, expected_revision, items
        )

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
                ordered_items = canonical_items(items)
                review.revisions.append(
                    _Revision(
                        review.revision + 1,
                        review.hold,
                        ordered_items,
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
