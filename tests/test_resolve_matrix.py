"""POST /resolve-matrix 的端到端测试。

覆盖：多环境逐个求解与按 id 排序的结果、matrix 的 shared/divergent 标记
（含 PEP 440 等价版本与缺失包的 null）、任一环境无解时整体 409 并携带
各失败环境的原冲突结果、环境数量与 id 的 422 校验，以及 /resolve 行为不变。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

LINUX = {"python_version": "3.11", "sys_platform": "linux",
         "platform_machine": "x86_64"}
WINDOWS = {"python_version": "3.9", "sys_platform": "win32",
           "platform_machine": "AMD64"}


def c(version, deps=None):
    return {"version": version, "dependencies": deps or []}


def d(name, spec="", marker=""):
    return {"name": name, "specifier": spec, "marker": marker}


def env(env_id, **fields):
    base = dict(LINUX)
    base.update(fields)
    return {"id": env_id, **base}


def matrix_payload(repo, environments, **overrides):
    body = {
        "repository": repo,
        "root": "app",
        "specifier": "",
        "allow_prereleases": False,
        "target_environments": environments,
    }
    body.update(overrides)
    return body


def _marker_repo():
    """与 examples/resolve-matrix.json 相同的跨平台快照。"""
    return {
        "app": [c("2.0.0", [
            d("linux-helper", ">=1.0", "sys_platform == 'linux'"),
            d("win-helper", ">=1.0", "sys_platform == 'win32'"),
            d("speedlib", "", "platform_machine == 'x86_64'"),
            d("modern-core", "", "python_version >= '3.10'"),
            d("legacy-core", "", "python_version < '3.10'"),
        ])],
        "linux-helper": [c("1.2.0", [d("native-bindings", "==2")])],
        "win-helper": [c("1.2.0", [d("native-bindings", "==1")])],
        "native-bindings": [c("1"), c("2"), c("3")],
        "speedlib": [c("1.0.0")],
        "modern-core": [c("4.0.0")],
        "legacy-core": [c("0.9.0")],
    }


# --------------------------------------------------------------------- #
# 成功路径：逐环境求解 + 版本矩阵
# --------------------------------------------------------------------- #
def test_matrix_ok_cross_platform():
    r = client.post("/resolve-matrix", json=matrix_payload(
        _marker_repo(),
        [env("linux-x86_64"), env("windows-amd64", **WINDOWS)],
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["root"] == "app"

    assert body["environments"] == [
        {"id": "linux-x86_64",
         "locked": {"app": "2.0.0", "linux-helper": "1.2.0",
                    "modern-core": "4.0.0", "native-bindings": "2",
                    "speedlib": "1.0.0"}},
        {"id": "windows-amd64",
         "locked": {"app": "2.0.0", "legacy-core": "0.9.0",
                    "native-bindings": "1", "win-helper": "1.2.0"}},
    ]

    # matrix 按包名排序；未出现的包为 null；shared/divergent 标记正确
    assert [row["name"] for row in body["matrix"]] == [
        "app", "legacy-core", "linux-helper", "modern-core",
        "native-bindings", "speedlib", "win-helper",
    ]
    by_name = {row["name"]: row for row in body["matrix"]}
    assert by_name["app"] == {
        "name": "app",
        "versions": {"linux-x86_64": "2.0.0", "windows-amd64": "2.0.0"},
        "status": "shared",
    }
    assert by_name["native-bindings"] == {
        "name": "native-bindings",
        "versions": {"linux-x86_64": "2", "windows-amd64": "1"},
        "status": "divergent",
    }
    assert by_name["win-helper"] == {
        "name": "win-helper",
        "versions": {"linux-x86_64": None, "windows-amd64": "1.2.0"},
        "status": "divergent",
    }
    assert by_name["speedlib"]["versions"] == {
        "linux-x86_64": "1.0.0", "windows-amd64": None,
    }
    assert by_name["speedlib"]["status"] == "divergent"


def test_matrix_results_sorted_by_id_not_request_order():
    repo = {"app": [c("1.0", [d("lib")])], "lib": [c("1.0")]}
    envs = [env("zeta"), env("alpha"), env("mid")]
    r = client.post("/resolve-matrix", json=matrix_payload(repo, envs))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["id"] for e in body["environments"]] == ["alpha", "mid", "zeta"]
    row = body["matrix"][0]
    assert list(row["versions"].keys()) == ["alpha", "mid", "zeta"]
    assert row["status"] == "shared"


def test_matrix_all_shared_when_environments_identical():
    repo = {"app": [c("1.0", [d("lib", ">=1.0")])],
            "lib": [c("1.0"), c("2.0")]}
    r = client.post("/resolve-matrix", json=matrix_payload(
        repo, [env("a"), env("b")]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert all(row["status"] == "shared" for row in body["matrix"])
    assert body["matrix"][1]["versions"] == {"a": "2.0", "b": "2.0"}


def test_matrix_pep440_equivalent_versions_are_shared():
    """1.0 与 1.0.0 按 PEP 440 相等，应标记 shared 而非 divergent。"""
    repo = {
        "app": [c("1.0", [
            d("lib", "==1.0", "sys_platform == 'linux'"),
            d("lib", "==1.0.0", "sys_platform == 'win32'"),
        ])],
        "lib": [c("1.0.0")],
    }
    r = client.post("/resolve-matrix", json=matrix_payload(
        repo, [env("linux"), env("windows", **WINDOWS)]))
    assert r.status_code == 200, r.text
    by_name = {row["name"]: row for row in r.json()["matrix"]}
    assert by_name["lib"]["versions"] == {"linux": "1.0.0", "windows": "1.0.0"}
    assert by_name["lib"]["status"] == "shared"


def test_matrix_specifier_and_prereleases_apply_to_every_environment():
    repo = {
        "app": [c("1.0"), c("2.0rc1"), c("1.5")],
    }
    r = client.post("/resolve-matrix", json=matrix_payload(
        repo, [env("a"), env("b")], specifier=">=1.0"))
    assert r.status_code == 200, r.text
    assert r.json()["environments"][0]["locked"] == {"app": "1.5"}

    r2 = client.post("/resolve-matrix", json=matrix_payload(
        repo, [env("a"), env("b")], allow_prereleases=True))
    assert r2.json()["environments"][0]["locked"] == {"app": "2.0rc1"}


def test_matrix_max_eight_environments():
    repo = {"app": [c("1.0")]}
    envs = [env(f"env-{i}") for i in range(8)]
    r = client.post("/resolve-matrix", json=matrix_payload(repo, envs))
    assert r.status_code == 200, r.text
    assert len(r.json()["environments"]) == 8


# --------------------------------------------------------------------- #
# 409：任一环境无解 -> 整体失败，列出各失败环境的原冲突结果
# --------------------------------------------------------------------- #
def _conflict_repo():
    """linux 下 left/right 对 core 的约束互斥；windows 下两条依赖均未激活。"""
    return {
        "app": [c("1.0", [
            d("left", "", "sys_platform == 'linux'"),
            d("right", "", "sys_platform == 'linux'"),
        ])],
        "left": [c("1.0", [d("core", "==1.0")])],
        "right": [c("1.0", [d("core", "==2.0")])],
        "core": [c("1.0"), c("2.0")],
    }


def test_matrix_one_environment_unresolvable_409():
    r = client.post("/resolve-matrix", json=matrix_payload(
        _conflict_repo(), [env("linux"), env("windows", **WINDOWS)]))
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "conflict"
    assert "linux" in body["message"]
    # 只列出失败环境，且携带与 /resolve 一致的原冲突结果
    assert [f["id"] for f in body["failures"]] == ["linux"]
    failure = body["failures"][0]
    assert failure["status"] == "conflict"
    assert failure["conflicts"][0]["subject"] == "core"
    chains = failure["conflicts"][0]["constraints"]
    assert all(entry["chain"][0]["package"] is None for entry in chains)
    # 成功的环境不出现在 409 响应中
    assert "environments" not in body
    assert "matrix" not in body


def test_matrix_conflict_payload_matches_resolve():
    """失败环境的冲突结果与直接调用 /resolve 的 409 响应逐字段一致。"""
    repo = _conflict_repo()
    single = client.post("/resolve", json={
        "repository": repo, "root": "app", "specifier": "",
        "allow_prereleases": False, "target_environment": LINUX,
    })
    assert single.status_code == 409

    multi = client.post("/resolve-matrix", json=matrix_payload(
        repo, [env("linux"), env("windows", **WINDOWS)]))
    assert multi.status_code == 409
    failure = multi.json()["failures"][0]
    assert failure["status"] == single.json()["status"]
    assert failure["message"] == single.json()["message"]
    assert failure["conflicts"] == single.json()["conflicts"]


def test_matrix_multiple_failures_sorted_by_id():
    repo = {
        "app": [c("1.0", [d("missing-pkg", "", "python_version >= '3.10'")])],
    }
    envs = [
        env("z-old", python_version="3.9"),
        env("a-new", python_version="3.12"),
        env("m-new", python_version="3.11"),
    ]
    r = client.post("/resolve-matrix", json=matrix_payload(repo, envs))
    assert r.status_code == 409
    body = r.json()
    # 3.9 环境下依赖未激活可解；3.11/3.12 无解，按 id 排序
    assert [f["id"] for f in body["failures"]] == ["a-new", "m-new"]
    assert body["failures"][0]["conflicts"][0]["subject"] == "missing-pkg"


# --------------------------------------------------------------------- #
# 422：环境数量、id 合法性、快照非法
# --------------------------------------------------------------------- #
def test_matrix_too_few_environments_422():
    r = client.post("/resolve-matrix", json=matrix_payload(
        {"app": [c("1.0")]}, [env("only-one")]))
    assert r.status_code == 422


def test_matrix_too_many_environments_422():
    envs = [env(f"env-{i}") for i in range(9)]
    r = client.post("/resolve-matrix", json=matrix_payload(
        {"app": [c("1.0")]}, envs))
    assert r.status_code == 422


def test_matrix_duplicate_ids_422():
    r = client.post("/resolve-matrix", json=matrix_payload(
        {"app": [c("1.0")]}, [env("same"), env("same", **WINDOWS)]))
    assert r.status_code == 422


def test_matrix_blank_id_422():
    r = client.post("/resolve-matrix", json=matrix_payload(
        {"app": [c("1.0")]}, [env("ok"), env("   ")]))
    assert r.status_code == 422


def test_matrix_missing_id_422():
    good = env("good")
    bad = dict(LINUX)  # 没有 id
    r = client.post("/resolve-matrix", json=matrix_payload(
        {"app": [c("1.0")]}, [good, bad]))
    assert r.status_code == 422


def test_matrix_environment_fields_still_validated_422():
    bad = env("bad", python_version="not-a-version")
    r = client.post("/resolve-matrix", json=matrix_payload(
        {"app": [c("1.0")]}, [env("good"), bad]))
    assert r.status_code == 422
    assert r.json()["status"] == "invalid_snapshot"
    assert "bad" in r.json()["message"]


def test_matrix_unknown_root_422():
    r = client.post("/resolve-matrix", json=matrix_payload(
        {"app": [c("1.0")]}, [env("a"), env("b")], root="missing"))
    assert r.status_code == 422
    assert r.json()["status"] == "invalid_snapshot"


def test_matrix_bad_snapshot_422():
    repo = {"app": [c("oops")]}
    r = client.post("/resolve-matrix", json=matrix_payload(
        repo, [env("a"), env("b")]))
    assert r.status_code == 422
    assert "PEP 440" in r.json()["message"]


def test_matrix_invalid_marker_422():
    repo = {"app": [c("1.0", [d("x", "", "os_name == 'posix'")])]}
    r = client.post("/resolve-matrix", json=matrix_payload(
        repo, [env("a"), env("b")]))
    assert r.status_code == 422
    assert "os_name" in r.json()["message"]


def test_matrix_extra_field_forbidden_422():
    body = matrix_payload({"app": [c("1.0")]}, [env("a"), env("b")])
    body["unknown_field"] = 1
    r = client.post("/resolve-matrix", json=body)
    assert r.status_code == 422


def test_matrix_environment_extra_field_forbidden_422():
    bad = env("bad", os_name="posix")
    r = client.post("/resolve-matrix", json=matrix_payload(
        {"app": [c("1.0")]}, [env("good"), bad]))
    assert r.status_code == 422


# --------------------------------------------------------------------- #
# 既有端点行为不变
# --------------------------------------------------------------------- #
def test_resolve_still_works_after_matrix_addition():
    r = client.post("/resolve", json={
        "repository": _marker_repo(), "root": "app", "specifier": "",
        "allow_prereleases": False, "target_environment": LINUX,
    })
    assert r.status_code == 200, r.text
    assert r.json()["locked"]["speedlib"] == "1.0.0"
