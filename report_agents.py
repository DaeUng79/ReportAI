#!/usr/bin/env python3
"""Supervisor/Writer multi-agent workflow for a local llama-server."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PROMPTS_DIRECTORY = Path(__file__).with_name("agent")


def load_agent_prompt(name: str) -> str:
    path = PROMPTS_DIRECTORY / f"{name}.md"
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LlamaServerError(f"프롬프트 파일을 읽지 못했습니다 ({path}): {exc}") from exc
    if not content:
        raise LlamaServerError(f"프롬프트 파일이 비어 있습니다: {path}")
    return content


def render_agent_prompt(name: str, **values: object) -> str:
    return re.sub(r"\{\{([a-z_]+)\}\}", lambda match: str(values.get(match.group(1), "")), load_agent_prompt(name))


SUPERVISOR_SYSTEM = """당신은 공공기관 보고서 작성 슈퍼바이저입니다.
사용자 요청이 행정·정책·사업 추진 보고로 적절한지 판단하고, 작성 에이전트가 만든 보고서가 공공기관 내부 A4 1쪽 문서 형식과 개조식 문체,문제 해결책 👉 반영했는지 기준을 충족하는지 엄격하게 검증합니다."""

WRITER_SYSTEM = """당신은 공공기관 내부 보고서 작성 에이전트입니다.
아래 [출력 형식]의 줄 구성과 기호(#, >, ---, ❖, ##, ❍, -, 👉)는 그대로 유지하고, <> 안의 설명만 실제 보고서 내용으로 교체해 작성하십시오.
[출력 형식]에 없는 안내 문장이나 코드블록(```)은 절대 출력하지 말고, 완성된 보고서 Markdown 전체만 출력하십시오.

[출력 형식]
# <간결한 사업명>
> 구분: □ 공약  □ 중점  ☑ 계속 | 소관: <추천 부서명 (☎ 055-392-0000)>
---
❖ <이 사업이 왜 필요한지 한 문장>
❖ <이 사업으로 무엇을 추진하는지 한 문장>
---

## 사업개요
❍ <추진배경>: <핵심 내용 한 문장>
❍ <필요성>: <핵심 내용 한 문장>
❍ <추진목표>: <핵심 내용 한 문장>
❍ 주요내용
- <핵심 내용 한 문장>
- <핵심 내용 한 문장>
- <핵심 내용 한 문장>

## 추진계획
❍ <분기별>: <1분기 추진과제 한 문장>
          <1분기 추진과제 한 문장>
❍ <분기별>: <2분기 추진과제 한 문장>
          <2분기 추진과제 한 문장>
❍ <분기별>: <3분기 추진과제 한 문장>
          <3분기 추진과제 한 문장>
❍ <분기별>: <4분기 추진과제 한 문장>
          <4분기 추진과제 한 문장>

## 예상문제점
❍ <예상되는 문제점 한 문장>
 👉 <해결방안 한 문장>
❍ <예상되는 문제점 한 문장>
 👉 <해결방안 한 문장>

## 기대효과
❍ <추진에 따른 효과 한 문장>
❍ <정량적 성과 한 문장>

[작성 규칙]
- 구분은 □ 공약, □ 중점, ☑ 계속 3개 표시를 모두 남기고, 요청 내용에 가장 알맞은 하나만 ☑로 바꾸십시오.
- 사업개요의 소제목 3개는 [추진배경, 사업기간, 소요예산, 추진체계, 근거법령, 사업대상] 중 보고서 주제에 가장 적합한 3개를 자동으로 선정하고, 어울리는 후보가 없으면 주제에 특화된 다른 소제목을 사용하십시오. 주요내용 4개 항목은 문제점·필요성·추진방향·추진목표 순서를 지키십시오.
- 추진계획은 요청에 별도 시기 정보가 없으면 분기 단위 3개 항목으로 작성하고, 담당자가 바로 실행할 수 있는 수준으로 구체화하십시오.
- 예상문제점은 ❍ 문제점과 바로 다음 줄의 👉 해결방안을 한 쌍으로 2쌍 작성하고, 각 👉 해결방안은 바로 위 ❍ 문제점에 대응하는 내용이어야 합니다.
- `❍`, `-`, `👉` 뒤에는 실제 완성 문장만 쓰고, 각 문장은 공백 제외 28~40자로 A4 본문 폭의 약 80%를 채우십시오.
- 모든 문장은 "~필요·~추진·~강화·~제고" 등 명사형으로 끝내고, "~입니다/~습니다/~합니다" 종결은 쓰지 마십시오.
- 표는 사용자가 표에 들어갈 분류·기준을 직접 제공한 경우에만 사업개요 마지막에 추가하고, 그 외에는 표를 만들지 마십시오.

[사실성 규칙]
- 요청에 없는 연도·월·인원·금액·비율·법령명·기관명·부서명·사업명·정책명·성과 수치를 임의로 만들지 마십시오.
- 근거가 없는 정보는 숫자 대신 `확인 필요`, 실적 대신 `현재 집계자료 없음`, 근거 대신 `내부 추진방침`으로 표기하십시오.

[분량]
- 표를 포함해 공백 포함 900~1,300자를 목표로 하되, 분량을 채우기 위해 사실이 아닌 내용을 추가하지 마십시오."""


class LlamaServerError(RuntimeError):
    pass


class LlamaClient:
    def __init__(self, base_url: str, model: str | None, timeout: int = 180):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.model = model or self._detect_model()

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            raise LlamaServerError(f"llama-server 요청 실패 ({url}): {exc}") from exc

    def _detect_model(self) -> str:
        result = self._request("/models")
        models = result.get("data", [])
        if not models or not models[0].get("id"):
            raise LlamaServerError("/v1/models에서 실행 중인 모델을 찾지 못했습니다.")
        return str(models[0]["id"])

    def chat(self, system: str, user: str, temperature: float = 0.3) -> str:
        result = self._request(
            "/chat/completions",
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": 4096,
                "stream": False,
            },
        )
        try:
            return str(result["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LlamaServerError(f"예상하지 못한 응답 형식: {result}") from exc


def parse_json_object(text: str) -> dict[str, Any]:
    """Accept plain JSON or JSON wrapped in a Markdown fence."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise ValueError(f"JSON 객체를 해석할 수 없습니다: {text[:200]}")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("응답이 JSON 객체가 아닙니다.")
    return value


@dataclass
class Review:
    approved: bool
    score: int
    feedback: list[str]
    summary: str


class SupervisorAgent:
    def __init__(self, client: LlamaClient):
        self.client = client

    def assess_request(self, request: str) -> dict[str, Any]:
        prompt = render_agent_prompt("supervisor_assessment", request=request)
        result = parse_json_object(self.client.chat(load_agent_prompt("supervisor"), prompt, 0.1))
        return {
            "suitable": bool(result.get("suitable", False)),
            "reason": str(result.get("reason", "판단 이유 없음")),
            "writing_brief": str(result.get("writing_brief", "요청에 충실하게 작성")),
        }

    def review(self, request: str, document: str) -> Review:
        prompt = render_agent_prompt("report_review", request=request, title="제목 미생성", document=document)
        result = parse_json_object(self.client.chat(load_agent_prompt("supervisor"), prompt, 0.1))
        score = max(0, min(100, int(result.get("score", 0))))
        feedback = result.get("feedback", [])
        if not isinstance(feedback, list):
            feedback = [str(feedback)]
        feedback = [str(item) for item in feedback]
        expected_problems = re.search(r"^##\s*예상문제점\s*$([\s\S]*?)(?=^##\s|\Z)", document, re.MULTILINE)
        problem_lines = re.findall(r"^❍\s*.+", expected_problems.group(1), re.MULTILINE) if expected_problems else []
        solution_pairs = re.findall(r"^❍\s*.+\n\s*(?:👉|\(대응방안\))\s*.+", expected_problems.group(1), re.MULTILINE) if expected_problems else []
        if len(problem_lines) < 2 or len(solution_pairs) != len(problem_lines):
            feedback.append("예상문제점별 👉 해결방안을 바로 다음 줄에 보완")
            score = min(score, 79)
        return Review(
            approved=bool(result.get("approved", False)) and score >= 80,
            score=score,
            feedback=feedback,
            summary=str(result.get("summary", "")),
        )


class WriterAgent:
    def __init__(self, client: LlamaClient):
        self.client = client

    @staticmethod
    def planning_year_instruction() -> str:
        planning_year = datetime.now().year + 1
        return f"추진계획 기준연도는 반드시 {planning_year}년입니다. 다른 연도는 사용하지 마십시오."

    def draft(self, request: str, brief: str) -> str:
        prompt = render_agent_prompt("report_writing", request=request, brief=brief, planning_year=datetime.now().year + 1)
        return self.client.chat(load_agent_prompt("document_writer"), prompt, 0.45)

    def revise(self, request: str, document: str, review: Review) -> str:
        feedback = "\n".join(f"- {item}" for item in review.feedback) or "- 총평을 반영해 완성도를 높일 것"
        prompt = render_agent_prompt(
            "report_revision",
            request=request,
            score=review.score,
            summary=review.summary,
            feedback=feedback,
            planning_year=datetime.now().year + 1,
            title="리포트 내 제목",
            document=document,
        )
        return self.client.chat(load_agent_prompt("document_writer"), prompt, 0.35)


@dataclass
class WorkflowResult:
    document: str
    approved: bool
    review: Review
    attempts: int


class ReportWorkflow:
    def __init__(self, client: LlamaClient, max_revisions: int = 2):
        self.supervisor = SupervisorAgent(client)
        self.writer = WriterAgent(client)
        self.max_revisions = max_revisions

    def run(self, request: str, verbose: bool = True) -> WorkflowResult:
        assessment = self.supervisor.assess_request(request)
        if verbose:
            print(f"[슈퍼바이저] 적절성: {'통과' if assessment['suitable'] else '부적절'}")
            print(f"[슈퍼바이저] {assessment['reason']}")
        if not assessment["suitable"]:
            raise ValueError(f"보고서 작성에 부적절한 요청입니다: {assessment['reason']}")

        document = self.writer.draft(request, assessment["writing_brief"])
        attempts = 1
        review = self.supervisor.review(request, document)
        if verbose:
            print(f"[슈퍼바이저] 초안 검증: {review.score}점 / {'승인' if review.approved else '수정 필요'}")

        for revision_no in range(1, self.max_revisions + 1):
            if review.approved:
                break
            if verbose:
                print(f"[문서작성] {revision_no}차 재작성 중...")
            document = self.writer.revise(request, document, review)
            attempts += 1
            review = self.supervisor.review(request, document)
            if verbose:
                print(f"[슈퍼바이저] 재검증: {review.score}점 / {'승인' if review.approved else '수정 필요'}")

        return WorkflowResult(document, review.approved, review, attempts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="로컬 Gemma 기반 보고서 작성 멀티 에이전트")
    parser.add_argument("request", nargs="?", help="작성할 보고서 요청")
    parser.add_argument("--request-file", type=Path, help="요청이 담긴 UTF-8 텍스트 파일")
    parser.add_argument("--output", type=Path, default=Path("report.md"), help="결과 Markdown 경로")
    parser.add_argument("--base-url", default=os.getenv("LLAMA_BASE_URL", "http://127.0.0.1:8080/v1"))
    parser.add_argument("--model", default=os.getenv("LLAMA_MODEL"), help="생략하면 /v1/models에서 자동 감지")
    parser.add_argument("--max-revisions", type=int, default=2, choices=range(0, 6))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    request = args.request_file.read_text(encoding="utf-8").strip() if args.request_file else (args.request or "").strip()
    if not request:
        print("오류: 보고서 요청 또는 --request-file을 입력하십시오.", file=sys.stderr)
        return 2
    try:
        client = LlamaClient(args.base_url, args.model)
        print(f"[시스템] 모델: {client.model}")
        result = ReportWorkflow(client, args.max_revisions).run(request)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result.document.rstrip() + "\n", encoding="utf-8")
        status = "승인" if result.approved else "최대 재작성 횟수 도달(미승인)"
        print(f"[완료] {args.output} / {status} / {result.review.score}점 / 총 {result.attempts}회 작성")
        return 0 if result.approved else 1
    except (LlamaServerError, ValueError, OSError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
