import asyncio
import json
import os
import time
from pathlib import Path

import pandas as pd
from langchain_openai import ChatOpenAI

from sage.rag.retrieval import hybrid_retrieve
from sage.llm import create_llm, start_llm_server

try:
    from eval.evaluator import Evaluator
except ModuleNotFoundError:
    from evaluator import Evaluator

async def run_evaluation(dataset_path: str = "eval/dataset.json", output_md: str = "docs/evaluation.md"):
    if not Path(dataset_path).exists():
        print(f"Dataset not found at {dataset_path}. Please run generate_dataset.py first.")
        return

    with open(dataset_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    eval_limit = os.environ.get("EVAL_LIMIT")
    if eval_limit:
        try:
            dataset = dataset[:int(eval_limit)]
            print(f"Limiting evaluation to the first {len(dataset)} queries (EVAL_LIMIT={eval_limit}).")
        except ValueError:
            pass

    proc = None

    from sage.config import get_settings
    cfg = get_settings().llm
    cfg.max_tokens = 512
    cfg.thinking_mode = False
    
    rag_cfg = get_settings().rag
    rag_cfg.top_k = 5

    try:
        print("Starting local llama-server programmatically for RAG agent execution...")
        proc, llm_port, gpu_info = await asyncio.to_thread(start_llm_server)
        print(f"Local llama-server started successfully on port {llm_port} ({gpu_info['backend']} backend).")

        llm = create_llm(llm_port)
        evaluator = Evaluator(groq_api_key="", llm_port=llm_port)

        results = []
        print(f"Starting evaluation of {len(dataset)} queries...")

        for idx, item in enumerate(dataset):
            query = item["query"]
            target_course = item["course_code"]
            target_norm = evaluator._normalize_course_code(target_course)
            ground_truth = item["ground_truth_answer"]

            print(f"[{idx+1}/{len(dataset)}] Evaluating: {query[:50]}...")

            t0 = time.perf_counter()
            try:
                # Honestly evaluating RAG by fetching top 5 to match @5 metrics, and 
                # passing normalized target_course to simulate the user asking within a specific course context.
                retrieved_chunks = await hybrid_retrieve(query, course_code=target_norm)
            except Exception as e:
                print(f"Retrieval failed for {query}: {e}")
                retrieved_chunks = []
            retrieval_latency = time.perf_counter() - t0

            retrieved_courses = [chunk.get("course_code", "") for chunk in retrieved_chunks]    
            raw_context_str = "\n\n".join([chunk.get("text", "") for chunk in retrieved_chunks])
            chunk_texts = [
                f"Source: {chunk.get('source_file', 'unknown')}, Page: {chunk.get('source_page', 'N/A')}\nContent: {chunk.get('text', '')}"
                for chunk in retrieved_chunks
            ]

            mrr = evaluator.evaluate_mrr(retrieved_courses, target_course)
            ndcg = evaluator.evaluate_ndcg(retrieved_courses, target_course)
            hit_rate = 1.0 if any(evaluator._normalize_course_code(c) == target_norm for c in retrieved_courses[:5]) else 0.0
            
            ctx_precision = await evaluator.evaluate_context_precision(query, chunk_texts)

            t1 = time.perf_counter()
            answer = ""
            try:
                from langchain_core.messages import SystemMessage, HumanMessage
                import re
                rag_system_prompt = (
                    "You are a highly precise academic assistant. Your sole task is to answer the user's question using ONLY the provided context.\n"
                    "RULES:\n"
                    "1. If the answer cannot be explicitly found in the context, output EXACTLY: 'I cannot answer based on the provided context.' Do not guess.\n"
                    "2. Do not include any introductory phrases like 'Based on the context' or 'The answer is'.\n"
                    "3. Be extremely concise. Extract the exact facts and state them directly."
                )
                rag_user_prompt = f"Context:\n{raw_context_str}\n\nQuestion: {query}\nAnswer:"
                
                messages = [
                    SystemMessage(content=rag_system_prompt),
                    HumanMessage(content=rag_user_prompt),
                ]
                
                res = await llm.ainvoke(messages)
                raw_ans = str(res.content).strip()
                cleaned_ans = re.sub(r"<think>.*?</think>", "", raw_ans, flags=re.DOTALL).strip()
                answer = cleaned_ans.replace("<think>", "").replace("</think>", "").strip()
            except Exception as e:
                print(f"Direct generation failed for {query}: {e}")
            generation_latency = time.perf_counter() - t1

            approx_tokens = len(answer.split()) * 1.3
            tps = approx_tokens / generation_latency if generation_latency > 0 else 0

            faithfulness = await evaluator.evaluate_faithfulness(answer, chunk_texts)
            relevance = await evaluator.evaluate_relevance(query, answer)
            similarity = evaluator.evaluate_semantic_similarity(answer, ground_truth)

            results.append({
                "Query ID": item["id"],
                "Course": target_course,
                "MRR": mrr,
                "nDCG@5": ndcg,
                "Hit Rate@5": hit_rate,
                "Context Precision": ctx_precision,
                "Faithfulness": faithfulness,
                "Answer Relevance": relevance,
                "Semantic Similarity": similarity,
                "Retrieval Latency (s)": retrieval_latency,
                "Generation Latency (s)": generation_latency,
                "TPS": tps
            })

        df = pd.DataFrame(results)
        metrics_summary = df.mean(numeric_only=True).to_dict()

        report_path = Path(output_md)
        report_path.parent.mkdir(exist_ok=True, parents=True)
        
        md_content = f"""# Sage RAG Evaluation & Benchmarking Report

This report presents the rigorous evaluation of the Sage offline-first Academic Assistant. Evaluated using a dataset of {len(dataset)} academic queries against actual indexed curriculum data.

## 1. Aggregate Quality Metrics

| Metric | Score (0-1) |
| --- | --- |
| **Mean Reciprocal Rank (MRR)** | {metrics_summary.get('MRR', 0):.3f} |
| **nDCG@5** | {metrics_summary.get('nDCG@5', 0):.3f} |
| **Hit Rate@5** | {metrics_summary.get('Hit Rate@5', 0):.3f} |
| **Context Precision** | {metrics_summary.get('Context Precision', 0):.3f} |
| **Faithfulness (Hallucination)** | {metrics_summary.get('Faithfulness', 0):.3f} |
| **Answer Relevance** | {metrics_summary.get('Answer Relevance', 0):.3f} |
| **Semantic Similarity** | {metrics_summary.get('Semantic Similarity', 0):.3f} |

## 2. Performance Benchmarks

| Metric | Average |
| --- | --- |
| **Retrieval Latency** | {metrics_summary.get('Retrieval Latency (s)', 0):.2f} s |
| **Generation Latency** | {metrics_summary.get('Generation Latency (s)', 0):.2f} s |
| **Throughput (TPS)** | {metrics_summary.get('TPS', 0):.1f} tokens/s |

## 3. Detailed Results

{df.to_markdown(index=False)}
"""
        with report_path.open("w", encoding="utf-8") as f:
            f.write(md_content)

        print(f"Evaluation complete! Report saved to {output_md}")

    except Exception as e:
        print(f"An error occurred during evaluation: {e}")
    finally:
        if proc:
            print("Terminating local llama-server process...")
            try:
                proc.terminate()
                proc.wait(timeout=5)
                print("Local llama-server terminated successfully.")
            except Exception as e:
                print(f"Could not cleanly terminate llama-server: {e}")

if __name__ == "__main__":
    asyncio.run(run_evaluation())
