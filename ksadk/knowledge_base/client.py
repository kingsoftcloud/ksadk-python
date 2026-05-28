"""KnowledgeBaseClient - 金山云知识库检索客户端

封装 AICP RetrieveKnowledge API，通过 kingsoftcloud SDK 调用。
支持从环境变量自动配置，由各框架 Runner 自动集成。

环境变量:
    KSADK_KB_DATASET_ID: 知识库 ID (必填，存在即启用)
    KSADK_KB_ACCESS_KEY: AK (可选，默认取 KSYUN_ACCESS_KEY)
    KSADK_KB_SECRET_KEY: SK (可选，默认取 KSYUN_SECRET_KEY)
    KSADK_KB_REGION: 区域 (默认 cn-beijing-6)
    KSADK_KB_ENDPOINT: API 端点 (默认 aicp.api.ksyun.com)
    KSADK_KB_TOP_K: 返回结果数 (默认 5)
    KSADK_KB_SEARCH_METHOD: 检索方法 (默认 intelligence_search)
    KSADK_KB_SCORE_THRESHOLD: 分数阈值 (可选)
    KSADK_KB_RERANKING_ENABLE: 是否启用重排序 (默认 false)

使用示例:
    # 自动从环境变量配置 (推荐)
    client = KnowledgeBaseClient.from_env()
    results = client.search("如何使用知识库？")

    # 手动创建
    client = KnowledgeBaseClient(
        dataset_id="your-kb-id",
        access_key="ak",
        secret_key="sk",
    )
"""

import json
import logging
import os
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict

from ksadk.common.aicp_env import resolve_aicp_connection

logger = logging.getLogger(__name__)


class KnowledgeBaseResult(BaseModel):
    """单条知识库检索结果"""

    content: str = ""
    score: float = 0.0
    segment_id: str = ""
    document_id: str = ""
    document_name: str = ""
    position: int = 0
    answer: str = ""
    keywords: List[str] = []


class KnowledgeBaseClient(BaseModel):
    """金山云知识库检索客户端

    Attributes:
        dataset_id: 知识库 ID (DatasetId)
        access_key: 访问密钥 ID (AK)
        secret_key: 访问密钥 (SK)
        region: API 区域
        endpoint: API 端点
        top_k: 返回结果数
        search_method: 检索方法
        score_threshold: 分数阈值
        score_threshold_enabled: 是否启用阈值过滤
        reranking_enable: 是否启用重排序
    """

    dataset_id: str
    access_key: str = ""
    secret_key: str = ""
    region: str = "cn-beijing-6"
    endpoint: str = "aicp.api.ksyun.com"
    scheme: str = "https"
    top_k: int = 5
    search_method: str = "intelligence_search"
    score_threshold: float = 0.0
    score_threshold_enabled: bool = False
    reranking_enable: bool = False

    _aicp_client: Any = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def model_post_init(self, __context: Any) -> None:
        if not self.dataset_id:
            raise ValueError("dataset_id is required for KnowledgeBaseClient")

        if not self.access_key or not self.secret_key:
            logger.warning(
                "KnowledgeBaseClient: AK/SK not provided, "
                "API calls will fail. Set KSADK_KB_ACCESS_KEY/KSADK_KB_SECRET_KEY "
                "or KSYUN_ACCESS_KEY/KSYUN_SECRET_KEY."
            )

    def _get_client(self):
        """懒加载 AICP 客户端"""
        if self._aicp_client is not None:
            return self._aicp_client

        try:
            from ksyun.common import credential
            from ksyun.common.profile.client_profile import ClientProfile
            from ksyun.common.profile.http_profile import HttpProfile
        except ImportError:
            raise ImportError(
                "kingsoftcloud-sdk-python is required for knowledge base. "
                "Install it with: pip install kingsoftcloud-sdk-python"
            )

        # 尝试导入 aicp client (多版本 fallback)
        aicp_module = None
        for version in ["v20251114", "v20251212", "v20240612"]:
            try:
                aicp_module = __import__(
                    f"ksyun.client.aicp.{version}.client",
                    fromlist=["AicpClient"],
                )
                logger.debug(f"Using aicp client version: {version}")
                break
            except ImportError:
                continue

        if aicp_module is None:
            raise ImportError(
                "Cannot import ksyun.client.aicp client. "
                "Ensure kingsoftcloud-sdk-python is installed and up to date."
            )

        cred = credential.Credential(self.access_key, self.secret_key)

        http_profile = HttpProfile()
        http_profile.endpoint = self.endpoint
        http_profile.reqMethod = "POST"
        http_profile.reqTimeout = 60
        http_profile.scheme = self.scheme

        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile

        self._aicp_client = aicp_module.AicpClient(
            cred, self.region, profile=client_profile
        )

        # 强制覆写 API 版本为 RetrieveKnowledge 所需的 2025-11-14
        # SDK 的 _apiVersion 由导入的模块版本决定，可能不匹配
        self._aicp_client._apiVersion = "2025-11-14"

        logger.info(
            f"KnowledgeBaseClient initialized: "
            f"dataset_id={self.dataset_id}, region={self.region}, "
            f"endpoint={self.endpoint}"
        )
        return self._aicp_client

    def _build_params(self, query: str, top_k: Optional[int] = None) -> dict:
        """构建 RetrieveKnowledge 请求参数 (JSON 嵌套格式)"""
        effective_top_k = top_k if top_k is not None else self.top_k

        params = {
            "DatasetId": self.dataset_id,
            "Query": query,
            "RetrievalModel": {
                "SearchMethod": self.search_method,
                "TopK": effective_top_k,
                "RerankingEnable": self.reranking_enable,
            },
        }

        if self.score_threshold_enabled:
            params["RetrievalModel"]["ScoreThresholdEnabled"] = True
            params["RetrievalModel"]["ScoreThreshold"] = self.score_threshold

        return params

    def _parse_response(self, response: str) -> List[KnowledgeBaseResult]:
        """解析 RetrieveKnowledge 响应"""
        try:
            data = json.loads(response) if isinstance(response, str) else response
        except (json.JSONDecodeError, TypeError):
            logger.error(f"Failed to parse response: {str(response)[:200]}")
            return []

        records = data.get("Records", [])
        results = []

        for record in records:
            segment = record.get("Segment", {})
            document = segment.get("Document", {})

            results.append(
                KnowledgeBaseResult(
                    content=segment.get("Content", ""),
                    score=record.get("Score", 0.0),
                    segment_id=segment.get("Id", ""),
                    document_id=segment.get("DocumentId", ""),
                    document_name=document.get("Name", ""),
                    position=segment.get("Position", 0),
                    answer=segment.get("Answer", ""),
                    keywords=segment.get("Keywords", []),
                )
            )

        return results

    def search(
        self, query: str, top_k: Optional[int] = None
    ) -> List[KnowledgeBaseResult]:
        """检索知识库

        Args:
            query: 检索关键词
            top_k: 返回结果数 (覆盖默认值)

        Returns:
            匹配的文档片段列表
        """
        client = self._get_client()
        params = self._build_params(query, top_k)

        logger.info(
            f"Searching knowledge base: dataset_id={self.dataset_id}, "
            f"query='{query[:50]}'"
        )

        try:
            response = client.call(
                "RetrieveKnowledge", params, options={"IsPostJson": True}
            )
            results = self._parse_response(response)
            logger.info(
                f"Knowledge base returned {len(results)} results "
                f"for query='{query[:50]}'"
            )
            return results
        except Exception as e:
            logger.error(f"Knowledge base search failed: {e}")
            raise

    @classmethod
    def from_env(cls) -> "KnowledgeBaseClient":
        """从环境变量创建 KnowledgeBaseClient

        Returns:
            KnowledgeBaseClient 实例

        Raises:
            ValueError: 如果 KSADK_KB_DATASET_ID 未设置
        """
        dataset_id = os.environ.get("KSADK_KB_DATASET_ID", "")
        if not dataset_id:
            raise ValueError(
                "KSADK_KB_DATASET_ID environment variable is required "
                "to enable knowledge base."
            )

        access_key = (
            os.environ.get("KSADK_KB_ACCESS_KEY")
            or os.environ.get("KSYUN_ACCESS_KEY")
            or os.environ.get("KSYUN_SECRET_ID", "")
        )
        secret_key = (
            os.environ.get("KSADK_KB_SECRET_KEY")
            or os.environ.get("KSYUN_SECRET_KEY")
            or os.environ.get("KSYUN_SECRET_KEY", "")
        )

        score_threshold_str = os.environ.get("KSADK_KB_SCORE_THRESHOLD", "")
        score_threshold = float(score_threshold_str) if score_threshold_str else 0.0
        score_threshold_enabled = bool(score_threshold_str)

        reranking_str = os.environ.get("KSADK_KB_RERANKING_ENABLE", "false")
        reranking_enable = reranking_str.lower() in ("true", "1", "yes")
        connection = resolve_aicp_connection("KSADK_KB")

        return cls(
            dataset_id=dataset_id,
            access_key=access_key,
            secret_key=secret_key,
            region=connection["region"],
            endpoint=connection["endpoint"],
            scheme=connection["scheme"],
            top_k=int(os.environ.get("KSADK_KB_TOP_K", "5")),
            search_method=os.environ.get(
                "KSADK_KB_SEARCH_METHOD", "intelligence_search"
            ),
            score_threshold=score_threshold,
            score_threshold_enabled=score_threshold_enabled,
            reranking_enable=reranking_enable,
        )

    @staticmethod
    def is_configured() -> bool:
        """检查环境变量是否已配置知识库"""
        return bool(os.environ.get("KSADK_KB_DATASET_ID"))
