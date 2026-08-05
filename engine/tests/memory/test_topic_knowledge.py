from __future__ import annotations

import asyncio
import json

import pytest

from engine.memory.knowledge import TopicAssociationStore
from engine.memory.knowledge import sync_durable_topics
from engine.memory.knowledge import topic_filename
from engine.llm.client import ChatResponse
from engine.memory.store import _without_durable_repetition
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

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
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
    (episodes_dir / "deployment.md.bak").write_text("old topic backup", encoding="utf-8")
    TopicAssociationStore(memory_dir).replace_topic(
        "deployment", ["- deploy with Kubernetes"]
    )

    assert asyncio.run(sync_durable_topics(memory_dir, TopicLLM())) == ()
    assert not (episodes_dir / "deployment.md").exists()
    assert not (episodes_dir / "deployment.md.bak").exists()
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


def test_vector_sync_reuses_stored_vectors_on_a_second_run(tmp_path) -> None:
    """The reuse branch is unreachable on a first sync, so it must be run twice."""
    index = TopicVectorIndex(tmp_path / "episodes")
    provider = FakeEmbedder()
    documents = {"deployment": "deploy rollout details"}

    async def run():
        await index.sync(documents, provider)
        after_first = provider.calls
        await index.sync(documents, provider)
        after_second = provider.calls
        hits = await index.search("deploy safely", ("deployment",), provider, 3)
        return after_first, after_second, hits

    after_first, after_second, hits = asyncio.run(run())

    assert after_second == after_first, "unchanged chunks must not be re-embedded"
    assert [hit["topic"] for hit in hits] == ["deployment"]


def test_vector_sync_re_embeds_a_corrupted_stored_vector(tmp_path) -> None:
    episodes_dir = tmp_path / "episodes"
    index = TopicVectorIndex(episodes_dir)
    provider = FakeEmbedder()
    documents = {"deployment": "deploy rollout details"}

    async def run():
        await index.sync(documents, provider)
        state_path = episodes_dir / "vectors.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for item in state["items"].values():
            item["vector"] = None
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        await index.sync(documents, provider)
        return await index.search("deploy safely", ("deployment",), provider, 3)

    assert [hit["topic"] for hit in asyncio.run(run())] == ["deployment"]


def test_empty_classifier_groups_do_not_wipe_the_snapshot(tmp_path) -> None:
    memory_dir = tmp_path / "memory"
    episodes_dir = memory_dir / "episodes"
    episodes_dir.mkdir(parents=True)
    (memory_dir / "durable.md").write_text(
        "# Durable\n\n- 生产环境使用 Kubernetes 部署。\n", encoding="utf-8"
    )
    (episodes_dir / "部署.md").write_text("既有主题页面", encoding="utf-8")
    store = TopicAssociationStore(memory_dir)
    store.replace_topic("部署", ["- 生产环境使用 Kubernetes 部署。"])

    class EmptyGroupLLM:
        async def chat(self, _messages, **_kwargs):
            return ChatResponse(text='{"topics": []}')

    with pytest.raises(RuntimeError):
        asyncio.run(sync_durable_topics(memory_dir, EmptyGroupLLM()))

    assert (episodes_dir / "部署.md").is_file()
    assert store.topics_for_entries(["- 生产环境使用 Kubernetes 部署。"]) == ("部署",)


def test_unchanged_topic_reuses_its_page_instead_of_regenerating(tmp_path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "durable.md").write_text(
        "# Durable\n\n- 生产环境使用 Kubernetes 部署。\n", encoding="utf-8"
    )
    first = TopicLLM()
    assert asyncio.run(sync_durable_topics(memory_dir, first)) == ("部署",)
    assert first.calls == 2  # classify, then generate the page

    second = TopicLLM()
    assert asyncio.run(sync_durable_topics(memory_dir, second)) == ("部署",)
    assert second.calls == 1, "an unchanged topic must not cost a generation call"


def test_duplicate_classifier_topics_merge_into_one_page(tmp_path) -> None:
    """A repeated topic name must not cost a second generation or drop coverage."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "durable.md").write_text(
        "# Durable\n\n- 生产环境使用 Kubernetes 部署。\n- 回滚依赖 Helm 历史版本。\n",
        encoding="utf-8",
    )

    class DuplicateTopicLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, _messages, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(
                    text='{"topics":[{"topic":"部署","entries":[0]},{"topic":"部署","entries":[1]}]}'
                )
            return ChatResponse(text="部署与回滚流程说明。")

    llm = DuplicateTopicLLM()

    assert asyncio.run(sync_durable_topics(memory_dir, llm)) == ("部署",)
    assert llm.calls == 2, "one classification plus one generation, not two"
    store = TopicAssociationStore(memory_dir)
    assert store.topics_for_entries(["- 生产环境使用 Kubernetes 部署。"]) == ("部署",)
    assert store.topics_for_entries(["- 回滚依赖 Helm 历史版本。"]) == ("部署",)


def test_snapshot_keeps_topics_and_files_consistent(tmp_path) -> None:
    """A topic whose slug resolves to nothing has no page, so it is not recorded."""
    store = TopicAssociationStore(tmp_path / "memory")
    store.replace_topic("!!!", ["- alpha fact"])
    store.replace_topic("deployment", ["- beta fact"])

    assert store.topics_for_entries(["- alpha fact"]) == ()

    removed = store.replace_all_topics({"deployment": ["- beta fact"]})

    assert removed == {}
    topics, files = store.snapshot()
    assert set(topics) == set(files) == {"deployment"}


def test_snapshot_does_not_strand_episodes_that_predate_it(tmp_path) -> None:
    """A topic snapshot scopes only its own pages; older episodes stay reachable."""
    memory_dir = tmp_path / "memory"
    episodes_dir = memory_dir / "episodes"
    episodes_dir.mkdir(parents=True)
    (memory_dir / "durable.md").write_text(
        "- 路由使用关键词匹配\n- 部署使用 docker compose\n", encoding="utf-8"
    )
    (episodes_dir / "legacy-deploy.md").write_text(
        "# 部署\n\n上线前先跑 docker compose config 校验。", encoding="utf-8"
    )
    (episodes_dir / "routing-topic.md").write_text(
        "# 路由\n\n路由优先关键词，其次 LLM 兜底。", encoding="utf-8"
    )
    TopicAssociationStore(memory_dir).replace_all_topics(
        {"路由": ["- 路由使用关键词匹配"]}, file_ids={"路由": "routing-topic"}
    )

    routed = asyncio.run(retrieve_relevant_memory(tmp_path, "路由 关键词"))
    unrouted = asyncio.run(retrieve_relevant_memory(tmp_path, "部署 docker compose"))

    assert "LLM 兜底" in routed.episodes
    assert "docker compose config" in unrouted.episodes, (
        "an episode predating the snapshot must not become unreachable"
    )


def test_routed_query_cannot_reach_an_unrouted_topic_page(tmp_path) -> None:
    """The scope still holds for generated pages: only routed topics are eligible."""
    memory_dir = tmp_path / "memory"
    episodes_dir = memory_dir / "episodes"
    episodes_dir.mkdir(parents=True)
    (memory_dir / "durable.md").write_text(
        "- 路由使用关键词匹配\n- 部署使用 docker compose\n", encoding="utf-8"
    )
    (episodes_dir / "routing-topic.md").write_text("# 路由\n\n关键词优先。", encoding="utf-8")
    (episodes_dir / "deploy-topic.md").write_text("# 部署\n\n关键词也出现在这里。", encoding="utf-8")
    TopicAssociationStore(memory_dir).replace_all_topics(
        {
            "路由": ["- 路由使用关键词匹配"],
            "部署": ["- 部署使用 docker compose"],
        },
        file_ids={"路由": "routing-topic", "部署": "deploy-topic"},
    )

    result = asyncio.run(retrieve_relevant_memory(tmp_path, "路由 关键词"))

    assert "关键词优先" in result.episodes
    assert "关键词也出现在这里" not in result.episodes


def test_durable_dedup_drops_whole_lines_without_cutting_sentences() -> None:
    # The second line *contains* the durable bullet as a substring, which is what
    # made the previous str.replace approach cut the subject out of the sentence.
    episode = "- 使用 pytest\n  - 使用 pytest 时要加 -p no:cacheprovider\n"

    cleaned = _without_durable_repetition(episode, ["- 使用 pytest"])

    assert cleaned.splitlines() == ["  - 使用 pytest 时要加 -p no:cacheprovider"]
    assert "使用 pytest 时要加" in cleaned
