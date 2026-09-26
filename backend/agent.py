"""
agent.py — ReflectQL

Defines the self-correcting text-to-SQL agent as a LangGraph state graph.

Flow:

    generate_sql --> execute_sql --(error)--> reflect_and_retry --> execute_sql
                          |     \
                          |      --(0 rows, looks wrong)--> reflect_and_retry (once)
                          |
                          --(success)--> format_answer --> END

    If retries are exhausted without success --> give_up --> END

Each node is a small, single-purpose function that takes the current
AgentState dict and returns a dict of fields to merge into it. This mirrors
how LangGraph nodes are meant to be written: pure functions over shared state.
"""

import os
import re
import sqlite3
from typing import List, Optional, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, END

from database import DB_PATH, get_schema_text

load_dotenv()

MAX_RETRIES = 3
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")


# ----------------------------------------------------------------------------
# State definition
# ----------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    question: str            # original natural-language question from the user
    schema: str               # full DB schema text, passed as context every attempt
    sql_query: str             # most recently generated SQL
    error: Optional[str]        # error message from the last execution attempt, if any
    columns: List[str]           # column names from the last successful execution
    rows: List[tuple]              # raw rows from the last successful execution
    attempts: int                   # number of execute_sql attempts made so far
    empty_retry_used: bool            # whether we've already retried once for "0 rows"
    history: List[dict]                # log of every attempt: {sql, error, attempt_number}
    success: bool                        # did we end with a usable result?
    answer: str                           # final natural-language answer for the user
    final_error: Optional[str]             # last error message, if we gave up


# ----------------------------------------------------------------------------
# LLM setup
# ----------------------------------------------------------------------------

def _get_llm(temperature: float = 0.0) -> ChatGoogleGenerativeAI:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Copy backend/.env.example to backend/.env "
            "and add your Gemini API key."
        )
    return ChatGoogleGenerativeAI(model=GEMINI_MODEL, temperature=temperature, google_api_key=api_key)


def _response_text(response) -> str:
    """
    Normalize an LLM response's `.content` into a plain string.

    Depending on the langchain-google-genai version and model, `.content`
    can come back as either a plain string OR a list of content blocks
    (e.g. [{"type": "text", "text": "..."}]). Without this, list responses
    crash downstream regex/string calls with
    "expected string or bytes-like object, got 'list'".
    """
    content = response.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content)


def _extract_sql(text: str) -> str:
    """Strip markdown code fences etc. so we're left with just the SQL statement."""
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    sql = match.group(1) if match else text
    sql = sql.strip().strip(";").strip()
    return sql


def _looks_like_should_have_data(question: str) -> bool:
    """
    Heuristic used by the conditional edge to decide whether an empty result
    set is suspicious enough to warrant one reflect-and-retry pass, vs. just
    being a legitimately empty (correct) answer.

    We deliberately keep this simple and transparent rather than making
    another LLM call just to judge "should this be empty" — it's cheap,
    fast, and good enough to demonstrate the self-correction loop.
    """
    q = question.lower()
    negative_hints = ["is there any", "are there any", "any tickets with no", "none"]
    if any(h in q for h in negative_hints):
        return False
    # Most analytical/aggregate/listing questions about this dataset should
    # return something, given the DB is seeded with ~100+ tickets.
    return True


# ----------------------------------------------------------------------------
# Nodes
# ----------------------------------------------------------------------------

def generate_sql(state: AgentState) -> dict:
    """
    Node 1: Ask the LLM to translate the natural-language question into a
    single SQLite SELECT statement, using the full DB schema as context.

    On retries (attempts > 0), reflect_and_retry has already put a corrected
    query into state["sql_query"], so this node is only responsible for the
    FIRST attempt. LangGraph routes straight past this node on retries.
    """
    print(f"[generate_sql] question={state['question']!r}")

    llm = _get_llm()
    system_prompt = (
        "You are an expert SQLite analyst. Given a database schema and a "
        "user's question, write ONE syntactically correct SQLite SELECT "
        "query that answers it. Only use tables/columns that exist in the "
        "schema below — read column names carefully, some names are not "
        "what you might expect (e.g. foreign keys are not always named "
        "'<table>_id'). Return ONLY the SQL query, no explanation, no "
        "markdown fences.\n\nSCHEMA:\n" + state["schema"]
    )
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=state["question"])]
    response = llm.invoke(messages)
    sql = _extract_sql(_response_text(response))

    print(f"[generate_sql] generated SQL: {sql}")

    return {
        "sql_query": sql,
        "attempts": 0,
        "empty_retry_used": False,
        "history": [],
        "error": None,
    }


def execute_sql(state: AgentState) -> dict:
    """
    Node 2: Run the current SQL against the local SQLite database.
    Captures execution errors instead of raising, so the graph's conditional
    edge can decide what to do next.
    """
    attempt_number = state["attempts"] + 1
    sql = state["sql_query"]
    print(f"[execute_sql] attempt #{attempt_number}: {sql}")

    history_entry = {"attempt_number": attempt_number, "sql": sql, "error": None}

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description] if cur.description else []
        conn.close()

        print(f"[execute_sql] success — {len(rows)} row(s) returned")
        return {
            "attempts": attempt_number,
            "columns": columns,
            "rows": rows,
            "error": None,
            "history": state.get("history", []) + [history_entry],
        }
    except sqlite3.Error as e:
        error_msg = str(e)
        print(f"[execute_sql] ERROR on attempt #{attempt_number}: {error_msg}")
        history_entry["error"] = error_msg
        return {
            "attempts": attempt_number,
            "error": error_msg,
            "rows": [],
            "columns": [],
            "history": state.get("history", []) + [history_entry],
        }


def reflect_and_retry(state: AgentState) -> dict:
    """
    Node 3: Self-correction step. Feeds the failing (or suspiciously empty)
    SQL query and the error/context back to the LLM, and asks it to produce
    a corrected query. This is the heart of the "self-correcting" behavior.
    """
    attempt_number = state["attempts"]
    if state.get("error"):
        reason = state["error"]
    elif _is_zero_aggregate(state.get("rows", [])):
        reason = (
            "the query ran without error but the aggregate result was 0 or NULL, "
            "which is suspicious for this question — it likely means the wrong "
            "column, table, or filter value was used (e.g. checking the wrong "
            "status column, or a value that doesn't exist in that column)"
        )
    else:
        reason = "the query returned 0 rows, which looks wrong for this question"
    print(f"[reflect_and_retry] fixing attempt #{attempt_number} because: {reason}")

    empty_retry_used = state.get("empty_retry_used", False)
    if not state.get("error"):
        empty_retry_used = True  # we only get one free pass for "0 rows"

    llm = _get_llm()
    system_prompt = (
        "You are an expert SQLite analyst fixing a broken query. You will be "
        "given the database schema, the original question, the SQL query "
        "that was tried, and what went wrong. Write ONE corrected SQLite "
        "SELECT query. Pay close attention to exact column and table names "
        "in the schema — do not assume conventional naming. Return ONLY the "
        "corrected SQL query, no explanation, no markdown fences.\n\n"
        "SCHEMA:\n" + state["schema"]
    )
    user_prompt = (
        f"Original question: {state['question']}\n\n"
        f"Query that was tried:\n{state['sql_query']}\n\n"
        f"What went wrong: {reason}\n\n"
        "Write the corrected SQL query."
    )
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    response = llm.invoke(messages)
    fixed_sql = _extract_sql(_response_text(response))

    print(f"[reflect_and_retry] corrected SQL: {fixed_sql}")

    return {
        "sql_query": fixed_sql,
        "empty_retry_used": empty_retry_used,
    }


def format_answer(state: AgentState) -> dict:
    """
    Node 4: Convert the raw SQL result set into a friendly natural-language
    answer for the end user.
    """
    print(f"[format_answer] formatting {len(state.get('rows', []))} row(s) into an answer")

    llm = _get_llm(temperature=0.3)
    preview = state["rows"][:25]  # keep the prompt small even for big result sets
    system_prompt = (
        "You are a helpful support-ticket-system analyst. Given the user's "
        "original question, the SQL query that was run, and the resulting "
        "rows, write a concise, natural-language answer. If the result set "
        "is empty, say so plainly. Do not mention SQL syntax or column "
        "internals unless directly relevant to the answer."
    )
    user_prompt = (
        f"Question: {state['question']}\n"
        f"SQL used: {state['sql_query']}\n"
        f"Columns: {state.get('columns', [])}\n"
        f"Rows (up to 25 shown): {preview}\n"
        f"Total row count: {len(state.get('rows', []))}\n\n"
        "Write the answer for the user now."
    )
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    response = llm.invoke(messages)

    return {"answer": _response_text(response).strip(), "success": True}


def give_up(state: AgentState) -> dict:
    """
    Node 5: Reached when retries are exhausted without a working query.
    Returns a graceful message instead of crashing or hanging.
    """
    print(f"[give_up] exhausted {state['attempts']} attempt(s) without success")
    return {
        "success": False,
        "answer": (
            "I couldn't find a working query for that question after "
            f"{state['attempts']} attempt(s). The last error was: "
            f"{state.get('error') or 'the result still looked incorrect.'}"
        ),
        "final_error": state.get("error"),
    }


# ----------------------------------------------------------------------------
# Conditional routing
# ----------------------------------------------------------------------------

def _is_zero_aggregate(rows: list) -> bool:
    """
    Detect the "silent zero" case: a query like SELECT COUNT(*) ... always
    returns exactly one row, even when the count is 0 or the SUM/AVG is
    NULL. That single row makes execute_sql look like a success even though
    the underlying answer is (probably) wrong — e.g. querying the wrong
    column so nothing matches. This catches that shape so it gets routed
    through reflect_and_retry instead of being treated as a real result.
    """
    if len(rows) != 1:
        return False
    row = rows[0]
    if len(row) != 1:
        return False
    value = row[0]
    return value is None or value == 0


def route_after_execute(state: AgentState) -> str:
    """
    Decides where to go after execute_sql:
      - real SQL error, retries remaining        -> reflect_and_retry
      - real SQL error, retries exhausted         -> give_up
      - 0 rows, question implies data, 1st time    -> reflect_and_retry
      - a single-row aggregate of 0/NULL, 1st time  -> reflect_and_retry
      - otherwise                                    -> format_answer
    """
    if state.get("error"):
        if state["attempts"] < MAX_RETRIES:
            return "reflect_and_retry"
        return "give_up"

    rows = state.get("rows", [])
    looks_empty = not rows
    looks_zero_aggregate = _is_zero_aggregate(rows)

    if (looks_empty or looks_zero_aggregate) and not state.get("empty_retry_used", False):
        if _looks_like_should_have_data(state["question"]):
            if state["attempts"] < MAX_RETRIES:
                return "reflect_and_retry"
            return "give_up"

    return "format_answer"


# ----------------------------------------------------------------------------
# Graph construction
# ----------------------------------------------------------------------------

def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("generate_sql", generate_sql)
    graph.add_node("execute_sql", execute_sql)
    graph.add_node("reflect_and_retry", reflect_and_retry)
    graph.add_node("format_answer", format_answer)
    graph.add_node("give_up", give_up)

    graph.set_entry_point("generate_sql")
    graph.add_edge("generate_sql", "execute_sql")

    graph.add_conditional_edges(
        "execute_sql",
        route_after_execute,
        {
            "reflect_and_retry": "reflect_and_retry",
            "give_up": "give_up",
            "format_answer": "format_answer",
        },
    )

    graph.add_edge("reflect_and_retry", "execute_sql")
    graph.add_edge("format_answer", END)
    graph.add_edge("give_up", END)

    return graph.compile()


_compiled_graph = None


def get_compiled_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


def run_agent(question: str) -> dict:
    """
    Entry point used by the FastAPI backend. Runs the full graph for a
    single question and returns the fields the API needs.
    """
    app = get_compiled_graph()
    initial_state: AgentState = {
        "question": question,
        "schema": get_schema_text(),
    }
    final_state = app.invoke(initial_state)

    return {
        "answer": final_state.get("answer", "Something went wrong."),
        "sql_query": final_state.get("sql_query", ""),
        "attempts": final_state.get("attempts", 0),
        "success": final_state.get("success", False),
        "history": final_state.get("history", []),
    }