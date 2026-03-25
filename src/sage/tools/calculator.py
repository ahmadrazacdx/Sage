"""
Safe mathematical expression evaluator for Sage.

Provides a LangChain `@tool`-decorated function that evaluates
mathematical expressions using Python's `ast` module — no `eval()`,
no `exec()`.  Only arithmetic operators and a curated whitelist of
`math` functions are permitted.

Security Model:
  - Parses the expression into an AST.
  - Walks the tree with a restricted `NodeVisitor` that allows only:
    `BinOp`, `UnaryOp`, `Constant`, `Call` (for whitelisted
    `math.*` functions), and `Name` (for math constants like `pi`).
  - Any disallowed node (attribute access, imports, comprehensions,
    subscripts, assignments, etc.) immediately raises `ValueError`.

Usage:

    from sage.tools.calculator import calculator
    result = calculator.invoke({"expression": "sqrt(16) + 3**2"})
    # "13.0"
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

import structlog
from langchain_core.tools import tool

log = structlog.get_logger(__name__)

# --- Allowed operators ---
_BINARY_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type, Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# --- Allowed math functions and constants ---
_SAFE_FUNCTIONS: dict[str, Any] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "ceil": math.ceil,
    "floor": math.floor,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "degrees": math.degrees,
    "radians": math.radians,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "pow": pow,
}

_SAFE_CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}

# DoS Guards.
_MAX_EXPRESSION_LENGTH: int = 500
_MAX_DEPTH: int = 50
_MAX_FACTORIAL: int = 5_000
_MAX_ARGS: int = 50
_MAX_COST: int = 10_000
_MAX_BIT_LENGTH: int = 50_000
_MAX_FLOAT: float = 1e100
_MAX_FLOAT_FUNC_ARG: float = 1e15


# --- AST Evaluator ---
class _SafeEvaluator(ast.NodeVisitor):
    """Restricted AST evaluator that permits only arithmetic operations."""

    def __init__(self) -> None:
        self._depth: int = 0
        self._cost: int = 0

    def _add_cost(self, amount: int = 1) -> None:
        self._cost += amount
        if self._cost > _MAX_COST:
            raise ValueError(f"Computation exceeded available cost limit ({_MAX_COST})")

    def _check_magnitude(self, value: Any) -> Any:
        """Assert intermediate results stay within reasonable memory bounds."""
        if isinstance(value, int):
            if value.bit_length() > _MAX_BIT_LENGTH:
                raise ValueError("Integer too large")
            return float(value)
        if isinstance(value, float):
            try:
                if math.isinf(value) or math.isnan(value) or abs(value) > _MAX_FLOAT:
                    raise ValueError("Float too large")
                if abs(value) < 1e-12:
                    return 0.0
            except OverflowError as e:
                raise ValueError("Float too large") from e
            return value
        return value

    def _check_pow(self, left: Any, right: Any) -> None:
        """Shared validation for exponentiation (ast.Pow and pow() function)."""
        if isinstance(left, int) and isinstance(right, int):
            if right > 0:
                if right * max(1, left.bit_length()) > _MAX_BIT_LENGTH:
                    raise ValueError(f"Exponentiation result exceeds {_MAX_BIT_LENGTH} bits")
                self._add_cost(right * max(1, left.bit_length() // 10))
        elif isinstance(right, (int, float)) and abs(right) > 2000:
            raise ValueError(f"Exponent too large: {right}")

    def visit(self, node: ast.AST) -> Any:  # type: ignore[override]
        """Wrap every node visit with a depth counter to catch deep nesting."""
        self._depth += 1
        self._add_cost(1)

        if self._depth > _MAX_DEPTH:
            raise ValueError(
                f"Expression too deeply nested (max depth {_MAX_DEPTH}).  "
                "Simplify the expression."
            )
        try:
            return super().visit(node)
        finally:
            self._depth -= 1

    def visit_Expression(self, node: ast.Expression) -> Any:  # noqa: N802
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:  # noqa: N802
        if isinstance(node.value, (int, float)):
            return self._check_magnitude(node.value)
        raise ValueError(f"Unsupported constant type: {type(node.value).__name__}")

    def visit_Name(self, node: ast.Name) -> Any:  # noqa: N802
        if node.id in _SAFE_CONSTANTS:
            return _SAFE_CONSTANTS[node.id]
        raise ValueError(f"Unknown variable: '{node.id}'")

    def visit_BinOp(self, node: ast.BinOp) -> Any:  # noqa: N802
        op_func = _BINARY_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        left = self.visit(node.left)
        right = self.visit(node.right)
        
        if isinstance(node.op, ast.Mult) and isinstance(left, int) and isinstance(right, int):
            if left.bit_length() + right.bit_length() > _MAX_BIT_LENGTH:
                raise ValueError(f"Multiplication result exceeds {_MAX_BIT_LENGTH} bits")
        if isinstance(node.op, ast.Pow):
            self._check_pow(left, right)
                
        try:
            result = op_func(left, right)
        except Exception as e:
            raise ValueError(f"Math error: {e}") from e
        return self._check_magnitude(result)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:  # noqa: N802
        op_func = _UNARY_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        try:
            result = op_func(self.visit(node.operand))
        except Exception as e:
            raise ValueError(f"Math error: {e}") from e
        return self._check_magnitude(result)

    def visit_Call(self, node: ast.Call) -> Any:  # noqa: N802
        # Only allow direct function calls (no method calls, no attribute access).
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only direct function calls are allowed (no method calls)")
        func_name = node.func.id
        if func_name not in _SAFE_FUNCTIONS:
            raise ValueError(
                f"Function '{func_name}' is not allowed.  "
                f"Allowed: {', '.join(sorted(_SAFE_FUNCTIONS))}"
            )
        # Guard against argument-list explosion (e.g. max(1,2,...,10^6)).
        if len(node.args) > _MAX_ARGS:
            raise ValueError(
                f"Too many arguments: {len(node.args)} (max {_MAX_ARGS})"
            )
        args = [self.visit(arg) for arg in node.args]
        
        # Guard against factorial computation blowup.
        if func_name == "factorial":
            n = args[0]
            if not isinstance(n, int) or n > _MAX_FACTORIAL:
                raise ValueError(
                    f"factorial argument too large: {n} (max {_MAX_FACTORIAL})"
                )
            self._add_cost(n // 2)
        elif func_name == "pow":
            if len(args) not in (2, 3):
                raise ValueError("pow() takes 2 or 3 arguments")
            self._check_pow(args[0], args[1])
        # Add base cost for expensive transcendental / floating-point functions.
        elif func_name in {
            "exp", "log", "log2", "log10", "sin", 
            "cos", "tan", "asin", "acos", "atan", "atan2", "sqrt"
        }:
            self._add_cost(5)
            # Prevent expensive C-level argument reduction for huge inputs (e.g. sin(1e100)).
            for i, arg in enumerate(args):
                if isinstance(arg, (int, float)) and abs(arg) > _MAX_FLOAT_FUNC_ARG:
                    raise ValueError(
                        f"Argument {i+1} to {func_name} is too large "
                        f"(max {_MAX_FLOAT_FUNC_ARG})"
                    )

        try:
            result = _SAFE_FUNCTIONS[func_name](*args)
        except Exception as e:
            raise ValueError(f"Math error: {e}") from e
        return self._check_magnitude(result)

    def generic_visit(self, node: ast.AST) -> Any:
        raise ValueError(
            f"Unsupported expression element: {type(node).__name__}.  "
            "Only arithmetic operations and math functions are allowed."
        )


def _evaluate(expression: str) -> float | int:
    """Parse and evaluate a mathematical expression safely."""
    tree = ast.parse(expression.strip(), mode="eval")
    return _SafeEvaluator().visit(tree)




# --- LangChain Tool ---
@tool
def calculator(expression: str) -> dict[str, Any]:
    """Evaluate a mathematical expression safely.

    Supports arithmetic (+, -, *, /, //, %, **), math functions
    (sqrt, log, sin, cos, etc.), and constants (pi, e, tau).
    Does NOT support variables, assignments, imports, or arbitrary
    Python code.

    Args:
        expression: A mathematical expression string, e.g.
            "sqrt(16) + 3**2" or "log(100, 10)".

    Returns:
        Structured dict containing {"result": value, "type": "float"},
        or an error message dict {"error": "..."} if invalid.
    """
    if len(expression) > _MAX_EXPRESSION_LENGTH:
        return {
            "success": False,
            "result": None,
            "error": f"Expression too long ({len(expression)} chars, max {_MAX_EXPRESSION_LENGTH})",
        }

    try:
        result = _evaluate(expression)
        log.debug("calculator_evaluated", expression=expression[:80], result=result)
        return {
            "success": True,
            "result": result,
            "type": "float",
        }
    except (ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        log.warning("calculator_failed", expression=expression[:80], error=str(exc))
        return {
            "success": False,
            "result": None,
            "error": str(exc),
        }
    except SyntaxError:
        log.warning("calculator_syntax_error", expression=expression[:80])
        return {
            "success": False,
            "result": None,
            "error": "Invalid expression syntax",
        }