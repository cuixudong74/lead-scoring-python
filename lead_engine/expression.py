"""Safe, deterministic expression language for rule conditions.

Grammar (no eval, no arbitrary Python access)::

    expr       := or_expr
    or_expr    := and_expr ("or" and_expr)*
    and_expr   := not_expr ("and" not_expr)*
    not_expr   := "not" not_expr | comparison
    comparison := operand (OP operand)?
    OP         := "==" | "!=" | ">=" | "<=" | ">" | "<"
                  | "in" | "not" "in" | "contains"
    operand    := field | literal | list | "(" expr ")"
    field      := IDENT ("." IDENT | "[" INT "]")*
    list       := "[" [literal ("," literal)*] "]"
    literal    := NUMBER | STRING | "true" | "false" | "null"

Semantics:
* A bare operand is a truthiness test.
* ``in`` / ``not in`` require a list/tuple/dict/string on the right side.
* ``contains`` is the mirror of ``in`` (left side is the container).
* Missing fields raise RuleExecutionError(missing_field=True) so callers can
  apply a per-rule degrade policy. Type mismatches raise RuleExecutionError.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .exceptions import RuleExecutionError, RuleSyntaxError

# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


class Node:
    pass


@dataclass(frozen=True)
class Literal(Node):
    value: Any


@dataclass(frozen=True)
class Field(Node):
    path: tuple[str | int, ...]
    raw: str


@dataclass(frozen=True)
class ListLit(Node):
    items: tuple[Node, ...]


@dataclass(frozen=True)
class Compare(Node):
    op: str
    left: Node
    right: Node | None  # None => truthiness test on left


@dataclass(frozen=True)
class BoolOp(Node):
    op: str  # "and" | "or" | "not"
    operands: tuple[Node, ...]


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_KEYWORDS = {"and", "or", "not", "in", "contains", "true", "false", "null"}
_TWO_CHAR_OPS = ("==", "!=", ">=", "<=")
_ONE_CHAR_OPS = (">", "<")


@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    pos: int


def _tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "#":  # comment to end of line
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c in "()[].,":
            tokens.append(Token(c, c, i))
            i += 1
            continue
        if text.startswith(_TWO_CHAR_OPS, i):
            tokens.append(Token("OP", text[i:i + 2], i))
            i += 2
            continue
        if c in _ONE_CHAR_OPS:
            tokens.append(Token("OP", c, i))
            i += 1
            continue
        if c in "\"'":
            quote = c
            j = i + 1
            buf: list[str] = []
            while j < n and text[j] != quote:
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", '"': '"',
                                "'": "'", "\\": "\\"}.get(esc, esc))
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            if j >= n:
                raise RuleSyntaxError("<tokenize>",
                                      f"unterminated string at position {i}")
            tokens.append(Token("STRING", "".join(buf), i))
            i = j + 1
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and
                           (text[i + 1].isdigit() or text[i + 1] == ".")):
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            raw = text[i:j]
            try:
                number: Any = float(raw) if "." in raw else int(raw)
            except ValueError:
                raise RuleSyntaxError("<tokenize>",
                                      f"bad number {raw!r} at position {i}")
            tokens.append(Token("NUMBER", number, i))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            low = word.lower()
            if low in ("true", "false", "null"):
                tokens.append(Token(low.upper(),
                                    {"true": True, "false": False,
                                     "null": None}[low], i))
            else:
                # field paths may contain dots; split them later
                tokens.append(Token("WORD", word, i))
            i = j
            continue
        raise RuleSyntaxError("<tokenize>",
                              f"unexpected character {c!r} at position {i}")
    tokens.append(Token("EOF", None, n))
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, text: str, rule_id: str) -> None:
        self.text = text
        self.rule_id = rule_id
        self.tokens = _tokenize(text)
        self.pos = 0

    def _syntax(self, msg: str) -> RuleSyntaxError:
        tok = self.tokens[min(self.pos, len(self.tokens) - 1)]
        return RuleSyntaxError(self.rule_id, f"{msg} at position {tok.pos}")

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def next(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind: str) -> Token:
        tok = self.peek()
        if tok.kind != kind:
            raise self._syntax(f"expected {kind!r}, got {tok.kind!r}")
        return self.next()

    def parse(self) -> Node:
        node = self.parse_or()
        if self.peek().kind != "EOF":
            raise self._syntax("unexpected trailing tokens")
        return node

    def parse_or(self) -> Node:
        operands = [self.parse_and()]
        while self._is_word("or"):
            self.next()
            operands.append(self.parse_and())
        if len(operands) == 1:
            return operands[0]
        return BoolOp("or", tuple(operands))

    def parse_and(self) -> Node:
        operands = [self.parse_not()]
        while self._is_word("and"):
            self.next()
            operands.append(self.parse_not())
        if len(operands) == 1:
            return operands[0]
        return BoolOp("and", tuple(operands))

    def parse_not(self) -> Node:
        if self._is_word("not"):
            self.next()
            if self._is_word("in"):
                # "not in" only legal as an infix operator
                raise self._syntax("'not' must be followed by a condition")
            return BoolOp("not", (self.parse_not(),))
        return self.parse_comparison()

    def parse_comparison(self) -> Node:
        left = self.parse_operand()
        tok = self.peek()
        if tok.kind == "OP":
            self.next()
            right = self.parse_operand()
            return Compare(tok.value, left, right)
        if tok.kind == "WORD" and tok.value.lower() == "contains":
            self.next()
            right = self.parse_operand()
            return Compare("contains", left, right)
        if tok.kind == "WORD" and tok.value.lower() == "in":
            self.next()
            right = self.parse_operand()
            return Compare("in", left, right)
        if tok.kind == "WORD" and tok.value.lower() == "not":
            save = self.pos
            self.next()
            nxt = self.peek()
            if nxt.kind == "WORD" and nxt.value.lower() == "in":
                self.next()
                right = self.parse_operand()
                return Compare("not in", left, right)
            self.pos = save
        return Compare("truthy", left, None)

    def parse_operand(self) -> Node:
        tok = self.peek()
        if tok.kind == "(":
            self.next()
            node = self.parse_or()
            self.expect(")")
            return node
        if tok.kind == "[":
            self.next()
            items: list[Node] = []
            if self.peek().kind != "]":
                items.append(self.parse_operand())
                while self.peek().kind == ",":
                    self.next()
                    items.append(self.parse_operand())
            self.expect("]")
            return ListLit(tuple(items))
        if tok.kind == "NUMBER":
            self.next()
            return Literal(tok.value)
        if tok.kind == "STRING":
            self.next()
            return Literal(tok.value)
        if tok.kind in ("TRUE", "FALSE", "NULL"):
            self.next()
            return Literal(tok.value)
        if tok.kind == "WORD":
            return self.parse_field()
        raise self._syntax(f"expected operand, got {tok.kind!r}")

    def parse_field(self) -> Node:
        parts: list[str | int] = [self.expect("WORD").value]
        while True:
            tok = self.peek()
            if tok.kind == ".":
                self.next()
                parts.append(self.expect("WORD").value)
            elif tok.kind == "[":
                self.next()
                idx_tok = self.expect("NUMBER")
                if not isinstance(idx_tok.value, int):
                    raise self._syntax("list index must be an integer")
                parts.append(idx_tok.value)
                self.expect("]")
            else:
                break
        return Field(tuple(parts), str(parts[0]))

    def _is_word(self, word: str) -> bool:
        tok = self.peek()
        return tok.kind == "WORD" and tok.value.lower() == word


def parse(expression: str, rule_id: str = "<anon>") -> Node:
    """Parse a condition string into an AST. Raises RuleSyntaxError."""
    if not isinstance(expression, str) or not expression.strip():
        raise RuleSyntaxError(rule_id, "empty condition")
    return _Parser(expression, rule_id).parse()


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def _resolve(field: Field, attributes: dict[str, Any], rule_id: str) -> Any:
    current: Any = attributes
    for part in field.path:
        if isinstance(part, int):
            if not isinstance(current, (list, tuple)):
                raise RuleExecutionError(
                    rule_id,
                    f"cannot index non-list with [{part}]")
            if part < -len(current) or part >= len(current):
                raise RuleExecutionError(
                    rule_id, f"list index {part} out of range",
                    missing_field=True)
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                raise RuleExecutionError(
                    rule_id, f"missing field {part!r}",
                    missing_field=True)
            current = current[part]
    return current


def _eval_node(node: Node, attributes: dict[str, Any], rule_id: str) -> Any:
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, Field):
        return _resolve(node, attributes, rule_id)
    if isinstance(node, ListLit):
        return [_eval_node(item, attributes, rule_id) for item in node.items]
    if isinstance(node, BoolOp):
        if node.op == "not":
            return not _truthy(_eval_node(node.operands[0], attributes, rule_id))
        # Three-valued boolean logic: a missing field inside AND/OR is
        # "unknown" -- it only matters if the other side cannot decide the
        # result, in which case the missing-field error propagates.
        saw_missing: RuleExecutionError | None = None
        results: list[bool] = []
        for operand in node.operands:
            try:
                results.append(_truthy(_eval_node(operand, attributes, rule_id)))
            except RuleExecutionError as exc:
                if not exc.missing_field:
                    raise
                saw_missing = exc
        if node.op == "and":
            if False in results:
                return False
            if saw_missing is not None:
                raise saw_missing
            return True
        else:  # or
            if True in results:
                return True
            if saw_missing is not None:
                raise saw_missing
            return False
    if isinstance(node, Compare):
        if node.op == "truthy":
            # A bare *field* flag check treats an absent flag as falsy, so
            # idioms like `not unsubscribed` work. Compound subexpressions
            # (e.g. parenthesised groups) still propagate missing-field
            # errors so comparisons inside them cannot silently mis-match.
            if isinstance(node.left, Field):
                try:
                    left = _eval_node(node.left, attributes, rule_id)
                except RuleExecutionError as exc:
                    if exc.missing_field:
                        return False
                    raise
            else:
                left = _eval_node(node.left, attributes, rule_id)
            return _truthy(left)
        left = _eval_node(node.left, attributes, rule_id)
        assert node.right is not None
        right = _eval_node(node.right, attributes, rule_id)
        return _apply_op(node.op, left, right, rule_id)
    raise RuleExecutionError(rule_id, f"unknown AST node {type(node).__name__}")


def _truthy(value: Any) -> bool:
    return bool(value)


def _apply_op(op: str, left: Any, right: Any, rule_id: str) -> bool:
    try:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == "in":
            if not isinstance(right, (list, tuple, dict, str)):
                raise RuleExecutionError(
                    rule_id,
                    f"'in' requires a list/string on the right, got "
                    f"{type(right).__name__}")
            return left in right
        if op == "not in":
            if not isinstance(right, (list, tuple, dict, str)):
                raise RuleExecutionError(
                    rule_id,
                    f"'not in' requires a list/string on the right, got "
                    f"{type(right).__name__}")
            return left not in right
        if op == "contains":
            if not isinstance(left, (list, tuple, dict, str)):
                raise RuleExecutionError(
                    rule_id,
                    f"'contains' requires a list/string on the left, got "
                    f"{type(left).__name__}")
            return right in left
    except TypeError as exc:
        raise RuleExecutionError(rule_id, f"type error for '{op}': {exc}")
    raise RuleExecutionError(rule_id, f"unknown operator {op!r}")


def evaluate(node: Node, attributes: dict[str, Any],
             rule_id: str = "<anon>") -> bool:
    """Evaluate an AST against lead attributes. Raises RuleExecutionError."""
    return _truthy(_eval_node(node, attributes, rule_id))
