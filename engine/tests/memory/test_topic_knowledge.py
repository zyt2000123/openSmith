from __future__ import annotations

import asyncio

from engine.memory.knowledge import TopicAssociationStore
from engine.memory.knowledge import sync_durable_topics
from engine.memory.knowledge import topic_filename
from engine.llm.client import ChatResponse
from engine.memory.store import retrieve_relevant_memory
from engine.memory.vector import TopicVectorIndex
from engine.memory.embeddings import embedding_provider_from_config
from engine.memory.search import SearchIndex


class TopicLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, _messages, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(text='{"topics":[{"topic":"部署","entries":[0]}]}')
        return ChatResponse(text="部署使用 Kubernetes。")


class FakeEmbedder:
    model = "test-embedder"

    async def embed(self, texts):
        return [[1.0, 0.0] if "deploy" in text.lower() else [0.0, 1.0] for text in texts]


def test_topic_association_store_tracks_durable_entries_and_removes_stale_topics(tmp_path) -> None:
    store = TopicAssociationStore(tmp_path / "memory")

    store.replace_topic("部署", ["- 生产环境使用 Kubernetes 部署。"])
    store.replace_topic("数据库", ["- 生产环境使用 Kubernetes 部署。", "- 主数据库是 PostgreSQL。"])

    assert store.topics_for_entries(["- 生产环境使用 Kubernetes 部署。"]) == ("数据库", "部署")

    store.replace_topic("部署", [])

    assert store.topics_for_entries(["- 生产环境使用 Kubernetes 部署。"]) == ("数据库",)


def test_sync_durable_topics_creates_an_episode_and_association(tmp_path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "durable.md").write_text(
        "# Durable Project Memory\n\n## Confirmed Facts\n\n- 生产环境使用 Kubernetes 部署。\n",
        encoding="utf-8",
    )

    updated = asyncio.run(sync_durable_topics(memory_dir, TopicLLM()))

    assert updated == ("部署",)
    assert (memory_dir / "episodes" / "部署.md").is_file()
    assert TopicAssociationStore(memory_dir).topics_for_entries(
        ["- 生产环境使用 Kubernetes 部署。"]
    ) == ("部署",)


def test_retrieval_searches_only_topics_routed_from_durable_memory(tmp_path) -> None:
    memory_dir = tmp_path / "memory"
    episodes_dir = memory_dir / "episodes"
    episodes_dir.mkdir(parents=True)
    deployment = "- Production deployment uses Kubernetes."
    database = "- Primary database is PostgreSQL."
    (memory_dir / "durable.md").write_text(
        "# Durable Project Memory\n\n## Confirmed Facts\n\n"
        f"{deployment}\n{database}\n",
        encoding="utf-8",
    )
    (episodes_dir / "deployment.md").write_text(
        "# Deployment\n\nKubernetes deployment rollout guide.", encoding="utf-8"
    )
    (episodes_dir / "database.md").write_text(
        "# Database\n\nPostgreSQL deployment tuning guide.", encoding="utf-8"
    )
    store = TopicAssociationStore(memory_dir)
    store.replace_topic("deployment", [deployment])
    store.replace_topic("database", [database])

    result = asyncio.run(retrieve_relevant_memory(tmp_path, "Kubernetes deployment"))

    assert "Kubernetes deployment rollout guide" in result.episodes
    assert "PostgreSQL deployment tuning guide" not in result.episodes


def test_retrieval_uses_vector_hits_when_an_embedding_provider_is_available(tmp_path) -> None:
    memory_dir = tmp_path / "memory"
    episodes_dir = memory_dir / "episodes"
    episodes_dir.mkdir(parents=True)
    durable = "- deploy with Kubernetes"
    (memory_dir / "durable.md").write_text(f"# Durable\n\n{durable}\n", encoding="utf-8")
    (episodes_dir / "deployment.md").write_text(
        "# Deployment\n\ndeploy rollout details", encoding="utf-8"
    )
    TopicAssociationStore(memory_dir).replace_topic("deployment", [durable])

    result = asyncio.run(
        retrieve_relevant_memory(tmp_path, "deploy safely", embedding_provider=FakeEmbedder())
    )

    assert result.episodes.startswith("## Relevant Topic Knowledge")
    assert "deploy rollout details" in result.episodes


def test_topic_snapshot_reconciles_removed_topics(tmp_path) -> None:
    store = TopicAssociationStore(tmp_path / "memory")
    store.replace_topic("部署", ["- deploy with Kubernetes"])
    store.replace_topic("数据库", ["- use PostgreSQL"])

    removed = store.replace_all_topics({"数据库": ["- use PostgreSQL"]})

    assert removed == {"部署": "部署"}
    assert store.topics_for_entries(["- deploy with Kubernetes"]) == ()


def test_vector_index_searches_only_the_routed_topics(tmp_path) -> None:
    index = TopicVectorIndex(tmp_path / "episodes")

    async def run():
        provider = FakeEmbedder()
        await index.sync({"deployment": "deploy rollout", "database": "database tuning"}, provider)
        return await index.search("deploy safely", ("deployment",), provider, 3)

    hits = asyncio.run(run())

    assert [hit["topic"] for hit in hits] == ["deployment"]


def test_embedding_provider_is_opt_in_and_reads_its_key_from_environment(monkeypatch) -> None:
    config = {
        "knowledge": {
            "embeddings": {
                "enabled": True,
                "base_url": "https://example.test/v1",
                "model": "text-embedding-test",
                "api_key_env": "SMITH_EMBEDDING_KEY",
            }
        }
    }
    assert embedding_provider_from_config(config) is None

    monkeypatch.setenv("SMITH_EMBEDDING_KEY", "test-key")
    provider = embedding_provider_from_config(config)

    assert provider is not None
    assert provider.model == "text-embedding-test"


def test_topic_filename_matches_the_episode_writer_slug() -> None:
    assert topic_filename("生产部署 / 回滚") == "生产部署-回滚.md"


def test_empty_durable_snapshot_removes_stale_topic_files(tmp_path) -> None:
    memory_dir = tmp_path / "memory"
    episodes_dir = memory_dir / "episodes"
    episodes_dir.mkdir(parents=True)
    (memory_dir / "durable.md").write_text("# Durable\n", encoding="utf-8")
    (episodes_dir / "deployment.md").write_text("old topic", encoding="utf-8")
    TopicAssociationStore(memory_dir).replace_topic(
        "deployment", ["- deploy with Kubernetes"]
    )

    assert asyncio.run(sync_durable_topics(memory_dir, TopicLLM())) == ()
    assert not (episodes_dir / "deployment.md").exists()
    assert not TopicAssociationStore(memory_dir).has_associations()


def test_association_persists_collision_safe_episode_id(tmp_path) -> None:
    store = TopicAssociationStore(tmp_path / "memory")
    store.replace_topic("deployment", ["- deploy"], file_id="deployment-a1b2c3d4")

    assert store.file_ids_for_topics(("deployment",)) == ("deployment-a1b2c3d4",)


def test_scoped_short_term_search_cannot_escape_topic_filter(tmp_path) -> None:
    async def run():
        episodes_dir = tmp_path / "episodes"
        episodes_dir.mkdir()
        (episodes_dir / "deployment.md").write_text("普通回滚流程", encoding="utf-8")
        (episodes_dir / "unrelated.md").write_text("部署常规说明", encoding="utf-8")
        index = SearchIndex(episodes_dir)
        await index.open()
        try:
            await index.index_entries([
                ("deployment", "普通回滚流程", "episode"),
                ("unrelated", "部署常规说明", "episode"),
            ])
            return await index.search("部署 回滚", entry_ids=("deployment",))
        finally:
            await index.close()

    assert [hit["id"] for hit in asyncio.run(run())] == ["deployment"]
