"""Unit tests for mcp.app helpers (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app import _safe_dsn, validate_sql_query  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402


# ---- _safe_dsn --------------------------------------------------------------
def test_safe_dsn_passthrough_when_no_special_chars():
    url = "postgresql://user:pass@host:5432/db?sslmode=require"
    assert _safe_dsn(url) == url


def test_safe_dsn_encodes_password_with_hash():
    url = "postgresql://user:pa#ss@host:5432/db"
    out = _safe_dsn(url)
    assert "pa%23ss" in out
    assert out.endswith("@host:5432/db")


def test_safe_dsn_preserves_query():
    url = "postgresql://u:p%23w@host:5432/db?sslmode=require"
    assert _safe_dsn(url).endswith("?sslmode=require")


def test_safe_dsn_no_userinfo():
    url = "postgresql://host:5432/db"
    assert _safe_dsn(url) == url


# ---- accepted ---------------------------------------------------------------
def test_simple_select_passes():
    validate_sql_query("SELECT 1")


def test_select_with_trailing_semicolon_passes():
    validate_sql_query("SELECT 1;")


def test_with_cte_passes():
    validate_sql_query("WITH x AS (SELECT 1) SELECT * FROM x")


# ---- rejected ---------------------------------------------------------------
@pytest.mark.parametrize(
    "query",
    [
        "DROP TABLE products",
        "DELETE FROM products",
        "INSERT INTO products VALUES (1)",
        "UPDATE products SET price = 0",
        "ALTER TABLE products ADD col INT",
        "CREATE TABLE foo (id INT)",
        "TRUNCATE products",
        "GRANT SELECT ON products TO public",
        "REVOKE SELECT ON products FROM public",
        "EXEC sp_evil",
        "EXECUTE sp_evil",
        "MERGE INTO products USING staging ON 1=1",
        "CALL evil_proc()",
        "COPY products TO '/tmp/x'",
    ],
)
def test_destructive_statements_rejected(query):
    with pytest.raises(ToolError):
        validate_sql_query(query)


def test_multiple_statements_rejected():
    with pytest.raises(ToolError):
        validate_sql_query("SELECT 1; SELECT 2")


def test_comment_rejected():
    with pytest.raises(ToolError):
        validate_sql_query("SELECT 1 -- evil")


def test_block_comment_rejected():
    with pytest.raises(ToolError):
        validate_sql_query("SELECT /* evil */ 1")


def test_non_select_keyword_rejected():
    with pytest.raises(ToolError):
        validate_sql_query("EXPLAIN SELECT 1")
