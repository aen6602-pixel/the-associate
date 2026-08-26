"""Skill 로더 — 밸류에이션 절차서를 **필요할 때만** 두뇌에 넣는다.

`skills/<이름>/SKILL.md` 하나가 skill 하나다. 앞쪽 YAML frontmatter 에서 name·description 을
읽고, 본문은 절차서다. 긴 세부 규칙은 `skills/<이름>/references/*.md` 로 나눠 두고 SKILL.md 가
필요한 것만 가리킨다.

**왜 상주시키지 않는가**: 지금 등록된 절차서만 25KB(≈7K 토큰)다. 전부 시스템 프롬프트에 넣으면
매 요청이 무거워지고, 절차 문장이 많아질수록 tool-calling 정확도가 떨어진다. 그래서 시스템
프롬프트에는 **이름·설명 목록만**(수백 토큰) 넣고, 두뇌가 필요하다고 판단하면 `load_skill`
→ (필요 시) `read_skill_reference` 로 단계적으로 가져간다.

새 skill 추가 = `skills/` 밑에 폴더 하나 만들고 SKILL.md 를 두는 것. 코드 수정은 없다.
"""
from __future__ import annotations

import re

from .paths import ROOT
from .schema import DataError

SKILLS_DIR = ROOT / "skills"

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_NAME_RE = re.compile(r"^\s*(\w+)\s*:\s*(.*?)\s*$")
_SAFE_NAME = re.compile(r"[\w.\-]+")


def _parse(text: str) -> tuple[dict, str]:
    """(frontmatter dict, 본문). frontmatter 가 없으면 ({}, 전체)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        kv = _NAME_RE.match(line)
        if kv:
            meta[kv.group(1).lower()] = kv.group(2).strip().strip('"\'')
    return meta, text[m.end():]


def _safe(part: str) -> str:
    """경로 조작 차단 — skills 디렉터리 밖으로 못 나가게 한다."""
    part = (part or "").strip()
    return part if _SAFE_NAME.fullmatch(part) else ""


def available() -> list[dict]:
    """등록된 skill 목록 [{name, description, references}]. 없으면 빈 목록."""
    out = []
    if not SKILLS_DIR.is_dir():
        return out
    for d in sorted(SKILLS_DIR.iterdir()):
        f = d / "SKILL.md"
        if not (d.is_dir() and f.is_file()):
            continue
        try:
            meta, _ = _parse(f.read_text(encoding="utf-8"))
        except OSError:
            continue
        refs = sorted(p.name for p in (d / "references").glob("*.md")) \
            if (d / "references").is_dir() else []
        out.append({
            "name": meta.get("name") or d.name,
            "dir": d.name,
            "description": meta.get("description") or "",
            "references": refs,
        })
    return out


def roster_text() -> str:
    """시스템 프롬프트에 넣을 짧은 목록. skill 이 없으면 빈 문자열."""
    items = available()
    if not items:
        return ""
    lines = [f"- `{s['name']}`: {s['description']}" for s in items]
    return "\n".join(lines)


def _find(name: str) -> dict:
    key = (name or "").strip().lower()
    for s in available():
        if key in (s["name"].lower(), s["dir"].lower()):
            return s
    known = ", ".join(s["name"] for s in available()) or "(없음)"
    raise DataError(f"'{name}' skill 을 찾지 못했습니다. 사용 가능: {known}")


def load(name: str) -> dict:
    """SKILL.md 본문 + 참조파일 목록."""
    s = _find(name)
    body = _parse((SKILLS_DIR / s["dir"] / "SKILL.md").read_text(encoding="utf-8"))[1]
    return {"name": s["name"], "description": s["description"],
            "references": s["references"], "body": body.strip()}


def reference(name: str, filename: str) -> dict:
    """skill 의 references/<filename> 내용."""
    s = _find(name)
    fname = filename if filename.endswith(".md") else f"{filename}.md"
    stem = _safe(fname[:-3])
    if not stem:
        raise DataError(f"참조 파일명이 올바르지 않습니다: {filename!r}")
    path = SKILLS_DIR / s["dir"] / "references" / f"{stem}.md"
    if not path.is_file():
        avail = ", ".join(s["references"]) or "(없음)"
        raise DataError(f"'{s['name']}' 에 '{stem}.md' 가 없습니다. 사용 가능: {avail}")
    return {"name": s["name"], "file": f"{stem}.md",
            "body": path.read_text(encoding="utf-8").strip()}
