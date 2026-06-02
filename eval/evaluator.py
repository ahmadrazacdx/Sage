import json
import math
import re

import numpy as np
from fastembed import TextEmbedding
from langchain_openai import ChatOpenAI


def _parse_json(text: str) -> dict:
    """Robustly extract and parse a JSON object from text."""
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"({.*})", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    cleaned = text
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    return {}


FAITHFULNESS_PROMPT = """
You are an expert evaluator. Your task is to evaluate the faithfulness of
an AI-generated answer based on the given context.
An answer is faithful if all the claims made in the answer can be directly
inferred from the context.
Please score the faithfulness on a scale from 1 to 5, where:
1 = Completely unfaithful (hallucinated or contradicts context)
5 = Completely faithful (all claims fully supported by context)

Context:
{context}

Generated Answer:
{answer}

Respond ONLY with a valid JSON object containing the score and a brief reason.
Example: {{"score": 5, "reason": "All claims are supported."}}
"""

RELEVANCE_PROMPT = """
You are an expert evaluator. Your task is to evaluate the relevance of an AI-generated answer to the user's question.
An answer is relevant if it directly addresses the question without adding tangential or irrelevant information.
Please score the answer relevance on a scale from 1 to 5, where:
1 = Completely irrelevant
5 = Highly relevant and precise

Question:
{question}

Generated Answer:
{answer}

Respond ONLY with a valid JSON object containing the score and a brief reason.
Example: {{"score": 4, "reason": "Directly addresses the question but includes minor extra info."}}
"""

CONTEXT_PRECISION_PROMPT = """
You are an expert evaluator. Given a question and a context chunk, determine if the
chunk contains information that is useful for answering the question.
Score 1 if it is useful, 0 if it is not.

Question:
{question}

Context Chunk:
{chunk}

Respond ONLY with a valid JSON object containing the score. Example: {{"score": 1}}
"""


class Evaluator:
    def __init__(
        self, llm_model: str = "llama-3.3-70b-versatile", groq_api_key: str | None = None, llm_port: int | None = None
    ):
        """
        Initializes the evaluator. Defaults to Groq API for fast LLM-as-a-judge.
        Falls back to local LLM if no API key provided.
        """
        self.groq_api_key = groq_api_key

        if self.groq_api_key:
            self.judge_llm = ChatOpenAI(
                model=llm_model,
                api_key=self.groq_api_key,
                base_url="https://api.groq.com/openai/v1",
                temperature=0.0,
                model_kwargs={"response_format": {"type": "json_object"}},
            )
        else:
            port = llm_port or 8080
            self.judge_llm = ChatOpenAI(
                model="local",
                api_key="local",
                base_url=f"http://127.0.0.1:{port}/v1",
                temperature=0.0,
                model_kwargs={"response_format": {"type": "json_object"}},
            )

        self.embedder = TextEmbedding(
            model_name="BAAI/bge-small-en-v1.5", cache_dir="artifacts/models/embedding-models"
        )

    def _extract_score(self, response_text: str, max_score: float = 5.0) -> float:
        try:
            data = _parse_json(response_text)
            return min(float(data.get("score", 0.0)), max_score)
        except Exception:
            return 0.0

    async def evaluate_faithfulness(self, answer: str, context_chunks: list[str]) -> float:
        """Scores 1-5 normalized to 0.0-1.0"""
        if not context_chunks or not answer:
            return 0.0
        context_text = "\n\n".join(context_chunks)
        prompt = FAITHFULNESS_PROMPT.format(context=context_text, answer=answer)
        try:
            res = await self.judge_llm.ainvoke(prompt)
            content = str(res.content)
            score = 1.0
            reason = "Failed to parse judge output."
            try:
                data = _parse_json(content)
                score = min(float(data.get("score", 1.0)), 5.0)
                reason = data.get("reason", "No reason provided")
                if score < 3.0:
                    print(f"  [Faithfulness Judge] Score: {score}/5 | Reason: {reason}")
                    print(f"  [Faithfulness Judge] Generated Answer: {answer[:300]}...")
            except Exception:
                pass
            return score / 5.0
        except Exception as e:
            print(f"Faithfulness eval failed: {e}")
            return 0.0

    async def evaluate_relevance(self, question: str, answer: str) -> float:
        """Scores 1-5 normalized to 0.0-1.0"""
        if not answer:
            return 0.0
        prompt = RELEVANCE_PROMPT.format(question=question, answer=answer)
        try:
            res = await self.judge_llm.ainvoke(prompt)
            content = str(res.content)
            score = 1.0
            reason = "Failed to parse judge output."
            try:
                data = _parse_json(content)
                score = min(float(data.get("score", 1.0)), 5.0)
                reason = data.get("reason", "No reason provided")
                if score < 3.0:
                    print(f"  [Relevance Judge] Score: {score}/5 | Reason: {reason}")
                    print(f"  [Relevance Judge] Generated Answer: {answer[:300]}...")
            except Exception:
                pass
            return score / 5.0
        except Exception as e:
            print(f"Relevance eval failed: {e}")
            return 0.0

    async def evaluate_context_precision(self, question: str, context_chunks: list[str]) -> float:
        """Calculates Precision@K weighted by relevance of chunks."""
        if not context_chunks:
            return 0.0

        scores = []
        for chunk in context_chunks:
            prompt = CONTEXT_PRECISION_PROMPT.format(question=question, chunk=chunk)
            try:
                res = await self.judge_llm.ainvoke(prompt)
                s = self._extract_score(str(res.content), max_score=1.0)
                scores.append(s)
            except Exception:
                scores.append(0.0)
        precision_at_k = [sum(scores[: i + 1]) / (i + 1) for i in range(len(scores)) if scores[i] == 1.0]
        if not precision_at_k:
            return 0.0
        return sum(precision_at_k) / sum(scores) if sum(scores) > 0 else 0.0

    def evaluate_semantic_similarity(self, answer: str, ground_truth: str) -> float:
        """Cosine similarity between answer and ground truth."""
        if not answer or not ground_truth:
            return 0.0
        vecs = list(self.embedder.embed([answer, ground_truth]))
        v1, v2 = np.array(vecs[0]), np.array(vecs[1])
        cos_sim = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))
        return float(max(0.0, min(1.0, cos_sim)))

    def _normalize_course_code(self, code: str) -> str:
        """Extracts the base course code prefix, e.g. 'CMPC104_ Software Engineering' -> 'CMPC104'"""
        if not code:
            return ""
        import re

        match = re.match(r"^([A-Z]{3,4}\d{3})", code.strip().upper())
        if match:
            return match.group(1)
        return re.split(r"[\s_-]", code.strip())[0].upper()

    def evaluate_mrr(self, retrieved_course_codes: list[str], target_course: str) -> float:
        """Mean Reciprocal Rank based on course code hit."""
        target_norm = self._normalize_course_code(target_course)
        if not target_norm:
            return 0.0
        for i, code in enumerate(retrieved_course_codes):
            if self._normalize_course_code(code) == target_norm:
                return 1.0 / (i + 1)
        return 0.0

    def evaluate_ndcg(self, retrieved_course_codes: list[str], target_course: str) -> float:
        """nDCG based on binary relevance of course code."""
        target_norm = self._normalize_course_code(target_course)
        if not target_norm:
            return 0.0
        dcg = sum(
            1.0 / math.log2(i + 2)
            for i, code in enumerate(retrieved_course_codes)
            if self._normalize_course_code(code) == target_norm
        )
        hits = sum(1 for c in retrieved_course_codes if self._normalize_course_code(c) == target_norm)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(hits))
        return dcg / idcg if idcg > 0 else 0.0
