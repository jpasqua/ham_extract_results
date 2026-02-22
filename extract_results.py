#!/usr/bin/env python3
"""Parse ham radio exam-result PDFs/text into structured exam analytics.

Overview
--------
This script converts one or more exam result documents into machine-readable
JSON and optional CSV files. It is designed for exam result sheets that contain:

1) Header metadata (candidate identity, pass/fail outcome, element/test info).
2) One or two columns of question rows such as:
      17. T5B04: D (should be A)
3) Footer metadata (start/grade timestamps and examiners).

Input shapes supported
----------------------
- A single exam in one file.
- Multiple concatenated exams in one file (same candidate, multiple attempts).
- Multiple input files in one run, with one combined aggregate output.

Parsing strategy
----------------
1) Text extraction:
   - PDFs are converted to plain text via Ghostscript `txtwrite`.
   - Plain text inputs are read directly.
2) Exam splitting:
   - If repeated "(PIN: ...)" headers appear, the text is split into exam blocks.
3) Question parsing:
   - Regex extracts question number, question id, selected answer, and optional
     "(should be X)" correction.
   - Rows are sorted by question number to normalize two-column extraction order.
4) Validation summary:
   - Computes totals and per-exam integrity checks (missing/duplicate/unexpected
     numbers), using reported exam total when available (works for 35 or 50).
5) Aggregation:
   - For multiple inputs, emits a single combined structure with cross-file
     statistics (per question, per section, per subsection).

Output model
------------
- Single input file:
  - Returns that file's parsed structure (single exam, or `exams` for
    concatenated files).
- Multiple input files:
  - Returns one aggregate object with:
    - `sources`
    - overall `summary`
    - `question_stats`  (e.g. T3A04)
    - `section_stats`   (e.g. T3)
    - `subsection_stats`(e.g. T3A)
    - raw per-file `results`

CSV outputs
-----------
- `--csv`: flattened row-level question records for all parsed exams.
- `--stats-csv`: per-question aggregate stats.
- `--section-stats-csv`: per-section aggregate stats.
- `--subsection-stats-csv`: per-subsection aggregate stats.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Optional

QUESTION_RE = re.compile(
    r"(\d{1,2})\.\s*([A-Z]\d[A-Z]\d{2}):\s*([A-D])\s*(?:\(\s*should be\s*([A-D])\s*\))?"
)
HEADER_RE = re.compile(r"^\s*.+?\s+\(PIN:\s*\d+\)\s*$", re.MULTILINE)
QUESTION_ID_PARTS_RE = re.compile(r"^([A-Z])(\d)([A-Z])\d{2}$")


@dataclass
class QuestionResult:
    number: int
    question_id: str
    selected: str
    correct: str
    is_correct: bool


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract plain text from a PDF using Ghostscript's `txtwrite` device."""
    cmd = ["gs", "-q", "-sDEVICE=txtwrite", "-o", "-", pdf_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("Ghostscript (gs) is required but was not found in PATH.") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(f"Ghostscript failed to extract text: {stderr}")

    return proc.stdout


def parse_questions(text: str) -> list[QuestionResult]:
    """Parse all question-result rows from one exam block of text.

    Each parsed row includes:
    - question number (1-based index within the exam),
    - question id (e.g. T5B04),
    - selected answer,
    - correct answer (from "(should be X)" when present, else selected),
    - correctness flag.
    """
    results: list[QuestionResult] = []
    for m in QUESTION_RE.finditer(text):
        number = int(m.group(1))
        question_id = m.group(2)
        selected = m.group(3)
        should_be = m.group(4)
        correct = should_be if should_be else selected
        results.append(
            QuestionResult(
                number=number,
                question_id=question_id,
                selected=selected,
                correct=correct,
                is_correct=(selected == correct),
            )
        )

    results.sort(key=lambda item: item.number)
    return results


def parse_metadata(text: str) -> dict[str, object]:
    """Extract exam/candidate metadata from one exam block.

    This uses targeted regex searches for known report phrases. Missing fields
    are omitted from the returned dictionary.
    """
    metadata: dict[str, object] = {}

    m = re.search(r"^\s*(.+?)\s+\(PIN:\s*(\d+)\)\s*$", text, re.MULTILINE)
    if m:
        metadata["candidate_name"] = m.group(1).strip()
        metadata["pin"] = m.group(2)

    m = re.search(r"\b(FAIL|PASS)\b", text)
    if m:
        metadata["outcome"] = m.group(1)

    m = re.search(r"Test\s+(?:Failed|Passed)\s+-\s+(\d+)\s+out of\s+(\d+)", text)
    if m:
        metadata["reported_correct"] = int(m.group(1))
        metadata["reported_total"] = int(m.group(2))

    m = re.search(r"Element\s+(\d+)", text)
    if m:
        metadata["element"] = int(m.group(1))

    m = re.search(r"Test\s+#(\d+)", text)
    if m:
        metadata["test_number"] = m.group(1)

    m = re.search(r"valid\s+(.+?)\s+—\s+(.+?)\n", text)
    if m:
        metadata["valid_from"] = m.group(1).strip()
        metadata["valid_to"] = m.group(2).strip()

    m = re.search(r"Exam started at\s+(.+?)\s+by\s+(\S+)", text)
    if m:
        metadata["exam_started_at"] = m.group(1).strip()
        metadata["exam_started_by"] = m.group(2)

    m = re.search(r"Exam graded at\s+(.+?)\s+by\s+(\S+)", text)
    if m:
        metadata["exam_graded_at"] = m.group(1).strip()
        metadata["exam_graded_by"] = m.group(2)

    return metadata


def split_exam_blocks(text: str) -> list[str]:
    """Split extracted text into per-exam blocks.

    Concatenated result PDFs repeat the candidate header line containing
    "(PIN: NNNN)". Each occurrence is treated as the start of a new exam block.
    """
    starts = [m.start() for m in HEADER_RE.finditer(text)]
    if not starts:
        return [text]

    blocks: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)
    return blocks or [text]


def build_exam_output(text: str, source_path: str) -> dict[str, object]:
    """Build normalized output for a single exam block."""
    questions = parse_questions(text)
    metadata = parse_metadata(text)

    total = len(questions)
    correct = sum(1 for q in questions if q.is_correct)
    incorrect = total - correct

    numbers = [q.number for q in questions]
    missing = []
    duplicates = []
    unexpected_numbers = []
    if numbers:
        seen: set[int] = set()
        for n in numbers:
            if n in seen and n not in duplicates:
                duplicates.append(n)
            seen.add(n)

        # Exam question numbering is 1-based. Prefer the reported total from
        # metadata so 35- and 50-question exams are validated consistently.
        reported_total = metadata.get("reported_total")
        expected_total = reported_total if isinstance(reported_total, int) else max(numbers)

        for n in range(1, expected_total + 1):
            if n not in seen:
                missing.append(n)
        for n in sorted(seen):
            if n < 1 or n > expected_total:
                unexpected_numbers.append(n)

    return {
        "source": os.path.abspath(source_path),
        "metadata": metadata,
        "summary": {
            "total_questions_parsed": total,
            "correct": correct,
            "incorrect": incorrect,
            "missing_numbers": missing,
            "duplicate_numbers": duplicates,
            "unexpected_numbers": unexpected_numbers,
        },
        "questions": [asdict(q) for q in questions],
    }


def build_output(text: str, source_path: str) -> dict[str, object]:
    """Build output for one source file (single exam or concatenated exams)."""
    blocks = split_exam_blocks(text)
    if len(blocks) == 1:
        return build_exam_output(blocks[0], source_path)

    exams = [build_exam_output(block, source_path) for block in blocks]
    total_questions = sum(
        exam["summary"]["total_questions_parsed"]  # type: ignore[index]
        for exam in exams
    )
    total_correct = sum(exam["summary"]["correct"] for exam in exams)  # type: ignore[index]
    total_incorrect = sum(exam["summary"]["incorrect"] for exam in exams)  # type: ignore[index]

    first_meta = exams[0]["metadata"] if exams else {}
    top_metadata: dict[str, object] = {}
    if isinstance(first_meta, dict):
        if "candidate_name" in first_meta:
            top_metadata["candidate_name"] = first_meta["candidate_name"]
        if "pin" in first_meta:
            top_metadata["pin"] = first_meta["pin"]

    return {
        "source": os.path.abspath(source_path),
        "metadata": top_metadata,
        "summary": {
            "total_exams_parsed": len(exams),
            "total_questions_parsed": total_questions,
            "correct": total_correct,
            "incorrect": total_incorrect,
        },
        "exams": exams,
    }


def write_csv(path: str, rows: list[dict[str, object]]) -> None:
    """Write rows to CSV using keys from the first row as the header schema."""
    if not rows:
        return

    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_question_id_parts(question_id: str) -> dict[str, str]:
    """Split question id into pool/section/subsection parts.

    Example:
      T3A04 -> {"pool": "T", "section": "3", "subsection": "A"}
    """
    m = QUESTION_ID_PARTS_RE.match(question_id)
    if not m:
        return {"pool": "", "section": "", "subsection": ""}
    return {"pool": m.group(1), "section": m.group(2), "subsection": m.group(3)}


def compute_group_stats(
    rows: list[dict[str, object]], key_name: str, make_group_key
) -> list[dict[str, object]]:
    """Compute aggregate correctness stats for an arbitrary grouping key."""
    stats_by_key: dict[str, dict[str, object]] = {}
    for row in rows:
        question_id = str(row.get("question_id", ""))
        if not question_id:
            continue
        parts = parse_question_id_parts(question_id)
        group_key = make_group_key(parts)
        if not group_key:
            continue

        is_correct = bool(row.get("is_correct", False))
        stats = stats_by_key.setdefault(group_key, {key_name: group_key, "attempts": 0, "correct": 0, "incorrect": 0})
        stats["attempts"] = int(stats["attempts"]) + 1
        if is_correct:
            stats["correct"] = int(stats["correct"]) + 1
        else:
            stats["incorrect"] = int(stats["incorrect"]) + 1

    stats_rows: list[dict[str, object]] = []
    for stats in stats_by_key.values():
        attempts = int(stats["attempts"])
        correct = int(stats["correct"])
        accuracy = (correct / attempts) if attempts else 0.0
        stats_rows.append({**stats, "accuracy": round(accuracy, 4)})

    stats_rows.sort(key=lambda r: (r["accuracy"], -int(r["attempts"]), r[key_name]))  # type: ignore[index]
    return stats_rows


def compute_question_stats(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Compute aggregate stats per question id (e.g. T3A04)."""
    question_stats: dict[str, dict[str, object]] = {}
    for row in rows:
        question_id = str(row.get("question_id", ""))
        if not question_id:
            continue
        is_correct = bool(row.get("is_correct", False))
        stats = question_stats.setdefault(
            question_id, {"question_id": question_id, "attempts": 0, "correct": 0, "incorrect": 0}
        )
        stats["attempts"] = int(stats["attempts"]) + 1
        if is_correct:
            stats["correct"] = int(stats["correct"]) + 1
        else:
            stats["incorrect"] = int(stats["incorrect"]) + 1

    question_stats_rows: list[dict[str, object]] = []
    for stats in question_stats.values():
        attempts = int(stats["attempts"])
        correct = int(stats["correct"])
        accuracy = (correct / attempts) if attempts else 0.0
        question_stats_rows.append({**stats, "accuracy": round(accuracy, 4)})

    question_stats_rows.sort(key=lambda r: (r["accuracy"], -int(r["attempts"]), r["question_id"]))  # type: ignore[index]
    return question_stats_rows


def compute_section_stats(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Compute aggregate stats per section id (e.g. T3, G2, E1)."""
    return compute_group_stats(rows, "section_id", lambda p: f"{p['pool']}{p['section']}" if p["pool"] and p["section"] else "")


def compute_subsection_stats(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Compute aggregate stats per subsection id (e.g. T3A, G2E, E1C)."""
    return compute_group_stats(
        rows,
        "subsection_id",
        lambda p: f"{p['pool']}{p['section']}{p['subsection']}" if p["pool"] and p["section"] and p["subsection"] else "",
    )


def extract_exam_list(data: dict[str, object]) -> list[dict[str, object]]:
    """Normalize single-exam and multi-exam outputs to a list of exam objects."""
    if "exams" in data:
        exams = data.get("exams", [])
        return exams if isinstance(exams, list) else []
    return [data]


def flatten_questions_for_csv(data: dict[str, object]) -> list[dict[str, object]]:
    """Flatten parsed output to question-level rows suitable for CSV export."""
    rows: list[dict[str, object]] = []
    source = str(data.get("source", ""))
    exams = extract_exam_list(data)

    for i, exam in enumerate(exams, start=1):
        if not isinstance(exam, dict):
            continue
        exam_metadata = exam.get("metadata", {})
        test_number = ""
        element: object = ""
        if isinstance(exam_metadata, dict):
            test_number = str(exam_metadata.get("test_number", ""))
            element = exam_metadata.get("element", "")

        questions = exam.get("questions", [])
        if not isinstance(questions, list):
            continue

        for q in questions:
            if not isinstance(q, dict):
                continue
            rows.append(
                {
                    "source": source,
                    "exam_index_in_source": i,
                    "test_number": test_number,
                    "element": element,
                    **q,
                }
            )
    return rows


def build_multi_file_output(outputs: list[dict[str, object]]) -> dict[str, object]:
    """Build a unified aggregate object across multiple input files."""
    all_rows: list[dict[str, object]] = []
    all_exams = 0
    all_questions = 0
    all_correct = 0
    all_incorrect = 0

    for out in outputs:
        exams = extract_exam_list(out)
        all_exams += len(exams)
        all_rows.extend(flatten_questions_for_csv(out))

        summary = out.get("summary", {})
        if isinstance(summary, dict):
            all_questions += int(summary.get("total_questions_parsed", 0))
            all_correct += int(summary.get("correct", 0))
            all_incorrect += int(summary.get("incorrect", 0))

    question_stats_rows = compute_question_stats(all_rows)
    section_stats_rows = compute_section_stats(all_rows)
    subsection_stats_rows = compute_subsection_stats(all_rows)

    return {
        "sources": [str(out.get("source", "")) for out in outputs],
        "summary": {
            "total_input_files": len(outputs),
            "total_exams_parsed": all_exams,
            "total_questions_parsed": all_questions,
            "correct": all_correct,
            "incorrect": all_incorrect,
        },
        "question_stats": question_stats_rows,
        "section_stats": section_stats_rows,
        "subsection_stats": subsection_stats_rows,
        "results": outputs,
    }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments for input files and output options."""
    parser = argparse.ArgumentParser(
        description="Parse exam result PDFs/text into structured question results"
    )
    parser.add_argument("input", nargs="+", help="Path(s) to input file(s) (.pdf or plain text)")
    parser.add_argument(
        "--input-type",
        choices=["auto", "pdf", "text"],
        default="auto",
        help="Input format (default: auto by extension)",
    )
    parser.add_argument(
        "--out",
        help="Path to write JSON output. If omitted, JSON is printed to stdout.",
    )
    parser.add_argument(
        "--csv",
        help="Optional path to write CSV of parsed question rows.",
    )
    parser.add_argument(
        "--stats-csv",
        help="Optional path to write CSV of per-question aggregate stats.",
    )
    parser.add_argument(
        "--section-stats-csv",
        help="Optional path to write CSV of per-section aggregate stats (e.g. T3).",
    )
    parser.add_argument(
        "--subsection-stats-csv",
        help="Optional path to write CSV of per-subsection aggregate stats (e.g. T3A).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)

    for path in args.input:
        if not os.path.exists(path):
            print(f"ERROR: Input not found: {path}", file=sys.stderr)
            return 2

    try:
        outputs: list[dict[str, object]] = []
        all_csv_rows: list[dict[str, object]] = []
        for input_path in args.input:
            input_type = args.input_type
            if input_type == "auto":
                input_type = "pdf" if input_path.lower().endswith(".pdf") else "text"

            if input_type == "pdf":
                text = extract_text_from_pdf(input_path)
            else:
                with open(input_path, "r", encoding="utf-8") as f:
                    text = f.read()

            file_output = build_output(text, input_path)
            outputs.append(file_output)
            all_csv_rows.extend(flatten_questions_for_csv(file_output))

        if len(outputs) == 1:
            data = outputs[0]
        else:
            data = build_multi_file_output(outputs)

        if args.csv:
            write_csv(args.csv, all_csv_rows)
        if args.stats_csv:
            write_csv(args.stats_csv, compute_question_stats(all_csv_rows))
        if args.section_stats_csv:
            write_csv(args.section_stats_csv, compute_section_stats(all_csv_rows))
        if args.subsection_stats_csv:
            write_csv(args.subsection_stats_csv, compute_subsection_stats(all_csv_rows))

        json_text = json.dumps(data, indent=2 if args.pretty else None)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(json_text)
                f.write("\n")
        else:
            print(json_text)

    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
