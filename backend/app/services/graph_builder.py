"""
Graph builder service: builds a standalone Zep knowledge graph.

Driven in-process by the cart-recovery pipeline via build_graph_sync.
"""

import os
import uuid
import time
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass

from zep_cloud.client import Zep
from zep_cloud import EpisodeData, EntityEdgeSourceTarget

from ..config import Config
from ..utils.zep_paging import fetch_all_nodes, fetch_all_edges
from .text_processor import TextProcessor


@dataclass
class GraphInfo:
    """图谱信息"""
    graph_id: str
    node_count: int
    edge_count: int
    entity_types: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "entity_types": self.entity_types,
        }


class GraphBuilderService:
    """
    图谱构建服务
    负责调用Zep API构建知识图谱
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or Config.ZEP_API_KEY
        if not self.api_key:
            raise ValueError("ZEP_API_KEY 未配置")
        
        self.client = Zep(api_key=self.api_key)

    def build_graph_sync(
        self,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        batch_size: int = 3,
        progress_callback: Optional[Callable[[str, int], None]] = None,
        on_graph_created: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """
        Build the knowledge graph synchronously: chunk -> create -> set ontology
        -> batch-write -> wait for processing -> read graph data.

        Now called only by the in-process cart-recovery orchestrator (issue #19);
        the /graph/build async route that previously shared it was removed with
        the OASIS endpoints (#24 prune).

        progress_callback(message, percent) drives the 0-100 progress scale;
        on_graph_created(graph_id) fires immediately after creation so the caller
        can persist graph_id before the later steps run.

        Returns:
            {graph_id, graph_data, node_count, edge_count, chunk_count}
        """
        def _progress(message: str, percent: int):
            if progress_callback:
                progress_callback(message, percent)

        # 文本分块
        _progress("文本分块中...", 5)
        chunks = TextProcessor.split_text(text, chunk_size=chunk_size, overlap=chunk_overlap)
        total_chunks = len(chunks)

        # 创建图谱
        _progress("创建Zep图谱...", 10)
        graph_id = self.create_graph(name=graph_name)
        if on_graph_created:
            on_graph_created(graph_id)

        # 设置本体
        _progress("设置本体定义...", 15)
        self.set_ontology(graph_id, ontology)

        # 添加文本（add_text_batches 的 progress_callback 签名是 (msg, progress_ratio)）
        _progress(f"开始添加 {total_chunks} 个文本块...", 15)
        episode_uuids = self.add_text_batches(
            graph_id,
            chunks,
            batch_size=batch_size,
            progress_callback=lambda msg, ratio: _progress(msg, 15 + int(ratio * 40)),  # 15%-55%
        )

        # 等待Zep处理完成
        _progress("等待Zep处理数据...", 55)
        self._wait_for_episodes(
            episode_uuids,
            lambda msg, ratio: _progress(msg, 55 + int(ratio * 35)),  # 55%-90%
        )

        # 获取图谱数据
        _progress("获取图谱数据...", 95)
        graph_data = self.get_graph_data(graph_id)

        return {
            "graph_id": graph_id,
            "graph_data": graph_data,
            "node_count": graph_data.get("node_count", 0),
            "edge_count": graph_data.get("edge_count", 0),
            "chunk_count": total_chunks,
        }

    def create_graph(self, name: str) -> str:
        """创建Zep图谱（公开方法）"""
        graph_id = f"mirofish_{uuid.uuid4().hex[:16]}"
        
        self.client.graph.create(
            graph_id=graph_id,
            name=name,
            description="MiroFish Social Simulation Graph"
        )
        
        return graph_id
    
    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]):
        """设置图谱本体（公开方法）"""
        import warnings
        from typing import Optional
        from pydantic import Field
        from zep_cloud.external_clients.ontology import EntityModel, EntityText, EdgeModel
        
        # 抑制 Pydantic v2 关于 Field(default=None) 的警告
        # 这是 Zep SDK 要求的用法，警告来自动态类创建，可以安全忽略
        warnings.filterwarnings('ignore', category=UserWarning, module='pydantic')
        
        # Zep 保留名称，不能作为属性名
        RESERVED_NAMES = {'uuid', 'name', 'group_id', 'name_embedding', 'summary', 'created_at'}
        
        def safe_attr_name(attr_name: str) -> str:
            """将保留名称转换为安全名称"""
            if attr_name.lower() in RESERVED_NAMES:
                return f"entity_{attr_name}"
            return attr_name
        
        # 动态创建实体类型
        entity_types = {}
        for entity_def in ontology.get("entity_types", []):
            name = entity_def["name"]
            description = entity_def.get("description", f"A {name} entity.")
            
            # 创建属性字典和类型注解（Pydantic v2 需要）
            attrs = {"__doc__": description}
            annotations = {}
            
            for attr_def in entity_def.get("attributes", []):
                attr_name = safe_attr_name(attr_def["name"])  # 使用安全名称
                attr_desc = attr_def.get("description", attr_name)
                # Zep API 需要 Field 的 description，这是必需的
                attrs[attr_name] = Field(description=attr_desc, default=None)
                annotations[attr_name] = Optional[EntityText]  # 类型注解
            
            attrs["__annotations__"] = annotations
            
            # 动态创建类
            entity_class = type(name, (EntityModel,), attrs)
            entity_class.__doc__ = description
            entity_types[name] = entity_class
        
        # 动态创建边类型
        edge_definitions = {}
        for edge_def in ontology.get("edge_types", []):
            name = edge_def["name"]
            description = edge_def.get("description", f"A {name} relationship.")
            
            # 创建属性字典和类型注解
            attrs = {"__doc__": description}
            annotations = {}
            
            for attr_def in edge_def.get("attributes", []):
                attr_name = safe_attr_name(attr_def["name"])  # 使用安全名称
                attr_desc = attr_def.get("description", attr_name)
                # Zep API 需要 Field 的 description，这是必需的
                attrs[attr_name] = Field(description=attr_desc, default=None)
                annotations[attr_name] = Optional[str]  # 边属性用str类型
            
            attrs["__annotations__"] = annotations
            
            # 动态创建类
            class_name = ''.join(word.capitalize() for word in name.split('_'))
            edge_class = type(class_name, (EdgeModel,), attrs)
            edge_class.__doc__ = description
            
            # 构建source_targets
            source_targets = []
            for st in edge_def.get("source_targets", []):
                source_targets.append(
                    EntityEdgeSourceTarget(
                        source=st.get("source", "Entity"),
                        target=st.get("target", "Entity")
                    )
                )
            
            if source_targets:
                edge_definitions[name] = (edge_class, source_targets)
        
        # 调用Zep API设置本体
        if entity_types or edge_definitions:
            self.client.graph.set_ontology(
                graph_ids=[graph_id],
                entities=entity_types if entity_types else None,
                edges=edge_definitions if edge_definitions else None,
            )
    
    def add_text_batches(
        self,
        graph_id: str,
        chunks: List[str],
        batch_size: int = 3,
        progress_callback: Optional[Callable] = None
    ) -> List[str]:
        """分批添加文本到图谱，返回所有 episode 的 uuid 列表"""
        episode_uuids = []
        total_chunks = len(chunks)
        
        for i in range(0, total_chunks, batch_size):
            batch_chunks = chunks[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total_chunks + batch_size - 1) // batch_size
            
            if progress_callback:
                progress = (i + len(batch_chunks)) / total_chunks
                progress_callback(
                    f"发送第 {batch_num}/{total_batches} 批数据 ({len(batch_chunks)} 块)...",
                    progress
                )
            
            # 构建episode数据
            episodes = [
                EpisodeData(data=chunk, type="text")
                for chunk in batch_chunks
            ]
            
            # 发送到Zep
            try:
                batch_result = self.client.graph.add_batch(
                    graph_id=graph_id,
                    episodes=episodes
                )
                
                # A successful HTTP response is not an ingestion receipt.
                # Require one distinct id for every submitted chunk, including
                # across batches, before any completion polling can succeed.
                if not isinstance(batch_result, list):
                    raise RuntimeError("graph ingestion returned incomplete episode identifiers")
                batch_ids = [getattr(ep, "uuid_", None) or getattr(ep, "uuid", None) for ep in batch_result]
                if (len(batch_ids) != len(batch_chunks)
                        or any(not isinstance(value, str) or not value.strip() for value in batch_ids)
                        or len(set(batch_ids)) != len(batch_ids)
                        or set(batch_ids).intersection(episode_uuids)):
                    raise RuntimeError("graph ingestion returned incomplete episode identifiers")
                episode_uuids.extend(batch_ids)

                # 避免请求过快
                time.sleep(1)
                
            except Exception as e:
                if progress_callback:
                    progress_callback(f"批次 {batch_num} 发送失败: {str(e)}", 0)
                raise
        
        return episode_uuids
    
    def _wait_for_episodes(
        self,
        episode_uuids: List[str],
        progress_callback: Optional[Callable] = None,
        timeout: int = 600
    ):
        """Require every submitted episode to finish, or fail explicitly.

        Status errors are retried three times per episode. The total wait and
        each HTTP call are bounded. No source exception text is persisted.
        """
        pending = set(episode_uuids)
        total = len(pending)
        if not pending:
            if progress_callback:
                progress_callback("No episodes to process", 1.0)
            return
        deadline = time.monotonic() + timeout
        failures = {}
        while pending:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"graph ingestion incomplete: {total - len(pending)}/{total}")
            for episode_id in list(pending):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"graph ingestion incomplete: {total - len(pending)}/{total}")
                try:
                    episode = self.client.graph.episode.get(
                        uuid_=episode_id,
                        request_options={"timeout_in_seconds": min(10, remaining), "max_retries": 0},
                    )
                except Exception:
                    failures[episode_id] = failures.get(episode_id, 0) + 1
                    if failures[episode_id] >= 3:
                        raise RuntimeError("graph episode status unavailable after 3 attempts") from None
                    continue
                failures[episode_id] = 0
                if getattr(episode, "processed", None) is True:
                    pending.remove(episode_id)
            if progress_callback:
                progress_callback(f"Episodes processed: {total - len(pending)}/{total}", (total - len(pending)) / total)
            if pending:
                time.sleep(min(3, max(0, deadline - time.monotonic())))

    def _get_graph_info(self, graph_id: str) -> GraphInfo:
        """获取图谱信息"""
        # 获取节点（分页）
        nodes = fetch_all_nodes(self.client, graph_id)

        # 获取边（分页）
        edges = fetch_all_edges(self.client, graph_id)

        # 统计实体类型
        entity_types = set()
        for node in nodes:
            if node.labels:
                for label in node.labels:
                    if label not in ["Entity", "Node"]:
                        entity_types.add(label)

        return GraphInfo(
            graph_id=graph_id,
            node_count=len(nodes),
            edge_count=len(edges),
            entity_types=list(entity_types)
        )
    
    def get_graph_data(self, graph_id: str) -> Dict[str, Any]:
        """
        获取完整图谱数据（包含详细信息）
        
        Args:
            graph_id: 图谱ID
            
        Returns:
            包含nodes和edges的字典，包括时间信息、属性等详细数据
        """
        nodes = fetch_all_nodes(self.client, graph_id)
        edges = fetch_all_edges(self.client, graph_id)

        # 创建节点映射用于获取节点名称
        node_map = {}
        for node in nodes:
            node_map[node.uuid_] = node.name or ""
        
        nodes_data = []
        for node in nodes:
            # 获取创建时间
            created_at = getattr(node, 'created_at', None)
            if created_at:
                created_at = str(created_at)
            
            nodes_data.append({
                "uuid": node.uuid_,
                "name": node.name,
                "labels": node.labels or [],
                "summary": node.summary or "",
                "attributes": node.attributes or {},
                "created_at": created_at,
            })
        
        edges_data = []
        for edge in edges:
            # 获取时间信息
            created_at = getattr(edge, 'created_at', None)
            valid_at = getattr(edge, 'valid_at', None)
            invalid_at = getattr(edge, 'invalid_at', None)
            expired_at = getattr(edge, 'expired_at', None)
            
            # 获取 episodes
            episodes = getattr(edge, 'episodes', None) or getattr(edge, 'episode_ids', None)
            if episodes and not isinstance(episodes, list):
                episodes = [str(episodes)]
            elif episodes:
                episodes = [str(e) for e in episodes]
            
            # 获取 fact_type
            fact_type = getattr(edge, 'fact_type', None) or edge.name or ""
            
            edges_data.append({
                "uuid": edge.uuid_,
                "name": edge.name or "",
                "fact": edge.fact or "",
                "fact_type": fact_type,
                "source_node_uuid": edge.source_node_uuid,
                "target_node_uuid": edge.target_node_uuid,
                "source_node_name": node_map.get(edge.source_node_uuid, ""),
                "target_node_name": node_map.get(edge.target_node_uuid, ""),
                "attributes": edge.attributes or {},
                "created_at": str(created_at) if created_at else None,
                "valid_at": str(valid_at) if valid_at else None,
                "invalid_at": str(invalid_at) if invalid_at else None,
                "expired_at": str(expired_at) if expired_at else None,
                "episodes": episodes or [],
            })
        
        return {
            "graph_id": graph_id,
            "nodes": nodes_data,
            "edges": edges_data,
            "node_count": len(nodes_data),
            "edge_count": len(edges_data),
        }
    
    def delete_graph(self, graph_id: str):
        """删除图谱"""
        self.client.graph.delete(graph_id=graph_id)

