# Sage RAG Evaluation & Benchmarking Report

This report presents the rigorous evaluation of the Sage offline-first Academic Assistant. Evaluated using a dataset of 3 academic queries against actual indexed curriculum data.

## 1. Aggregate Quality Metrics

| Metric | Score (0-1) |
| --- | --- |
| **Mean Reciprocal Rank (MRR)** | 0.000 |
| **nDCG@5** | 0.000 |
| **Hit Rate@5** | 0.000 |
| **Context Precision** | 0.000 |
| **Faithfulness (Hallucination)** | 0.000 |
| **Answer Relevance** | 1.000 |
| **Semantic Similarity** | 0.688 |

## 2. Performance Benchmarks

| Metric | Average |
| --- | --- |
| **Retrieval Latency** | 0.01 s |
| **Generation Latency** | 13.08 s |
| **Throughput (TPS)** | 5.8 tokens/s |

## 3. Detailed Results

| Query ID   | Course                          |   MRR |   nDCG@5 |   Hit Rate@5 |   Context Precision |   Faithfulness |   Answer Relevance |   Semantic Similarity |   Retrieval Latency (s) |   Generation Latency (s) |     TPS |
|:-----------|:--------------------------------|------:|---------:|-------------:|--------------------:|---------------:|-------------------:|----------------------:|------------------------:|-------------------------:|--------:|
| Q001       | CMPC104_ Software Engineering   |     0 |        0 |            0 |                   0 |              0 |                  1 |              0.746135 |               0.0002497 |                  17.1456 | 5.38331 |
| Q002       | CMPC101_Programing_Fundamentals |     0 |        0 |            0 |                   0 |              0 |                  1 |              0.904766 |               0.0098044 |                  13.5276 | 5.47768 |
| Q003       | ITCC402_Cyber Security          |     0 |        0 |            0 |                   0 |              0 |                  1 |              0.413495 |               0.0101742 |                   8.569  | 6.67523 |
