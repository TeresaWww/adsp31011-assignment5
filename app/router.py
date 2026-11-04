"""Keyword router: turn a plain-English question into one tool call, or an honest refusal.

No LLM is involved. Every decision is a visible rule, so a wrong route can be traced
to the rule that made it. `route()` never touches Snowflake.
"""

import calendar
import re
from dataclasses import dataclass, field
from datetime import date

from server import CATEGORIES, DATA_END, DATA_START

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
SEASONS = {"spring": (3, 5), "summer": (6, 8), "fall": (9, 11), "autumn": (9, 11)}

PEOPLE_WORDS = r"\b(customers?|people|persons?|renters?|users?|members?|who)\b"
EVENT_WORDS = r"\b(rentals?|rented|rents|transactions?|times|orders?)\b"

# Things that look like an attempt to make the tools do something they shouldn't.
UNSAFE = [
    # SQL write verbs followed by a SQL object, so "revenue drop in 2007" is not caught.
    (r"\b(drop|truncate|alter|create|replace)\s+(a\s+|an\s+|new\s+|the\s+)*(table|schema|database|view|warehouse|role|user|stage)\b"
     r"|\bdelete\s+from\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b|\b(grant|revoke)\b.+\b(to|from)\s+(role|user)\b",
     "This app is read-only and only runs four fixed queries. It cannot change data."),
    (r"\bselect\b.+\bfrom\b|\braw sql\b|\brun (this|my|the following) (sql|query)\b|;\s*--",
     "This app does not run SQL that you type. It only runs its own four bounded queries."),
    (r"\bignore (all |any |the )?(previous|prior|above|earlier)\b|\bsystem prompt\b|\bjailbreak\b",
     "There are no instructions to override here: questions are matched by fixed rules, not by a chatbot."),
    (r"\b(e-?mails?|phone|passwords?|addresses?|credit cards?)\b",
     "This app does not return customers' personal contact details."),
]

# Questions about data the warehouse does not have.
NOT_IN_DATA = [
    (r"\b(profit|margins?|cogs|expenses?|operating costs?)\b", "costs or profit (only payments received are stored)"),
    (r"\b(salary|salaries|wages?|payroll)\b", "staff pay"),
    (r"\b(reviews?|stars|satisfaction|nps|feedback)\b", "customer reviews or satisfaction"),
    (r"\b(age|gender|income|demographics?)\b", "customer demographics"),
    (r"\b(forecast|predict|projection|next (year|month|quarter))\b", "forecasts (only historical rentals are stored)"),
    (r"\b(streaming|online|website|app downloads?|marketing|ads?|campaigns?)\b", "online, streaming, or marketing activity"),
    (r"\b(weather|stock price|competitors?)\b", "anything outside the rental business"),
    (r"\b(actors?|actress(es)?|cast)\b", "actors (not in the dimensional model)"),
    (r"\b(staff member|employee|cashier)s?\b.*\b(best|most|performance)\b", "staff performance"),
]

# A ranking word shortly before a film/customer noun: "best seller film", "top 10 movies", "most rented title".
RANKING = (r"\b(top|best|most popular|best[- ]?sell(er|ers|ing)|highest|most rented|favou?rite)\b(\s+\S+){0,3}?\s+"
           r"(films?|movies?|titles?|customers?|renters?|actors?)\b")

VAGUE = r"^\s*(how are we doing|how('s| is) (it|business) going|any (insights?|news)|what('s| is) up|status|overview|summary)\W*$"


@dataclass
class Plan:
    kind: str                      # "answer", "refuse", "unsupported", "clarify"
    tool: str | None = None
    args: dict = field(default_factory=dict)
    message: str = ""
    notes: list = field(default_factory=list)     # assumptions the router made
    asks_people: bool = False      # question is about customers (people), not rentals
    asks_why: bool = False         # question asks for a cause


def _period(q):
    """Return (start, end, description) if the question names a period, else None."""
    years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", q)]
    year = years[0] if years else None

    m = re.search(r"\bq([1-4])\b", q)
    if m and year:
        qn = int(m.group(1))
        start = date(year, 3 * qn - 2, 1)
        end_month = 3 * qn
        return start, date(year, end_month, calendar.monthrange(year, end_month)[1]), f"Q{qn} {year}"

    for name, (m1, m2) in SEASONS.items():
        if re.search(rf"\b{name}\b", q):
            y = year or 2005
            note = "" if year else " (year assumed 2005)"
            return date(y, m1, 1), date(y, m2, calendar.monthrange(y, m2)[1]), f"{name} {y}{note}"

    for name, num in MONTHS.items():
        if name == "may" and not re.search(r"\bmay\s+(19|20)\d{2}\b|\bin may\b", q):
            continue  # "may" is usually the verb
        if re.search(rf"\b{name}\b", q):
            y = year or 2005
            note = "" if year else " (year assumed 2005)"
            return date(y, num, 1), date(y, num, calendar.monthrange(y, num)[1]), f"{calendar.month_name[num]} {y}{note}"

    if year:
        return date(year, 1, 1), date(year, 12, 31), str(year)
    return None


def _category(q):
    if re.search(r"\bsci[- ]?fi\b|\bscience fiction\b", q):
        return "Sci-Fi"
    if re.search(r"\bkids?\b", q):
        return "Children"
    for c in CATEGORIES:
        if c == "New":
            if re.search(r"\b(new (category|releases?)|category new)\b", q):
                return c
            continue
        if re.search(rf"\b{re.escape(c.lower())}\b", q):
            return c
    return None


def _store(q):
    if re.search(r"\bstore\s*(#|no\.?|number)?\s*1\b|\blethbridge\b|\bcanad(a|ian)\b", q):
        return 1
    if re.search(r"\bstore\s*(#|no\.?|number)?\s*2\b|\bwoodridge\b|\baustralian?\b", q):
        return 2
    return None


def _group_by(q):
    rules = [
        (r"\b(by|per|each|every) month\b|\bmonthly\b|\btrend\b|\bover time\b|\bmonth by month\b", "month"),
        (r"\b(by|per|each) year\b|\byearly\b|\bannual\b", "year"),
        (r"\bday of (the )?week\b|\bweekdays?\b|\bweekends?\b|\b(by|per) day\b", "day_of_week"),
        (r"\b(by|per|each|both|compare|across|which|best|top|busiest) stores?\b|\bstores?\b.*\bcompare\b", "store"),
        (r"\b(by|per|each|which|top|across) (film )?(categor(y|ies)|genres?)\b|\bcategor(y|ies)\b.*\b(most|best|top)\b", "category"),
        (r"\b(by|per|each|which) (rating|mpaa)\b|\bratings\b", "rating"),
        (r"\b(by|per|each|which) countr(y|ies)\b|\bcountries\b", "customer_country"),
    ]
    for pattern, g in rules:
        if re.search(pattern, q):
            return g
    return None


def _metric(q):
    """Return (metric, asks_people) or (None, asks_people)."""
    people = bool(re.search(PEOPLE_WORDS, q))
    events = bool(re.search(EVENT_WORDS, q))
    if re.search(r"\b(average|avg|mean|typical)\b", q):
        if re.search(r"\b(days?|duration|long|kept|keep)\b", q):
            return "avg_rental_days", people
        if re.search(r"\b(amount|spend|spent|paid|payment|revenue|price|pay|dollars?|\$)\b", q):
            return "avg_amount_per_rental", people
    if re.search(r"\b(revenue|sales|money|earn(s|ed|ings)?|income|paid|payments?|dollars?|how much)\b|\$", q):
        return "revenue", people
    if re.search(r"\b(make|makes|made)\b", q) and not re.search(r"\bhow many\b", q):
        return "revenue", people  # "how much did store 1 make", not "how many rentals did they make"
    # People are counted only when they are what's being counted: "how many customers ..."
    # but not "how many customer rentals ..." or "how many times did customers ...".
    counts_people = re.search(
        r"\b(how many|number of|count of|count)\s+(unique |distinct |different |individual )?"
        r"(customers|people|persons|renters|users|members)\b(?!\s+rentals)", q)
    if counts_people or re.search(r"\b(unique|distinct|different|individual) (customers|people|renters)\b|\bwho\b", q):
        return "distinct_customers", people
    if events or re.search(r"\b(how many|number of|count|busiest|volume|activity)\b", q):
        return "rental_count", people
    if people:
        return "distinct_customers", people
    return None, people


def route(question, default_start, default_end, default_category=None, default_store=None):
    q = question.strip().lower()
    if not q:
        return Plan("clarify", message="Type a question to get started.")

    for pattern, msg in UNSAFE:
        if re.search(pattern, q):
            return Plan("refuse", message=msg)

    if re.search(r"\b(tables?|schema|columns?|what data|which data|data (do you|you) have|what can you answer)\b", q):
        return Plan("answer", tool="describe_schema")

    if re.search(r"\bcredits?\b", q) or (re.search(r"\b(cost|how long|elapsed|expensive)\b", q) and re.search(r"\bquer(y|ies)\b", q)):
        return Plan("answer", tool="get_query_cost")

    if re.search(RANKING, q):
        return Plan("unsupported", message=(
            "These tools can't rank individual films, customers, or actors. They only break numbers down by "
            "category, rating, store, customer country, or time. The closest question they can answer is "
            "\"Which category earns the most revenue?\""))

    for pattern, what in NOT_IN_DATA:
        if re.search(pattern, q):
            return Plan("unsupported", message=f"These tools can't answer that: there is no data on {what}.")

    if re.search(VAGUE, q):
        return Plan("clarify", message=(
            "That question is too broad to answer with one number. Try asking for a specific measure, "
            "for example revenue by month, how many customers rented in July 2005, or which category earns the most."))

    notes = []
    period = _period(q)
    if period:
        start, end, label = period
        if re.search(r"\b(after|since)\b", q):
            end = max(end, DATA_END)
            label = f"{label} through the end of the data ({DATA_END})"
        notes.append(f"Period taken from your question: {label}.")
    else:
        start, end = default_start, default_end
        notes.append(f"No period in your question, so the sidebar dates were used ({start} to {end}).")

    category = _category(q)
    if category:
        notes.append(f"Category taken from your question: {category}.")
    elif default_category:
        category = default_category
        notes.append(f"Category from the sidebar: {category}.")

    store = _store(q)
    if store:
        notes.append(f"Store taken from your question: store {store}.")
    elif default_store:
        store = default_store
        notes.append(f"Store from the sidebar: store {store}.")

    asks_why = bool(re.search(r"\bwhy\b|\bcaus(e|ed)\b|\breason\b|\bexplain\b", q))

    if re.search(r"\b(show|list|see|examples?|sample|recent|latest|which rentals)\b", q) and not re.search(r"\b(how many|total|sum|count)\b", q) \
            and not _group_by(q) and not re.search(r"\b(revenue|sales|average|avg)\b", q):
        return Plan("answer", tool="run_query", notes=notes,
                    args={"start_date": start.isoformat(), "end_date": end.isoformat(),
                          "category": category, "store_id": store, "limit": 20})

    metric, people = _metric(q)
    group_by = _group_by(q)
    if re.search(r"\b(drop|decline|fall|fell|decrease|increase|grow|growth|rise|rose|change)\b", q) and not group_by:
        group_by = "month"
        notes.append("Your question is about change over time, so results are broken down by month.")
    if metric is None:
        if group_by:
            metric = "rental_count"
            notes.append("No measure named, so rentals were counted.")
        else:
            return Plan("unsupported", message=(
                "I couldn't match that to anything the tools can measure (revenue, rentals, customers, "
                "average payment, average rental length). Try rephrasing with one of those."))

    return Plan("answer", tool="get_aggregate", notes=notes, asks_people=people, asks_why=asks_why,
                args={"metric": metric, "start_date": start.isoformat(), "end_date": end.isoformat(),
                      "group_by": group_by, "category": category, "store_id": store})


def outside_coverage(args):
    """True when the plan's whole period falls outside the data."""
    s, e = date.fromisoformat(args["start_date"]), date.fromisoformat(args["end_date"])
    return e < DATA_START or s > DATA_END
