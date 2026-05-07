import argparse
import logging

from core.config import CONFIG
from core.infra.file_store import FileStore
from core.infra.index_repository import IndexRepository
from core.infra.llm_client import LLMClient
from core.infra.prompt_registry import PromptRegistry
from core.parsers import SafeParser
from core.services.audit_service import AuditService
from core.services.cleaner import CleanerService
from core.services.compiler import CompilerService
from core.services.indexer import IndexPolicyService
from core.services.log_service import LogService
from core.services.query_service import QueryService
from core.services.research_draft_service import ResearchDraftService
from core.services.source_page import SourcePageService
from core.services.splitter import SplitService
from core.services.topicer import TopicPageService
from helper import print_help


def _setup_logging() -> None:
    """根据配置初始化日志，保证全链路输出可追踪。"""
    level = getattr(logging, CONFIG.log_level, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def build_app_services():
    """创建并返回 knowflow 主流程所需的服务实例。"""
    file_store = FileStore()
    index_repository = IndexRepository(CONFIG, file_store)
    llm_client = LLMClient(CONFIG)
    prompt_registry = PromptRegistry(CONFIG)

    cleaner = CleanerService(
        CONFIG,
        index_repository,
        llm_client,
        prompt_registry,
        file_store,
        SafeParser,
    )
    compiler = CompilerService(
        CONFIG,
        index_repository,
        llm_client,
        prompt_registry,
        file_store,
        SafeParser,
    )
    policy = IndexPolicyService(index_repository, llm_client, prompt_registry, SafeParser)
    split_service = SplitService(index_repository, llm_client, prompt_registry, SafeParser)
    topic_page = TopicPageService(
        CONFIG,
        index_repository,
        llm_client,
        prompt_registry,
        file_store,
    )
    source_page = SourcePageService(CONFIG, file_store, index_repository)
    log_service = LogService(CONFIG, file_store)
    audit_service = AuditService(CONFIG, index_repository, file_store)
    research_draft_service = ResearchDraftService(CONFIG, index_repository, llm_client, prompt_registry, file_store)
    query_service = QueryService(CONFIG, file_store)
    return cleaner, compiler, policy, split_service, topic_page, source_page, log_service, audit_service, research_draft_service, query_service


def _run_private_pipeline(cleaner, compiler, policy, split_service, topic_page, source_page, *, no_split: bool) -> None:
    cleaner.clean()
    policy.reconcile_index()
    if not no_split:
        split_service.split()
    topic_page.build_topics()
    source_page.build_sources()

    try:
        compiler.build_private_graph()
    except Exception:
        logging.getLogger(__name__).exception("private knowledge graph 生成失败")


def _run_private_incremental_pipeline(cleaner, compiler, policy, split_service, topic_page, source_page, *, raw_paths, no_split: bool) -> None:
    cleaner.clean_paths(raw_paths)
    policy.reconcile_index()
    if not no_split:
        split_service.split()
    topic_page.build_topics()
    source_page.build_sources()

    try:
        compiler.build_private_graph()
    except Exception:
        logging.getLogger(__name__).exception("private knowledge graph 生成失败")


def _run_public_pipeline(compiler, audit_service, log_service, *, command_name: str, write_log: bool) -> dict:
    compiler.compile()
    payload = audit_service.build_report()
    if write_log:
        log_service.append_run(command_name)
    return payload


def _run_only_pipeline(cleaner, compiler, policy, topic_page, source_page, log_service, audit_service, *, raw_path: str, command_name: str) -> None:
    cleaner.clean_paths([raw_path])
    policy.reconcile_index()
    topic_page.build_topics()
    source_page.build_sources()

    try:
        compiler.build_private_graph()
    except Exception:
        logging.getLogger(__name__).exception("private knowledge graph 生成失败")

    _run_public_pipeline(
        compiler,
        audit_service,
        log_service,
        command_name=command_name,
        write_log=True,
    )


def _run_build(cleaner, compiler, policy, split_service, topic_page, source_page, log_service, audit_service, research_draft_service, *, no_split: bool, command_name: str) -> None:
    _run_private_pipeline(
        cleaner,
        compiler,
        policy,
        split_service,
        topic_page,
        source_page,
        no_split=no_split,
    )
    payload = _run_public_pipeline(
        compiler,
        audit_service,
        log_service,
        command_name=command_name,
        write_log=False,
    )
    research_manifest = research_draft_service.materialize_from_audit(payload)

    created_raw_paths = [
        str(item.get("raw_path") or "").strip()
        for item in research_manifest.get("created", [])
        if isinstance(item, dict) and str(item.get("raw_path") or "").strip()
    ]
    if created_raw_paths:
        _run_private_incremental_pipeline(
            cleaner,
            compiler,
            policy,
            split_service,
            topic_page,
            source_page,
            raw_paths=created_raw_paths,
            no_split=no_split,
        )
        _run_public_pipeline(
            compiler,
            audit_service,
            log_service,
            command_name=command_name,
            write_log=False,
        )

    log_service.append_run(command_name)


def _run_lint(audit_service) -> int:
    payload = audit_service.build_report()
    findings = payload.get("findings", {}) if isinstance(payload, dict) else {}
    loop = payload.get("loop", {}) if isinstance(payload, dict) else {}

    blocking_items = [
        ("raw_without_meta", "raw 文档缺少 metadata"),
        ("posts_missing_concepts", "文章缺少 concepts"),
        ("posts_missing_sources", "文章缺少 sources"),
        ("concept_refs_without_pages", "frontmatter 引用了不存在的概念页"),
        ("source_refs_without_pages", "frontmatter 引用了不存在的来源页"),
    ]
    warning_items = [
        ("unused_sources", "来源页未进入任何 post"),
        ("oversized_topics", "topic 仍然偏大"),
        ("topics_without_summary", "topic 缺少 summary"),
        ("single_source_dense_topics", "单来源高密度主题"),
    ]

    blocking_count = 0
    warning_count = 0

    print("lint summary")
    for key, label in blocking_items:
        items = findings.get(key, [])
        count = len(items) if isinstance(items, list) else 0
        blocking_count += count
        print(f"- blocking {label}: {count}")

    for key, label in warning_items:
        items = findings.get(key, [])
        count = len(items) if isinstance(items, list) else 0
        warning_count += count
        print(f"- warning {label}: {count}")

    for stage in ("ingest", "governance", "linking", "insight", "research"):
        info = loop.get(stage, {})
        print(f"- stage {stage}: {info.get('status', 'unknown')}")

    if blocking_count > 0:
        if len(findings.get("raw_without_meta", [])) > 0:
            print("- hint: 先执行 python3 knowflow.py build 重新生成 metadata")
        print("lint failed")
        return 1

    print("lint ok" if warning_count == 0 else "lint ok with warnings")
    return 0


def _run_build_target(
    target: str,
    *,
    cleaner,
    compiler,
    policy,
    split_service,
    topic_page,
    source_page,
    log_service,
    audit_service,
    research_draft_service,
    no_split: bool,
) -> int:
    normalized = str(target or "all").strip().lower()
    command_name = "build" if normalized == "all" else f"build {normalized}"

    if normalized == "all":
        _run_build(
            cleaner,
            compiler,
            policy,
            split_service,
            topic_page,
            source_page,
            log_service,
            audit_service,
            research_draft_service,
            no_split=no_split,
            command_name=command_name,
        )
        return 0

    if normalized == "private":
        _run_private_pipeline(
            cleaner,
            compiler,
            policy,
            split_service,
            topic_page,
            source_page,
            no_split=no_split,
        )
        log_service.append_run(command_name)
        return 0

    if normalized == "public":
        _run_public_pipeline(
            compiler,
            audit_service,
            log_service,
            command_name=command_name,
            write_log=True,
        )
        return 0

    if normalized == "clean":
        cleaner.clean()
        return 0
    if normalized == "index":
        policy.reconcile_index()
        return 0
    if normalized == "split":
        split_service.split()
        return 0
    if normalized == "topics":
        topic_page.build_topics()
        return 0
    if normalized == "sources":
        source_page.build_sources()
        return 0
    if normalized == "compile":
        compiler.compile()
        return 0
    if normalized == "audit":
        audit_service.build_report()
        return 0
    if normalized == "graph":
        compiler.build_graph()
        return 0
    if normalized == "log":
        log_service.append_run(command_name)
        return 0

    print(f"unknown build target: {target}")
    print_help()
    return 1


def main() -> None:
    """CLI 入口。"""
    _setup_logging()
    logger = logging.getLogger(__name__)

    cleaner, compiler, policy, split_service, topic_page, source_page, log_service, audit_service, research_draft_service, query_service = build_app_services()
    p = argparse.ArgumentParser(add_help=False, prog="knowflow")
    p.add_argument("cmd", nargs="?")
    p.add_argument("--no-split", action="store_true", help="build 时跳过 split")
    p.add_argument("--only", metavar="PATH", help="增量构建：只处理指定的 raw 目录")
    args, remaining = p.parse_known_args()
    args.rest = remaining

    if not args.cmd:
        print_help()
        return

    logger.debug("执行命令: %s", args.cmd)
    if args.cmd == "build":
        if args.only:
            if args.rest:
                print("build --only 不能与 stage 参数一起使用")
                print_help()
                raise SystemExit(1)
            raise SystemExit(
                _run_only_pipeline(
                    cleaner,
                    compiler,
                    policy,
                    topic_page,
                    source_page,
                    log_service,
                    audit_service,
                    raw_path=args.only,
                    command_name=f"build --only {args.only}",
                )
            )
        if len(args.rest) > 1:
            print(f"build 只接受一个 stage，收到: {' '.join(args.rest)}")
            print_help()
            raise SystemExit(1)
        raise SystemExit(
            _run_build_target(
                args.rest[0] if args.rest else "all",
                cleaner=cleaner,
                compiler=compiler,
                policy=policy,
                split_service=split_service,
                topic_page=topic_page,
                source_page=source_page,
                log_service=log_service,
                audit_service=audit_service,
                research_draft_service=research_draft_service,
                no_split=args.no_split,
            )
        )
    elif args.cmd == "lint":
        raise SystemExit(_run_lint(audit_service))
    elif args.cmd == "query":
        phrase = " ".join(args.rest).strip()
        if not phrase:
            print("query 需要关键字，例如: python3 knowflow.py query ai agent")
            raise SystemExit(1)
        raise SystemExit(query_service.print_query(phrase))
    elif args.cmd == "research":
        payload = audit_service.build_report()
        research_draft_service.materialize_from_audit(payload)
    else:
        print(f"unknown command: {args.cmd}")
        print_help()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
