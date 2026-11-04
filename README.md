# adsp31011-assignment5

An MCP server and Streamlit app that make the Week 4 Sakila dimensional model (`SAKILA.DW` in Snowflake) answerable in plain English.

## Why four bounded tools instead of one "run any SQL" tool

A single tool that executes whatever SQL it is handed makes the model the only safeguard, and models are confidently wrong in exactly the ways that matter here. With four bounded tools, every value that reaches SQL is either a bound parameter or picked from an allow-list, so there is no injection path and no way to write, drop, or scan something unbounded; the read-only `CLAUDE_RO` role is a second layer, not the only one. More importantly, bounded tools carry meaning that raw SQL does not: `get_aggregate` forces a choice between `rental_count` and `distinct_customers`, which differ by 27x in Sakila, and every tool reports what one row of its result represents and whether the requested period is even covered by the data. That makes answers checkable — the app can show the exact SQL, the row grain, and the cost from `QUERY_HISTORY` — and it gives the model a small menu it can choose from correctly instead of a blank page it can fill with plausible but wrong queries. When a question falls outside the four tools, the honest answer is "I can't answer that," and bounded tools make that the default instead of an improvised query.

## MCP server

Four read-only tools over `SAKILA.DW`, in [mcp_server/server.py](mcp_server/server.py):

| Tool | What it does | Validation |
|------|--------------|------------|
| `describe_schema` | Tables, what one row of each represents, row counts, data coverage | none needed |
| `run_query` | Individual rentals for a date range, optional category / store | parsed dates, category and store allow-lists, `LIMIT` capped at 100 |
| `get_aggregate` | Revenue, rental count, distinct customers, or averages, optionally grouped | metric and `group_by` allow-lists, parsed dates, category and store allow-lists |
| `get_query_cost` | Elapsed time, bytes scanned, and credits for a query id, from `QUERY_HISTORY` | query id format check |

Invalid arguments are returned to the client as tool errors that list the allowed values, so a model can correct itself.

## How to Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real Snowflake credentials
python mcp_server/server.py          # MCP server over stdio
streamlit run app/streamlit_app.py   # Streamlit app
```

To use the MCP server from Claude Code:

```bash
claude mcp add sakila-dw -- "$(pwd)/.venv/bin/python" "$(pwd)/mcp_server/server.py"
```

## AI Usage

I used Claude Code (Claude Opus 5.5) as a pair programmer for this whole assignment. I reviewed every file, ran every tool and test question myself against Snowflake, and I am responsible for all of it.

**What I asked for**
- A project scaffold, inventory SQL for `SAKILA.DW`, and a summary of what one row of each table represents.
- Help connecting to Snowflake with a dedicated read-only user and role.
- The four MCP tools with allow-list validation, then a Streamlit app with a keyword router (I chose rules over an LLM so every routing decision is traceable).
- A first draft of the Part 4 break tests and this README.

**What I kept**
- The tool design: allow-lists for metric, `group_by`, category, and store; parsed dates; `LIMIT` capped at 100; bound parameters only.
- The honesty checks: the customers-vs-rentals warning, "no data, not zero" wording, the out-of-range and data-gap notes.
- The data findings from the inventory: SCD Type 2 duplicates in `DIM_CUSTOMER` and `DIM_FILM`, and rentals existing on only 41 days.

**What I threw out or changed**
- The first `CREATE USER` script used `TYPE = PERSON`, which requires MFA for password logins. It was replaced with `TYPE = LEGACY_SERVICE` and an explicit read-only role.
- Code written against the MCP SDK 1.x `FastMCP` API, which no longer exists in 2.x. It was rewritten for `MCPServer`, and validation errors were changed to `ToolError` so clients can see them.
- Router rules that failed my own tests: "drop" treated as SQL injection, "how many customer rentals" counted as people, "how many rentals did they make" read as revenue, and a generic message for "best seller film".
- An option to add a "group by film" ranking. I chose to keep ranking questions as an honest refusal instead.
