#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOME = Path.home()


def load_bridge_config():
    config_path = ROOT / "config.json"
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


BRIDGE_CONFIG = load_bridge_config()
CODEX = os.environ.get("CODEX_PATH", BRIDGE_CONFIG.get("codex_path", "codex"))
RAW_ROOT = Path(os.environ.get("RAW_ROOT", BRIDGE_CONFIG.get("log_dir", HOME / "telegram-codex-bridge-logs/raw")))
WIKI = Path(os.environ.get("WIKI_ROOT", RAW_ROOT.parent))
OUT_ROOT = WIKI / "queries/daily_ingest_candidates"
CHANNELS = BRIDGE_CONFIG.get("daily_ingest_channels") or sorted(
    [path.name for path in RAW_ROOT.iterdir() if path.is_dir()]
) if RAW_ROOT.exists() else ["telegram"]


def today_kst():
    return dt.datetime.now().strftime("%Y-%m-%d")


def previous_date(date_str):
    date = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    return (date - dt.timedelta(days=1)).isoformat()


def read_text(path):
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def collect_raw(date_str):
    parts = []
    for channel in CHANNELS:
        path = RAW_ROOT / channel / f"{date_str}.md"
        text = read_text(path)
        if text.strip():
            parts.append(f"\n\n# RAW {channel} {date_str}\n\n{text}")
    return "\n".join(parts)


def build_prompt(date_str, raw_text):
    return f"""너는 Obsidian 위키 daily ingest 후보를 만드는 로컬 작업자다.

목표:
- raw 원문을 보존한 상태에서, 위키에 승격할 만한 후보만 추린다.
- 실제 위키 파일(index/log/concepts/entities)은 수정하지 않는다.
- 결과는 Markdown 리포트로만 작성한다.
- 설명/인사/스킬 사용 안내 없이 frontmatter `---`로 바로 시작한다.
- "다음 쓰기 가능 세션" 같은 표현은 쓰지 않는다. 후보만 판단한다.

판단 기준:
- 새 운영규칙, 사용자 피드백, 선호, 반복 실수 방지 규칙
- 프로젝트 상태 변화, 다음 액션, 마감/대기물
- 보험/현대차/부동산/CFA/자동화 관련 재사용 가능한 인사이트
- 인물/도메인/워크플로우에 붙일 수 있는 사실
- 단순 잡담, 확인, 임시 오류 로그는 제외

출력 형식:
---
date: {date_str}
type: daily-ingest-candidates
status: candidate
---
# Daily Ingest Candidates — {date_str}

## Summary

## Promote Candidates
- [ ] title:
  - target: concepts|entities|comparisons|log|handoff
  - confidence: verified|experimental|hypothesis
  - evidence: raw/{{channel}}/{date_str}.md
  - note:

## Raw-Only

## Open Questions

## Suggested Next Actions

raw:
{raw_text}
"""


def parse_codex_final(output):
    messages = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(item["text"])
    return "\n\n".join(messages).strip() or output.strip()


def run_codex(prompt):
    proc = subprocess.run(
        [
            CODEX,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "-C",
            str(HOME),
            "-",
        ],
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=None,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout)
    return parse_codex_final(proc.stdout)


def fallback_report(date_str, raw_text):
    lines = raw_text.splitlines()
    hits = []
    keywords = ["기억", "규칙", "피드백", "앞으로", "중요", "자동화", "위키", "raw", "텔레그램", "디스코드", "보험", "현대차"]
    for idx, line in enumerate(lines, start=1):
        if any(k in line for k in keywords):
            hits.append(f"- line {idx}: {line[:240]}")
    hits_text = "\n".join(hits[:80]) or "- 후보 없음"
    return f"""---
date: {date_str}
type: daily-ingest-candidates
status: fallback
---
# Daily Ingest Candidates — {date_str}

## Summary
Codex 실행 실패 시 사용하는 키워드 기반 후보 초안.

## Promote Candidates
{hits_text}

## Raw-Only
- 전체 raw는 원문 보존됨.

## Open Questions
- Codex 기반 판단 재실행 필요.

## Suggested Next Actions
- `python3 ~/telegram-codex-bridge/daily_wiki_ingest.py --date {date_str}` 재실행.
"""


def write_report(date_str, content):
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    path = OUT_ROOT / f"{date_str}.md"
    marker = content.find("---")
    if marker > 0:
        content = content[marker:]
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=previous_date(today_kst()))
    parser.add_argument("--today", action="store_true")
    args = parser.parse_args()
    date_str = today_kst() if args.today else args.date

    raw_text = collect_raw(date_str)
    if not raw_text.strip():
        report = f"""---
date: {date_str}
type: daily-ingest-candidates
status: no-raw
---
# Daily Ingest Candidates — {date_str}

## Summary
해당 날짜 raw 없음.
"""
        path = write_report(date_str, report)
        print(path)
        return

    prompt = build_prompt(date_str, raw_text)
    try:
        report = run_codex(prompt)
    except Exception as exc:
        print(f"[daily-ingest] Codex failed, writing fallback: {exc}", file=sys.stderr)
        report = fallback_report(date_str, raw_text)
    path = write_report(date_str, report)
    print(path)


if __name__ == "__main__":
    main()
