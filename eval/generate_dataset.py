import json
import os
import random
from pathlib import Path
from langchain_openai import ChatOpenAI

GENERATE_PROMPT = """
You are an expert curriculum designer. Given the following academic text excerpt, generate ONE high-quality question and its correct ground-truth answer. 
The question should test the student's understanding of the text.

Text Excerpt:
{text}

Respond ONLY with a valid JSON object containing "query" (the question) and "ground_truth_answer" (the correct answer based on the text).
Example: {{"query": "What is the primary function of TCP?", "ground_truth_answer": "TCP guarantees reliable, ordered delivery of packets."}}
"""

def get_llm():
    groq_api_key = os.environ.get("GROQ_API_KEY")
    if groq_api_key:
        return ChatOpenAI(
            model="llama-3.3-70b-versatile",
            api_key=groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            temperature=0.7,
            model_kwargs={"response_format": {"type": "json_object"}}
        )
    return ChatOpenAI(
        model="local",
        api_key="local",
        base_url="http://127.0.0.1:8080/v1",
        temperature=0.7,
        model_kwargs={"response_format": {"type": "json_object"}}
    )

def extract_chunks(processed_dir: Path, max_chunks: int = 60) -> list[dict]:
    chunks = []
    if not processed_dir.exists():
        print(f"Directory {processed_dir} does not exist.")
        return chunks

    for md_file in processed_dir.rglob("*.md"):
        course_code = md_file.parent.name
        text = md_file.read_text(encoding="utf-8")
        
        paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 150]
        for p in paragraphs:
            chunks.append({
                "text": p,
                "course_code": course_code,
                "source_file": md_file.name
            })
    random.shuffle(chunks)
    return chunks[:max_chunks]

async def generate_dataset(processed_dir_path: str = "artifacts/data/processed", output_file: str = "eval/dataset.json", num_queries: int = 60):
    processed_dir = Path(processed_dir_path)
    chunks = extract_chunks(processed_dir, max_chunks=num_queries)
    llm = get_llm()
    
    dataset = []
    print(f"Found {len(chunks)} suitable text chunks. Generating queries...")
    
    for i, chunk in enumerate(chunks):
        prompt = GENERATE_PROMPT.format(text=chunk["text"])
        try:
            res = await llm.ainvoke(prompt)
            data = json.loads(res.content)
            if "query" in data and "ground_truth_answer" in data:
                dataset.append({
                    "id": f"Q{i+1:03d}",
                    "query": data["query"],
                    "course_code": chunk["course_code"],
                    "ground_truth_answer": data["ground_truth_answer"],
                    "expected_document": chunk["source_file"],
                    "complexity": random.choice(["factual", "reasoning", "summarization"])
                })
                print(f"Generated Q{i+1:03d} for {chunk['course_code']}")
        except Exception as e:
            print(f"Failed to generate query for chunk {i}: {e}")
            
    output_path = Path(output_file)
    output_path.parent.mkdir(exist_ok=True, parents=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2)
        
    print(f"Successfully generated {len(dataset)} queries to {output_file}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(generate_dataset())
