import json
import os
import time
from typing import List, Dict, Tuple, Any, Optional

import requests

"""
统一的 LLM 客户端封装。

提供商/模型命名规则：'provider/model'，provider 大小写不敏感，model 保留大小写与路径。
当前支持：deepseek、siliconflow、ollama、blt、cstcloud（科技云）。
"""

# 单次实验级别的全局 token 统计（需由调用方在实验开始前手动 reset）
GLOBAL_TOKENS = {
    'prompt': 0,    # 提示词（prompt）部分 token
    'thinking': 0,  # 推理/思维链部分 token（reasoning_tokens）
    'content': 0,   # 可见输出部分 token（completion_tokens - reasoning_tokens）
    'total': 0,     # provider 返回的总 token（通常含 prompt + completion）
}
# 单次实验级别的全局时间统计（秒）
GLOBAL_TIME_SECONDS: float = 0.0

PRIMARY_LLM_BASE_URL = "https://api.gptbest.vip/v1"
DEFAULT_BLT_BASE_URL = "https://api.bltcy.ai/v1"


def reset_global_tokens():
    """重置本次实验的全局 token 统计。"""
    GLOBAL_TOKENS['prompt'] = 0
    GLOBAL_TOKENS['thinking'] = 0
    GLOBAL_TOKENS['content'] = 0
    GLOBAL_TOKENS['total'] = 0


def get_global_tokens() -> Dict[str, int]:
    """获取本次实验的全局 token 统计（thinking/content/total）。"""
    return dict(GLOBAL_TOKENS)


def reset_global_time():
    """重置本次实验的大模型总耗时统计（秒）。"""
    global GLOBAL_TIME_SECONDS
    GLOBAL_TIME_SECONDS = 0.0


def get_global_time() -> float:
    """获取本次实验的大模型总耗时（秒）。"""
    return float(GLOBAL_TIME_SECONDS)


class LLMClient:
    tokens = {
        'prompt': 0,
        'content': 0,
        'reasoning': 0,
        'total': 0,
    }

    def __init__(self, api_key: str, model: str, base_url: str):
        """
        初始化 LLM 客户端。

        :param api_key: API 密钥
        :param model: 模型名称
        :param base_url: API 的基础 URL（会自动移除末尾的 /chat/completions）
        """
        # 自动移除 base_url 末尾的 /chat/completions 避免重复
        clean_base_url = base_url
        # 移除 /chat/completions 或 /v1/chat/completions 等后缀
        import re
        clean_base_url = re.sub(r'/chat/completions/?$', '', clean_base_url)
        clean_base_url = re.sub(r'/v\d+/chat/completions/?$', '', clean_base_url)

        self.api_key = api_key
        self.model = model
        self.base_url = clean_base_url
        self._base_urls = self._normalize_base_urls([clean_base_url])
        # 实例级别的累计统计（无需显式 reset；通常每个实验构造一个 client）
        self._call_index = 0
        self._cum_tokens = {
            'prompt': 0,
            'thinking': 0,
            'content': 0,
            'total': 0,
        }
        # 实例级别的累计耗时（秒）
        self._cum_time_seconds: float = 0.0
        self.kwargs: Dict[str, Any] = {
            'max_tokens': 4000,  # 更安全的默认值，避免超过部分模型上限
            'temperature': 0.6,
            'top_p': 0.3,
            'top_k': 50,
            'frequency_penalty': 0.5,
            'n': 1,
            'stream': False,
        }

    @staticmethod
    def _normalize_base_urls(urls: List[str | None]) -> List[str]:
        out: List[str] = []
        for url in urls:
            if not url:
                continue
            candidate = str(url).strip().rstrip("/")
            if candidate and candidate not in out:
                out.append(candidate)
        return out

    def _iter_request_bases(self) -> List[str]:
        return self._normalize_base_urls(self._base_urls)

    def _iter_retry_bases(self, total_attempts: int = 6) -> List[str]:
        bases = self._iter_request_bases()
        if total_attempts <= 0:
            return []
        if not bases:
            return []

        if len(bases) == 1:
            return [bases[0]] * total_attempts

        attempts: List[str] = []
        for idx in range(total_attempts):
            attempts.append(bases[idx % len(bases)])
        return attempts

    def _provider_name(self, base_url: str | None = None) -> str:
        try:
            url = (base_url or self.base_url or '').lower()
            if 'deepseek' in url:
                return 'deepseek'
            if 'siliconflow' in url or 'siliconflow.cn' in url:
                return 'siliconflow'
            if 'gptbest' in url:
                return 'blt'
            if 'bltcy' in url or 'blt' in url:
                return 'blt'
            if 'ollama' in url or 'localhost' in url:
                return 'ollama'
            if 'cstcloud' in url or 'uni-api.cstcloud.cn' in url:
                return 'cstcloud'
            if 'glm' in url or 'bigmodel' in url or 'zhipu' in url:
                return 'glm'
        except Exception:
            pass
        return 'llm'

    def chat(self, messages: List[Dict[str, str]], response_format: Optional[Dict[str, Any]] = None) -> dict:
        """
        统一 Chat Completions 请求。

        :param messages: OpenAI 格式的消息列表
        :param response_format: 可选，结构化输出配置（柏拉图支持）
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        model_name = self.model
        if 'qwen3' in model_name.lower():
            if '/think' in model_name:
                self.kwargs['enable_thinking'] = True
                model_name = model_name.replace('/think', '')
            else:
                self.kwargs['enable_thinking'] = False
                model_name = model_name.replace('/think', '')

        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": messages,
        }
        # 仅透传 OpenAI Chat Completions 兼容字段，避免提供商拒绝未知参数
        allowed_keys = {
            'max_tokens', 'temperature', 'top_p', 'n', 'stream',
            'presence_penalty', 'frequency_penalty', 'stop', 'logprobs',
            'tools', 'tool_choice', 'logit_bias',
            'response_format',
        }
        if isinstance(self.kwargs, dict):
            for k, v in self.kwargs.items():
                if k in allowed_keys:
                    payload[k] = v
        if response_format is not None:
            payload['response_format'] = response_format

        # 对输出 token 上限做保护（部分模型 4k 上限，统一取不超过 10000）
        try:
            if isinstance(payload.get('max_tokens'), int) and payload['max_tokens'] > 10000:
                payload['max_tokens'] = 10000
        except Exception:
            pass

        start_time = time.time()
        request_bases = self._iter_retry_bases(total_attempts=6)
        last_error: Exception | None = None
        for attempt_idx, req_base in enumerate(request_bases, start=1):
            request_url = f"{req_base.rstrip('/')}/chat/completions"
            try:
                response = requests.post(request_url, headers=headers, json=payload, timeout=120)
                response.raise_for_status()
                try:
                    response_data = response.json()
                except ValueError:
                    print("API 响应无法解析为 JSON，原始文本预览:", response.text[:500])
                    raise

                debug_raw = os.getenv("BLT_DEBUG_RAW") == "1" or os.getenv("LLM_DEBUG_RAW") == "1"
                if debug_raw and self._provider_name(req_base) == "blt":
                    print("[DEBUG] BLT 原始响应包:", response.text)

                if isinstance(response_data, dict) and 'error' in response_data:
                    err = response_data.get('error') or {}
                    print("API 返回错误:", {
                        'type': err.get('type'),
                        'code': err.get('code'),
                        'message': err.get('message') or err,
                    })
                    raise requests.exceptions.HTTPError(f"API error: {err}")

                if 'choices' not in response_data or not response_data['choices']:
                    print("API 响应不包含 choices 字段或为空：", str(response_data)[:500])
                    raise requests.exceptions.HTTPError("API response missing choices")

                message = response_data['choices'][0].get('message', {})
                content = message.get('content', '') or ''
                reasoning_content = message.get('reasoning_content', '') or ''

                usage = response_data.get('usage', {})
                prompt_tokens = usage.get('prompt_tokens', 0)
                completion_tokens = usage.get('completion_tokens', 0)
                total_tokens = usage.get('total_tokens', 0)
                reasoning_tokens = 0
                if 'completion_tokens_details' in usage:
                    reasoning_tokens = usage['completion_tokens_details'].get('reasoning_tokens', 0)

                self.tokens['prompt'] += prompt_tokens
                self.tokens['content'] += completion_tokens - reasoning_tokens
                self.tokens['reasoning'] += reasoning_tokens
                self.tokens['total'] += total_tokens

                try:
                    GLOBAL_TOKENS['prompt'] += int(prompt_tokens)
                    GLOBAL_TOKENS['thinking'] += int(reasoning_tokens)
                    GLOBAL_TOKENS['content'] += int(completion_tokens - reasoning_tokens)
                    GLOBAL_TOKENS['total'] += int(total_tokens)
                except Exception:
                    pass

                try:
                    elapsed = time.time() - start_time
                    self._cum_time_seconds += float(elapsed)
                    try:
                        global GLOBAL_TIME_SECONDS
                        GLOBAL_TIME_SECONDS += float(elapsed)
                    except Exception:
                        pass

                    self._call_index += 1
                    self._cum_tokens['prompt'] += int(prompt_tokens)
                    self._cum_tokens['thinking'] += int(reasoning_tokens)
                    self._cum_tokens['content'] += int(completion_tokens - reasoning_tokens)
                    self._cum_tokens['total'] += int(total_tokens)

                    provider = self._provider_name(req_base)
                    header = f"[{provider}][{self.model}] 第{self._call_index}次"
                    line_cur = (
                        f"本次 tokens：prompt={int(prompt_tokens)}, thinking={int(reasoning_tokens)}, "
                        f"content={int(completion_tokens - reasoning_tokens)}, total={int(total_tokens)}"
                    )
                    line_cum = (
                        f"累计 tokens：prompt={self._cum_tokens['prompt']}, thinking={self._cum_tokens['thinking']}, "
                        f"content={self._cum_tokens['content']}, total={self._cum_tokens['total']}"
                    )
                    line_time = (
                        f"本次用时：{elapsed:.2f}s，"
                        f"累计用时：{self._cum_time_seconds:.2f}s"
                    )
                    print(header + "\n" + line_cur + "\n" + line_cum + "\n" + line_time)
                except Exception:
                    pass

                return {
                    "content": content,
                    "reasoning_content": reasoning_content,
                    "tokens": {
                        "prompt": prompt_tokens,
                        "content": completion_tokens - reasoning_tokens,
                        "reasoning": reasoning_tokens,
                        "total": total_tokens
                    }
                }

            except Exception as e:
                last_error = e
                if attempt_idx < len(request_bases):
                    next_base = request_bases[attempt_idx] if attempt_idx < len(request_bases) else ''
                    print(
                        f"请求失败（base={req_base}，第 {attempt_idx} 次），"
                        f"将回退到 {next_base}"
                    )
                    if hasattr(e, "response") and e.response is not None:
                        try:
                            print("错误详情(JSON):", e.response.json())
                        except ValueError:
                            try:
                                print("错误详情(TEXT):", e.response.text[:500])
                            except Exception:
                                pass
                    continue
                print(f"通过 requests 调用 API 时出错: {e}")
                if hasattr(e, "response") and e.response is not None:
                    try:
                        print("错误详情(JSON):", e.response.json())
                    except ValueError:
                        try:
                            print("错误详情(TEXT):", e.response.text[:500])
                        except Exception:
                            pass
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("LLM 请求未命中可用 base")

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
        model: Optional[str] = None,
    ) -> dict:
        """重排序接口（默认不支持，只有 BLT 提供）。"""
        raise NotImplementedError("rerank 仅支持 BltClient，请使用 BltClient 调用。")


class DeepSeekClient(LLMClient):
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.deepseek.com"):
        super().__init__(api_key=api_key, model=model, base_url=base_url)


class SiliconflowClient(LLMClient):
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.siliconflow.cn/v1"):
        super().__init__(api_key=api_key, model=model, base_url=base_url)


class CSTCloudClient(LLMClient):
    """CSTCloud（科技云）提供商，OpenAI Chat Completions 兼容接口。

    默认基址：https://uni-api.cstcloud.cn/v1
    使用示例：model="CSTCloud/gpt-oss-120b" 或 "CSTCloud/qwen3:235b"
    建议环境变量：CSTCLOUD_API_KEY
    """
    def __init__(self, api_key: str, model: str, base_url: str = "https://uni-api.cstcloud.cn/v1"):
        super().__init__(api_key=api_key, model=model, base_url=base_url)


SliconflowClient = SiliconflowClient


class OllamaClient(LLMClient):
    def __init__(self, api_key: str, model: str, base_url: str = "http://localhost:11111/v1"):
        super().__init__(api_key=api_key, model=model, base_url=base_url)


class GLMClient(LLMClient):
    """GLM (智谱) 提供商，OpenAI Chat Completions 兼容接口。

    默认基址：https://open.bigmodel.cn/api/paas/v4
    使用示例：model="glm/glm-4-plus" 或 "glm/glm-4-flash"
    建议环境变量：GLM_API_KEY
    """
    # def __init__(self, api_key: str, model: str, base_url: str = "https://open.bigmodel.cn/api/paas/v4"):
    def __init__(self, api_key: str, model: str, base_url: str = "https://open.bigmodel.cn/api/paas/v4"):
        super().__init__(api_key=api_key, model=model, base_url=base_url)


class BltClient(LLMClient):
    """BLT（柏拉图）网关，OpenAI Chat Completions 兼容接口。"""
    def __init__(self, api_key: str, model: str, base_url: str = None):
        legacy_base = base_url or os.getenv('BLT_API_BASE', DEFAULT_BLT_BASE_URL)
        primary_base = (
            os.getenv("LLM_PRIMARY_BASE_URL")
            or os.getenv("BLT_PRIMARY_BASE_URL")
            or os.getenv("GPTBEST_BASE_URL")
            or PRIMARY_LLM_BASE_URL
        ).strip() or PRIMARY_LLM_BASE_URL
        super().__init__(api_key=api_key, model=model, base_url=primary_base)
        self._base_urls = self._normalize_base_urls([primary_base, legacy_base])

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
        model: Optional[str] = None,
    ) -> dict:
        """
        调用柏拉图 Rerank 接口（/v1/rerank）。

        :param query: 查询文本
        :param documents: 待排序文档列表
        :param top_n: 返回的 Top N（可选）
        :param model: 重排模型名（可选，默认使用 self.model）
        """
        if not query:
            raise ValueError("rerank: query 不能为空")
        if not documents:
            raise ValueError("rerank: documents 不能为空")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            payload["top_n"] = int(top_n)

        request_bases = self._iter_retry_bases(total_attempts=6)
        last_error: Exception | None = None
        for attempt_idx, req_base in enumerate(request_bases, start=1):
            request_url = f"{req_base.rstrip('/')}/rerank"
            try:
                response = requests.post(request_url, headers=headers, json=payload, timeout=120)
                response.raise_for_status()
                try:
                    response_data = response.json()
                except ValueError:
                    print("Rerank 响应无法解析为 JSON，原始文本预览:", response.text[:500])
                    raise

                if isinstance(response_data, dict) and 'error' in response_data:
                    err = response_data.get('error') or {}
                    print("Rerank 返回错误:", {
                        'type': err.get('type'),
                        'code': err.get('code'),
                        'message': err.get('message') or err,
                    })
                    raise requests.exceptions.HTTPError(f"Rerank API error: {err}")

                return response_data
            except Exception as e:
                last_error = e
                if attempt_idx < len(request_bases):
                    next_base = request_bases[attempt_idx] if attempt_idx < len(request_bases) else ''
                    print(
                        f"Rerank 请求失败（base={req_base}，第 {attempt_idx} 次），"
                        f"将回退到 {next_base}"
                    )
                    continue
                print(f"通过 requests 调用 Rerank API 时出错: {e}")
                print("Rerank 请求摘要:", {
                    "url": request_url,
                    "model": payload.get("model"),
                    "query_len": len(query or ""),
                    "documents": len(documents),
                    "top_n": payload.get("top_n"),
                })
                if e.response is not None:
                    try:
                        print("错误详情(JSON):", e.response.json())
                    except ValueError:
                        try:
                            print("错误详情(TEXT):", e.response.text[:500])
                        except Exception:
                            pass
                else:
                    print("错误详情: 未收到服务端响应（可能是网络/SSL问题）。")
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("rerank 未命中可用 base")


def parse_provider_model(model_str: str) -> Tuple[str, str]:
    """
    解析模型字符串为 (provider, model)。

    规则：第一个 '/' 之前为提供商（大小写不敏感），之后的全部为模型名（大小写敏感，允许包含 '/').
    示例：
    - "deepseek/deepseek-chat" -> ("deepseek", "deepseek-chat")
    - "SiliconFlow/Qwen/Qwen3-8B" -> ("siliconflow", "Qwen/Qwen3-8B")
    - "ollama/llama3.1:8b" -> ("ollama", "llama3.1:8b")
    """
    if not isinstance(model_str, str) or '/' not in model_str:
        raise ValueError("缺少模型提供商：请使用 'provider/model' 格式，例如 'CSTCloud/gpt-oss-120b'")
    provider, model = model_str.split('/', 1)
    return provider.lower(), model


class ClientFactory:
    @staticmethod
    def from_env():
        """
        基于环境变量创建具体客户端。

        必填：
        - LLM_MODEL：形如 'provider/model'。
        选填：
        - LLM_API_KEY：通用 API key（优先级高于各 provider 专用 key）
        - LLM_BASE_URL：通用 base_url（优先级高于默认 base_url）
        """
        model_env = (os.getenv('LLM_MODEL') or '').strip()
        if not model_env:
            raise ValueError("缺少必要环境变量: LLM_MODEL（格式为 'provider/model'）")

        provider, model = parse_provider_model(model_env)
        api_key = (os.getenv('LLM_API_KEY') or '').strip() or None
        base_url = (os.getenv('LLM_BASE_URL') or '').strip() or None

        if provider == 'deepseek':
            base_url = base_url or "https://api.deepseek.com"
            return DeepSeekClient(api_key=api_key or os.getenv('DEEPSEEK_API_KEY', ''), model=model, base_url=base_url)
        if provider in ('siliconflow', 'silicon-flow', 'sflow'):
            base_url = base_url or "https://api.siliconflow.cn/v1"
            return SiliconflowClient(api_key=api_key or os.getenv('SILICONFLOW_API_KEY', ''), model=model, base_url=base_url)
        if provider == 'ollama':
            base_url = base_url or "http://localhost:11111/v1"
            return OllamaClient(api_key=api_key or '', model=model, base_url=base_url)
        if provider in ('blt', 'bltcy', 'plato'):
            return BltClient(api_key=api_key or os.getenv('BLT_API_KEY', ''), model=model, base_url=base_url or os.getenv('BLT_API_BASE', 'https://api.bltcy.ai/v1'))
        if provider in ('cstcloud', 'cst', 'cst-cloud', 'keji', 'keji-yun'):
            return CSTCloudClient(api_key=api_key or os.getenv('CSTCLOUD_API_KEY', ''), model=model, base_url=base_url or 'https://uni-api.cstcloud.cn/v1')
        if provider in ('glm', 'zhipu', 'bigmodel'):
            return GLMClient(api_key=api_key or os.getenv('GLM_API_KEY', ''), model=model, base_url=base_url or 'https://open.bigmodel.cn/api/paas/v4')
        raise ValueError(f"不支持的提供商: {provider}，请使用 'deepseek'、'siliconflow'、'blt'、'cstcloud'、'glm' 或 'ollama'")

    @staticmethod
    def from_config(_config: dict | None = None):
        """
        兼容旧调用入口，但不再读取 config 文件，统一从环境变量读取。
        """
        return ClientFactory.from_env()

    @staticmethod
    def from_config_file(config_path: str | None = None):
        """
        从配置文件创建客户端。

        支持两种配置格式：
        1. 新格式（推荐）：只需要 base_url, model, api_key
        2. 旧格式：provider + model + api_key (+ optional base_url)

        :param config_path: 配置文件路径，默认为根目录的 llm_config.json
        """
        config = load_llm_config(config_path)
        if config is None:
            # 回退到环境变量
            return ClientFactory.from_env()

        llm_config = config.get('llm', {})

        # 新格式：直接使用 base_url, model, api_key
        base_url = llm_config.get('base_url', '')
        model = llm_config.get('model', '')
        api_key = llm_config.get('api_key', '')

        # 旧格式：provider + model
        provider = llm_config.get('provider', '')

        # 环境变量优先级高于配置文件
        api_key = os.getenv('LLM_API_KEY', '').strip() or api_key
        base_url = os.getenv('LLM_BASE_URL', '').strip() or base_url

        # 判断使用哪种格式
        if base_url and model:
            # 新格式：从 base_url 推断提供商
            provider_inferred = CustomClient._infer_provider_from_url(base_url)

            if provider_inferred == 'deepseek':
                return DeepSeekClient(api_key=api_key, model=model, base_url=base_url)
            if provider_inferred == 'siliconflow':
                return SiliconflowClient(api_key=api_key, model=model, base_url=base_url)
            if provider_inferred == 'ollama':
                return OllamaClient(api_key=api_key, model=model, base_url=base_url)
            if provider_inferred == 'blt':
                return BltClient(api_key=api_key, model=model, base_url=base_url)
            if provider_inferred == 'cstcloud':
                return CSTCloudClient(api_key=api_key, model=model, base_url=base_url)
            if provider_inferred == 'glm':
                return GLMClient(api_key=api_key, model=model, base_url=base_url)

            # 通用回退：使用 LLMClient
            return LLMClient(api_key=api_key, model=model, base_url=base_url)

        # 旧格式兼容
        if provider and model:
            if provider == 'glm':
                base_url = base_url or 'https://open.bigmodel.cn/api/paas/v4'
                return GLMClient(api_key=api_key, model=model, base_url=base_url)
            if provider == 'deepseek':
                base_url = base_url or 'https://api.deepseek.com'
                return DeepSeekClient(api_key=api_key, model=model, base_url=base_url)
            if provider == 'siliconflow':
                base_url = base_url or 'https://api.siliconflow.cn/v1'
                return SiliconflowClient(api_key=api_key, model=model, base_url=base_url)
            if provider == 'ollama':
                base_url = base_url or 'http://localhost:11111/v1'
                return OllamaClient(api_key=api_key, model=model, base_url=base_url)
            if provider == 'blt':
                return BltClient(api_key=api_key, model=model, base_url=base_url)
            if provider == 'cstcloud':
                base_url = base_url or 'https://uni-api.cstcloud.cn/v1'
                return CSTCloudClient(api_key=api_key, model=model, base_url=base_url)

        raise ValueError("配置文件缺少必要的字段（需要 base_url + model，或 provider + model）")


def get_default_config_path() -> str:
    """获取默认配置文件路径（项目根目录的 llm_config.json）"""
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root_dir, "llm_config.json")


def load_llm_config(config_path: str | None = None) -> dict | None:
    """
    加载 LLM 配置文件。

    :param config_path: 配置文件路径，默认为根目录的 llm_config.json
    :return: 配置字典，如果文件不存在则返回 None
    """
    if config_path is None:
        config_path = get_default_config_path()

    if not os.path.exists(config_path):
        return None

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] 读取配置文件失败: {e}")
        return None


class CustomClient:
    """
    从 llm_config.json 配置文件创建的 LLM 客户端封装。

    配置格式（简化版，兼容任何 OpenAI Chat Completions 格式的服务）：
    {
      "llm": {
        "base_url": "https://api.example.com/v1",
        "model": "model-name",
        "api_key": "your-api-key"
      },
      "rerank": {
        "enabled": false
      }
    }

    使用示例：
        client = CustomClient()
        resp = client.chat(messages)
        if client.has_rerank():
            results = client.rerank(query, documents)
    """

    def __init__(self, config_path: str | None = None):
        """
        初始化自定义客户端。

        :param config_path: 配置文件路径，默认为根目录的 llm_config.json
        """
        self._config = load_llm_config(config_path)
        if self._config is None:
            raise FileNotFoundError(
                f"配置文件不存在: {config_path or get_default_config_path()}\n"
                f"请从 llm_config.json.template 复制并填写你的配置。"
            )

        # 创建 LLM 客户端
        llm_config = self._config.get('llm', {})
        base_url = llm_config.get('base_url', '')
        model = llm_config.get('model', '')
        api_key = llm_config.get('api_key', '')

        # 环境变量优先
        api_key = os.getenv('LLM_API_KEY', '').strip() or api_key
        base_url = os.getenv('LLM_BASE_URL', '').strip() or base_url

        if not base_url or not model:
            raise ValueError("配置文件缺少必要的 base_url 或 model 字段")

        self._llm_base_url = base_url
        self._llm_model = model

        # 从 base_url 推断提供商类型（用于日志显示和特殊处理）
        self._llm_provider = self._infer_provider_from_url(base_url)

        # 根据提供商创建对应的客户端（如果有特定需求）
        # 否则使用通用 LLMClient
        if self._llm_provider == 'deepseek':
            self._client = DeepSeekClient(api_key=api_key, model=model, base_url=base_url)
        elif self._llm_provider == 'siliconflow':
            self._client = SiliconflowClient(api_key=api_key, model=model, base_url=base_url)
        elif self._llm_provider == 'ollama':
            self._client = OllamaClient(api_key=api_key, model=model, base_url=base_url)
        elif self._llm_provider == 'blt':
            self._client = BltClient(api_key=api_key, model=model, base_url=base_url)
        elif self._llm_provider == 'cstcloud':
            self._client = CSTCloudClient(api_key=api_key, model=model, base_url=base_url)
        elif self._llm_provider == 'glm':
            self._client = GLMClient(api_key=api_key, model=model, base_url=base_url)
        else:
            # 通用回退：使用 LLMClient
            self._client = LLMClient(api_key=api_key, model=model, base_url=base_url)

        # Rerank 配置
        self._rerank_mode: str = 'disabled'
        rerank_config = self._config.get('rerank', {})

        if rerank_config.get('enabled', False):
            # 使用 Chat 接口实现 Rerank（复用主客户端）
            self._rerank_mode = 'chat'

    @staticmethod
    def _infer_provider_from_url(base_url: str) -> str:
        """从 base_url 推断提供商类型"""
        url = str(base_url).lower()
        if 'deepseek' in url:
            return 'deepseek'
        if 'siliconflow' in url or 'siliconflow.cn' in url:
            return 'siliconflow'
        if 'gptbest' in url or 'bltcy' in url or 'blt' in url:
            return 'blt'
        if 'ollama' in url or 'localhost' in url or '127.0.0.1' in url:
            return 'ollama'
        if 'cstcloud' in url or 'uni-api.cstcloud.cn' in url:
            return 'cstcloud'
        if 'glm' in url or 'bigmodel' in url or 'zhipu' in url:
            return 'glm'
        return 'generic'

    def chat(self, messages: List[Dict[str, str]], response_format: Optional[Dict[str, Any]] = None) -> dict:
        """
        Chat Completions 请求。

        :param messages: OpenAI 格式的消息列表
        :param response_format: 可选，结构化输出配置
        """
        return self._client.chat(messages, response_format=response_format)

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
        model: Optional[str] = None,
    ) -> dict:
        """
        重排序请求。

        :param query: 查询文本
        :param documents: 待排序文档列表
        :param top_n: 返回的 Top N
        :param model: 重排模型名（仅 blt 模式有效）
        """
        if self._rerank_mode == 'disabled':
            raise NotImplementedError("Rerank 未启用。请在 llm_config.json 中设置 rerank.enabled=true")

        if self._rerank_mode == 'blt' and self._rerank_client:
            # 使用 BLT 专用 Rerank API
            return self._rerank_client.rerank(query, documents, top_n=top_n, model=model)

        # 使用 Chat 接口实现 Rerank
        if self._rerank_mode == 'chat':
            return self._rerank_by_chat(query, documents, top_n)

        raise NotImplementedError(f"Rerank 模式 '{self._rerank_mode}' 未正确配置")

    def _rerank_by_chat(
        self,
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

请以 JSON 格式返回评分结果，格式如下：
{{
  "results": [
    {{"index": 0, "relevance_score": 0.95}},
    {{"index": 1, "relevance_score": 0.75}}
  ]
}}

要求：
1. relevance_score 为 0-1 之间的分数
2. 只返回 JSON，不要其他说明"""

        try:
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )

            content = response.get("content", "")

            # 尝试解析 JSON
            try:
                result = json.loads(content)
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

            except json.JSONDecodeError:
                # JSON 解析失败，回退到简单评分
                print("[WARN] Rerank JSON 解析失败，回退到原始顺序")
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
            print(f"[WARN] Chat Rerank 失败: {e}，回退到原始顺序")
            return {
                "results": [
                    {"index": i, "relevance_score": 1.0 - (i * 0.01), "document": doc}
                    for i, doc in enumerate(documents)
                ]
            }

    def has_rerank(self) -> bool:
        """是否配置了 Rerank 客户端"""
        return self._rerank_mode != 'disabled'

    @property
    def rerank_mode(self) -> str:
        """返回 Rerank 模式：disabled, blt, chat"""
        return self._rerank_mode

    @property
    def provider(self) -> str:
        """当前 LLM 提供商"""
        return self._llm_provider

    @property
    def model(self) -> str:
        """当前 LLM 模型"""
        return self._llm_model

    @property
    def tokens(self) -> Dict[str, int]:
        """获取 token 统计"""
        return self._client.tokens
