from __future__ import annotations

import json
from pathlib import Path

from app.codex_service.grading import GradingResult, load_grading_result


VALID = {
    "summary": "整体不错",
    "knowledge_points": [{"name": "加法", "mastery": "good"}],
    "questions": [
        {
            "index": 1,
            "question": "1+1=?",
            "student_answer": "2",
            "correct": True,
            "correct_answer": "2",
            "error_reason": None,
            "explanation": "答对了",
        }
    ],
    "suggestions": ["继续保持"],
}


def test_grading_result_roundtrip():
    result = GradingResult.model_validate(VALID)
    assert result.questions[0].correct is True
    assert result.knowledge_points[0].mastery == "good"


def test_load_grading_result_valid(tmp_path: Path):
    (tmp_path / "grading_result.json").write_text(json.dumps(VALID), encoding="utf-8")
    result = load_grading_result(tmp_path)
    assert result is not None
    assert result.summary == "整体不错"


def test_load_grading_result_missing(tmp_path: Path):
    assert load_grading_result(tmp_path) is None


def test_load_grading_result_invalid_json(tmp_path: Path):
    (tmp_path / "grading_result.json").write_text("not json", encoding="utf-8")
    assert load_grading_result(tmp_path) is None


def test_load_grading_result_schema_violation(tmp_path: Path):
    bad = dict(VALID, questions=[{"index": "one"}])
    (tmp_path / "grading_result.json").write_text(json.dumps(bad), encoding="utf-8")
    assert load_grading_result(tmp_path) is None


def test_load_grading_result_bad_mastery(tmp_path: Path):
    bad = dict(VALID, knowledge_points=[{"name": "加法", "mastery": "excellent"}])
    (tmp_path / "grading_result.json").write_text(json.dumps(bad), encoding="utf-8")
    assert load_grading_result(tmp_path) is None
