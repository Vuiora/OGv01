"""M4 假说对象与不可覆盖版本（P07 交付物）。

本模块把《SDL算法框架说明》§3 的假说对象 ``H`` 与 ``INTERFACES.md`` §2.3 的
字段约束落成一个**可构造、可校验、可序列化、不可覆盖**的数据结构，并配套一个
只追加（append-only）的版本仓库 :class:`HypothesisStore`。

本阶段只做**对象与版本链本身**。它不产生假说、不评估假说、不计算任何指标：

    P07（本模块）      定义 H 的形状、版本链、字段校验
    P08（generate.py） 从 M3 的模式生成假说、补齐零假说与竞争解释
    P09（metrics.py）  计算 G / S / N / C

公开接口
--------

- :class:`Hypothesis`：不可变的假说对象，字段与框架说明 §3 的 ``H`` 一一对应。
- :class:`HypothesisStore`：只追加的版本仓库，负责「版本不可覆盖」的强制。
- :func:`make_identity` / :func:`parse_identity` / :func:`bump_version`：
  版本标识的构造与解析。
- :func:`require_provenance` / :func:`require_representation`：把两条字段级契约
  抽成可独立调用的校验函数，便于 P08 在构造前预检。

三条验收标准与实现对应关系
--------------------------

① **同一 ID 的新版本保留 ``parent_ids`` 链**
   :meth:`Hypothesis.new_version` 不接受调用方传入 ``parent_ids``——该字段是
   **派生值**：新版本的 ``parent_ids`` 恒为「父版本的 ``parent_ids`` + 父版本自身的
   标识」。因此链只会被**追加**，不会被覆盖或清空；跨代修订时链条完整保留
   （v1 → v2 得 ``["h@1"]``，v2 → v3 得 ``["h@1", "h@2"]``）。
   调用方若试图在 ``changes`` 里塞入 ``parent_ids`` 或 ``id``，一律报错。

② **已发布版本不可原地修改（修改需生成新版本）**
   三层保障，逐层收紧：

   - **对象层**：:class:`Hypothesis` 是 frozen dataclass，且所有嵌套容器在构造时
     被**深度冻结**（映射转为只读视图、序列转为元组）。因此
     ``h.statement = ...`` 与 ``h.provenance["data_ids"] = ...`` 都会失败——
     不只是顶层属性，嵌套内容同样改不动。
   - **生命周期层**：``status`` 在 ``draft`` / ``published`` / ``retired`` 三态间
     迁移。``draft`` 允许 :meth:`Hypothesis.publish`（仅改状态）与
     :meth:`Hypothesis.with_evidence`（仅追加日志）；转为 ``published`` 之后，
     追加证据也会被拒绝，必须走 :meth:`Hypothesis.new_version`。
   - **仓库层**：:class:`HypothesisStore` 只追加。:meth:`~HypothesisStore.register`
     遇到已存在的 ``id@version`` 直接报错（没有「覆盖」这个 API）；
     :meth:`~HypothesisStore.revise` 从已发布版本派生**新标识**的新版本，
     被修订的已发布版本在仓库中保持逐字节不变。

③ **``provenance`` 必填字段缺失时报错而非默认填充**
   本模块**不提供任何 provenance 缺省值**——没有默认 ``generator``、没有默认
   ``code_version``、没有默认 ``knowledge_version``。``provenance`` 整体缺失、
   不是映射、或缺少四个必填键（``data_ids`` / ``knowledge_version`` /
   ``generator`` / ``code_version``）中的任意一个，均抛
   :class:`HypothesisProvenanceError`。同样地，``representation`` 必须显式写明
   缺失值处理与标准化（``INTERFACES.md`` §2.3 要求「完整变换」）。

版本号约定
----------

``version`` 是**正整数字符串**（``"1"`` / ``"2"`` …），与 ``IMPLEMENTATION_CONTRACT.md``
的 frozen plan 夹具（``"version": "1"``）一致。选择整数串而非语义化版本，是为了让
「下一版是什么」有唯一确定答案，从而 :meth:`Hypothesis.new_version` 可自动递增，
不需要调用方记版本号。

假说标识 ``id`` 的字符集被限制为 ``[A-Za-z0-9._:-]``，首字符必须是字母或数字。
原因：标识需要与版本号拼成 ``id@version`` 形式的引用（见 :func:`make_identity`），
若允许 ``@``，解析就不再唯一。

错误约定
--------

- **契约违规** → :class:`HypothesisValidationError`：字段缺失、类型不对、
  取值不在受控词表内、键名未知、数值非有限等。
- **来源信息违规** → :class:`HypothesisProvenanceError`
  （:class:`HypothesisValidationError` 子类）：``provenance`` 缺失或缺少必填键。
- **覆盖／原地修改尝试** → :class:`HypothesisImmutabilityError`：
  重复注册同一标识、对已发布版本追加证据、重复发布、修订已归档版本、
  修订时试图指定派生字段。

三者的基类都是 :class:`HypothesisError`（进而 :class:`ValueError`），
便于调用方按粒度捕获。

设计边界（严格遵守 P07 增量卡）
--------------------------------

- **不生成假说**（属 P08）：本模块没有任何接受 M3 模式对象作为入参的函数，
  也不导入 ``sdl_m02`` / ``sdl_m03``（P07 的依赖只有 P00）。假说的 ``type`` 由调用方
  声明，本模块只校验它落在受控词表内。
- **不计算任何指标**（属 P09）：``complexity`` / ``development`` /
  ``confirmation_plan`` 三个字段本模块**只存放、不推导**；没有 ``gain``、
  ``novelty``、``stability``、``complexity`` 的计算函数，也没有加权求和。
- **不授予证据等级**：``status`` 只是版本对象的**生命周期标记**，不是证据等级。
  证据等级（E0/E1/E2）属 P15 的发现档案，本模块既不读取也不写入。
- **不读数据、不碰令牌**：本模块不访问 M1 的任何接口，不读取数据集或数据库。
  ``provenance.data_ids`` 仅是**引用标识的字符串列表**，不携带任何记录正文。
  另设一道防线：``provenance`` 与 ``evidence_log`` 中若出现令牌类键名
  （见 :data:`FORBIDDEN_KEYS`），直接报错，避免令牌正文被写进假说对象。
- **不做去重**（属 P08）：本模块按标识判等，不做语义去重或等价公式合并。

已知限制
--------

1. **``scope`` / ``representation`` / ``predictions`` 只做结构校验，不做语义校验。**
   本模块能确认 ``predictions`` 里每条都写了 ``incompatible_with``，
   但无法判断这条「不相容声明」在科学上是否恰当。语义审查属 P08 及之后的人工复核。
2. **不做跨库一致性检查。** :class:`HypothesisStore` 只能保证**本仓库内**的标识唯一
   与父子链可解析；它不知道别处是否已存在同名标识，也不与 P15 的归档对账。
3. **``strict_chain`` 默认关闭。** 开启后 :meth:`HypothesisStore.register` 会要求
   每个父标识都已在**本仓库**中——这在「分批复建历史」的场景下会误伤，
   因此默认不启用，由调用方按需选择。
4. **深度冻结改变容器类型。** 传入的 ``list`` 会被转为 ``tuple``、``dict`` 转为只读视图；
   :meth:`Hypothesis.to_dict` 会还原为可 JSON 序列化的 ``list`` / ``dict``，
   但若调用方直接读属性（如 ``h.predictions``），拿到的是元组而非列表。
5. **``complexity`` 的口径未在本层固定。** 框架说明 §4 明确指出「公式长度近似不能
   宣称为严格 MDL」。本模块只保留该字段的槽位并原样存放，口径统一由 P09 负责，
   以免在 P07 就把一个未定口径写死。
6. **不做并发写入保护。** :class:`HypothesisStore` 是内存对象，不是事务型存储；
   持久化与并发一致性属 M8（P15）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

__all__ = [
    "HypothesisError",
    "HypothesisValidationError",
    "HypothesisProvenanceError",
    "HypothesisImmutabilityError",
    "HYPOTHESIS_MODULE_VERSION",
    "HYPOTHESIS_STATUSES",
    "STATUS_DRAFT",
    "STATUS_PUBLISHED",
    "STATUS_RETIRED",
    "HYPOTHESIS_TYPES",
    "PROVENANCE_REQUIRED_KEYS",
    "REPRESENTATION_REQUIRED_KEYS",
    "HYPOTHESIS_FIELDS",
    "FORBIDDEN_KEYS",
    "IDENTITY_SEPARATOR",
    "bump_version",
    "make_identity",
    "parse_identity",
    "require_provenance",
    "require_representation",
    "Hypothesis",
    "HypothesisStore",
]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 交付物版本号，写入序列化结果，便于追溯对象形状的变更。
HYPOTHESIS_MODULE_VERSION = "P07-v1.0"

#: 版本对象的生命周期状态。**不是证据等级**（证据等级属 P15）。
STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"
STATUS_RETIRED = "retired"

#: ``status`` 的受控词表。
HYPOTHESIS_STATUSES: tuple[str, ...] = (STATUS_DRAFT, STATUS_PUBLISHED, STATUS_RETIRED)

#: 只读状态集合，供内部快速判定。
_PUBLISHED_STATES: frozenset[str] = frozenset({STATUS_PUBLISHED, STATUS_RETIRED})

#: 假说的受控类型词表。前四项与 ``INTERFACES.md`` §2.2 的 Pattern.kind 对齐，
#: 使 P08 从模式生成假说时能直接映射；``composite`` 用于「多个模式的组合解释」。
#: 本模块只做词表校验，不判断类型选择是否恰当。
HYPOTHESIS_TYPES: tuple[str, ...] = (
    "relation",
    "cluster",
    "changepoint",
    "invariant",
    "composite",
)

#: ``provenance`` 的必填键。**没有默认值**——缺失即报错（验收标准 ③）。
PROVENANCE_REQUIRED_KEYS: tuple[str, ...] = (
    "data_ids",
    "knowledge_version",
    "generator",
    "code_version",
)

#: ``representation`` 的必填键。``INTERFACES.md`` §2.3 要求「完整变换，
#: 含缺失值处理与标准化」，此处把该要求落成两个可机检的键。
REPRESENTATION_REQUIRED_KEYS: tuple[str, ...] = (
    "missing_value_handling",
    "standardization",
)

#: 假说对象的完整字段集（框架说明 §3 的 ``H`` 结构）。
#: :meth:`Hypothesis.from_dict` 用它做**严格**键名校验：未知键一律报错，
#: 避免反序列化时静默丢字段。
HYPOTHESIS_FIELDS: tuple[str, ...] = (
    "id",
    "version",
    "parent_ids",
    "type",
    "statement",
    "scope",
    "representation",
    "model",
    "fitted_parameters",
    "predictions",
    "null_hypotheses",
    "alternatives",
    "assumptions",
    "complexity",
    "provenance",
    "development",
    "confirmation_plan",
    "evidence_log",
    "status",
)

#: 禁止出现在 ``provenance`` / ``evidence_log`` 中的键名（小写比较）。
#: 依据 ``INTERFACES.md`` §4.4：令牌正文不得写入日志、提示词或版本库。
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "tokens",
        "role_token",
        "raw_token",
        "token_bytes",
        "secret",
        "password",
        "credential",
    }
)

#: 标识与版本号之间的分隔符。
IDENTITY_SEPARATOR = "@"

#: 版本号必须是正整数字符串。
_VERSION_PATTERN = re.compile(r"^[1-9][0-9]*$")

#: 假说标识的字符集：首字符为字母或数字，其余允许 ``. _ : -``。
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class HypothesisError(ValueError):
    """假说对象的基类异常。"""


class HypothesisValidationError(HypothesisError):
    """字段缺失、类型不符或取值不在受控词表内。"""


class HypothesisProvenanceError(HypothesisValidationError):
    """来源信息（``provenance``）缺失或缺少必填键。

    单列一个子类，是因为「来源缺失」在验收标准 ③ 中被单独提出，
    调用方与测试需要能把它从一般的字段校验失败中区分出来。
    """


class HypothesisImmutabilityError(HypothesisError):
    """尝试覆盖已存在的版本，或对已发布版本做原地修改。"""


# ---------------------------------------------------------------------------
# 通用校验工具
# ---------------------------------------------------------------------------


def _reject_forbidden_keys(mapping: Mapping[str, Any], label: str) -> None:
    """拒绝令牌类键名，避免敏感正文被写入假说对象。

    只做**键名**层面的拦截（大小写不敏感）。它是一道廉价的防线，
    用于兜住「调用方顺手把令牌塞进 provenance」这类失误。
    """
    for key in mapping:
        if isinstance(key, str) and key.strip().lower() in FORBIDDEN_KEYS:
            raise HypothesisValidationError(
                f"{label} 不允许包含令牌类字段 {key!r}；"
                "角色令牌正文不得写入假说对象（INTERFACES.md §4.4）"
            )


def _require_nonempty_str(value: Any, label: str) -> str:
    """要求非空字符串（去空白后非空）。"""
    if not isinstance(value, str):
        raise HypothesisValidationError(f"{label} 必须是字符串，实际得到：{type(value).__name__}")
    if not value.strip():
        raise HypothesisValidationError(f"{label} 不能是空白字符串")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """要求映射（``Mapping`` 而非 list / str）。"""
    if not isinstance(value, Mapping):
        raise HypothesisValidationError(
            f"{label} 必须是映射（dict），实际得到：{type(value).__name__}"
        )
    return value


def _require_optional_mapping(value: Any, label: str) -> Mapping[str, Any] | None:
    """要求映射或 ``None``。``None`` 表示「本阶段未提供」。"""
    if value is None:
        return None
    return _require_mapping(value, label)


def _require_sequence(value: Any, label: str) -> Sequence[Any]:
    """要求序列。字符串与映射不作为序列接受，避免「一个词被当成一条列表」。"""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise HypothesisValidationError(
            f"{label} 必须是序列（list / tuple），实际得到：{type(value).__name__}"
        )
    return value


def _require_entries(value: Any, label: str) -> list[Any]:
    """要求非空序列，且每个元素是「非空字符串」或「非空映射」。

    用于 ``null_hypotheses`` / ``alternatives`` / ``assumptions``：
    它们既可以是简短的文字标签，也可以是结构化条目，但不能是空占位。
    """
    items = _require_sequence(value, label)
    if not items:
        raise HypothesisValidationError(f"{label} 不能为空序列")
    out: list[Any] = []
    for index, item in enumerate(items):
        where = f"{label}[{index}]"
        if isinstance(item, str):
            if not item.strip():
                raise HypothesisValidationError(f"{where} 不能是空白字符串")
        elif isinstance(item, Mapping):
            if not item:
                raise HypothesisValidationError(f"{where} 不能是空映射")
            _reject_forbidden_keys(item, where)
        else:
            raise HypothesisValidationError(
                f"{where} 必须是字符串或映射，实际得到：{type(item).__name__}"
            )
        out.append(item)
    return out


def require_provenance(value: Any) -> Mapping[str, Any]:
    """校验 ``provenance`` 并返回它；缺失或缺少必填键时**报错**。

    本函数**不做任何默认填充**：这不是「补一个默认值继续跑」的位置，
    而是「来源不明就不得构造假说」的闸门（验收标准 ③）。

    :param value: 待校验的来源信息。
    :raises HypothesisProvenanceError: 整体缺失、不是映射、缺少必填键、
        或必填键的取值为空。
    :raises HypothesisValidationError: 出现令牌类键名。
    """
    if value is None:
        raise HypothesisProvenanceError(
            "provenance 是必填字段，缺失即报错，不得默认填充；"
            f"需要提供的键：{', '.join(PROVENANCE_REQUIRED_KEYS)}"
        )
    mapping = _require_mapping(value, "provenance")
    _reject_forbidden_keys(mapping, "provenance")

    missing = [key for key in PROVENANCE_REQUIRED_KEYS if key not in mapping]
    if missing:
        raise HypothesisProvenanceError(
            "provenance 缺少必填键，不得默认填充："
            + ", ".join(repr(key) for key in missing)
            + f"；完整必填键为 {', '.join(PROVENANCE_REQUIRED_KEYS)}"
        )

    # data_ids：引用 E 的标识列表，必须非空且逐条为非空字符串。
    data_ids = mapping["data_ids"]
    ids = _require_sequence(data_ids, "provenance.data_ids")
    if not ids:
        raise HypothesisProvenanceError(
            "provenance.data_ids 不能为空：假说必须指向它所用的数据引用"
        )
    for index, item in enumerate(ids):
        if not isinstance(item, str) or not item.strip():
            raise HypothesisProvenanceError(
                f"provenance.data_ids[{index}] 必须是非空字符串（引用标识），"
                f"实际得到：{item!r}"
            )

    # 其余三个必填键：非空字符串。
    for key in ("knowledge_version", "generator", "code_version"):
        item = mapping[key]
        if not isinstance(item, str) or not item.strip():
            raise HypothesisProvenanceError(
                f"provenance.{key} 必须是非空字符串，实际得到：{item!r}；"
                "该字段不得留空或由系统默认填充"
            )
    return mapping


def require_representation(value: Any) -> Mapping[str, Any]:
    """校验 ``representation`` 必须是**完整变换**声明。

    ``INTERFACES.md`` §2.3 要求 ``representation`` 写全缺失值处理与标准化，
    以免出现「只冻结公式、却继续修改数据处理」的情形（框架说明 §3）。
    因此这里要求两个键显式存在且非空。

    :raises HypothesisValidationError: 不是映射、缺键或键值为空。
    """
    mapping = _require_mapping(value, "representation")
    if not mapping:
        raise HypothesisValidationError("representation 不能为空映射：必须写明完整变换")
    _reject_forbidden_keys(mapping, "representation")
    for key in REPRESENTATION_REQUIRED_KEYS:
        if key not in mapping:
            raise HypothesisValidationError(
                f"representation 缺少必填键 {key!r}：必须写明完整变换"
                f"（含缺失值处理与标准化）；当前键：{sorted(mapping, key=str)}"
            )
        item = mapping[key]
        if isinstance(item, str):
            if not item.strip():
                raise HypothesisValidationError(f"representation.{key} 不能是空白字符串")
        elif isinstance(item, Mapping):
            if not item:
                raise HypothesisValidationError(f"representation.{key} 不能是空映射")
        else:
            raise HypothesisValidationError(
                f"representation.{key} 必须是字符串或映射，实际得到：{type(item).__name__}"
            )
    return mapping


def _require_predictions(value: Any) -> list[Any]:
    """校验 ``predictions``：每条都必须声明「什么结果与假说不相容」。

    这是可证伪性的机检下限（``INTERFACES.md`` §2.3：``predictions`` 必填）。
    每条预测必须是含非空 ``incompatible_with`` 的映射；``expectation`` 可选。
    """
    items = _require_sequence(value, "predictions")
    if not items:
        raise HypothesisValidationError(
            "predictions 不能为空：假说必须写明什么结果会与它不相容"
        )
    out: list[Any] = []
    for index, item in enumerate(items):
        where = f"predictions[{index}]"
        mapping = _require_mapping(item, where)
        if not mapping:
            raise HypothesisValidationError(f"{where} 不能是空映射")
        _reject_forbidden_keys(mapping, where)
        if "incompatible_with" not in mapping:
            raise HypothesisValidationError(
                f"{where} 缺少必填键 'incompatible_with'："
                "每条预测都必须写明什么结果与假说不相容"
            )
        statement = mapping["incompatible_with"]
        if not isinstance(statement, str) or not statement.strip():
            raise HypothesisValidationError(
                f"{where}.incompatible_with 必须是非空字符串，实际得到：{statement!r}"
            )
        out.append(mapping)
    return out


# ---------------------------------------------------------------------------
# 深度冻结 / 还原 / 规范化
# ---------------------------------------------------------------------------


def _freeze(value: Any, label: str) -> Any:
    """把 JSON 兼容结构**深度冻结**为不可变形态。

    - 映射 → :class:`types.MappingProxyType`（只读视图，键按字典序排列）；
    - 列表 / 元组 → 元组；
    - 标量 → 原样；非有限浮点数拒绝；
    - 其他类型 → 报错（顺带保证对象可 JSON 序列化）。

    键按字典序排列，使「同一内容、不同书写顺序」的输入得到相等的对象。
    """
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise HypothesisValidationError(
                    f"{label} 的键必须是字符串，实际得到：{key!r}"
                )
        frozen: dict[str, Any] = {}
        for key in sorted(value):
            frozen[key] = _freeze(value[key], f"{label}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, f"{label}[{index}]") for index, item in enumerate(value))
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise HypothesisValidationError(f"{label} 必须是有限实数，实际得到：{value!r}")
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    raise HypothesisValidationError(
        f"{label} 必须是 JSON 兼容类型（映射 / 序列 / 标量），"
        f"实际得到：{type(value).__name__}"
    )


def _thaw(value: Any) -> Any:
    """把 :func:`_freeze` 的产物还原为可 JSON 序列化的普通结构。"""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# 标识与版本号
# ---------------------------------------------------------------------------


def bump_version(version: str) -> str:
    """把正整数字符串版本号递增一位（``"1"`` → ``"2"``）。

    :raises HypothesisValidationError: 版本号不是正整数字符串。
    """
    if not isinstance(version, str) or not _VERSION_PATTERN.match(version):
        raise HypothesisValidationError(
            f"版本号必须是正整数字符串（例如 '1'），实际得到：{version!r}"
        )
    return str(int(version) + 1)


def make_identity(hypothesis_id: str, version: str) -> str:
    """构造 ``id@version`` 形式的版本标识。"""
    _validate_id(hypothesis_id)
    if not isinstance(version, str) or not _VERSION_PATTERN.match(version):
        raise HypothesisValidationError(
            f"版本号必须是正整数字符串（例如 '1'），实际得到：{version!r}"
        )
    return f"{hypothesis_id}{IDENTITY_SEPARATOR}{version}"


def parse_identity(identity: str) -> tuple[str, str]:
    """解析 ``id@version``，返回 ``(id, version)``。

    因为 ``id`` 的字符集已排除 ``@``，此处按**最后一个** ``@`` 切分是唯一解。
    """
    if not isinstance(identity, str) or IDENTITY_SEPARATOR not in identity:
        raise HypothesisValidationError(
            f"版本标识必须是 'id@version' 形式，实际得到：{identity!r}"
        )
    hypothesis_id, _, version = identity.rpartition(IDENTITY_SEPARATOR)
    _validate_id(hypothesis_id)
    if not _VERSION_PATTERN.match(version):
        raise HypothesisValidationError(
            f"版本标识中的版本号必须是正整数字符串，实际得到：{version!r}"
        )
    return hypothesis_id, version


def _validate_id(hypothesis_id: Any) -> str:
    """校验假说标识：非空、字符集受限、不含分隔符。"""
    if not isinstance(hypothesis_id, str):
        raise HypothesisValidationError(
            f"假说 id 必须是字符串，实际得到：{type(hypothesis_id).__name__}"
        )
    if not _ID_PATTERN.match(hypothesis_id):
        raise HypothesisValidationError(
            f"假说 id 只能由字母、数字与 '._:-' 组成且首字符为字母或数字，"
            f"实际得到：{hypothesis_id!r}"
        )
    return hypothesis_id


def _validate_parent_ids(value: Any) -> tuple[str, ...]:
    """校验 ``parent_ids``：每个条目都是可解析的 ``id@version`` 标识。"""
    items = _require_sequence(value, "parent_ids")
    out: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise HypothesisValidationError(
                f"parent_ids[{index}] 必须是字符串标识，实际得到：{item!r}"
            )
        parse_identity(item)  # 复用解析器做格式校验
        if item in out:
            raise HypothesisValidationError(f"parent_ids 中存在重复条目：{item!r}")
        out.append(item)
    return tuple(out)


# ---------------------------------------------------------------------------
# 假说对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False, repr=False)
class Hypothesis:
    """一个假说版本，字段与框架说明 §3 的 ``H`` 结构一一对应。

    对象**天然不可变**：frozen dataclass 挡住属性赋值，深度冻结挡住嵌套修改。
    任何内容变更都必须经 :meth:`new_version` 生成**新版本**（验收标准 ②）。

    :param id: 稳定标识，跨版本不变。
    :param version: 正整数字符串版本号。
    :param type: 受控类型词表 :data:`HYPOTHESIS_TYPES` 之一。
    :param statement: 假说的自然语言陈述。
    :param representation: 完整变换声明，必含缺失值处理与标准化。
    :param predictions: 可证伪预测；每条必须写明什么结果与假说不相容。
    :param null_hypotheses: 零假说，**至少一条**。
    :param provenance: 来源信息，必含 :data:`PROVENANCE_REQUIRED_KEYS`。
    :param parent_ids: 派生字段——父版本标识链，由 :meth:`new_version` 维护。
    :param scope: 对象、环境、时间与排除条件；空映射表示未限定。
    :param model: 模型描述（字符串或映射）；``None`` 表示未指定。
    :param fitted_parameters: 拟合参数；本模块只存放、不拟合。
    :param alternatives: 竞争解释；可空。
    :param assumptions: 假设声明；可空。
    :param complexity: 复杂度槽位；**本模块不计算**，口径由 P09 统一。
    :param development: 开发期度量槽位；**本模块不计算**。
    :param confirmation_plan: 确证计划槽位；**本模块不生成**。
    :param evidence_log: 证据日志，只追加。
    :param status: 生命周期状态，取值 :data:`HYPOTHESIS_STATUSES`。
    """

    # -- 必填字段 ---------------------------------------------------------
    id: str
    version: str
    type: str
    statement: str
    representation: Any
    predictions: Any
    null_hypotheses: Any
    provenance: Any

    # -- 可选字段 ---------------------------------------------------------
    parent_ids: Any = ()
    scope: Any = field(default_factory=dict)
    model: Any = None
    fitted_parameters: Any = field(default_factory=dict)
    alternatives: Any = ()
    assumptions: Any = ()
    complexity: Any = None
    development: Any = None
    confirmation_plan: Any = None
    evidence_log: Any = ()
    status: str = STATUS_DRAFT

    # -- 构造即校验 -------------------------------------------------------

    def __post_init__(self) -> None:
        _validate_id(self.id)

        if not isinstance(self.version, str) or not _VERSION_PATTERN.match(self.version):
            raise HypothesisValidationError(
                f"version 必须是正整数字符串（例如 '1'），实际得到：{self.version!r}"
            )

        if self.type not in HYPOTHESIS_TYPES:
            raise HypothesisValidationError(
                f"未知的假说类型：{self.type!r}；仅支持 {', '.join(HYPOTHESIS_TYPES)}"
            )

        _require_nonempty_str(self.statement, "statement")
        require_representation(self.representation)
        _require_predictions(self.predictions)
        _require_entries(self.null_hypotheses, "null_hypotheses")
        require_provenance(self.provenance)

        if self.status not in HYPOTHESIS_STATUSES:
            raise HypothesisValidationError(
                f"未知的状态：{self.status!r}；仅支持 {', '.join(HYPOTHESIS_STATUSES)}"
            )

        # 可选容器：允许为空，但不允许写成 None 之外的错误类型。
        _require_optional_mapping(self.scope, "scope")
        _require_optional_mapping(self.fitted_parameters, "fitted_parameters")
        _require_optional_mapping(self.complexity, "complexity")
        _require_optional_mapping(self.development, "development")
        _require_optional_mapping(self.confirmation_plan, "confirmation_plan")
        if not isinstance(self.model, (str, Mapping)) and self.model is not None:
            raise HypothesisValidationError(
                f"model 必须是字符串、映射或 None，实际得到：{type(self.model).__name__}"
            )
        if isinstance(self.model, str) and not self.model.strip():
            raise HypothesisValidationError("model 若是字符串则不能为空白")

        if self.alternatives:
            _require_entries(self.alternatives, "alternatives")
        if self.assumptions:
            _require_entries(self.assumptions, "assumptions")
        if self.evidence_log:
            _require_entries(self.evidence_log, "evidence_log")

        # -- 深度冻结：对象不可变的第二层保障 -----------------------------
        object.__setattr__(self, "parent_ids", _validate_parent_ids(self.parent_ids))
        for name in (
            "scope",
            "representation",
            "fitted_parameters",
            "predictions",
            "null_hypotheses",
            "alternatives",
            "assumptions",
            "complexity",
            "provenance",
            "development",
            "confirmation_plan",
            "evidence_log",
        ):
            object.__setattr__(self, name, _freeze(getattr(self, name), name))
        if self.model is not None:
            object.__setattr__(self, "model", _freeze(self.model, "model"))

    # -- 相等性与哈希 -----------------------------------------------------

    def __eq__(self, other: object) -> bool:
        """按**内容**判等（字典序冻结后比较），与书写顺序无关。"""
        if not isinstance(other, Hypothesis):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self) -> int:
        """以**内容摘要**为哈希键，与 :meth:`__eq__` 的内容判等保持一致。

        一致性要求：``a == b`` 必须蕴含 ``hash(a) == hash(b)``。既然
        :meth:`__eq__` 按内容判等，哈希也必须按内容派生，否则
        「同标识、不同内容」的两个对象会哈希相同却判不相等，
        在集合／字典中产生难以察觉的重复项。

        对象不可变，因此用内容做哈希键是安全的。
        """
        return hash(self.content_digest())

    # -- 性质 -------------------------------------------------------------

    @property
    def identity(self) -> str:
        """版本标识 ``id@version``。"""
        return make_identity(self.id, self.version)

    @property
    def is_draft(self) -> bool:
        """是否为草稿（允许发布与追加证据）。"""
        return self.status == STATUS_DRAFT

    @property
    def is_published(self) -> bool:
        """是否为已发布版本（内容冻结）。"""
        return self.status == STATUS_PUBLISHED

    @property
    def is_retired(self) -> bool:
        """是否为已归档版本（终态）。"""
        return self.status == STATUS_RETIRED

    @property
    def is_frozen(self) -> bool:
        """是否已冻结内容（已发布或已归档）。"""
        return self.status in _PUBLISHED_STATES

    @property
    def generation(self) -> int:
        """代次：初始版本为 0，每修订一代加一（等于父链长度）。"""
        return len(self.parent_ids)

    def lineage_ids(self) -> tuple[str, ...]:
        """返回父链（不含自身），由最远祖先到直接父版本。"""
        return tuple(self.parent_ids)

    # -- 生命周期 ---------------------------------------------------------

    def publish(self) -> "Hypothesis":
        """返回**同一版本**的已发布副本（内容逐字节不变，仅状态变更）。

        发布是把内容冻结的动作本身，因此不改变标识、不改变父链。
        对已发布版本重复调用会报错。

        :raises HypothesisImmutabilityError: 已发布或已归档。
        """
        if self.status == STATUS_PUBLISHED:
            raise HypothesisImmutabilityError(
                f"{self.identity} 已是已发布版本，不可重复发布；"
                "如需修改请调用 new_version() 生成新版本"
            )
        if self.status == STATUS_RETIRED:
            raise HypothesisImmutabilityError(
                f"{self.identity} 已归档，不可再发布；如需修改请生成新版本"
            )
        return self._with({"status": STATUS_PUBLISHED})

    def retire(self) -> "Hypothesis":
        """返回已归档副本（终态）。

        :raises HypothesisImmutabilityError: 已归档。
        """
        if self.status == STATUS_RETIRED:
            raise HypothesisImmutabilityError(f"{self.identity} 已归档，不可重复归档")
        return self._with({"status": STATUS_RETIRED})

    def with_evidence(self, entry: Any) -> "Hypothesis":
        """向证据日志**追加**一条记录，返回新对象。

        仅草稿可追加。已发布版本的内容是冻结的，追加证据同样属于修改，
        必须走 :meth:`new_version`。

        :raises HypothesisImmutabilityError: 已发布或已归档。
        :raises HypothesisValidationError: 记录不是「非空字符串或非空映射」。
        """
        if self.is_frozen:
            raise HypothesisImmutabilityError(
                f"{self.identity} 已冻结（status={self.status}），不可原地追加证据；"
                "请调用 new_version() 生成新版本后再追加"
            )
        _require_entries([entry], "evidence_log 追加项")
        return self._with({"evidence_log": [*self.evidence_log, entry]})

    def new_version(self, *, changes: Mapping[str, Any] | None = None,
                    version: str | None = None) -> "Hypothesis":
        """由本版本派生**新版本**（验收标准 ①）。

        ``parent_ids`` 是派生值：新版本父链恒为「本版本父链 + 本版本标识」，
        因此链条只增不改。调用方不得在 ``changes`` 中指定 ``id`` 或
        ``parent_ids``，也不得指定 ``version``（请用 ``version`` 参数）。

        新版本的状态恒为 ``draft``——发布状态不继承，需重新发布，
        以免「改了内容却仍显示为已发布」。

        :param changes: 需要覆盖的字段。
        :param version: 显式指定新版本号；省略则自动递增。
        :raises HypothesisImmutabilityError: 本版本已归档，或试图指定派生字段。
        :raises HypothesisValidationError: 新版本号非法或与当前相同。
        """
        if self.is_retired:
            raise HypothesisImmutabilityError(
                f"{self.identity} 已归档，是终态，不可再派生子版本"
            )

        payload = self.to_dict()
        if changes:
            mapping = _require_mapping(changes, "changes")
            for forbidden in ("id", "parent_ids", "version", "status"):
                if forbidden in mapping:
                    raise HypothesisImmutabilityError(
                        f"changes 不允许指定受控字段 {forbidden!r}："
                        "id 与 parent_ids 由版本链自动维护，version 请用 version 参数，"
                        "status 恒为 draft（发布状态不继承）"
                    )
            _reject_forbidden_keys(mapping, "changes")
            payload.update({key: mapping[key] for key in mapping})

        new_version_number = bump_version(self.version) if version is None else version
        if not isinstance(new_version_number, str) or not _VERSION_PATTERN.match(
            new_version_number
        ):
            raise HypothesisValidationError(
                f"新版本号必须是正整数字符串，实际得到：{new_version_number!r}"
            )
        if new_version_number == self.version:
            raise HypothesisValidationError(
                f"新版本号与当前版本相同（{self.version}）；"
                "同版本不可覆盖，请递增版本号"
            )

        payload["version"] = new_version_number
        # 父链只追加：保留既有祖先链，再补上直接父版本。
        payload["parent_ids"] = [*self.parent_ids, self.identity]
        payload["status"] = STATUS_DRAFT
        return Hypothesis.from_dict(payload)

    def _with(self, changes: Mapping[str, Any]) -> "Hypothesis":
        """内部：按字段覆盖构造同标识的新对象（不做父链推导）。"""
        payload = self.to_dict()
        payload.update(dict(changes))
        return Hypothesis.from_dict(payload)

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典，键序与框架说明 §3 的 ``H`` 一致。"""
        return {
            "id": self.id,
            "version": self.version,
            "parent_ids": [*self.parent_ids],
            "type": self.type,
            "statement": self.statement,
            "scope": _thaw(self.scope),
            "representation": _thaw(self.representation),
            "model": _thaw(self.model),
            "fitted_parameters": _thaw(self.fitted_parameters),
            "predictions": _thaw(self.predictions),
            "null_hypotheses": _thaw(self.null_hypotheses),
            "alternatives": _thaw(self.alternatives),
            "assumptions": _thaw(self.assumptions),
            "complexity": _thaw(self.complexity),
            "provenance": _thaw(self.provenance),
            "development": _thaw(self.development),
            "confirmation_plan": _thaw(self.confirmation_plan),
            "evidence_log": _thaw(self.evidence_log),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Hypothesis":
        """由 :meth:`to_dict` 的产物还原。

        **严格**校验键名：未知键报错（避免静默丢字段），必填键缺失报错
        （``provenance`` 缺失抛 :class:`HypothesisProvenanceError`）。

        :raises HypothesisValidationError: 不是映射、键名未知或字段不合法。
        """
        if not isinstance(payload, Mapping):
            raise HypothesisValidationError(
                f"假说序列化必须是映射，实际得到：{type(payload).__name__}"
            )
        unknown = sorted(set(payload) - set(HYPOTHESIS_FIELDS))
        if unknown:
            raise HypothesisValidationError(
                "假说序列化包含未知字段：" + ", ".join(repr(key) for key in unknown)
            )
        # 必填键：缺失时逐一给出明确错误（provenance 单独走来源异常）。
        if "provenance" not in payload:
            raise HypothesisProvenanceError(
                "假说序列化缺少必填字段 'provenance'：缺失即报错，不得默认填充"
            )
        for required in (
            "id",
            "version",
            "type",
            "statement",
            "representation",
            "predictions",
            "null_hypotheses",
        ):
            if required not in payload:
                raise HypothesisValidationError(f"假说序列化缺少必填字段 {required!r}")
        return cls(**{key: payload[key] for key in payload})

    def canonical_json(self) -> str:
        """规范式 JSON 文本（键排序、紧凑分隔符），用于稳定比对与哈希。"""
        return json.dumps(
            _thaw(self.to_dict()),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def content_digest(self) -> str:
        """内容摘要：规范式 JSON 的 SHA-256 十六进制串。

        供 M8 归档做「同一版本是否被改动过」的比对。摘要覆盖全部字段
        （含 ``status``），因此发布动作本身也会改变摘要。
        """
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"Hypothesis(id={self.id!r}, version={self.version!r}, "
            f"type={self.type!r}, status={self.status!r}, "
            f"parents={list(self.parent_ids)!r})"
        )


# ---------------------------------------------------------------------------
# 版本仓库
# ---------------------------------------------------------------------------


class HypothesisStore:
    """只追加的假说版本仓库，负责「版本不可覆盖」的强制（验收标准 ②）。

    仓库是一张 ``id@version → Hypothesis`` 的映射，**没有覆盖写入口**：

    - :meth:`register` 遇到已存在的标识直接报错；
    - :meth:`publish` 只允许 ``draft → published`` 的状态迁移，
      并在迁移前复核「除 ``status`` 外内容逐字节相同」；
    - :meth:`revise` 从既有版本派生**新标识**的新版本，
      被修订的已发布版本在仓库中保持不变。

    :param strict_chain: 为 ``True`` 时，:meth:`register` 要求每个父标识
        都已在本仓库中。默认关闭（见模块文档「已知限制 3」）。
    """

    def __init__(self, *, strict_chain: bool = False) -> None:
        if not isinstance(strict_chain, bool):
            raise HypothesisValidationError(
                f"strict_chain 必须是布尔值，实际得到：{strict_chain!r}"
            )
        self._strict_chain = strict_chain
        self._items: dict[str, Hypothesis] = {}

    # -- 基本容器协议 -----------------------------------------------------

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, identity: object) -> bool:
        return isinstance(identity, str) and identity in self._items

    def __iter__(self) -> Iterator[Hypothesis]:
        """按 ``(id, 版本号升序)`` 迭代，顺序完全确定。"""
        for identity in self._ordered_identities():
            yield self._items[identity]

    def _ordered_identities(self) -> list[str]:
        return sorted(self._items, key=lambda item: (parse_identity(item)[0],
                                                     int(parse_identity(item)[1])))

    # -- 写入 -------------------------------------------------------------

    def register(self, hypothesis: Hypothesis) -> Hypothesis:
        """登记一个版本。**只追加**：标识已存在即报错。

        :raises HypothesisImmutabilityError: 标识已存在（版本不可覆盖）。
        :raises HypothesisValidationError: 不是 :class:`Hypothesis` 实例，
            或（开启 ``strict_chain`` 时）父标识不在本仓库。
        """
        if not isinstance(hypothesis, Hypothesis):
            raise HypothesisValidationError(
                f"只能登记 Hypothesis 实例，实际得到：{type(hypothesis).__name__}"
            )
        identity = hypothesis.identity
        if identity in self._items:
            raise HypothesisImmutabilityError(
                f"{identity} 已存在于仓库中，版本不可覆盖；"
                "如需修改请调用 revise() 生成新版本"
            )
        if self._strict_chain:
            missing = [p for p in hypothesis.parent_ids if p not in self._items]
            if missing:
                raise HypothesisValidationError(
                    f"{identity} 的父版本未登记：" + ", ".join(missing)
                )
        self._items[identity] = hypothesis
        return hypothesis

    def publish(self, identity: str) -> Hypothesis:
        """把草稿标记为已发布，返回仓库中的新状态对象。

        这是**唯一**允许在既有标识上发生的变更，且只改 ``status``：
        迁移前会复核「除 ``status`` 外内容相同」，防止借发布之名改内容。

        :raises HypothesisValidationError: 标识不存在。
        :raises HypothesisImmutabilityError: 不是草稿（已发布或已归档）。
        """
        current = self.get(identity)
        published = current.publish()
        before = current.to_dict()
        after = published.to_dict()
        before.pop("status")
        after.pop("status")
        if before != after:  # pragma: no cover - 防御性不变量
            raise HypothesisImmutabilityError(
                f"{identity} 的发布过程改变了内容；发布只允许变更 status"
            )
        self._items[identity] = published
        return published

    def revise(self, identity: str, *, changes: Mapping[str, Any] | None = None,
               version: str | None = None) -> Hypothesis:
        """由既有版本派生新版本并登记，返回新版本。

        被修订的版本（无论草稿还是已发布）在仓库中**保持原样**——
        这正是验收标准 ② 的落点：修改不发生在旧对象上，而发生在新版本上。

        :raises HypothesisValidationError: 标识不存在，或新版本不合法。
        :raises HypothesisImmutabilityError: 旧版本已归档。
        """
        current = self.get(identity)
        new_hypothesis = current.new_version(changes=changes, version=version)
        return self.register(new_hypothesis)

    # -- 读取 -------------------------------------------------------------

    def get(self, identity: str) -> Hypothesis:
        """按标识取版本。

        :raises HypothesisValidationError: 标识格式非法或不存在。
        """
        if not isinstance(identity, str):
            raise HypothesisValidationError(
                f"版本标识必须是字符串，实际得到：{type(identity).__name__}"
            )
        if identity not in self._items:
            raise HypothesisValidationError(f"仓库中不存在版本标识：{identity!r}")
        return self._items[identity]

    def get_version(self, hypothesis_id: str, version: str) -> Hypothesis:
        """按 ``(id, version)`` 取版本。"""
        return self.get(make_identity(hypothesis_id, version))

    def versions(self, hypothesis_id: str) -> tuple[str, ...]:
        """返回某假说的全部版本号（升序）。不存在则返回空元组。"""
        found = [
            parse_identity(identity)[1]
            for identity in self._items
            if parse_identity(identity)[0] == hypothesis_id
        ]
        return tuple(sorted(found, key=int))

    def latest(self, hypothesis_id: str) -> Hypothesis:
        """返回某假说的最新版本。

        :raises HypothesisValidationError: 该 id 无任何版本。
        """
        versions = self.versions(hypothesis_id)
        if not versions:
            raise HypothesisValidationError(f"仓库中不存在假说 id：{hypothesis_id!r}")
        return self._items[make_identity(hypothesis_id, versions[-1])]

    def history(self, hypothesis_id: str) -> tuple[Hypothesis, ...]:
        """返回某假说的全部版本，按版本号升序。"""
        return tuple(self._items[make_identity(hypothesis_id, v)]
                     for v in self.versions(hypothesis_id))

    def lineage(self, identity: str) -> tuple[Hypothesis, ...]:
        """沿 ``parent_ids`` 回溯祖先链，返回「最远祖先 → 目标版本」。

        只回溯**本仓库中已登记**的祖先；缺失的父标识被跳过（不报错），
        以便在分批复建历史时也能使用。检测到环或链条超长时抛错。

        :raises HypothesisValidationError: 标识不存在或父链成环。
        """
        target = self.get(identity)
        chain: list[Hypothesis] = [target]
        seen = {target.identity}
        cursor = target
        while cursor.parent_ids:
            parent_identity = cursor.parent_ids[-1]
            if parent_identity in seen:
                raise HypothesisValidationError(
                    f"检测到父链成环：{parent_identity!r} 重复出现"
                )
            seen.add(parent_identity)
            parent = self._items.get(parent_identity)
            if parent is None:
                break
            chain.append(parent)
            cursor = parent
        chain.reverse()
        return tuple(chain)

    def ids(self) -> tuple[str, ...]:
        """返回仓库中全部假说 id（升序）。"""
        return tuple(sorted({parse_identity(identity)[0] for identity in self._items}))

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典，版本按 ``(id, 版本号)`` 升序排列。"""
        return {
            "module_version": HYPOTHESIS_MODULE_VERSION,
            "strict_chain": self._strict_chain,
            "size": len(self._items),
            "hypotheses": [self._items[i].to_dict() for i in self._ordered_identities()],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "HypothesisStore":
        """由 :meth:`to_dict` 的产物还原仓库，并逐条重放登记。

        :raises HypothesisValidationError: 不是映射、结构不合法或条目非法。
        :raises HypothesisImmutabilityError: 序列化中存在重复标识。
        """
        if not isinstance(payload, Mapping):
            raise HypothesisValidationError(
                f"仓库序列化必须是映射，实际得到：{type(payload).__name__}"
            )
        unknown = sorted(set(payload) - {"module_version", "strict_chain", "size",
                                         "hypotheses"})
        if unknown:
            raise HypothesisValidationError(
                "仓库序列化包含未知字段：" + ", ".join(repr(key) for key in unknown)
            )
        hypotheses = payload.get("hypotheses", [])
        items = _require_sequence(hypotheses, "hypotheses")
        store = cls(strict_chain=bool(payload.get("strict_chain", False)))
        for item in items:
            store.register(Hypothesis.from_dict(item))
        declared = payload.get("size")
        if declared is not None and declared != len(store):
            raise HypothesisValidationError(
                f"仓库序列化声明的 size={declared!r} 与实际条目数 {len(store)} 不一致"
            )
        return store

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"HypothesisStore(size={len(self._items)}, strict_chain={self._strict_chain})"
