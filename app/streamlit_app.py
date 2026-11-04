"""Streamlit app: ask a question about Sakila rentals in plain English.

Every answer shows which tool ran, the SQL it executed, how many rows came back,
what one row represents, and what the query cost.

Run:  streamlit run app/streamlit_app.py
"""

import sys
import time
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "mcp_server"), str(ROOT / "app")]

import server as tools  # noqa: E402
from router import route  # noqa: E402

BAR_COLOR = "#2a78d6"
DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
             "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
METRIC_LABELS = {
    "revenue": "Revenue",
    "rental_count": "Rentals",
    "distinct_customers": "Distinct customers",
    "avg_amount_per_rental": "Average paid per rental",
    "avg_rental_days": "Average rental length (days)",
}
GROUP_LABELS = {
    "month": "Month", "year": "Year", "day_of_week": "Day of week", "store": "Store",
    "category": "Category", "rating": "Rating", "customer_country": "Customer country",
}
EXAMPLES = [
    "How many customers rented Horror films in July 2005?",
    "Revenue by month",
    "Which category earns the most revenue?",
    "Compare stores by rentals",
    "Show me recent Sci-Fi rentals",
    "Why did revenue drop in 2007?",
]

st.set_page_config(page_title="Sakila Rentals Q&A", page_icon="🎬", layout="wide")


# ---------------------------------------------------------------------------
# Tool calls (cached so reruns from widget changes don't re-query Snowflake)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=600, show_spinner=False)
def call_tool(tool, args):
    return getattr(tools, tool)(**dict(args))


@st.cache_data(ttl=600, show_spinner=False)
def query_cost(query_id):
    # QUERY_HISTORY can take a moment to show a query that just finished.
    for _ in range(3):
        cost = tools.get_query_cost(query_id)
        if cost["found"]:
            return cost
        time.sleep(1)
    return cost


def run(tool, **args):
    """Call a tool and record it for the 'how this answer was produced' panel."""
    result = call_tool(tool, tuple(sorted(args.items())))
    if not st.session_state.calls and tool != "get_query_cost":
        st.session_state.last_query_id = result["query_id"]  # main query of this answer
    st.session_state.calls.append(result)
    return result


# ---------------------------------------------------------------------------
# Formatting and charts
# ---------------------------------------------------------------------------

def fmt(metric, value):
    if value is None:
        return "no data"
    if metric == "revenue":
        return f"${value:,.2f}"
    if metric == "avg_amount_per_rental":
        return f"${value:,.2f}"
    if metric == "avg_rental_days":
        return f"{value:,.2f} days"
    return f"{value:,.0f}"


def bar_chart(df, x, y, title, x_title, y_title, horizontal=False, sort=None):
    base = alt.Chart(df, title=title)
    if horizontal:
        chart = base.mark_bar(color=BAR_COLOR, cornerRadiusEnd=4, height={"band": 0.7}).encode(
            y=alt.Y(f"{x}:N", title=x_title, sort="-x"),
            x=alt.X(f"{y}:Q", title=y_title),
            tooltip=[alt.Tooltip(f"{x}:N", title=x_title), alt.Tooltip(f"{y}:Q", title=y_title, format=",.2f")],
        )
    else:
        chart = base.mark_bar(color=BAR_COLOR, cornerRadiusEnd=4, width={"band": 0.7}).encode(
            x=alt.X(f"{x}:N", title=x_title, sort=sort, axis=alt.Axis(labelAngle=0)),
            y=alt.Y(f"{y}:Q", title=y_title),
            tooltip=[alt.Tooltip(f"{x}:N", title=x_title), alt.Tooltip(f"{y}:Q", title=y_title, format=",.2f")],
        )
    st.altair_chart(chart.properties(height=320), width="stretch")


# ---------------------------------------------------------------------------
# Answer renderers, one per tool
# ---------------------------------------------------------------------------

def answer_schema(_plan):
    r = run("describe_schema")
    cov = r["data_coverage"]
    st.subheader(f"The warehouse has {r['row_count']} tables")
    st.write(f"Rentals run from **{cov['start']}** to **{cov['end']}**. {cov['gaps']}")
    df = pd.DataFrame(r["rows"]).rename(columns={"table": "Table", "one_row_is": "One row is", "row_count": "Rows"})
    bar_chart(df, "Table", "Rows", "Rows per table", "Table", "Rows", horizontal=True)
    st.dataframe(df, hide_index=True, width="stretch")


def answer_cost(_plan):
    qid = st.session_state.get("last_query_id")
    if not qid:
        st.info("No query has run yet in this session. Ask a data question first, then ask what it cost.")
        return
    c = run("get_query_cost", query_id=qid)
    if not c["found"]:
        st.warning("That query isn't in Snowflake's query history yet, so its cost can't be shown.")
        return
    st.subheader("Cost of the previous query")
    cols = st.columns(4)
    cols[0].metric("Elapsed", f"{c['elapsed_ms']:,} ms")
    cols[1].metric("Bytes scanned", f"{c['bytes_scanned']:,}")
    cols[2].metric("Estimated credits", f"{c['estimated_credits']:.6f}")
    cols[3].metric("Warehouse", f"{c['warehouse'] or 'none'} ({c['warehouse_size'] or 'metadata only'})")
    df = pd.DataFrame({"Phase": ["Compilation", "Execution"],
                       "Milliseconds": [c["compilation_ms"], c["execution_ms"]]})
    bar_chart(df, "Phase", "Milliseconds", "Where the time went", "Phase", "Milliseconds")


def answer_rows(plan):
    r = run("run_query", **plan.args)
    if r["row_count"] == 0:
        no_data(r)
        return
    st.subheader(f"{r['row_count']} rentals, newest first")
    if r["truncated"]:
        st.caption(f"Capped at {r['limit']} rows. This is a sample, not a total. Ask \"how many rentals ...\" for a count.")
    df = pd.DataFrame(r["rows"])
    counts = df.groupby("category").size().reset_index(name="rentals")
    bar_chart(counts, "category", "rentals", f"Categories among the {len(df)} rentals shown",
              "Category", "Rentals in this sample", horizontal=True)
    st.dataframe(df.rename(columns=lambda c: c.replace("_", " ").capitalize()), hide_index=True, width="stretch")
    if r["coverage_note"]:
        st.info(r["coverage_note"])


def answer_aggregate(plan):
    args = plan.args
    metric = args["metric"]
    label = METRIC_LABELS[metric]
    r = run("get_aggregate", **args)

    if r["row_count"] == 0:
        no_data(r)
        if plan.asks_why:
            st.warning("Because there is no data for that period, there is nothing to explain. "
                       "The question assumes a change that the data cannot confirm.")
        return

    # The question is about people, but the number isn't a count of people.
    if plan.asks_people and metric in ("rental_count", "avg_amount_per_rental", "avg_rental_days"):
        other = run("get_aggregate", **{**args, "metric": "distinct_customers", "group_by": None})
        n = other["rows"][0]["distinct_customers"] if other["rows"] else 0
        st.warning(f"**Check the question.** You mentioned customers, but this answer measures "
                   f"{r['metric_means']}. The number of distinct customers for the same filters is **{n:,}**.")

    if args["group_by"]:
        g = args["group_by"]
        df = pd.DataFrame(r["rows"])
        df[g] = df[g].astype(str)
        if g == "day_of_week":
            df = df.sort_values(g, key=lambda col: col.map(DAY_ORDER.index)).reset_index(drop=True)
        top, bottom = df.loc[df[metric].idxmax()], df.loc[df[metric].idxmin()]
        st.subheader(f"{label} by {GROUP_LABELS[g].lower()}")
        st.write(f"Highest: **{top[g]}** ({fmt(metric, top[metric])}). "
                 f"Lowest: **{bottom[g]}** ({fmt(metric, bottom[metric])}).")
        sort = DAY_ORDER if g == "day_of_week" else None
        bar_chart(df, g, metric, f"{label} by {GROUP_LABELS[g].lower()}", GROUP_LABELS[g], label,
                  horizontal=g in ("category", "customer_country"), sort=sort)
        st.dataframe(df.rename(columns={g: GROUP_LABELS[g], metric: label}), hide_index=True, width="stretch")
    else:
        value = r["rows"][0][metric]
        st.subheader(f"{label}: {fmt(metric, value)}")
        st.caption(f"This is {r['metric_means']}, from {args['start_date']} to {args['end_date']}"
                   + (f", category {args['category']}" if args["category"] else "")
                   + (f", store {args['store_id']}" if args["store_id"] else "") + ".")
        if metric in ("rental_count", "distinct_customers"):
            twin = "distinct_customers" if metric == "rental_count" else "rental_count"
            t = run("get_aggregate", **{**args, "metric": twin})
            tv = t["rows"][0][twin] if t["rows"] else 0
            rentals, customers = (value, tv) if metric == "rental_count" else (tv, value)
            if customers:
                st.write(f"For comparison: **{customers:,}** distinct customers made **{rentals:,}** rentals "
                         f"({rentals / customers:.1f} rentals per customer). These are different numbers.")
        # Chart the same number broken down by the first dimension that has more than one value.
        for g in ("month", "store", "category"):
            if (g == "store" and args["store_id"]) or (g == "category" and args["category"]):
                continue
            df = pd.DataFrame(run("get_aggregate", **{**args, "group_by": g})["rows"])
            if len(df) > 1:
                df[g] = df[g].astype(str)
                bar_chart(df, g, metric, f"{label} by {GROUP_LABELS[g].lower()}, same filters",
                          GROUP_LABELS[g], label, horizontal=g == "category")
                break

    if plan.asks_why:
        st.info("This app can show **what** happened, not **why**. "
                "The warehouse has no data on causes such as prices, marketing, or competition.")
    if r["data_caveat"]:
        st.warning(f"**Gaps in the data.** {r['data_caveat']}")
    if r["coverage_note"]:
        st.info(r["coverage_note"])


def no_data(r):
    st.subheader("No data for that")
    st.write("The query returned **zero rows**. That means there is no data matching the question, "
             "**not** that the answer is zero.")
    if r.get("coverage_note"):
        st.info(r["coverage_note"])
    else:
        # Inside the covered span but still empty: one of the gaps.
        st.info(f"The period is inside the data's overall range, but it falls in a gap. {tools.DATA_GAPS_NOTE}")


RENDERERS = {"describe_schema": answer_schema, "get_query_cost": answer_cost,
             "run_query": answer_rows, "get_aggregate": answer_aggregate}


# ---------------------------------------------------------------------------
# "How this answer was produced"
# ---------------------------------------------------------------------------

def provenance(plan):
    st.divider()
    st.markdown("#### How this answer was produced")
    if plan.notes:
        st.markdown("**How the question was read:** " + " ".join(plan.notes))
    for i, c in enumerate(st.session_state.calls):
        with st.container(border=True):
            heading = "Main query" if i == 0 else "Supporting query"
            st.markdown(f"**{heading}** · tool `{c['tool']}`")
            cols = st.columns(3)
            cols[0].metric("Rows returned", f"{c.get('row_count', 1):,}")
            cols[1].markdown(f"**One row is**  \n{c.get('row_represents', '-')}")
            if c["tool"] == "get_query_cost":
                cols[2].markdown("**Query cost**  \nThis lookup reads query history; its own cost is negligible.")
            else:
                cost = query_cost(c["query_id"])
                if cost["found"]:
                    cols[2].markdown(
                        f"**Query cost**  \n{cost['elapsed_ms']:,} ms elapsed · "
                        f"{cost['bytes_scanned']:,} bytes scanned · ~{cost['estimated_credits']:.6f} credits "
                        f"({cost['warehouse_size'] or 'no warehouse'})")
                else:
                    cols[2].markdown("**Query cost**  \nNot in query history yet.")
            st.code(c["sql"], language="sql")
    st.caption("Credits are estimated as execution time × the warehouse's credits per hour. Snowflake bills "
               "a warehouse for at least 60 seconds each time it resumes, so the real charge can be higher. "
               "Repeated questions are served from the app's cache and show the original query's cost.")


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def set_question(q):
    st.session_state.question = q


with st.sidebar:
    st.header("Filters")
    st.caption("Used when your question doesn't name its own period, category, or store. "
               "They keep their values as you ask new questions.")
    period = st.date_input("Period", value=(tools.DATA_START, tools.DATA_END),
                           min_value=date(2005, 1, 1), max_value=date(2007, 1, 1), key="period")
    store = st.selectbox("Store", [None, 1, 2], key="store",
                         format_func=lambda s: "All stores" if s is None else f"Store {s} ({tools.STORES[s]})")
    category = st.selectbox("Category", [None] + tools.CATEGORIES, key="category",
                            format_func=lambda c: "All categories" if c is None else c)
    st.divider()
    st.caption(f"Data covers {tools.DATA_START} to {tools.DATA_END}, with gaps. "
               "Ask \"what data do you have?\" for details.")

st.title("🎬 Sakila Rentals Q&A")
st.write("Ask a question about rentals, revenue, or customers. Every answer shows the query behind it.")

if "question" not in st.session_state and "q" in st.query_params:
    st.session_state.question = st.query_params["q"]  # e.g. ?q=Revenue+by+month, for sharable links

st.text_input("Your question", key="question", placeholder="e.g. How many customers rented Horror films in July 2005?")
st.caption("Try one of these:")
example_cols = st.columns(3)
for i, ex in enumerate(EXAMPLES):
    example_cols[i % 3].button(ex, on_click=set_question, args=(ex,), width="stretch")

question = st.session_state.get("question", "").strip()
if question:
    start, end = period if isinstance(period, tuple) and len(period) == 2 else (tools.DATA_START, tools.DATA_END)
    plan = route(question, start, end, category, store)
    st.session_state.calls = []

    if plan.kind == "refuse":
        st.error(f"**Not allowed.** {plan.message}")
    elif plan.kind == "unsupported":
        st.warning(f"**I can't answer that.** {plan.message}")
        st.caption("These tools cover rentals, revenue, customers, film categories, ratings, stores, "
                   "and customer countries, from May 2005 to February 2006.")
    elif plan.kind == "clarify":
        st.info(plan.message)
    else:
        try:
            with st.spinner("Querying Snowflake..."):
                RENDERERS[plan.tool](plan)
        except ValueError as e:
            st.warning(f"**I can't answer that.** {e}")
        if st.session_state.calls:
            provenance(plan)
