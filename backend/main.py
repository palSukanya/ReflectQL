"""
main.py — ReflectQL FastAPI backend.

Endpoints:
    POST /ask     -> run the self-correcting SQL agent for a question
    GET  /schema  -> return the current DB schema (for the frontend sidebar)
    GET  /health  -> simple liveness check
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent import run_agent
from database import DB_PATH, get_schema_dict, seed_database

load_dotenv()

app = FastAPI(title="ReflectQL", description="Self-Correcting SQL Agent")

# Allow the plain-HTML frontend (opened via file:// or a local static server)
# to call this API during local/demo use.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    sql_query: str
    attempts: int
    success: bool


@app.on_event("startup")
def ensure_database_exists() -> None:
    """If the DB file doesn't exist yet, seed it automatically so `/ask`
    doesn't fail on a completely fresh checkout that skipped `python database.py`."""
    if not os.path.exists(DB_PATH):
        print("No database found — seeding a fresh one on startup...")
        seed_database()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/schema")
def schema():
    """Return the DB schema as structured JSON for the frontend sidebar panel."""
    try:
        return get_schema_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read schema: {e}")


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest):
    """
    Run the LangGraph self-correcting agent for a natural-language question
    and return the final answer along with transparency info (SQL used,
    number of attempts, success flag).
    """
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")

    try:
        result = run_agent(request.question.strip())
    except RuntimeError as e:
        # e.g. missing GOOGLE_API_KEY
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent failed unexpectedly: {e}")

    return AskResponse(
        answer=result["answer"],
        sql_query=result["sql_query"],
        attempts=result["attempts"],
        success=result["success"],
    )
