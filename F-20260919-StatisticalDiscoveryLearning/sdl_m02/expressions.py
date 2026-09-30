"""M2 受限表达式语法与规范化（P01 交付物）。

本模块定义探索阶段可用的一类**受限表达式**，并提供一个规范化（canonicalization）
过程，使得「语义等价的不同写法」在规范化后得到**逐字节相同的 AST 表示**。
这是后续阶段（P02 变量枚举、P03 去重剪枝）能够可靠判等的基础。

设计边界（严格遵守 P01 增量卡）：

- 本模块**只做语法层面的解析、规范化、度量与序列化**；
- **不实现变量枚举**（属 P02）、**不执行任何求值**（即不把表达式作用于数据）；
- **不涉及数据访问**，不引用 M1 的任何令牌、数据库或分区；
- 仅使用 Python 3.11+ 标准库。

受限语法子集（P01 验收要求「覆盖加、减、乘、除与一元变换」）：

    expr    := term (('+' | '-') term)*
    term    := factor (('*' | '/') factor)*
    factor  := ('+' | '-') factor | power
    power   := atom ('^' atom)?          # 右结合，指数须为非负整数常量
    atom    := NUMBER | IDENT | '(' expr ')' | FUNC '(' expr ')'
    FUNC    := 'abs' | 'log' | 'sqrt' | 'exp'

其中一元变换包括：一元正号／负号，以及 `abs` / `log` / `sqrt` / `exp` 四个函数。
`log`、`sqrt`、`exp` 属「有定义域的变换」，其定义域条件由 P02 的 `DomainSpec` 承担；
本模块只在 AST 中如实保留函数节点，不推断定义域。

规范化的核心规则（保证语义等价 ⟹ AST 相等）：

1. **常数折叠**：纯常数子式在规范化阶段直接求值（此处是编译期折叠，不是对数据的求值）。
2. **加法／乘法的交换律归一**：`a + b` 与 `b + a` 归一为同一顺序（按子树规范式字典序升序）。
3. **加法／乘法的结合律归一**：嵌套同运算符展平为一元 n 元节点（n-ary），
   例如 `(a + b) + c` 与 `a + (b + c)` 均归一为 `add(a, b, c)`，从而天然判等。
4. **减法／除法改写为「加法／乘法的逆元」**：`a - b` → `add(a, neg(b))`；
   `a / b` → `mul(a, inv(b))`。这样交换律与结合律归一可统一处理四则运算，
   且 `a - b` 与 `a + (-b)` 得到相同 AST。
5. **中性元消去**：`x + 0` → `x`；`x * 1` → `x`；`x * 0` → `0`。
   注意**不做** `x / x → 1` 这类改写——它会改变定义域（`x = 0` 时原式无意义），
   属于不合法化简。
6. **双重取负消去**：`-(-x)` → `x`。
7. **幂次归一**：`x ^ 1` → `x`；`x ^ 0` → `1`。
   指数必须是非负整数常量，非整指数（会产生非实数值或分支）一律拒绝。
8. **符号集中在叶子**：所有负号被下沉到尽可能深的位置（`neg` 只包裹原子），
   使得 `-(a * b)` 与 `(-a) * b` 归一为同一形式。

规范化后的 AST 一律为**元组**形式，可直接比较、可哈希、可 JSON 序列化：

    ('num', 2.0)                 常数（值经规范化，整数以浮点整数存储）
    ('var', 'x')                 变量
    ('neg', <ast>)               取负
    ('inv', <ast>)               取倒数（表示除法产生的因子）
    ('add', <ast>, <ast>, ...)   加法（n-ary，参数已排序、已展平）
    ('mul', <ast>, <ast>, ...)   乘法（n-ary，参数已排序、已展平）
    ('pow', <ast>, n)            幂（n 为非负整数）
    ('func', 'abs', <ast>)       一元函数

复杂度度量（供 M5 的复杂度 C 与候选预算使用）：

- `depth(ast)`：树的深度，叶节点深度为 1；**上限 3**（框架说明的初版配置）。
- `node_count(ast)`：节点总数；**上限默认 25**，可配置。

超限时 `parse` / `normalize` 抛 `ExpressionLimitError`（继承自 `ExpressionError`）。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

__all__ = [
    "ExpressionError",
    "ExpressionSyntaxError",
    "ExpressionLimitError",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NODES",
    "SUPPORTED_FUNCTIONS",
    "parse",
    "normalize",
    "depth",
    "node_count",
    "to_ast",
    "from_ast",
    "to_canonical_json",
    "variables",
]

# ---------------------------------------------------------------------------
# 常量与上限
# ---------------------------------------------------------------------------

#: 表达式深度上限。框架说明的初版配置为「深度不超过 3」。
DEFAULT_MAX_DEPTH = 3

#: 节点数上限。用于把候选规模压在可搜索范围内。
DEFAULT_MAX_NODES = 25

#: 支持的一元变换函数。
SUPPORTED_FUNCTIONS = ("abs", "log", "sqrt", "exp")

#: 浮点比较容差，用于常数折叠与中性元判定。
_EPS = 1e-12

_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<number>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<op>\*\*|[+\-*/^()])
    """,
    re.VERBOSE,
)


class ExpressionError(Exception):
    """表达式处理的基类异常。"""


class ExpressionSyntaxError(ExpressionError):
    """表达式不符合受限语法。"""


class ExpressionLimitError(ExpressionError):
    """表达式超出深度或节点数预算。"""


# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------


class _Token:
    """一个词法单元。``kind`` 取 ``number`` / ``ident`` / ``op`` / ``eof``。"""

    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: str, pos: int) -> None:
        self.kind = kind
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"_Token({self.kind!r}, {self.value!r}, {self.pos})"


def _tokenize(text: str) -> list[_Token]:
    """把源码切分为词法单元；遇到非法字符报错。"""
    if not isinstance(text, str):
        raise ExpressionSyntaxError("表达式必须是字符串")
    tokens: list[_Token] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if m is None:
            raise ExpressionSyntaxError(f"第 {pos} 个字符处出现非法字符：{text[pos]!r}")
        if m.lastgroup == "space":
            pos = m.end()
            continue
        tokens.append(_Token(m.lastgroup or "op", m.group(), pos))
        pos = m.end()
    tokens.append(_Token("eof", "", n))
    return tokens


# ---------------------------------------------------------------------------
# 语法分析（递归下降）
# ---------------------------------------------------------------------------


class _Parser:
    """受限语法的递归下降解析器。

    产出的是**带显式二元运算符**的原始 AST（元组形式），
    规范化（交换律/结合律归一、常数折叠等）由 :func:`normalize` 负责。
    """

    def __init__(self, tokens: list[_Token], max_depth: int, max_nodes: int) -> None:
        self._tokens = tokens
        self._index = 0
        self._max_depth = max_depth
        self._max_nodes = max_nodes

    # -- 基础工具 ---------------------------------------------------------

    def _peek(self) -> _Token:
        return self._tokens[self._index]

    def _next(self) -> _Token:
        tok = self._tokens[self._index]
        self._index += 1
        return tok

    def _accept(self, value: str) -> bool:
        tok = self._peek()
        if tok.kind == "op" and tok.value == value:
            self._index += 1
            return True
        return False

    def _expect(self, value: str) -> None:
        if not self._accept(value):
            tok = self._peek()
            raise ExpressionSyntaxError(
                f"第 {tok.pos} 个字符处期望 {value!r}，实际得到 {tok.value!r}"
            )

    # -- 语法规则 ---------------------------------------------------------

    def parse_program(self) -> tuple:
        """解析整个输入并确保没有多余字符。"""
        ast = self._parse_expr()
        tok = self._peek()
        if tok.kind != "eof":
            raise ExpressionSyntaxError(
                f"第 {tok.pos} 个字符处存在多余内容：{tok.value!r}"
            )
        return ast

    def _parse_expr(self) -> tuple:
        """加法与减法层（左结合）。"""
        node = self._parse_term()
        while True:
            tok = self._peek()
            if tok.kind == "op" and tok.value in ("+", "-"):
                self._next()
                rhs = self._parse_term()
                node = ("add", node, rhs) if tok.value == "+" else ("sub", node, rhs)
            else:
                return node

    def _parse_term(self) -> tuple:
        """乘法与除法层（左结合）。"""
        node = self._parse_factor()
        while True:
            tok = self._peek()
            if tok.kind == "op" and tok.value in ("*", "/"):
                self._next()
                rhs = self._parse_factor()
                node = ("mul", node, rhs) if tok.value == "*" else ("div", node, rhs)
            else:
                return node

    def _parse_factor(self) -> tuple:
        """一元正负号层（右结合，可叠加）。"""
        if self._accept("+"):
            return self._parse_factor()
        if self._accept("-"):
            return ("neg", self._parse_factor())
        return self._parse_power()

    def _parse_power(self) -> tuple:
        """幂层：``atom ^ atom``，右结合。

        指数在语法上只接受原子，且规范化阶段要求它是**非负整数常量**。
        这样可以避免 ``x ^ y`` 这类会产生非实数值或分支的表达式进入候选空间。
        """
        base = self._parse_atom()
        if self._accept("^") or self._accept("**"):
            exponent = self._parse_atom()
            return ("pow", base, exponent)
        return base

    def _parse_atom(self) -> tuple:
        """原子层：常数、变量、括号、函数调用。"""
        tok = self._peek()

        if tok.kind == "number":
            self._next()
            value = float(tok.value)
            if not math.isfinite(value):
                raise ExpressionSyntaxError(f"第 {tok.pos} 个字符处的数值超出可表示范围")
            return ("num", value)

        if tok.kind == "ident":
            self._next()
            name = tok.value
            if self._accept("("):
                if name not in SUPPORTED_FUNCTIONS:
                    raise ExpressionSyntaxError(
                        f"不支持的一元函数 {name!r}；仅支持 {', '.join(SUPPORTED_FUNCTIONS)}"
                    )
                arg = self._parse_expr()
                self._expect(")")
                return ("func", name, arg)
            return ("var", name)

        if tok.kind == "op" and tok.value == "(":
            self._next()
            inner = self._parse_expr()
            self._expect(")")
            return inner

        raise ExpressionSyntaxError(
            f"第 {tok.pos} 个字符处期望常数、变量、函数或括号，实际得到 {tok.value!r}"
        )


# ---------------------------------------------------------------------------
# 度量为（在原始 AST 上计算，用于预算检查）
# ---------------------------------------------------------------------------


def _raw_depth(ast: tuple) -> int:
    """原始（未规范化）AST 的深度，叶节点深度为 1。"""
    kind = ast[0]
    if kind in ("num", "var"):
        return 1
    if kind in ("neg",):
        return 1 + _raw_depth(ast[1])
    if kind in ("func",):
        return 1 + _raw_depth(ast[2])
    if kind in ("add", "sub", "mul", "div"):
        return 1 + max(_raw_depth(ast[1]), _raw_depth(ast[2]))
    if kind == "pow":
        return 1 + max(_raw_depth(ast[1]), _raw_depth(ast[2]))
    raise ExpressionSyntaxError(f"未知的 AST 节点类型：{kind!r}")


def _raw_node_count(ast: tuple) -> int:
    """原始（未规范化）AST 的节点总数。"""
    kind = ast[0]
    if kind in ("num", "var"):
        return 1
    if kind == "neg":
        return 1 + _raw_node_count(ast[1])
    if kind == "func":
        return 1 + _raw_node_count(ast[2])
    if kind in ("add", "sub", "mul", "div", "pow"):
        return 1 + _raw_node_count(ast[1]) + _raw_node_count(ast[2])
    raise ExpressionSyntaxError(f"未知的 AST 节点类型：{kind!r}")


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------


def _is_num(ast: tuple, value: float | None = None) -> bool:
    if ast[0] != "num":
        return False
    return value is None or abs(ast[1] - value) <= _EPS


def _num(value: float) -> tuple:
    """构造常数节点；整数值统一存为浮点整数以避免 ``2`` 与 ``2.0`` 不等。"""
    if not math.isfinite(value):
        raise ExpressionError("常数折叠产生了非有限值")
    rounded = round(value)
    if abs(value - rounded) <= _EPS and abs(rounded) < 1e15:
        return ("num", float(rounded))
    return ("num", float(value))


def _sort_key(ast: tuple) -> str:
    """为 n-ary 参数排序提供稳定的字典序键（用规范 JSON 串）。"""
    return _canonical_repr(ast)


def _canonical_repr(ast: tuple) -> str:
    """规范化 AST 的紧凑字符串表示，用于排序与哈希。"""
    return json.dumps(_ast_to_jsonable(ast), ensure_ascii=False, separators=(",", ":"))


def _ast_to_jsonable(ast: tuple) -> list:
    """把元组 AST 转成可 JSON 序列化的嵌套列表。"""
    return [ast[0], *[_ast_to_jsonable(child) if isinstance(child, tuple) else child
                      for child in ast[1:]]]


def _flatten(ast: tuple, kind: str) -> list[tuple]:
    """把同运算符的嵌套节点展平为一组因子／项。

    ``add(a, add(b, c))`` → ``[a, b, c]``。仅在规范化完成后调用。
    """
    out: list[tuple] = []
    if ast[0] == kind:
        for child in ast[1:]:
            out.extend(_flatten(child, kind))
    else:
        out.append(ast)
    return out


def _negate(ast: tuple) -> tuple:
    """对已是规范式的子树取负。"""
    kind = ast[0]
    if kind == "num":
        return _num(-ast[1])
    if kind == "neg":
        return ast[1]
    return ("neg", ast)


def _invert(ast: tuple) -> tuple:
    """对已是规范式的子树取倒数（表示除法产生的因子）。

    注意：这里**只做语法改写**，不检查分母是否为零——
    定义域条件（分母非零）由 P02 的 ``DomainSpec`` 显式承载。
    """
    if ast[0] == "inv":
        return ast[1]
    return ("inv", ast)


def _normalize_node(ast: tuple) -> tuple:
    """递归规范化一个原始 AST 节点。"""
    kind = ast[0]

    # -- 叶子 -------------------------------------------------------------
    if kind == "num":
        return _num(ast[1])
    if kind == "var":
        return ast

    # -- 一元变换 ---------------------------------------------------------
    if kind == "func":
        name, arg = ast[1], _normalize_node(ast[2])
        # 常数折叠：abs 可安全折叠；log/sqrt/exp 只在定义域内且结果有限时折叠。
        if arg[0] == "num":
            folded = _try_fold_function(name, arg[1])
            if folded is not None:
                return folded
        return ("func", name, arg)

    if kind == "neg":
        return _negate(_normalize_node(ast[1]))

    # 'inv' 是规范式专有节点（由除法改写产生）。normalize() 必须能接受
    # 已规范化的 AST 并保持幂等，故此处也要处理它。
    if kind == "inv":
        return _invert(_normalize_node(ast[1]))

    # -- 幂 ---------------------------------------------------------------
    if kind == "pow":
        base = _normalize_node(ast[1])
        # 规范式中指数已是 int；原始 AST 中指数是节点，需先规范化。
        raw_exp = ast[2]
        if isinstance(raw_exp, int):
            n = raw_exp
        else:
            exponent = _normalize_node(raw_exp)
            if exponent[0] != "num":
                raise ExpressionSyntaxError(
                    "幂指数必须是常量；变量指数会产生非实数值或分支，不在受限语法内"
                )
            e = exponent[1]
            if abs(e - round(e)) > _EPS or e < 0:
                raise ExpressionSyntaxError("幂指数必须是非负整数")
            n = int(round(e))
        if n < 0:
            raise ExpressionSyntaxError("幂指数必须是非负整数")
        if n == 0:
            return _num(1.0)
        if n == 1:
            return base
        if base[0] == "num":
            return _num(base[1] ** n)
        if base[0] == "pow":
            # (x^a)^b → x^(a*b)，避免嵌套幂造成的重复表示
            return ("pow", base[1], base[2] * n)
        return ("pow", base, n)

    # -- 减法 / 除法：改写为加法与乘法的逆元，便于统一归一 ----------------
    if kind == "sub":
        return _normalize_add([_normalize_node(ast[1]), _negate(_normalize_node(ast[2]))])
    if kind == "div":
        return _normalize_mul([_normalize_node(ast[1]), _invert(_normalize_node(ast[2]))])

    if kind == "add":
        # 必须遍历全部操作数：规范式中的 add / mul 是 n-ary 的。
        return _normalize_add([_normalize_node(child) for child in ast[1:]])
    if kind == "mul":
        return _normalize_mul([_normalize_node(child) for child in ast[1:]])

    raise ExpressionSyntaxError(f"未知的 AST 节点类型：{kind!r}")


def _try_fold_function(name: str, value: float) -> tuple | None:
    """尝试对一元函数做常数折叠；超出定义域时返回 ``None``（保留为符号节点）。"""
    try:
        if name == "abs":
            return _num(abs(value))
        if name == "exp":
            return _num(math.exp(value))
        if name == "sqrt":
            return _num(math.sqrt(value)) if value >= 0 else None
        if name == "log":
            return _num(math.log(value)) if value > 0 else None
    except (OverflowError, ValueError):
        return None
    return None


def _normalize_add(items: Iterable[tuple]) -> tuple:
    """把若干已规范化的项归一为加法节点（展平、消零、常数合并、排序）。"""
    flat: list[tuple] = []
    for item in items:
        flat.extend(_flatten(item, "add"))

    # 消去零项
    flat = [t for t in flat if not _is_num(t, 0.0)]
    if not flat:
        return _num(0.0)

    # 常数合并
    constant = 0.0
    rest: list[tuple] = []
    for t in flat:
        if t[0] == "num":
            constant += t[1]
        else:
            rest.append(t)
    if abs(constant) > _EPS:
        rest.append(_num(constant))

    if not rest:
        return _num(0.0)
    if len(rest) == 1:
        return rest[0]

    # 归并完全相同的项：x + x → 2 * x
    merged = _merge_like_terms(rest)
    if len(merged) == 1:
        return merged[0]

    merged.sort(key=_sort_key)
    return ("add", *merged)


def _merge_like_terms(items: list[tuple]) -> list[tuple]:
    """合并完全相同的加项：``x + x`` → ``mul(2, x)``。

    仅合并**结构完全相同**的项，不做系数提取（那属于代数化简，超出 P01 范围）。
    """
    counts: dict[str, tuple[tuple, int]] = {}
    order: list[str] = []
    for item in items:
        key = _canonical_repr(item)
        if key in counts:
            ast, cnt = counts[key]
            counts[key] = (ast, cnt + 1)
        else:
            counts[key] = (item, 1)
            order.append(key)
    out: list[tuple] = []
    for key in order:
        ast, cnt = counts[key]
        if cnt == 1:
            out.append(ast)
        else:
            out.append(_normalize_mul([_num(float(cnt)), ast]))
    return out


def _normalize_mul(items: Iterable[tuple]) -> tuple:
    """把若干已规范化的因子归一为乘法节点（展平、消 1、零因子、常数合并、排序）。"""
    flat: list[tuple] = []
    for item in items:
        flat.extend(_flatten(item, "mul"))

    # 零因子：任一因子为 0 则整体为 0
    for f in flat:
        if _is_num(f, 0.0):
            return _num(0.0)

    # 消去单位因子
    flat = [f for f in flat if not _is_num(f, 1.0)]
    if not flat:
        return _num(1.0)

    # 常数合并
    constant = 1.0
    rest: list[tuple] = []
    for f in flat:
        if f[0] == "num":
            constant *= f[1]
        else:
            rest.append(f)
    if not math.isfinite(constant):
        raise ExpressionError("常数折叠产生了非有限值")

    if abs(constant) <= _EPS:
        return _num(0.0)
    if abs(constant - 1.0) > _EPS:
        rest.append(_num(constant))

    if not rest:
        return _num(1.0)
    if len(rest) == 1:
        return rest[0]

    # 归并相同因子：x * x → pow(x, 2)
    rest = _merge_like_factors(rest)
    if len(rest) == 1:
        return rest[0]

    rest.sort(key=_sort_key)
    return ("mul", *rest)


def _merge_like_factors(items: list[tuple]) -> list[tuple]:
    """合并完全相同的乘因子：``x * x`` → ``pow(x, 2)``。"""
    counts: dict[str, tuple[tuple, int]] = {}
    order: list[str] = []
    for item in items:
        key = _canonical_repr(item)
        if key in counts:
            ast, cnt = counts[key]
            counts[key] = (ast, cnt + 1)
        else:
            counts[key] = (item, 1)
            order.append(key)
    out: list[tuple] = []
    for key in order:
        ast, cnt = counts[key]
        if cnt == 1:
            out.append(ast)
        elif ast[0] == "pow":
            out.append(("pow", ast[1], ast[2] * cnt))
        else:
            out.append(("pow", ast, cnt))
    return out


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def parse(
    text: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    canonicalize: bool = True,
) -> tuple:
    """解析表达式文本，返回 AST。

    :param text: 表达式源码，例如 ``"2*x + 3*(y - 1)"``。
    :param max_depth: 深度上限，默认 :data:`DEFAULT_MAX_DEPTH`。
    :param max_nodes: 节点数上限，默认 :data:`DEFAULT_MAX_NODES`。
    :param canonicalize: 为 ``True``（默认）时返回规范化 AST；
        为 ``False`` 时返回原始 AST（保留二元运算符结构），便于调试与教学。
    :raises ExpressionSyntaxError: 语法非法或含不支持的构造。
    :raises ExpressionLimitError: 超出深度或节点数预算。
    """
    tokens = _tokenize(text)
    raw = _Parser(tokens, max_depth, max_nodes).parse_program()

    # 预算检查在原始 AST 上做，避免规范化（如展平）反而放宽限制。
    d = _raw_depth(raw)
    n = _raw_node_count(raw)
    if d > max_depth:
        raise ExpressionLimitError(f"表达式深度 {d} 超过上限 {max_depth}")
    if n > max_nodes:
        raise ExpressionLimitError(f"表达式节点数 {n} 超过上限 {max_nodes}")

    if not canonicalize:
        return raw

    result = _normalize_node(raw)
    _check_canonical_limits(result, max_depth, max_nodes)
    return result


def normalize(ast: tuple) -> tuple:
    """把任意（原始或规范）AST 归一为规范式。

    幂等：``normalize(normalize(x)) == normalize(x)``。
    """
    if not isinstance(ast, tuple) or not ast:
        raise ExpressionSyntaxError("AST 必须是非空元组")
    return _normalize_node(ast)


def _check_canonical_limits(ast: tuple, max_depth: int, max_nodes: int) -> None:
    """对规范化后的 AST 再做一次预算检查。

    规范化可能合并常数从而降低规模，也可能把 ``a - b`` 改写为
    ``add(a, neg(b))`` 而略微增加节点。这里统一按规范式复核，保证对外承诺一致。
    """
    d = depth(ast)
    n = node_count(ast)
    if d > max_depth:
        raise ExpressionLimitError(f"规范化后深度 {d} 超过上限 {max_depth}")
    if n > max_nodes:
        raise ExpressionLimitError(f"规范化后节点数 {n} 超过上限 {max_nodes}")


def depth(ast: tuple) -> int:
    """返回 AST 的深度，叶节点深度为 1。"""
    kind = ast[0]
    if kind in ("num", "var"):
        return 1
    if kind == "neg":
        return 1 + depth(ast[1])
    if kind == "inv":
        return 1 + depth(ast[1])
    if kind == "func":
        return 1 + depth(ast[2])
    if kind == "pow":
        return 1 + depth(ast[1])
    if kind in ("add", "mul"):
        if len(ast) == 2:
            return 1
        return 1 + max(depth(child) for child in ast[1:])
    raise ExpressionSyntaxError(f"未知的 AST 节点类型：{kind!r}")


def node_count(ast: tuple) -> int:
    """返回 AST 的节点总数（每个运算符、叶子各计一个节点）。"""
    kind = ast[0]
    if kind in ("num", "var"):
        return 1
    if kind in ("neg", "inv"):
        return 1 + node_count(ast[1])
    if kind == "func":
        return 1 + node_count(ast[2])
    if kind == "pow":
        return 1 + node_count(ast[1])
    if kind in ("add", "mul"):
        return 1 + sum(node_count(child) for child in ast[1:])
    raise ExpressionSyntaxError(f"未知的 AST 节点类型：{kind!r}")


def to_ast(ast: tuple) -> list:
    """把元组 AST 转成可 JSON 序列化的嵌套列表。"""
    return _ast_to_jsonable(ast)


def from_ast(payload: Any) -> tuple:
    """从 :func:`to_ast` 产出的嵌套列表还原为元组 AST。

    还原后再次规范化，保证与直接 :func:`parse` 的结果一致。
    """
    node = _list_to_tuple(payload)
    return _normalize_node(node)


def _list_to_tuple(payload: Any) -> tuple:
    """把嵌套列表递归转成元组 AST。"""
    if not isinstance(payload, list) or not payload:
        raise ExpressionSyntaxError("AST 序列化必须是形如 ['kind', ...] 的非空列表")
    kind = payload[0]
    if not isinstance(kind, str):
        raise ExpressionSyntaxError("AST 节点的首元素必须标明节点类型")
    if kind in ("num",):
        return ("num", float(payload[1]))
    if kind == "var":
        return ("var", str(payload[1]))
    if kind in ("neg", "inv"):
        return (kind, _list_to_tuple(payload[1]))
    if kind == "pow":
        return ("pow", _list_to_tuple(payload[1]), int(payload[2]))
    if kind == "func":
        return ("func", str(payload[1]), _list_to_tuple(payload[2]))
    if kind in ("add", "mul"):
        return (kind, *[_list_to_tuple(item) for item in payload[1:]])
    raise ExpressionSyntaxError(f"未知的 AST 节点类型：{kind!r}")


def to_canonical_json(ast: tuple) -> str:
    """返回规范式 AST 的紧凑 JSON 串，可用于哈希与持久化。"""
    return _canonical_repr(ast)


def variables(ast: tuple) -> set[str]:
    """收集 AST 中出现的变量名（供 P02 枚举与 P03 剪枝使用）。"""
    kind = ast[0]
    if kind == "var":
        return {ast[1]}
    if kind in ("num",):
        return set()
    if kind in ("neg", "inv"):
        return variables(ast[1])
    if kind == "pow":
        return variables(ast[1])
    if kind == "func":
        return variables(ast[2])
    if kind in ("add", "mul"):
        out: set[str] = set()
        for child in ast[1:]:
            out |= variables(child)
        return out
    raise ExpressionSyntaxError(f"未知的 AST 节点类型：{kind!r}")
