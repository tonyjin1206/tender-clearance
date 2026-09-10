#!/usr/bin/env python3
"""采购条款映射草案提取：从 procurement/ 采购文件抓取含资格/否决关键词的候选条款，
生成待人工确认的映射 YAML（`采购条款映射草案.yaml`）。

- 只是草案：所有条款 confirmed=false、rule_mapping 为空，必须由人工核对后确认；
- 确认方式：把确认的条款 confirmed 改为 true，并在 rule_mapping 填
  规则ID -> [条款ID]；然后在 project.yaml 设
  procurement_clauses_mapping: 采购条款映射草案.yaml，重跑 assess_risk.py；
- 纯本地文本检索，不做任何网络访问。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer
import yaml

from tc import parsers
from tc.canon import write_json
from tc.projio import ensure_output_dirs

# 关键词 → 建议规则（草案建议，人工确认后生效）
KEYWORD_RULES = {
    "失信": ["DIS-003", "DIS-001"],
    "严重违法": ["DIS-003"],
    "政府采购法": ["DIS-003"],
    "禁止参加": ["DIS-003", "DIS-001"],
    "军队": ["DIS-002"],
    "供应商负面": ["DIS-002"],
    "行贿": ["JUD-001"],
    "犯罪": ["JUD-001"],
    "诉讼": ["JUD-001"],
    "串通": ["ID-001", "META-002"],
    "围标": ["ID-001", "META-002"],
    "虚假材料": ["ID-001"],
    "同一单位": ["ID-001", "OWN-001"],
    "关联": ["OWN-001"],
}
CLAUSE_KEYWORDS = re.compile(
    r"(资格|否决|拒绝|不予|不得|禁止|失信|严重违法|惩戒|处罚|行贿|串通|围标|虚假|信用)"
)

app = typer.Typer(add_completion=False)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。；;！!])|\n", text)
    return [p.strip() for p in parts if p and p.strip()]


def _collect_docs(project_dir: Path):
    """从 inventory 找 category=procurement 的已提取文档；找不到则扫 procurement/ 目录。"""
    docs: list[Path] = []
    inv_path = project_dir / "output/interim/inventory.json"
    if inv_path.exists():
        import json

        inv = json.loads(inv_path.read_text(encoding="utf-8"))
        for doc in inv.get("documents", []):
            if doc.get("category") == "procurement" and doc.get("extraction_status") == "ok":
                docs.append(project_dir / doc["relative_path"])
    pdir = project_dir / "procurement"
    if pdir.exists():
        known = {str(x.resolve()) for x in docs}
        for x in sorted(pdir.rglob("*")):
            if x.suffix.lower() in (".pdf", ".docx") and x.is_file() \
                    and str(x.resolve()) not in known:
                docs.append(x)
    return docs


@app.command()
def main(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
    max_per_file: int = typer.Option(20, help="每个文件最多抽取的候选条款数"),
) -> None:
    ensure_output_dirs(project_dir)
    docs = _collect_docs(project_dir)
    if not docs:
        typer.secho("[提示] 未找到 procurement/ 采购文件（或 inventory 中无 procurement 文档）",
                    fg="yellow", err=True)
        raise typer.Exit(code=2)

    clauses: list[dict] = []
    seen: set[str] = set()
    for doc in docs:
        chunks: list[tuple[str, dict]] = []
        if doc.suffix.lower() == ".pdf":
            try:
                parsed = parsers.parse_pdf(doc, ocr_provider="none")
            except Exception as exc:  # noqa: BLE001
                typer.secho(f"[跳过] {doc.name}: {type(exc).__name__}", fg="yellow", err=True)
                continue
            for page in parsed.pages:
                for sent in _sentences(page.text or ""):
                    chunks.append((sent, {"kind": "pdf_page", "page": page.page}))
        elif doc.suffix.lower() == ".docx":
            try:
                parsed = parsers.parse_docx(doc)
            except Exception as exc:  # noqa: BLE001
                typer.secho(f"[跳过] {doc.name}: {type(exc).__name__}", fg="yellow", err=True)
                continue
            for i, para in enumerate(parsed.paragraphs):
                for sent in _sentences(para):
                    chunks.append((sent, {"kind": "docx_body", "index": i}))

        count = 0
        for sent, location in chunks:
            if count >= max_per_file:
                break
            if not (8 <= len(sent) <= 200) or not CLAUSE_KEYWORDS.search(sent):
                continue
            key = re.sub(r"\s+", "", sent)[:80]
            if key in seen:
                continue
            seen.add(key)
            suggested = sorted({r for kw, rules in KEYWORD_RULES.items()
                                if kw in sent for r in rules})
            count += 1
            clauses.append({
                "id": f"CL-{len(clauses) + 1:03d}",
                "source_file": str(doc.relative_to(project_dir)),
                "location": location,
                "quote": sent[:200],
                "suggested_rules": suggested,
                "confirmed": False,
            })

    draft = {
        "draft": True,
        "说明": "草案：人工核对后把确认条款的 confirmed 改为 true，并在 rule_mapping 填 "
               "规则ID -> [条款ID]；再在 project.yaml 设 "
               "procurement_clauses_mapping: 采购条款映射草案.yaml 重跑 assess_risk.py。",
        "clauses": clauses,
        "rule_mapping": {},
    }
    out = project_dir / "采购条款映射草案.yaml"
    out.write_text(yaml.safe_dump(draft, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    typer.secho(f"[OK] 条款草案已生成：{out}（候选 {len(clauses)} 条，来自 {len(docs)} 个采购文件）",
                fg="green")
    typer.echo("下一步：人工核对确认后，按文件头说明接入 assess_risk.py。")


if __name__ == "__main__":
    app()
