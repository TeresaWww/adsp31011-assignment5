"""MCP server exposing the SAKILA.DW warehouse as four bounded, read-only tools.

The tool functions are plain Python and are also imported by the Streamlit app,
so the app and any MCP client run exactly the same SQL.

Run:  python mcp_server/server.py   (stdio transport)
"""

import os
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import snowflake.connector
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

mcp = MCPServer("sakila-dw")


class InvalidArgument(ToolError, ValueError):
    """Bad tool argument. Its message reaches the MCP client so the model can correct itself."""

# ---------------------------------------------------------------------------
# Allow-lists and constants
# ---------------------------------------------------------------------------

CATEGORIES = [
    "Action", "Animation", "Children", "Classics", "Comedy", "Documentary",
    "Drama", "Family", "Foreign", "Games", "Horror", "Music", "New",
    "Sci-Fi", "Sports", "Travel",
]
STORES = {1: "Lethbridge, Canada", 2: "Woodridge, Australia"}

# Rental activity in FACT_RENTAL spans these dates (see docs/schema_notes.md).
DATA_START = date(2005, 5, 24)
DATA_END = date(2006, 2, 14)
# Within that span rentals exist on only 41 days, in bursts (see docs/schema_notes.md).
DATA_GAPS_NOTE = (
    "Rentals exist on only 41 days: roughly one week out of every two from 2005-05-24 to "
    "2005-08-23, then nothing until a single day, 2006-02-14. Empty months and drops between "
    "periods are gaps in the data, not real changes in business."
)

MAX_LIMIT = 100

# metric name -> (SQL expression, what the number means)
METRICS = {
    "revenue": ("SUM(f.amount_paid)", "total dollars paid for rentals"),
    "rental_count": ("COUNT(*)", "number of rental transactions (rows in FACT_RENTAL)"),
    "distinct_customers": (
        "COUNT(DISTINCT c.customer_id)",
        "number of different customers (each person counted once)",
    ),
    "avg_amount_per_rental": ("AVG(f.amount_paid)", "average dollars paid per rental"),
    "avg_rental_days": ("AVG(f.rental_days)", "average days a film was kept (returned rentals only)"),
}

# group_by name -> SQL expression. Only these ever reach a GROUP BY.
GROUP_BYS = {
    "month": "d.year_month",
    "year": "d.year",
    "day_of_week": "d.day_name",
    "store": "s.store_label",
    "category": "fm.category",
    "rating": "fm.rating",
    "customer_country": "c.country",
}

# Snowflake credits per hour by warehouse size.
CREDITS_PER_HOUR = {
    "X-Small": 1, "Small": 2, "Medium": 4, "Large": 8, "X-Large": 16,
    "2X-Large": 32, "3X-Large": 64, "4X-Large": 128,
}

TABLES = [
    ("FACT_RENTAL", "One rental transaction (RENTAL_ID is unique)."),
    ("AGG_DAILY_STORE_CATEGORY", "One day x store x film category, with pre-summed revenue and rentals."),
    ("DIM_CUSTOMER", "One version of a customer (SCD Type 2). Count people with DISTINCT CUSTOMER_ID."),
    ("DIM_FILM", "One version of a film (SCD Type 2). Filter IS_CURRENT = TRUE to list films."),
    ("DIM_DATE", "One calendar day. DATE_KEY is an integer YYYYMMDD."),
    ("DIM_STORE", "One store."),
    ("DIM_STAFF", "One staff member."),
]

FACT_JOINS = """
FROM fact_rental f
JOIN dim_date d      ON d.date_key = f.rental_date_key
JOIN dim_film fm     ON fm.film_sk = f.film_sk
JOIN dim_customer c  ON c.customer_sk = f.customer_sk
JOIN dim_store s     ON s.store_id = f.store_id
"""

# ---------------------------------------------------------------------------
# Connection and helpers
# ---------------------------------------------------------------------------

_conn = None


def _connection():
    global _conn
    if _conn is None or _conn.is_closed():
        _conn = snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            password=os.environ["SNOWFLAKE_PASSWORD"],
            warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
            database=os.environ["SNOWFLAKE_DATABASE"],
            schema=os.environ["SNOWFLAKE_SCHEMA"],
            role=os.environ.get("SNOWFLAKE_ROLE", "CLAUDE_RO"),
            session_parameters={
                "STATEMENT_TIMEOUT_IN_SECONDS": 60,
                "QUERY_TAG": "adsp31011-assignment5",
                "USE_CACHED_RESULT": False,  # so reported cost reflects real work
            },
        )
    return _conn


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _execute(sql, params=None):
    """Run SQL with bound parameters; return (rows as dicts, columns, query_id)."""
    cur = _connection().cursor()
    try:
        cur.execute(sql, params or {})
        cols = [c[0].lower() for c in cur.description]
        rows = [{k: _jsonable(v) for k, v in zip(cols, r)} for r in cur.fetchall()]
        return rows, cols, cur.sfqid
    finally:
        cur.close()


def _parse_date(value, name):
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise InvalidArgument(f"{name} must be a date in YYYY-MM-DD format, got {value!r}.")


def _validate_period(start_date, end_date):
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if start > end:
        raise InvalidArgument(f"start_date {start} is after end_date {end}.")
    return start, end


def _coverage_note(start, end):
    """Explain when a requested period falls (partly) outside the data."""
    if end < DATA_START or start > DATA_END:
        return (f"The requested period {start} to {end} is entirely outside the data, "
                f"which only covers {DATA_START} to {DATA_END}. No rows means no data, not zero.")
    if start < DATA_START or end > DATA_END:
        return (f"The requested period only partly overlaps the data ({DATA_START} to {DATA_END}); "
                f"results cover the overlapping part only.")
    return None


def _validate_category(category):
    if category is None:
        return None
    match = next((c for c in CATEGORIES if c.lower() == str(category).strip().lower()), None)
    if match is None:
        raise InvalidArgument(f"Unknown category {category!r}. Allowed: {', '.join(CATEGORIES)}.")
    return match


def _validate_store(store_id):
    if store_id is None:
        return None
    try:
        store_id = int(store_id)
    except (TypeError, ValueError):
        raise InvalidArgument(f"store_id must be an integer, got {store_id!r}.")
    if store_id not in STORES:
        raise InvalidArgument(f"Unknown store_id {store_id}. Allowed: {', '.join(f'{k} ({v})' for k, v in STORES.items())}.")
    return store_id


def _filters(start, end, category, store_id):
    where = ["d.full_date BETWEEN %(start)s AND %(end)s"]
    params = {"start": start, "end": end}
    if category:
        where.append("fm.category = %(category)s")
        params["category"] = category
    if store_id:
        where.append("f.store_id = %(store_id)s")
        params["store_id"] = store_id
    return "WHERE " + "\n  AND ".join(where), params


def _render_sql(sql, params):
    """SQL with parameters inlined, for display only (execution always binds)."""
    def lit(v):
        return str(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'"
    return re.sub(r"%\((\w+)\)s", lambda m: lit(params[m.group(1)]), sql).strip()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def describe_schema() -> dict:
    """Describe the warehouse: which tables exist, what ONE ROW of each table represents,
    current row counts, and the date range the data covers.

    Use this first when the question is about what data is available, what a table means,
    or whether a period/subject is covered at all. It does not answer business questions.
    """
    sql = """SELECT table_name, row_count
FROM information_schema.tables
WHERE table_schema = CURRENT_SCHEMA()
ORDER BY table_name"""
    rows, _, qid = _execute(sql)
    counts = {r["table_name"]: r["row_count"] for r in rows}
    tables = [{"table": t, "one_row_is": grain, "row_count": counts.get(t)} for t, grain in TABLES]
    return {
        "tool": "describe_schema",
        "sql": sql,
        "query_id": qid,
        "row_count": len(tables),
        "row_represents": "one table in SAKILA.DW",
        "rows": tables,
        "data_coverage": {"start": DATA_START.isoformat(), "end": DATA_END.isoformat(),
                          "gaps": DATA_GAPS_NOTE},
        "allowed_categories": CATEGORIES,
        "stores": STORES,
    }


@mcp.tool()
def run_query(
    start_date: str,
    end_date: str,
    category: str | None = None,
    store_id: int | None = None,
    limit: int = 20,
) -> dict:
    """List individual rentals (one row per rental transaction) in a date range,
    optionally filtered to one film category and/or one store. Newest first.

    Use this when the user wants to SEE example rentals or records ("show me rentals of
    Horror films in July 2005"). Do NOT use it to count or total anything: it returns at
    most `limit` rows, so its row count is not a total. Use get_aggregate for numbers.

    Args:
        start_date: first day, YYYY-MM-DD (inclusive).
        end_date: last day, YYYY-MM-DD (inclusive).
        category: one film category, e.g. "Horror". Must be one of the 16 Sakila categories.
        store_id: 1 (Lethbridge, Canada) or 2 (Woodridge, Australia).
        limit: rows to return, 1-100 (default 20).
    """
    start, end = _validate_period(start_date, end_date)
    category = _validate_category(category)
    store_id = _validate_store(store_id)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise InvalidArgument(f"limit must be an integer, got {limit!r}.")
    limit = max(1, min(limit, MAX_LIMIT))

    where, params = _filters(start, end, category, store_id)
    params["limit"] = limit
    sql = f"""SELECT f.rental_id, f.rental_ts, fm.title, fm.category, s.store_label AS store,
       c.customer_id, c.full_name AS customer, f.amount_paid, f.is_returned
{FACT_JOINS.strip()}
{where}
ORDER BY f.rental_ts DESC
LIMIT %(limit)s"""
    rows, cols, qid = _execute(sql, params)
    return {
        "tool": "run_query",
        "sql": _render_sql(sql, params),
        "query_id": qid,
        "columns": cols,
        "rows": rows,
        "row_count": len(rows),
        "row_represents": "one rental transaction",
        "limit": limit,
        "truncated": len(rows) == limit,
        "coverage_note": _coverage_note(start, end),
    }


@mcp.tool()
def get_aggregate(
    metric: str,
    start_date: str,
    end_date: str,
    group_by: str | None = None,
    category: str | None = None,
    store_id: int | None = None,
) -> dict:
    """Compute ONE number (or one number per group) over a period: revenue, rental counts,
    distinct customers, or averages.

    Use this for any "how much / how many / average / trend / top" question.
    Pick the metric carefully:
      - "how many CUSTOMERS / people / renters"  -> metric="distinct_customers"
      - "how many RENTALS / transactions / times" -> metric="rental_count"
    These differ by ~27x in this data (16,044 rentals vs 599 customers).

    Args:
        metric: one of revenue, rental_count, distinct_customers,
                avg_amount_per_rental, avg_rental_days.
        start_date: first day, YYYY-MM-DD (inclusive).
        end_date: last day, YYYY-MM-DD (inclusive).
        group_by: optional breakdown, one of month, year, day_of_week, store,
                  category, rating, customer_country. Omit for a single total.
        category: optional filter to one film category.
        store_id: optional filter, 1 (Lethbridge, Canada) or 2 (Woodridge, Australia).
    """
    if metric not in METRICS:
        raise InvalidArgument(f"Unknown metric {metric!r}. Allowed: {', '.join(METRICS)}.")
    if group_by is not None and group_by not in GROUP_BYS:
        raise InvalidArgument(f"Unknown group_by {group_by!r}. Allowed: {', '.join(GROUP_BYS)}.")
    start, end = _validate_period(start_date, end_date)
    category = _validate_category(category)
    store_id = _validate_store(store_id)

    expr, meaning = METRICS[metric]
    where, params = _filters(start, end, category, store_id)
    if group_by:
        g = GROUP_BYS[group_by]
        sql = f"""SELECT {g} AS {group_by}, {expr} AS {metric}
{FACT_JOINS.strip()}
{where}
GROUP BY {g}
ORDER BY {g}"""
        represents = f"one {group_by.replace('_', ' ')}"
    else:
        # HAVING drops the single row when nothing matched, so "no data" is never shown as 0.
        sql = f"""SELECT {expr} AS {metric}
{FACT_JOINS.strip()}
{where}
HAVING COUNT(*) > 0"""
        represents = "the whole filtered period (a single total)"
    rows, cols, qid = _execute(sql, params)
    return {
        "tool": "get_aggregate",
        "sql": _render_sql(sql, params),
        "query_id": qid,
        "metric": metric,
        "metric_means": meaning,
        "group_by": group_by,
        "columns": cols,
        "rows": rows,
        "row_count": len(rows),
        "row_represents": represents,
        "coverage_note": _coverage_note(start, end),
        # Time breakdowns are where the gaps mislead, so flag them there.
        "data_caveat": DATA_GAPS_NOTE if group_by in ("month", "year", "day_of_week") else None,
    }


@mcp.tool()
def get_query_cost(query_id: str) -> dict:
    """Report what a previous query cost: elapsed time, compute time, bytes scanned,
    warehouse size, and credits, read from Snowflake QUERY_HISTORY.

    Use this after describe_schema, run_query or get_aggregate, passing the query_id
    they returned. Only queries run by this app's user in the last 7 days can be found.

    Credits: `estimated_credits` is computed immediately as execution time x the
    warehouse's credits-per-hour. `billed_credits` comes from
    SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY, which lags by up to several hours,
    so it is often null for a query that just ran.

    Args:
        query_id: a Snowflake query id such as "01c826f3-020c-c3cb-0034-17df0058a4ce".
    """
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", str(query_id)):
        raise InvalidArgument(f"query_id must look like 01c826f3-020c-c3cb-0034-17df0058a4ce, got {query_id!r}.")

    sql = """SELECT query_id, execution_status, total_elapsed_time, execution_time,
       compilation_time, queued_overload_time, bytes_scanned, rows_produced,
       warehouse_name, warehouse_size
FROM TABLE(information_schema.query_history_by_user(result_limit => 1000))
WHERE query_id = %(query_id)s"""
    rows, _, qid = _execute(sql, {"query_id": query_id})
    if not rows:
        return {
            "tool": "get_query_cost",
            "sql": _render_sql(sql, {"query_id": query_id}),
            "query_id": qid,
            "found": False,
            "message": "No query with that id in this user's recent query history.",
        }
    r = rows[0]
    rate = CREDITS_PER_HOUR.get(r["warehouse_size"], 0)  # no warehouse -> metadata-only, 0 credits
    estimated = (r["execution_time"] or 0) / 3_600_000 * rate

    billed = None
    try:
        b, _, _ = _execute(
            """SELECT credits_attributed_compute
FROM snowflake.account_usage.query_attribution_history
WHERE query_id = %(query_id)s""",
            {"query_id": query_id},
        )
        billed = b[0]["credits_attributed_compute"] if b else None
    except snowflake.connector.errors.ProgrammingError:
        pass  # role cannot read ACCOUNT_USAGE; estimate only

    return {
        "tool": "get_query_cost",
        "sql": _render_sql(sql, {"query_id": query_id}),
        "query_id": qid,
        "found": True,
        "status": r["execution_status"],
        "elapsed_ms": r["total_elapsed_time"],
        "execution_ms": r["execution_time"],
        "compilation_ms": r["compilation_time"],
        "bytes_scanned": r["bytes_scanned"],
        "rows_produced": r["rows_produced"],
        "warehouse": r["warehouse_name"],
        "warehouse_size": r["warehouse_size"],
        "estimated_credits": round(estimated, 8),
        "billed_credits": billed,
        "row_represents": "one executed query",
    }


if __name__ == "__main__":
    mcp.run()
