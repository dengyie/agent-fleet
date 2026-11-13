"""Deterministic, bounded question answer validation; no I/O or clock."""
from collections.abc import Mapping
import re

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:.-]{0,127}\Z")


def validate_answer_ids(data: Mapping) -> tuple[str, str]:
    if not isinstance(data, Mapping) or set(data) - {"request_id", "client_token", "selected", "text"}:
        raise ValueError("invalid_interaction_answer")
    values = (data.get("request_id"), data.get("client_token"))
    if any(not isinstance(value, str) or not _OPAQUE.fullmatch(value) for value in values):
        raise ValueError("invalid_interaction_answer")
    return values


def normalize_answer(question: Mapping, data: Mapping) -> tuple[dict, str]:
    """O(options + selections); canonical order is the declared option order."""
    selected, text = data.get("selected", []), data.get("text", "")
    if isinstance(selected, str):
        selected = [selected]
    if (not isinstance(selected, list) or len(selected) > 32
            or any(not isinstance(item, str) for item in selected)
            or not isinstance(text, str) or len(text) > 2000):
        raise ValueError("invalid_interaction_answer")
    chosen = set(selected)
    options = {option["id"]: option["label"] for option in question["options"]}
    if len(chosen) != len(selected) or not chosen.issubset(options):
        raise ValueError("invalid_interaction_answer")
    kind = question["question_type"]
    if (kind in ("single", "confirm") and len(chosen) > 1
            or kind == "text" and chosen
            or text.strip() and kind != "text" and not question["allow_custom"]
            or not chosen and not text.strip()):
        raise ValueError("invalid_interaction_answer")
    ordered = [item for item in options if item in chosen]
    answer = {"request_id": question["request_id"], "selected": ordered, "text": text.strip()}
    message = "\n".join(["Answer to " + question["request_id"],
                         *(options[item] for item in ordered), answer["text"]]).strip()
    if len(message) > 2000:
        raise ValueError("invalid_interaction_answer")
    return answer, message
