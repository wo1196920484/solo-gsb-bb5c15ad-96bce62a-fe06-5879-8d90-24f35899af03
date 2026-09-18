"""FastAPI 入口：全部数据随请求在本地内存中处理。

* POST /resolve     提交仓库快照 + 根包 + 版本约束 + 目标环境（PEP 508 marker
  在请求环境下求值），返回锁定清单或冲突来源链
* POST /upgrade     在 /resolve 语义基础上附加 current_lock 与单个升级目标：
  仅考虑严格高于当前版本的候选，取能完成整体解析的最高版本，并在该版本
  的所有有效解中最小化非目标包相对 current_lock 的变化数量
* POST /audit-lock  只审计不求解：按 current_lock 的锁定版本从根包遍历
  已激活依赖，报告 missing/unavailable/constraint_mismatch/extraneous
* POST /dependency-paths
  先按 /audit-lock 规则校验 current_lock；无效返回 409 审计问题，
  有效则按锁定版本与目标环境已激活依赖建图，返回根包到目标包的
  全部最短包路径（菱形保留等长路径，环安全、确定性排序）
* GET  /healthz     健康检查
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from packaging.version import Version

from .audit import audit_lock
from .paths import build_locked_graph, shortest_paths
from .repository import (
    SnapshotError,
    build_repository,
    canonical,
    parse_current_lock,
    parse_root_specifier,
)
from .resolver import ResolutionLimit, Resolver
from .schemas import (
    AuditLockRequest,
    AuditLockResponse,
    DependencyPathsRequest,
    DependencyPathsResponse,
    InvalidLockResponse,
    InvalidSnapshotResponse,
    ResolveRequest,
    ResolveResponse,
    UnresolvedResponse,
    UpgradeRequest,
    UpgradeResponse,
)

app = FastAPI(
    title="本地依赖解析 API",
    description=(
        "基于 Python packaging（PEP 440/503/508）与回溯搜索的依赖求解器。"
        "所有仓库数据与目标环境随请求提交，本地解析，不访问任何远程索引、"
        "不读取宿主机平台信息。"
    ),
    version="1.3.0",
)


def _conflict_payload(raw: dict) -> dict:
    """把 Resolver.build_conflicts() 的内部结构转为对外 JSON。"""
    return {
        "status": raw["status"],
        "message": raw["message"],
        "conflicts": [
            {
                "subject": c.subject,
                "kind": c.kind,
                "message": c.message,
                "constraints": [
                    {
                        "chain": [
                            {
                                "package": link.package,
                                "version": link.version,
                                "requires": link.requires,
                            }
                            for link in chain
                        ]
                    }
                    for chain in c.chains
                ],
                "partial_selection": c.partial_selection,
            }
            for c in raw["_conflicts"]
        ],
    }


@app.get("/healthz", summary="健康检查")
def healthz() -> dict:
    return {"status": "ok"}


@app.post(
    "/resolve",
    summary="解析依赖并生成确定性锁定清单",
    response_model=ResolveResponse,
    responses={
        409: {"model": UnresolvedResponse, "description": "约束无解或超过搜索上限"},
        422: {"model": InvalidSnapshotResponse, "description": "快照或请求非法"},
    },
)
def resolve(request: ResolveRequest):
    root_canonical = canonical(request.root)
    payload = request.model_dump()

    try:
        repo = build_repository(payload["repository"], payload["target_environment"])
        root_spec = parse_root_specifier(request.specifier)
    except SnapshotError as exc:
        return JSONResponse(
            status_code=422,
            content={"status": "invalid_snapshot", "message": str(exc)},
        )

    if not repo.known(root_canonical):
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_snapshot",
                "message": f"根包 {root_canonical!r} 不存在于仓库快照中",
            },
        )

    resolver = Resolver(repo, allow_prereleases=request.allow_prereleases)
    try:
        state = resolver.resolve(root_canonical, root_spec, request.specifier)
    except ResolutionLimit:
        return JSONResponse(
            status_code=409,
            content=_conflict_payload(resolver.build_conflicts(limit=True)),
        )

    if state is not None:
        return resolver.build_output(state, root_canonical)

    return JSONResponse(
        status_code=409,
        content=_conflict_payload(resolver.build_conflicts()),
    )


def _lock_changes(
    current: dict[str, Version], locked: dict[str, str]
) -> list[dict]:
    """current_lock 与新锁定清单的逐项差异，按包名排序。

    版本按 PEP 440 比较（如 1.0 与 1.0.0 视为相同，不产生变化项）；
    新增包 from 为 None，被删除的包 to 为 None。
    """
    changes = []
    for name in sorted(set(current) | set(locked)):
        old = current.get(name)
        new = locked.get(name)
        if old is not None and new is not None and old == Version(new):
            continue
        changes.append(
            {
                "name": name,
                "from": str(old) if old is not None else None,
                "to": new,
            }
        )
    return changes


@app.post(
    "/upgrade",
    summary="在现有锁定清单上升级单个目标包",
    response_model=UpgradeResponse,
    responses={
        409: {"model": UnresolvedResponse, "description": "无可行升级或超过搜索上限"},
        422: {"model": InvalidSnapshotResponse, "description": "快照、current_lock 或请求非法"},
    },
)
def upgrade(request: UpgradeRequest):
    root_canonical = canonical(request.root)
    target_canonical = canonical(request.target)
    payload = request.model_dump()

    try:
        repo = build_repository(payload["repository"], payload["target_environment"])
        root_spec = parse_root_specifier(request.specifier)
        current_lock = parse_current_lock(payload["current_lock"])
    except SnapshotError as exc:
        return JSONResponse(
            status_code=422,
            content={"status": "invalid_snapshot", "message": str(exc)},
        )

    if not repo.known(root_canonical):
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_snapshot",
                "message": f"根包 {root_canonical!r} 不存在于仓库快照中",
            },
        )

    if target_canonical not in current_lock:
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_snapshot",
                "message": f"目标包 {target_canonical!r} 不存在于 current_lock 中",
            },
        )

    resolver = Resolver(repo, allow_prereleases=request.allow_prereleases)
    try:
        state = resolver.upgrade(
            root_canonical,
            root_spec,
            request.specifier,
            target=target_canonical,
            current_version=current_lock[target_canonical],
            reference=current_lock,
        )
    except ResolutionLimit:
        return JSONResponse(
            status_code=409,
            content=_conflict_payload(resolver.build_conflicts(limit=True)),
        )

    if state is None:
        content = _conflict_payload(resolver.build_conflicts())
        content["message"] = (
            f"目标包 {target_canonical!r} 没有可行的升级："
            "不存在严格高于当前版本且能完成整体解析的候选版本"
        )
        return JSONResponse(status_code=409, content=content)

    locked = {pkg: str(ver) for pkg, ver in sorted(state.selection.items())}
    return {
        "status": "ok",
        "root": root_canonical,
        "root_version": str(state.selection[root_canonical]),
        "locked": locked,
        "changes": _lock_changes(current_lock, locked),
    }


@app.post(
    "/audit-lock",
    summary="审计现有锁定清单而不重新求解",
    response_model=AuditLockResponse,
    responses={
        422: {"model": InvalidSnapshotResponse, "description": "快照或 current_lock 非法"},
    },
)
def audit_lock_endpoint(request: AuditLockRequest):
    root_canonical = canonical(request.root)
    payload = request.model_dump()

    try:
        repo = build_repository(payload["repository"], payload["target_environment"])
        root_spec = parse_root_specifier(request.specifier)
        current_lock = parse_current_lock(payload["current_lock"])
    except SnapshotError as exc:
        return JSONResponse(
            status_code=422,
            content={"status": "invalid_snapshot", "message": str(exc)},
        )

    if not repo.known(root_canonical):
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_snapshot",
                "message": f"根包 {root_canonical!r} 不存在于仓库快照中",
            },
        )

    result = audit_lock(
        repo,
        root_name=root_canonical,
        root_spec=root_spec,
        root_raw=request.specifier,
        current_lock=current_lock,
        allow_prereleases=request.allow_prereleases,
    )
    return {"status": "ok", **result}


@app.post(
    "/dependency-paths",
    summary="解释目标包为何出现在锁定清单中：根包到目标包的全部最短路径",
    response_model=DependencyPathsResponse,
    responses={
        409: {"model": InvalidLockResponse, "description": "锁定清单未通过审计"},
        422: {"model": InvalidSnapshotResponse, "description": "快照或 current_lock 非法"},
    },
)
def dependency_paths_endpoint(request: DependencyPathsRequest):
    root_canonical = canonical(request.root)
    target_canonical = canonical(request.target)
    payload = request.model_dump()

    try:
        repo = build_repository(payload["repository"], payload["target_environment"])
        root_spec = parse_root_specifier(request.specifier)
        current_lock = parse_current_lock(payload["current_lock"])
    except SnapshotError as exc:
        return JSONResponse(
            status_code=422,
            content={"status": "invalid_snapshot", "message": str(exc)},
        )

    if not repo.known(root_canonical):
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_snapshot",
                "message": f"根包 {root_canonical!r} 不存在于仓库快照中",
            },
        )

    # 先按现有审计规则校验锁定清单；无效则带确定性排序的问题返回 409，
    # 不执行任何路径查询。
    result = audit_lock(
        repo,
        root_name=root_canonical,
        root_spec=root_spec,
        root_raw=request.specifier,
        current_lock=current_lock,
        allow_prereleases=request.allow_prereleases,
    )
    if not result["valid"]:
        return JSONResponse(
            status_code=409,
            content={
                "status": "invalid_lock",
                "message": "锁定清单未通过审计，未执行路径查询",
                "issues": result["issues"],
            },
        )

    # 有效锁：按锁定版本与目标环境激活的依赖建图（无版本选择）。
    graph = build_locked_graph(
        repo, root_name=root_canonical, current_lock=current_lock
    )
    paths = shortest_paths(graph, root_canonical, target_canonical)
    return {
        "status": "ok",
        "root": root_canonical,
        "target": target_canonical,
        "reachable": bool(paths),
        "paths": paths,
    }
