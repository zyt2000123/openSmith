"""task_router 单测：YAML 身份目录路由 + Evaluation-Sensitive 检测。"""

import sys
from pathlib import Path

from engine.execution.routing.task_router import detect_eval_sensitive, route_task
from engine.identity import IdentityCatalog


def _catalog(tmp_path: Path) -> IdentityCatalog:
    (tmp_path / "smith.yaml").write_text(
        """
schema: agentsmith.identity/v1
id: smith
name: Smith
default: true
routes:
  - id: bugfix
    examples: ["Traceback (most recent call last)"]
    keywords: [bug, \u62a5\u9519]
    pipeline: bugfix
  - id: feature
    examples: ["\u7528\u6237\u6545\u4e8b", "\u9a8c\u6536\u6807\u51c6"]
    keywords: [\u5b9e\u73b0, \u65b0\u589e]
    pipeline: feature
  - id: git
    keywords: [git, \u5206\u652f]
    priority: 30
""".strip(),
        encoding="utf-8",
    )
    return IdentityCatalog.load(tmp_path)


def test_route_task_uses_declared_example_route(tmp_path: Path) -> None:
    decision = route_task(
        'Traceback (most recent call last)\n  File "app.py", line 3\nValueError: bad',
        _catalog(tmp_path),
    )

    assert decision.identity_id == "smith"
    assert decision.route_id == "bugfix"
    assert decision.pipeline_id == "bugfix"


def test_route_task_uses_declared_feature_route(tmp_path: Path) -> None:
    decision = route_task("作为用户，我希望能导出报表。验收标准：支持 CSV 格式。", _catalog(tmp_path))

    assert decision.route_id == "feature"
    assert decision.pipeline_id == "feature"


def test_plain_chat_uses_default_identity_direct_fallback(tmp_path: Path) -> None:
    decision = route_task("今天天气怎么样", _catalog(tmp_path))

    assert decision.identity_id == "smith"
    assert decision.route_id == "direct"
    assert decision.pipeline_id is None


def test_git_branch_request_uses_direct_git_route_instead_of_feature_pipeline(tmp_path: Path) -> None:
    decision = route_task("\u4f60\u80fd\u5e2e\u6211\u521b\u5efa\u4e00\u4e2agit\u5206\u652f\u5417\uff1f", _catalog(tmp_path))

    assert decision.route_id == "git"
    assert decision.pipeline_id is None


def test_smith_identity_routes_git_branch_requests_directly() -> None:
    identities_dir = Path(__file__).resolve().parents[3] / "agents" / "identities"
    decision = route_task("请创建一个 git 分支", IdentityCatalog.load(identities_dir))

    assert decision.route_id == "git"
    assert decision.pipeline_id is None


def test_smith_identity_routes_coding_work_through_the_shipped_pipeline() -> None:
    identities_dir = Path(__file__).resolve().parents[3] / "agents" / "identities"
    catalog = IdentityCatalog.load(identities_dir)

    assert route_task("修复登录报错", catalog).pipeline_id == "coding"
    assert route_task("新增导出功能", catalog).pipeline_id == "coding"
    assert route_task("重构用户模块", catalog).pipeline_id == "coding"


def test_eval_sensitive_positive():
    assert detect_eval_sensitive("跑一下 benchmark 看看分数")
    assert detect_eval_sensitive("让所有测试通过就行")
    assert detect_eval_sensitive("make the tests pass")
    assert detect_eval_sensitive("这题按评分标准判分")


def test_eval_sensitive_negative():
    assert not detect_eval_sensitive("帮我写个单元测试")
    assert not detect_eval_sensitive("今天天气怎么样")
    assert not detect_eval_sensitive("修一下登录页的 bug")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
