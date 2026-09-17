"""Pydantic schemas for the result contracts codex writes into the workspace
(grading_result.json / english_result.json)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

RESULT_FILENAME = "grading_result.json"
ENGLISH_RESULT_FILENAME = "english_result.json"


class KnowledgePoint(BaseModel):
    name: str
    mastery: Literal["good", "weak", "poor"]


class GradedQuestion(BaseModel):
    index: int
    question: str
    student_answer: str
    correct: bool
    correct_answer: str
    error_reason: str | None = None
    explanation: str


class GradingResult(BaseModel):
    summary: str
    knowledge_points: list[KnowledgePoint]
    questions: list[GradedQuestion]
    suggestions: list[str]


def load_grading_result(workspace: Path) -> GradingResult | None:
    """Read and validate grading_result.json from a workspace; None if absent/invalid."""
    path = workspace / RESULT_FILENAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("grading result unreadable in %s: %s", workspace, exc)
        return None
    try:
        return GradingResult.model_validate(raw)
    except ValidationError as exc:
        logger.warning("grading result failed validation in %s: %s", workspace, exc)
        return None


class EnglishAnswer(BaseModel):
    blank: int
    answer: str
    word: str
    meaning: str


class AnnotatedSentence(BaseModel):
    text: str
    words: list[tuple[str, str]]


class WordCard(BaseModel):
    word: str
    meaning: str


class EnglishPassageResult(BaseModel):
    title: str = ""
    answers: list[EnglishAnswer] = []
    sentences: list[AnnotatedSentence] = []
    word_cards: list[WordCard] = []
    notes: list[str] = []


def load_english_result(workspace: Path) -> EnglishPassageResult | None:
    """Read and validate english_result.json from a workspace; None if absent/invalid."""
    path = workspace / ENGLISH_RESULT_FILENAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("english result unreadable in %s: %s", workspace, exc)
        return None
    try:
        return EnglishPassageResult.model_validate(raw)
    except ValidationError as exc:
        logger.warning("english result failed validation in %s: %s", workspace, exc)
        return None
