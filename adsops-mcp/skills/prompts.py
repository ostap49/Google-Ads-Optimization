"""Playbooks in skills/*.md exposed as MCP prompts (and as tools for clients without prompt support)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tools._registry import tool

SKILLS_DIR = Path(__file__).resolve().parent
LANGS = {"uk": "Ukrainian (українська)", "en": "English"}


def _parse(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        return yaml.safe_load(fm) or {}, body.strip()
    return {}, text


def playbooks() -> dict[str, dict[str, Any]]:
    out = {}
    for p in sorted(SKILLS_DIR.glob("*.md")):
        if p.name.startswith("_"):
            continue
        meta, body = _parse(p)
        out[p.stem] = {"title": meta.get("title", p.stem), "description": meta.get("description", ""), "body": body}
    return out


def render(name: str, customer_id: str, lang: str = "uk", context: str = "") -> str:
    books = playbooks()
    if name not in books:
        raise ValueError(f"Unknown playbook '{name}'. Available: {sorted(books)}")
    lang = lang if lang in LANGS else "uk"
    _, common = _parse(SKILLS_DIR / "_common.md")
    values = {"customer_id": customer_id, "lang": lang, "lang_name": LANGS[lang], "context": context or "(none)"}
    text = books[name]["body"] + "\n\n" + common
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return f"Run the adsops playbook '{books[name]['title']}' for account {customer_id}. Final answer in {LANGS[lang]}.\n\n{text}"


def _make_prompt(name: str):
    def prompt(customer_id: str, lang: str = "uk", context: str = "") -> str:
        return render(name, customer_id, lang, context)
    prompt.__name__ = name
    return prompt


def register_prompts(mcp: Any) -> None:
    for name, meta in playbooks().items():
        mcp.prompt(name=name, title=meta["title"],
                   description=f"{meta['description']} Args: customer_id (ID or alias), lang (uk|en), context (free text).")(_make_prompt(name))


@tool(title="List playbooks")
def list_playbooks() -> dict[str, Any]:
    """Available playbooks (also exposed as MCP prompts / slash commands)."""
    return {"playbooks": [{"name": k, "title": v["title"], "description": v["description"]} for k, v in playbooks().items()]}


@tool(title="Get playbook")
def get_playbook(name: str, customer_id: str, lang: str = "uk", context: str = "") -> dict[str, Any]:
    """Return a playbook's full instructions (tools to call, order, thresholds, output format) for clients that do
    not support MCP prompts. Then follow it step by step."""
    return {"name": name, "instructions": render(name, customer_id, lang, context)}
