#!/usr/bin/env python
# 使用柏拉图 Rerank API 对候选论文做重排序（简化版）。

import argparse
import json
import os
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from llm import LLMClient

SCRIPT_DIR = os.path.dirname(__file__)
ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
TODAY_STR = str(os.getenv("DPR_RUN_DATE") or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%d")
ARCHIVE_DIR = os.path.join(ROOT_DIR, "archive", TODAY_STR)
FILTERED_DIR = os.path.join(ARCHIVE_DIR, "filtered")
RANKED_DIR = os.path.join(ARCHIVE_DIR, "rank")

MAX_CHARS_PER_DOC = 850
BATCH_SIZE = 100
TOKEN_SAFETY = 29000
RRF_K = 60

# API 速率限制配置：批次之间添加延迟避免触发 QPS 限制
BATCH_DELAY_SECONDS = 2  # 每批次之间延迟 2 秒
QUERY_DELAY_SECONDS = 5  # 不同查询之间延迟 5 秒
LANE_TOP_K_BASE = 30
LANE_TOP_K_STEP = 10
LANE_TOP_K_MAX = 120
GLOBAL_POOL_GUARANTEED_MIN = 5
GLOBAL_POOL_GUARANTEED_MAX = 20
GLOBAL_POOL_RRF_MIN = 60
GLOBAL_POOL_RRF_MAX = 300


def log(message: str) -> None:
  ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
  print(f"[{ts}] {message}", flush=True)


def group_start(title: str) -> None:
  print(f"::group::{title}", flush=True)


def group_end() -> None:
  print("::endgroup::", flush=True)

def build_token_encoder():
  try:
    import tiktoken  # type: ignore
    return tiktoken.get_encoding("cl100k_base")
  except Exception:
    return None


def estimate_tokens(text: str, encoder) -> int:
  if encoder is None:
    return max(1, len(text) // 3)
  return len(encoder.encode(text))


def score_to_stars(score: float) -> int:
  if score >= 0.9:
    return 5
  if score >= 0.5:
    return 4
  if score >= 0.1:
    return 3
  if score >= 0.01:
    return 2
  return 1


def load_json(path: str) -> Dict[str, Any]:
  if not os.path.exists(path):
    raise FileNotFoundError(f"找不到文件：{path}")
  with open(path, "r", encoding="utf-8") as f:
    return json.load(f)


def save_json(data: Dict[str, Any], path: str) -> None:
  os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
  with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
  log(f"[INFO] 已将打分结果写入：{path}")


def format_doc(title: str, abstract: str) -> str:
  content = f"Title: {title}\nAbstract: {abstract}".strip()
  if len(content) > MAX_CHARS_PER_DOC:
    content = content[:MAX_CHARS_PER_DOC]
  return content


def build_documents(papers_by_id: Dict[str, Dict[str, Any]], paper_ids: List[str]) -> List[str]:
  docs: List[str] = []
  for pid in paper_ids:
    p = papers_by_id.get(pid)
    if not p:
      docs.append(f"[Missing paper {pid}]")
      continue
    title = (p.get("title") or "").strip()
    abstract = (p.get("abstract") or "").strip()
    if title or abstract:
      docs.append(format_doc(title, abstract))
    else:
      docs.append(f"[Empty paper {pid}]")
  return docs


def get_top_ids(query_obj: Dict[str, Any]) -> List[str]:
  sim_scores = query_obj.get("sim_scores") or {}
  top_ids = query_obj.get("top_ids") or []
  if not top_ids and isinstance(sim_scores, dict) and sim_scores:
    top_ids = sorted(sim_scores.keys(), key=lambda pid: sim_scores[pid].get("rank", 1e9))
  return list(top_ids)


def _unique_keep_order(items: List[str]) -> List[str]:
  seen = set()
  out: List[str] = []
  for item in items:
    pid = str(item or "").strip()
    if not pid or pid in seen:
      continue
    seen.add(pid)
    out.append(pid)
  return out


def _clamp_int(value: float | int, min_value: int, max_value: int) -> int:
  return max(min_value, min(int(value), max_value))


def resolve_global_pool_budget(
  total_papers: int,
  intent_query_count: int,
) -> Tuple[int, int, int]:
  """
  统一候选池预算：
  - lane_top_k 随论文总数递增：1000 篇内 30，每增加 1000 篇 +10，上限 120；
  - guaranteed_per_lane = lane_top_k 的 25%，限制在 [5, 20]；
  - global_rrf_top = lane_top_k * intent_query_count，限制在 [60, 300]。
  """
  total = max(int(total_papers or 0), 0)
  intent_count = max(int(intent_query_count or 0), 1)
  if total <= 0:
    lane_top_k = LANE_TOP_K_BASE
  else:
    blocks = (total - 1) // 1000
    lane_top_k = min(LANE_TOP_K_BASE + LANE_TOP_K_STEP * blocks, LANE_TOP_K_MAX)
  guaranteed_per_lane = _clamp_int(
    round(lane_top_k * 0.25),
    GLOBAL_POOL_GUARANTEED_MIN,
    GLOBAL_POOL_GUARANTEED_MAX,
  )
  global_rrf_top = _clamp_int(
    lane_top_k * intent_count,
    GLOBAL_POOL_RRF_MIN,
    GLOBAL_POOL_RRF_MAX,
  )
  return lane_top_k, guaranteed_per_lane, global_rrf_top


def build_global_candidate_ids(
  queries: List[Dict[str, Any]],
  *,
  guaranteed_per_lane: int,
  global_limit: int,
) -> List[str]:
  """
  将所有 query lane 的候选论文合并成统一候选池。
  - 不区分 keyword / intent_query 来源；
  - 使用 rank-based RRF 做全局聚合，避免不同分数量纲直接混用；
  - 每条 lane 的前 guaranteed_per_lane 固定保留；
  - 再加入全局 RRF 前 global_limit 篇；
  - 最终按“固定保留 + 全局排序”去重合并。
  """
  score_map: Dict[str, float] = {}
  hit_count: Dict[str, int] = {}
  guaranteed_ids: List[str] = []

  for q in queries or []:
    top_ids = get_top_ids(q)
    if not top_ids:
      continue
    if guaranteed_per_lane > 0:
      guaranteed_ids.extend(top_ids[:guaranteed_per_lane])
    for rank_idx, pid in enumerate(top_ids, start=1):
      paper_id = str(pid or "").strip()
      if not paper_id:
        continue
      score_map[paper_id] = score_map.get(paper_id, 0.0) + 1.0 / (RRF_K + rank_idx)
      hit_count[paper_id] = hit_count.get(paper_id, 0) + 1

  ranked = sorted(
    score_map.items(),
    key=lambda item: (
      -item[1],
      -hit_count.get(item[0], 0),
      item[0],
    ),
  )
  global_ids = [pid for pid, _score in ranked]
  if global_limit > 0:
    global_ids = global_ids[:global_limit]
  return _unique_keep_order(list(guaranteed_ids) + list(global_ids))


def iter_batches(
  docs_with_idx: List[Tuple[int, str]],
  query_tokens: int,
  encoder,
) -> List[Tuple[List[int], List[str]]]:
  batches: List[Tuple[List[int], List[str]]] = []
  pos = 0
  while pos < len(docs_with_idx):
    total_tokens = query_tokens
    batch_docs: List[str] = []
    batch_indices: List[int] = []

    while pos < len(docs_with_idx) and len(batch_docs) < BATCH_SIZE:
      orig_idx, doc = docs_with_idx[pos]
      doc_tokens = estimate_tokens(doc, encoder)
      if total_tokens + doc_tokens > TOKEN_SAFETY and batch_docs:
        break
      batch_docs.append(doc)
      batch_indices.append(orig_idx)
      total_tokens += doc_tokens
      pos += 1

    if not batch_docs:
      pos += 1
      continue
    batches.append((batch_indices, batch_docs))
  return batches


def rrf_merge(scores: Dict[int, float], rank_idx: int, orig_idx: int) -> None:
  scores[orig_idx] = scores.get(orig_idx, 0.0) + 1.0 / (RRF_K + rank_idx)


def rerank_by_chat(
  client: LLMClient,
  query: str,
  documents: List[str],
  top_n: Optional[int] = None,
) -> dict:
  """
  使用 Chat 接口实现重排序（让模型对每个文档打分）。

  返回格式与 BLT Rerank API 兼容：
  {
    "results": [
      {"index": 0, "relevance_score": 0.95, "document": "..."},
      {"index": 2, "relevance_score": 0.87, "document": "..."},
      ...
    ]
  }
  """
  if not documents:
    return {"results": []}

  # 构建评分提示
  doc_list = "\n\n".join([f"[{i}] {doc[:500]}" for i, doc in enumerate(documents)])
  prompt = f"""请根据查询对以下文档进行相关性评分。

查询：{query}

文档列表：
{doc_list}

请直接返回纯 JSON 格式的评分结果，不要使用 markdown 代码块包裹，不要添加任何其他说明文字。

返回格式：
{{
  "results": [
    {{"index": 0, "relevance_score": 0.95}},
    {{"index": 1, "relevance_score": 0.75}}
  ]
}}

要求：
1. relevance_score 为 0-1 之间的分数
2. 只返回 JSON 对象，不要有任何额外文字、markdown 代码块或其他说明
3. 必须是可直接解析的纯 JSON"""

  try:
    response = client.chat(
      messages=[{"role": "user", "content": prompt}],
      response_format={"type": "json_object"}
    )

    content = response.get("content", "")

    # 尝试解析 JSON
    import json as json_lib
    try:
      # 先尝试清理可能的 markdown 包裹
      cleaned_content = content.strip()
      if cleaned_content.startswith("```"):
        # 移除 markdown 代码块包裹
        lines = cleaned_content.split('\n')
        if len(lines) >= 3:
          # 移除第一行 ```json 和最后一行 ```
          cleaned_content = '\n'.join(lines[1:-1])
      elif cleaned_content.startswith('{"'):
        # 尝试找到 JSON 对象的结束位置
        last_brace = cleaned_content.rfind('}')
        if last_brace > 0:
          cleaned_content = cleaned_content[:last_brace + 1]

      result = json_lib.loads(cleaned_content)
      results = result.get("results", [])

      # 补充 document 字段
      for item in results:
        idx = item.get("index")
        if isinstance(idx, int) and 0 <= idx < len(documents):
          item["document"] = documents[idx]

      # 应用 top_n 截断
      if top_n is not None and top_n > 0:
        results = results[:top_n]

      return {"results": results}

    except json_lib.JSONDecodeError as je:
      # JSON 解析失败，记录原始内容用于调试
      log(f"[WARN] Rerank JSON 解析失败: {je}")
      log(f"[DEBUG] 模型原始响应（前500字符）: {content[:500]}")
      # 回退到简单评分
      return {
        "results": [
          {"index": i, "relevance_score": 1.0 - (i * 0.01), "document": doc}
          for i, doc in enumerate(documents)
        ][:top_n] if top_n else [
          {"index": i, "relevance_score": 1.0 - (i * 0.01), "document": doc}
          for i, doc in enumerate(documents)
        ]
      }

  except Exception as e:
    # 检查是否有 HTTP 错误信息
    error_detail = str(e)
    if hasattr(e, "response") and e.response is not None:
        try:
            status = e.response.status_code
            try:
                error_json = e.response.json()
                error_detail = f"HTTP {status} - {error_json}"
            except:
                error_detail = f"HTTP {status} - {e.response.text[:200]}"
        except:
            error_detail = f"HTTP 错误 - {status}"
    elif hasattr(e, "__cause__") and e.__cause__ is not None:
        error_detail = f"{e} (caused by: {e.__cause__})"

    log(f"[WARN] Chat Rerank 失败: {error_detail}，回退到原始顺序")
    return {
      "results": [
        {"index": i, "relevance_score": 1.0 - (i * 0.01), "document": doc}
        for i, doc in enumerate(documents)
      ]
    }


def process_file(
  reranker: LLMClient,
  input_path: str,
  output_path: str,
  top_n: Optional[int],
  rerank_model: str,
) -> None:
  data = load_json(input_path)
  papers_list = data.get("papers") or []
  all_queries = data.get("queries") or []
  if not papers_list or not all_queries:
    log(f"[WARN] 文件 {os.path.basename(input_path)} 中缺少 papers 或 queries，跳过。")
    return

  # 仅使用语义查询（intent_query 或兼容旧的 llm_query）进行 rerank。
  def _is_intent_rerank_query(q: Dict[str, Any]) -> bool:
    q_type = str(q.get("type") or "").strip().lower()
    return q_type in {"intent_query", "llm_query"}

  queries = [q for q in all_queries if _is_intent_rerank_query(q)]
  if not queries:
    log("[WARN] 当前输入中没有可用于 rerank 的意图查询，跳过 rerank。")
    # 保持输出结构一致，避免后续步骤读不到文件
    meta_generated_at = data.get("generated_at") or ""
    data["reranked_at"] = datetime.now(timezone.utc).isoformat()
    data["generated_at"] = meta_generated_at
    save_json(data, output_path)
    return

  papers_by_id = {str(p.get("id")): p for p in papers_list if p.get("id")}
  lane_top_k, guaranteed_per_lane, global_rrf_top = resolve_global_pool_budget(
    len(papers_list),
    len(queries),
  )
  global_candidate_ids = build_global_candidate_ids(
    all_queries,
    guaranteed_per_lane=guaranteed_per_lane,
    global_limit=global_rrf_top,
  )
  data["global_candidate_ids"] = global_candidate_ids
  data["global_pool_lane_top_k"] = lane_top_k
  data["global_pool_limit"] = global_rrf_top
  data["global_pool_guaranteed_per_lane"] = guaranteed_per_lane
  if not global_candidate_ids:
    log("[WARN] 未能从任意 query 中构建统一候选池，跳过 rerank。")
    meta_generated_at = data.get("generated_at") or ""
    data["reranked_at"] = datetime.now(timezone.utc).isoformat()
    data["generated_at"] = meta_generated_at
    save_json(data, output_path)
    return
  encoder = build_token_encoder()
  group_start(f"Step 3 - rerank {os.path.basename(input_path)}")
  log(
    f"[INFO] 开始 rerank：queries={len(queries)}（仅 intent/语义查询），papers={len(papers_list)}，"
    f"global_pool={len(global_candidate_ids)}（lane_top_k={lane_top_k}, "
    f"guaranteed_per_lane={guaranteed_per_lane}, global_top={global_rrf_top}），"
    f"batch_size={BATCH_SIZE}，"
    f"max_chars={MAX_CHARS_PER_DOC}，token_safety={TOKEN_SAFETY}"
  )

  for q_idx, q in enumerate(queries, start=1):
    q_text = (q.get("rewrite") or q.get("query_text") or "").strip()
    top_ids = list(global_candidate_ids)
    if not q_text or not top_ids:
      continue

    group_start(f"Query {q_idx}/{len(queries)} tag={q.get('tag') or ''}")
    documents = build_documents(papers_by_id, top_ids)
    docs_with_idx = list(enumerate(documents))
    random.shuffle(docs_with_idx)

    query_tokens = estimate_tokens(q_text, encoder)
    batches = iter_batches(docs_with_idx, query_tokens, encoder)
    log(
      f"[INFO] Query {q_idx}/{len(queries)} tag={q.get('tag') or ''} | candidates={len(top_ids)} "
      f"| batches={len(batches)} | query_tokens≈{query_tokens}"
    )

    rrf_scores: Dict[int, float] = {}

    try:
      for batch_idx, (batch_indices, batch_docs) in enumerate(batches, 1):
        log(
          f"[INFO] 发送批次 {batch_idx}/{len(batches)} | docs={len(batch_docs)}"
        )
        response = rerank_by_chat(
          client=reranker,
          query=q_text,
          documents=batch_docs,
          top_n=len(batch_docs),
        )
        results = response.get("results", [])

        # 批次之间添加延迟，避免触发速率限制
        if batch_idx < len(batches):
          import time
          log(f"[INFO] 等待 {BATCH_DELAY_SECONDS} 秒后处理下一批次...")
          time.sleep(BATCH_DELAY_SECONDS)

        ranked = sorted(
          results or [],
          key=lambda x: x.get("relevance_score", x.get("score", 0.0)),
          reverse=True,
        )
        for rank_idx, item in enumerate(ranked, start=1):
          idx = int(item.get("index", -1))
          if idx < 0 or idx >= len(batch_indices):
            continue
          orig_idx = batch_indices[idx]
          rrf_merge(rrf_scores, rank_idx, orig_idx)

      if not rrf_scores:
        log("[WARN] 本次 query 未得到有效 rerank 结果，跳过。")
        continue
    finally:
      group_end()

    if not rrf_scores:
      continue

    sorted_items = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    if top_n is not None:
      sorted_items = sorted_items[:top_n]

    rrf_values = [v for _, v in sorted_items]
    min_rrf = min(rrf_values)
    max_rrf = max(rrf_values)
    denom = max_rrf - min_rrf if max_rrf > min_rrf else 1.0

    ranked_for_query: List[Dict[str, Any]] = []
    for idx, rrf_score in sorted_items:
      norm_score = (rrf_score - min_rrf) / denom
      paper_id = top_ids[idx]
      ranked_for_query.append(
        {
          "paper_id": paper_id,
          "score": norm_score,
          "star_rating": score_to_stars(norm_score),
        }
      )

    ranked_for_query.sort(key=lambda x: x["score"], reverse=True)
    q["ranked"] = ranked_for_query

  meta_generated_at = data.get("generated_at") or ""
  data["reranked_at"] = datetime.now(timezone.utc).isoformat()
  data["generated_at"] = meta_generated_at

  save_json(data, output_path)
  group_end()


def main() -> None:
  parser = argparse.ArgumentParser(
    description="步骤 3：使用 LLM Chat 接口对候选论文做重排序。",
  )
  parser.add_argument(
    "--input",
    type=str,
    default=os.path.join(FILTERED_DIR, f"arxiv_papers_{TODAY_STR}.json"),
    help="筛选结果 JSON 路径。",
  )
  parser.add_argument(
    "--output",
    type=str,
    default=os.path.join(RANKED_DIR, f"arxiv_papers_{TODAY_STR}.json"),
    help="打分后的输出 JSON 路径。",
  )
  parser.add_argument(
    "--top-n",
    type=int,
    default=None,
    help="最终保留的 Top N（默认保留全部候选）。",
  )
  parser.add_argument(
    "--rerank-model",
    type=str,
    default=os.getenv("LLM_MODEL") or os.getenv("BLT_RERANK_MODEL") or os.getenv("RERANK_MODEL") or "glm-4-flash",
    help="LLM 模型名称（用于 Chat 接口 Rerank）。",
  )

  args = parser.parse_args()

  input_path = args.input
  if not os.path.isabs(input_path):
    input_path = os.path.abspath(os.path.join(ROOT_DIR, input_path))

  output_path = args.output
  if not os.path.isabs(output_path):
    output_path = os.path.abspath(os.path.join(ROOT_DIR, output_path))

  if not os.path.exists(input_path):
    log(f"[WARN] 输入文件不存在（今天可能没有新论文）：{input_path}，将跳过 Step 3。")
    return

  # 从环境变量读取 LLM 配置
  api_key = os.getenv("LLM_API_KEY") or os.getenv("BLT_API_KEY")
  base_url = os.getenv("LLM_BASE_URL") or os.getenv("BLT_BASE_URL")
  model = os.getenv("LLM_MODEL") or args.rerank_model

  if not api_key:
    raise RuntimeError("缺少 LLM_API_KEY 环境变量，无法调用 LLM API。")

  if not base_url:
    raise RuntimeError("缺少 LLM_BASE_URL 环境变量，无法调用 LLM API。")

  log(f"[INFO] 使用 LLM 配置：model={model}, base_url={base_url}")

  reranker = LLMClient(api_key=api_key, model=model, base_url=base_url)
  process_file(
    reranker=reranker,
    input_path=input_path,
    output_path=output_path,
    top_n=args.top_n,
    rerank_model=model,
  )


if __name__ == "__main__":
  main()
