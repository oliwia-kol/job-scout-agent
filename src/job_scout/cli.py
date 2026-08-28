"""Local command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import atexit
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from .career_benchmark import run_career_benchmark, write_career_benchmark
from .collector import collect_sources, write_collection
from .demo_data import load_candidate_profile, load_frozen_demo_offers
from .demo_flow import (
    load_extractions,
    run_demo_flow_db,
)
from .extractor_benchmark import run_extractor_benchmark, write_benchmark_report
from .review_report import write_review_report
from .settings import Settings
from .source_health import scan_sources, write_report
from .sources import load_sources
from .storage import initialize_database, persist_collection, persist_monitored_collection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Job Scout local-first MVP")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db", help="create or update the local SQLite schema")
    subcommands.add_parser("check-config", help="validate the official career sources")
    scan = subcommands.add_parser("scan-sources", help="live-check sources and clean one offer")
    scan.add_argument("--output", type=Path, default=Path("data/source-health.json"))
    collect = subcommands.add_parser("collect", help="discover and clean offers to JSON")
    collect.add_argument("--source", action="append", dest="source_ids")
    collect.add_argument("--limit-per-source", type=int)
    collect.add_argument("--max-candidates-per-source", type=int, default=30)
    collect.add_argument("--all-offers", action="store_true")
    collect.add_argument("--output", type=Path, default=Path("data/clean-offers.json"))
    collect.add_argument("--review-html", type=Path)
    import_json = subcommands.add_parser(
        "import-json", help="import an existing clean collection snapshot into SQLite"
    )
    import_json.add_argument("path", type=Path)
    serve = subcommands.add_parser("serve", help="run the private local offers panel")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    benchmark = subcommands.add_parser(
        "benchmark-extractor", help="run Gemma extraction on the frozen 10-offer sample"
    )
    benchmark.add_argument(
        "--manifest", type=Path, default=Path("config/demo/frozen-offers-v1.json")
    )
    flow = subcommands.add_parser(
        "demo-flow", help="evaluate and self-review all frozen offers with a test profile"
    )
    flow.add_argument("--profile", type=Path, default=Path("config/demo/dummy-profile-v1.json"))
    flow.add_argument("--manifest", type=Path, default=Path("config/demo/frozen-offers-v1.json"))
    flow.add_argument(
        "--extractions",
        type=Path,
        default=Path("data/demo/extractor-benchmark-qwen-v3.json"),
    )
    notion_demo = subcommands.add_parser(
        "notion-demo", help="run local DeepSeek evaluation and save dummy offers to Notion"
    )
    notion_demo.add_argument(
        "--profile", type=Path, default=Path("config/demo/dummy-profile-v1.json")
    )
    notion_demo.add_argument(
        "--manifest", type=Path, default=Path("config/demo/frozen-offers-v1.json")
    )
    notion_demo.add_argument("--limit", type=int, default=1)
    notion_scan = subcommands.add_parser(
        "notion-scan",
        help="collect live offers and run the Notion-first local-model demo",
    )
    notion_scan.add_argument("--mode", choices=("quick", "full"), default="quick")
    notion_scan.add_argument(
        "--profile", type=Path, default=Path("config/demo/dummy-profile-v1.json")
    )
    demo_v2_scan = subcommands.add_parser(
        "demo-v2-scan",
        help="refresh the Demo v2 local panel with one offer per source or the full collection",
    )
    demo_v2_scan.add_argument("--mode", choices=("sample", "full"), default="sample")
    demo_v2_scan.add_argument(
        "--notify",
        action="store_true",
        help="send a minimal Telegram run summary after SQLite persistence",
    )
    demo_v2_scan.add_argument(
        "--output", type=Path, default=Path("data/demo-v2/latest-collection.json")
    )
    subcommands.add_parser("telegram-test", help="send a real [TEST] Telegram message")
    career_benchmark = subcommands.add_parser(
        "benchmark-career",
        help="run the five-scenario live Bielik career-interview acceptance benchmark",
    )
    career_benchmark.add_argument(
        "--output",
        type=Path,
        default=Path("data/benchmarks/career-interview-bielik-v1.json"),
    )
    subcommands.add_parser(
        "language-backfill",
        help="detect PL/EN/mixed for all offers already stored in SQLite",
    )
    subcommands.add_parser(
        "ledger-backfill",
        help="create baseline content versions for offers stored before the ledger",
    )
    subcommands.add_parser(
        "repair-offer-text",
        help="remove legacy HTML markup from stored offer text and queue clean re-evaluation",
    )
    subcommands.add_parser(
        "evidence-backfill",
        help="create deterministic section evidence for current stored offer versions",
    )
    golden_validate = subcommands.add_parser(
        "validate-golden-dataset",
        help="validate the manually curated Golden dataset v0 before calibration",
    )
    golden_validate.add_argument(
        "--path", type=Path, default=Path("docs/golden-dataset/v0/cases.json")
    )
    golden_extract = subcommands.add_parser(
        "verify-golden-extraction",
        help="verify deterministic stored offer evidence against Golden dataset v0",
    )
    golden_extract.add_argument(
        "--path", type=Path, default=Path("docs/golden-dataset/v0/cases.json")
    )
    golden_quiz = subcommands.add_parser(
        "build-golden-quiz-pack",
        help="prepare a local cited quiz pack for human Golden dataset decisions",
    )
    golden_quiz.add_argument("--offer-id", action="append", type=int, dest="offer_ids")
    golden_quiz.add_argument(
        "--output", type=Path, default=Path("docs/golden-dataset/v0/quiz-pack.json")
    )
    golden_v1 = subcommands.add_parser(
        "validate-golden-v1",
        help="validate Golden v1 balance, holdout isolation and instruction hygiene",
    )
    golden_v1.add_argument(
        "--root", type=Path, default=Path("docs/golden-dataset/v1")
    )
    golden_v1.add_argument(
        "--private-key",
        type=Path,
        default=Path("data/golden-private/v1/validation.answer-key.json"),
    )
    golden_score = subcommands.add_parser(
        "score-golden-v1",
        help="score already persisted Golden v1 model outputs against the private holdout key",
    )
    golden_score.add_argument("results", type=Path)
    golden_score.add_argument(
        "--inputs",
        type=Path,
        default=Path("docs/golden-dataset/v1/validation.inputs.synthetic.json"),
    )
    golden_score.add_argument(
        "--private-key",
        type=Path,
        default=Path("data/golden-private/v1/validation.answer-key.json"),
    )
    golden_score.add_argument("--report", type=Path, required=True)
    golden_calibration_run = subcommands.add_parser(
        "run-golden-v1-calibration",
        help="run the local model on labelled Golden v1 calibration cases",
    )
    golden_calibration_run.add_argument(
        "--cases",
        type=Path,
        default=Path("docs/golden-dataset/v1/calibration.synthetic.json"),
    )
    golden_calibration_run.add_argument(
        "--profile-context",
        type=Path,
        default=Path("docs/golden-dataset/v1/PROFILE_CONTEXT.json"),
    )
    golden_calibration_run.add_argument(
        "--instruction",
        type=Path,
        default=Path("docs/golden-dataset/v1/MODEL_EVALUATION_INSTRUCTION.md"),
    )
    golden_calibration_run.add_argument(
        "--output",
        type=Path,
        default=Path("data/benchmarks/golden-v1-calibration-results.json"),
    )
    golden_calibration_run.add_argument("--max-tokens", type=int, default=1400)
    golden_calibration_run.add_argument("--context-size", type=int, default=8192)
    golden_calibration_run.add_argument(
        "--case", action="append", dest="case_ids", help="run only a selected case id"
    )
    golden_calibration_run.add_argument(
        "--resume", action="store_true", help="keep completed cases and rerun only missing ones"
    )
    golden_calibration_run.add_argument(
        "--reasoning-budget",
        type=int,
        default=384,
        help="maximum reasoning tokens for reasoning-capable local models",
    )
    golden_calibration_score = subcommands.add_parser(
        "score-golden-v1-calibration",
        help="score Golden v1 calibration outputs against their public labels",
    )
    golden_calibration_score.add_argument("results", type=Path)
    golden_calibration_score.add_argument(
        "--cases",
        type=Path,
        default=Path("docs/golden-dataset/v1/calibration.synthetic.json"),
    )
    golden_calibration_score.add_argument("--report", type=Path, required=True)
    golden_calibration_score.add_argument(
        "--allow-partial", action="store_true", help="score a selected calibration subset"
    )
    runtime_smoke = subcommands.add_parser(
        "runtime-smoke",
        help="probe a local llama.cpp or LM Studio OpenAI-compatible runtime",
    )
    runtime_smoke.add_argument(
        "--runtime", choices=("llama_cpp", "lm_studio"), default="llama_cpp"
    )
    backup = subcommands.add_parser(
        "backup-db", help="create a verified SQLite backup and enforce local retention"
    )
    backup.add_argument("--retention", type=int, default=14)
    restore_drill = subcommands.add_parser(
        "restore-drill", help="restore a backup to a separate SQLite file and verify it"
    )
    restore_drill.add_argument("backup", type=Path)
    restore_drill.add_argument("--output", type=Path, required=True)
    translate_offer = subcommands.add_parser(
        "translate-offer",
        help="create a local, evidence-mapped canonical English translation",
    )
    translate_offer.add_argument("offer_id", type=int)
    translate_offer.add_argument(
        "--approve",
        action="store_true",
        help="approve the validated translation immediately",
    )
    language_benchmark = subcommands.add_parser(
        "benchmark-language",
        help="compare direct multilingual evaluation with canonical English",
    )
    language_benchmark.add_argument(
        "--dataset",
        type=Path,
        default=Path("config/demo/offers-language-pairs-v1.json"),
    )
    language_benchmark.add_argument(
        "--profile",
        type=Path,
        default=Path("config/demo/dummy-profile-v1.json"),
    )
    language_benchmark.add_argument(
        "--output",
        type=Path,
        default=Path("data/benchmarks/language-strategy-v1.json"),
    )
    language_benchmark.add_argument(
        "--pair",
        action="append",
        help="run only a selected pair id; repeat to include more pairs",
    )
    bielik_offer_benchmark = subcommands.add_parser(
        "benchmark-offers-bielik",
        help="evaluate selected stored offers with Bielik without replacing live scores",
    )
    bielik_offer_benchmark.add_argument(
        "--profile-id", default="candidate-profile"
    )
    bielik_offer_benchmark.add_argument(
        "--offer-id", action="append", type=int, dest="offer_ids"
    )
    bielik_offer_benchmark.add_argument(
        "--output",
        type=Path,
        default=Path("data/benchmarks/bielik-offer-evaluation-v1.json"),
    )
    career_benchmark.add_argument(
        "--scenario",
        action="append",
        choices=(
            "cv-audit",
            "follow-up-depth",
            "skip-redundant-question",
            "negative-preference",
            "uncertainty-discipline",
        ),
        help="run only the selected scenario; repeat for more than one",
    )

    benchmark.add_argument(
        "--output", type=Path, default=Path("data/demo/extractor-benchmark-v1.json")
    )
    return parser


def run() -> int:
    args = build_parser().parse_args()
    settings = Settings.from_env()

    if args.command == "init-db":
        initialize_database(settings.database_path)
        print(f"SQLite initialized: {settings.database_path}")
        return 0

    if args.command == "language-backfill":
        from .storage import backfill_offer_languages

        counts = backfill_offer_languages(settings.database_path)
        print(
            "Offer languages updated: "
            + ", ".join(f"{language}={count}" for language, count in counts.items())
        )
        return 0

    if args.command == "ledger-backfill":
        from .storage import backfill_offer_ledger

        created = backfill_offer_ledger(settings.database_path)
        print(f"Offer ledger baseline versions created: {created}")
        return 0

    if args.command == "repair-offer-text":
        from .storage import repair_offer_text_markup

        repaired = repair_offer_text_markup(settings.database_path)
        print(f"Offers with repaired text: {repaired}")
        return 0

    if args.command == "evidence-backfill":
        from .storage import backfill_offer_section_evidence

        result = backfill_offer_section_evidence(settings.database_path)
        print(
            "Offer evidence backfill: "
            f"offers={result['offers_processed']}, evidence={result['evidence_created']}"
        )
        return 0

    if args.command == "validate-golden-dataset":
        from .golden_dataset import load_golden_dataset

        try:
            dataset = load_golden_dataset(args.path)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        cases = dataset["cases"]
        calibration = sum(case["split"] == "calibration" for case in cases)
        print(
            "Golden dataset valid: "
            f"cases={len(cases)}, calibration={calibration}, validation={len(cases) - calibration}"
        )
        return 0

    if args.command == "verify-golden-extraction":
        from .golden_dataset import verify_golden_extraction

        try:
            result = verify_golden_extraction(args.path, settings.database_path)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            "Golden extraction verified: "
            f"cases={result['cases_verified']}, validation={result['validation_cases']}"
        )
        return 0

    if args.command == "build-golden-quiz-pack":
        from .golden_dataset import DEFAULT_QUIZ_OFFER_IDS, build_golden_quiz_pack

        try:
            pack = build_golden_quiz_pack(
                settings.database_path, args.offer_ids or list(DEFAULT_QUIZ_OFFER_IDS)
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Golden quiz pack written: {args.output} ({len(pack['cases'])} cases)")
        return 0

    if args.command == "validate-golden-v1":
        from .golden_v1 import validate_golden_v1

        try:
            report = validate_golden_v1(
                calibration_path=args.root / "calibration.synthetic.json",
                validation_inputs_path=args.root / "validation.inputs.synthetic.json",
                validation_key_path=args.private_key,
                prefilter_path=args.root / "prefilter.synthetic.json",
                instruction_path=args.root / "MODEL_EVALUATION_INSTRUCTION.md",
                profile_context_path=args.root / "PROFILE_CONTEXT.json",
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            "Golden v1 valid: "
            f"calibration={report['calibration_cases']}, "
            f"validation={report['validation_cases']}, "
            f"prefilter={report['prefilter_cases']}, tags={report['unique_tags']}"
        )
        print(f"Validation inputs SHA-256: {report['validation_inputs_sha256']}")
        print(f"Private key SHA-256: {report['validation_key_sha256']}")
        print(f"Instruction SHA-256: {report['instruction_sha256']}")
        print(f"Profile context SHA-256: {report['profile_context_sha256']}")
        return 0

    if args.command == "score-golden-v1":
        from .golden_v1 import score_golden_v1_results

        try:
            report = score_golden_v1_results(
                results_path=args.results,
                validation_inputs_path=args.inputs,
                validation_key_path=args.private_key,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            "Golden v1 scored: "
            f"accuracy={report['decision_accuracy']:.3f}, macro_f1={report['macro_f1']:.3f}, "
            f"grounded={report['grounded_evidence_rate']:.3f}, "
            f"passed={report['passed_working_thresholds']}"
        )
        print(f"Report: {args.report}")
        return 0

    if args.command == "run-golden-v1-calibration":
        from .golden_runner import run_golden_v1_cases
        from .llama_server import LlamaServerManager
        from .local_llm import LocalLlmClient

        if args.context_size < 4096:
            raise SystemExit("--context-size must be at least 4096")
        if args.max_tokens < 256:
            raise SystemExit("--max-tokens must be at least 256")
        if args.reasoning_budget < 0:
            raise SystemExit("--reasoning-budget cannot be negative")

        async def run_calibration():
            async with LlamaServerManager(
                executable=settings.llama_server_executable,
                model_path=settings.local_llm_model_path,
                base_url=settings.local_llm_base_url,
                context_size=args.context_size,
                reasoning=settings.local_llm_reasoning,
                reasoning_budget=(
                    args.reasoning_budget if settings.local_llm_reasoning else None
                ),
                startup_timeout_seconds=120,
                log_path=Path("data/logs/golden-v1-calibration-llama.log"),
            ):
                async with LocalLlmClient(
                    settings.local_llm_base_url,
                    timeout_seconds=settings.local_llm_stage_timeout_seconds,
                ) as client:
                    return await run_golden_v1_cases(
                        client=client,
                        model=settings.local_llm_model,
                        cases_path=args.cases,
                        profile_context_path=args.profile_context,
                        instruction_path=args.instruction,
                        output_path=args.output,
                        run_type="calibration",
                        max_tokens=args.max_tokens,
                        reasoning=settings.local_llm_reasoning,
                        reasoning_budget=(
                            args.reasoning_budget if settings.local_llm_reasoning else None
                        ),
                        case_ids=set(args.case_ids) if args.case_ids else None,
                        resume=args.resume,
                    )

        result = asyncio.run(run_calibration())
        print(
            f"Golden v1 calibration: status={result['status']}, "
            f"completed={len(result['results'])}, failed={len(result['errors'])}"
        )
        print(f"Results: {args.output}")
        return 0 if result["status"] == "completed" else 1

    if args.command == "score-golden-v1-calibration":
        from .golden_v1 import score_golden_v1_calibration_results

        try:
            report = score_golden_v1_calibration_results(
                results_path=args.results,
                calibration_path=args.cases,
                allow_partial=args.allow_partial,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            "Golden v1 calibration scored: "
            f"accuracy={report['decision_accuracy']:.3f}, "
            f"macro_f1={report['macro_f1']:.3f}, "
            f"grounded={report['grounded_evidence_rate']:.3f}, "
            f"passed={report['passed_working_thresholds']}"
        )
        print(f"Report: {args.report}")
        return 0

    if args.command == "runtime-smoke":
        from .model_runtime import probe_runtime

        base_url = (
            settings.local_llm_base_url
            if args.runtime == "llama_cpp"
            else settings.lm_studio_base_url
        )
        status = asyncio.run(probe_runtime(args.runtime, base_url))
        print(
            f"{status.kind}: {'ready' if status.ready else 'not ready'} · "
            f"{status.base_url} · {status.detail}"
        )
        for model in status.models:
            print(f"model: {model}")
        return 0 if status.ready else 1

    if args.command == "backup-db":
        import sqlite3

        if args.retention < 1:
            raise SystemExit("--retention must be at least 1")
        source = settings.database_path
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = source.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        destination = backup_dir / f"job-scout-{timestamp}.db"
        with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as backup_db:
            source_db.backup(backup_db)
            result = backup_db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            destination.unlink(missing_ok=True)
            raise SystemExit(f"backup integrity check failed: {result}")
        backups = sorted(backup_dir.glob("job-scout-*.db"), reverse=True)
        for stale in backups[args.retention :]:
            stale.unlink()
        print(f"Verified backup: {destination}")
        return 0

    if args.command == "restore-drill":
        from .storage import restore_backup_drill

        result = restore_backup_drill(args.backup, args.output)
        print(
            "Restore drill verified: "
            f"offers={result['offers']}, profiles={result['profiles']}, "
            f"runs={result['runs']}, events={result['events']}"
        )
        return 0

    if args.command == "translate-offer":
        from .language import TRANSLATOR_PROMPT_VERSION, LocalOfferTranslator
        from .llama_server import LlamaServerManager
        from .local_llm import LocalLlmClient
        from .storage import (
            approve_offer_translation,
            get_offer,
            save_offer_translation,
        )

        offer = get_offer(settings.database_path, args.offer_id)
        if not offer:
            raise SystemExit(f"Offer {args.offer_id} not found")
        source_language = offer["original_language"]
        if source_language == "en":
            raise SystemExit("Offer is already in English; canonical translation is unnecessary")
        if source_language not in {"pl", "mixed"}:
            raise SystemExit("Detect or correct the offer language before translation")
        source_text = offer["offer"].get("analysis_text") or offer["offer"]["description"]

        async def translate():
            async with LlamaServerManager(
                executable=settings.llama_server_executable,
                model_path=settings.career_llm_model_path,
                base_url=settings.career_llm_base_url,
                context_size=settings.local_llm_context_size,
            ):
                async with LocalLlmClient(
                    settings.career_llm_base_url,
                    timeout_seconds=settings.local_llm_stage_timeout_seconds,
                ) as client:
                    translator = LocalOfferTranslator(client, settings.career_llm_model)
                    return await translator.translate(source_text, source_language)

        response = asyncio.run(translate())
        translation_id = f"offer-translation-{uuid.uuid4().hex}"
        save_offer_translation(
            settings.database_path,
            translation_id=translation_id,
            offer_id=args.offer_id,
            source_text=source_text,
            source_language=source_language,
            translation=response.value,
            translator_model=settings.career_llm_model,
            prompt_version=TRANSLATOR_PROMPT_VERSION,
        )
        if args.approve:
            approve_offer_translation(settings.database_path, translation_id)
        print(
            f"Translation {translation_id}: "
            f"{'approved' if args.approve else 'draft'}, {response.elapsed_ms} ms, "
            f"retry={response.retries}"
        )
        return 0

    if args.command == "benchmark-language":
        from .language_benchmark import (
            load_language_pairs,
            run_language_strategy_benchmark,
            write_language_benchmark,
        )
        from .llama_server import LlamaServerManager
        from .local_llm import LocalLlmClient

        dataset = load_language_pairs(args.dataset)
        if args.pair:
            selected = [pair for pair in dataset.pairs if pair.id in set(args.pair)]
            missing = sorted(set(args.pair) - {pair.id for pair in selected})
            if missing:
                raise SystemExit(f"Unknown language pair: {', '.join(missing)}")
            dataset = dataset.model_copy(update={"pairs": selected})
        profile = load_candidate_profile(args.profile)

        async def benchmark_languages():
            async with LlamaServerManager(
                executable=settings.llama_server_executable,
                model_path=settings.local_llm_model_path,
                base_url=settings.local_llm_base_url,
                context_size=settings.local_llm_context_size,
                reasoning=settings.local_llm_reasoning,
                reasoning_budget=settings.local_llm_reasoning_budget,
            ):
                async with LocalLlmClient(
                    settings.local_llm_base_url,
                    timeout_seconds=settings.local_llm_stage_timeout_seconds,
                ) as client:
                    return await run_language_strategy_benchmark(
                        dataset,
                        profile,
                        client=client,
                        model=settings.local_llm_model,
                    )

        report = asyncio.run(benchmark_languages())
        write_language_benchmark(report, args.output)
        print(
            f"Language benchmark: completed={report['completed_pairs']}/"
            f"{report['total_pairs']}, stability={report['recommendation_stability_rate']}, "
            f"median_delta={report['median_absolute_score_delta']}"
        )
        print(f"Report: {args.output}")
        return 0 if report["failed_pairs"] == 0 else 1

    if args.command == "benchmark-offers-bielik":
        from .domain import CandidateProfile
        from .local_llm import LocalLlmClient
        from .offer_evaluation_benchmark import (
            run_bielik_offer_benchmark,
            write_bielik_offer_benchmark,
        )
        from .storage import get_user_profile, is_profile_ready_for_scoring

        stored_profile = get_user_profile(settings.database_path, args.profile_id)
        if not stored_profile or not is_profile_ready_for_scoring(
            settings.database_path, args.profile_id
        ):
            raise SystemExit("A ready, approved profile is required")
        profile = CandidateProfile.model_validate(stored_profile["profile"])
        offer_ids = args.offer_ids or [10, 15, 5]

        async def benchmark_offers():
            async with LocalLlmClient(
                settings.career_llm_base_url,
                timeout_seconds=settings.local_llm_stage_timeout_seconds,
            ) as client:
                await client.health()
                return await run_bielik_offer_benchmark(
                    settings.database_path,
                    offer_ids=offer_ids,
                    profile=profile,
                    client=client,
                    model=settings.career_llm_model,
                )

        report = asyncio.run(benchmark_offers())
        write_bielik_offer_benchmark(report, args.output)
        print(
            f"Bielik offer benchmark: completed={report['completed']}/{report['total']}, "
            f"mean_total_ms={report['mean_total_ms']}"
        )
        for item in report["results"]:
            if item["status"] == "completed":
                print(
                    f"PASS {item['company']} — {item['title']} "
                    f"score={item['assessment']['final_score']:.1f} "
                    f"recommendation={item['assessment']['recommendation']} "
                    f"total={item['timings_ms']['total']}ms"
                )
            else:
                print(
                    f"FAIL offer={item['offer_id']} "
                    f"{item.get('error', 'unknown error')}"
                )
        print(f"Report: {args.output}")
        return 0 if report["failed"] == 0 else 1

    if args.command == "serve":
        from .web import create_app

        initialize_database(settings.database_path)
        uvicorn.run(create_app(settings.database_path), host=args.host, port=args.port)
        return 0

    if args.command == "benchmark-extractor":
        project_root = Path.cwd()
        offers = load_frozen_demo_offers(args.manifest, project_root)
        report = asyncio.run(run_extractor_benchmark(offers, settings, args.output))
        write_benchmark_report(report, args.output)
        print(
            f"Extractor benchmark: completed={report.completed}/{report.total}, "
            f"failed={report.failed}"
        )
        for item in report.items:
            flags = ",".join(item.quality_flags) or "none"
            print(
                f"{item.status:9} {item.company:12} {item.elapsed_ms or 0:>6}ms "
                f"retry={item.retries} flags={flags}"
            )
        print(f"Report: {args.output}")
        return 0 if report.failed == 0 else 1

    if args.command == "benchmark-career":
        prompt_path = Path("prompts/career_knowledge_base_system_pl.md")
        report = asyncio.run(
            run_career_benchmark(
                base_url=settings.career_llm_base_url,
                model=settings.career_llm_model,
                system_prompt=prompt_path.read_text(encoding="utf-8"),
                scenario_ids=set(args.scenario) if args.scenario else None,
            )
        )
        write_career_benchmark(report, args.output)
        print(
            f"Career benchmark: passed={report['passed']}/{report['total']}, "
            f"failed={report['failed']}"
        )
        for item in report["results"]:
            failed_checks = [
                name for name, passed in item["checks"].items() if not passed
            ]
            detail = item["error"] or ",".join(failed_checks) or "ok"
            print(f"{item['scenario_id']:24} {'PASS' if item['passed'] else 'FAIL'} {detail}")
        print(f"Report: {args.output}")
        return 0 if report["failed"] == 0 else 1

    if args.command == "demo-flow":
        project_root = Path.cwd()
        offers = load_frozen_demo_offers(args.manifest, project_root)
        profile = load_candidate_profile(args.profile)
        extractions = load_extractions(args.extractions)

        from .domain import (
            EvaluationItemStatus,
            EvaluationRunItem,
            EvaluationRunStatus,
            PipelineRun,
        )
        from .prompts import EVALUATOR_PROMPT_VERSION, JUDGE_PROMPT_VERSION
        from .storage import (
            connect,
            create_evaluation_run,
            get_pipeline_run,
            list_evaluation_run_items,
            persist_clean_offer,
            update_run_state,
        )

        started_at = datetime.now(UTC)
        run_id = (
            "demo-flow-"
            + hashlib.sha256(f"{started_at.isoformat()}:{profile.profile_id}".encode()).hexdigest()[
                :16
            ]
        )

        with connect(settings.database_path) as connection:
            offer_ids = {}
            for offer in offers:
                offer_id, _ = persist_clean_offer(
                    connection, offer, source_url=str(offer.url), seen_at=started_at
                )
                offer_ids[(offer.company, offer.title)] = offer_id

        run = PipelineRun(
            run_id=run_id,
            source_id="frozen-demo",
            run_type="dummy_full_flow",
            started_at=started_at,
            status=EvaluationRunStatus.RUNNING,
            total_items=len(offers),
            completed_items=0,
            current_stage="starting",
            profile_id=profile.profile_id,
            profile_version=profile.version,
            prompt_versions={"evaluator": EVALUATOR_PROMPT_VERSION, "judge": JUDGE_PROMPT_VERSION},
            model_configurations={"model": settings.local_llm_model},
        )

        items = []

        for offer in offers:
            offer_id = offer_ids[(offer.company, offer.title)]
            snapshot = {
                "company": offer.company,
                "title": offer.title,
                "url": str(offer.url),
                "analysis_text": offer.analysis_text,
                "supplemental_info": offer.supplemental_info.model_dump(mode="json"),
                "raw_sha256": offer.raw_sha256,
            }
            snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            input_sha256 = hashlib.sha256(snapshot_json.encode()).hexdigest()
            items.append(
                EvaluationRunItem(
                    item_id=f"{run_id}:{offer_id}",
                    run_id=run_id,
                    offer_id=offer_id,
                    status=EvaluationItemStatus.PENDING,
                    input_sha256=input_sha256,
                    input_snapshot=snapshot,
                )
            )

        create_evaluation_run(settings.database_path, run, profile, items)

        try:
            asyncio.run(
                run_demo_flow_db(
                    settings.database_path,
                    run_id,
                    offers,
                    profile,
                    extractions,
                    settings,
                )
            )
        except Exception as exc:
            update_run_state(
                settings.database_path,
                run_id,
                status=EvaluationRunStatus.FAILED.value,
                current_stage="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=datetime.now(UTC),
            )
            raise

        run_info = get_pipeline_run(settings.database_path, run_id)
        print(
            f"Demo flow: completed={run_info['completed_items']}/{run_info['total_items']}, "
            f"profile={run_info['profile_id']}"
        )

        final_items = list_evaluation_run_items(settings.database_path, run_id)
        for item in final_items:
            assessment = (
                json.loads(item["final_assessment_json"]) if item["final_assessment_json"] else None
            )
            score = assessment.get("final_score", 0) if assessment else 0
            timings = json.loads(item["timings_json"]) if item["timings_json"] else {}
            print(
                f"{item['status']:9} {item['company']:12} score={score:.1f} "
                f"eval={timings.get('evaluator', 0)}ms judge={timings.get('judge', 0)}ms"
            )
        print(f"Run ID: {run_id} in SQLite")
        return 0 if run_info["status"] == "completed" else 1

    if args.command == "notion-demo":
        if not settings.notion_api_key or not settings.notion_database_id:
            raise SystemExit("NOTION_API_KEY and NOTION_DATABASE_ID must be set in .env")
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        from .notion_demo import run_notion_demo
        from .notion_store import NotionOffersStore

        project_root = Path.cwd()
        offers = load_frozen_demo_offers(args.manifest, project_root)[: args.limit]
        profile = load_candidate_profile(args.profile)
        with NotionOffersStore(settings.notion_api_key, settings.notion_database_id) as store:
            results = asyncio.run(run_notion_demo(offers, profile, settings, store))
        for item in results:
            score = f" score={item.score:.1f}" if item.score is not None else ""
            detail = f" error={item.error}" if item.error else ""
            print(
                f"Notion demo: {item.status:17} {item.offer.company} — "
                f"{item.offer.title}{score}{detail}"
            )
        return 0 if all(item.status != "failed" for item in results) else 1

    if args.command == "telegram-test":
        from .telegram_integration import send_test_message

        if not send_test_message(
            token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        ):
            raise SystemExit("Telegram test failed; check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        print("Telegram [TEST] message sent")
        return 0

    sources = load_sources(settings.sources_path)
    if args.command == "check-config":
        print(f"Validated {len(sources)} official career sources")
        return 0

    if args.command == "import-json":
        from .collector import CollectionResult

        result = CollectionResult.model_validate_json(args.path.read_text(encoding="utf-8"))
        persisted, _ = persist_collection(
            settings.database_path,
            result.offers,
            {source.id: str(source.career_url) for source in sources},
        )
        print(f"Imported {persisted} offers into {settings.database_path}")
        return 0

    if args.command == "notion-scan":
        if not settings.notion_api_key or not settings.notion_database_id:
            raise SystemExit("NOTION_API_KEY and NOTION_DATABASE_ID must be set in .env")
        from .notion_demo import run_notion_demo
        from .notion_store import NotionOffersStore

        if args.mode == "quick":
            quick_ids = ("xebia", "epam", "inpost")
            by_id = {source.id: source for source in sources}
            missing = [source_id for source_id in quick_ids if source_id not in by_id]
            if missing:
                raise SystemExit(
                    f"Quick demo sources are missing from config: {', '.join(missing)}"
                )
            selected = [by_id[source_id] for source_id in quick_ids]
            limit_per_source = 1
        else:
            selected = sources
            limit_per_source = None

        profile = load_candidate_profile(args.profile)
        with NotionOffersStore(settings.notion_api_key, settings.notion_database_id) as store:
            collection = asyncio.run(
                collect_sources(
                    selected,
                    limit_per_source=limit_per_source,
                    apply_prefilter=True,
                    max_candidates_per_source=None,
                )
            )
            results = asyncio.run(run_notion_demo(collection.offers, profile, settings, store))
        print(
            f"Notion scan ({args.mode}): offers={len(collection.offers)} "
            f"rejected={len(collection.rejected)} source_errors={len(collection.errors)}"
        )
        for error in collection.errors:
            print(f"source error: {error.company}: {error.error}")
        for item in results:
            score = f" score={item.score:.1f}" if item.score is not None else ""
            detail = f" error={item.error}" if item.error else ""
            print(f"{item.status:17} {item.offer.company} — {item.offer.title}{score}{detail}")
        return 0 if all(item.status != "failed" for item in results) else 1

    if args.command == "demo-v2-scan":
        from .run_lock import RunAlreadyActive, RunLock

        scan_lock = RunLock(settings.database_path.parent / "locks/demo-v2-scan.lock")
        try:
            scan_lock.__enter__()
        except RunAlreadyActive as exc:
            print(f"Demo v2 skipped: {exc}")
            return 2
        atexit.register(scan_lock.__exit__, None, None, None)
        enabled_sources = [source for source in sources if source.enabled]
        limit_per_source = 1 if args.mode == "sample" else None
        collection = asyncio.run(
            collect_sources(
                enabled_sources,
                limit_per_source=limit_per_source,
                apply_prefilter=True,
                max_candidates_per_source=None,
            )
        )
        write_collection(collection, args.output)
        started_at = datetime.now(UTC)
        run_id = "demo-v2-" + hashlib.sha256(
            f"{started_at.isoformat()}:{args.mode}".encode()
        ).hexdigest()[:16]
        summary = persist_monitored_collection(
            settings.database_path,
            run_id=run_id,
            mode=args.mode,
            offers=collection.offers,
            source_urls={source.id: str(source.career_url) for source in enabled_sources},
            observations=collection.observations,
            started_at=started_at,
        )
        summary["mode"] = args.mode
        print(
            f"Demo v2 ({args.mode}): saved={summary['offers_saved']} "
            f"sources_ok={summary['sources_ok']}/{summary['sources_total']} "
            f"unavailable={summary['offers_marked_unavailable']}"
        )
        print(f"SQLite: {settings.database_path} · snapshot: {args.output}")
        for observation in collection.observations:
            detail = f" error={observation.error}" if observation.error else ""
            print(
                f"{observation.status:8} {observation.company:20} "
                f"discovered={observation.discovered_count:<4} "
                f"selected={observation.selected_count}{detail}"
            )
        if args.notify:
            from .telegram_integration import send_collection_summary

            if not send_collection_summary(
                summary,
                token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
            ):
                print("Telegram summary failed; SQLite result is preserved")
                return 1
            print("Telegram Demo v2 summary sent")
        return 0 if not collection.errors else 1

    if args.command == "collect":
        selected = sources
        if args.source_ids:
            requested = set(args.source_ids)
            selected = [source for source in sources if source.id in requested]
            missing = requested - {source.id for source in selected}
            if missing:
                raise SystemExit(f"Unknown source ids: {', '.join(sorted(missing))}")
        result = asyncio.run(
            collect_sources(
                selected,
                args.limit_per_source,
                apply_prefilter=not args.all_offers,
                max_candidates_per_source=args.max_candidates_per_source,
            )
        )
        write_collection(result, args.output)
        persisted, new_versions = persist_collection(
            settings.database_path,
            result.offers,
            {source.id: str(source.career_url) for source in selected},
        )
        if args.review_html:
            write_review_report(result, args.review_html)
        print(
            f"Collected {len(result.offers)} clean offers; "
            f"rejected={len(result.rejected)}; errors={len(result.errors)}"
        )
        print(f"Output: {args.output}")
        print(
            f"SQLite: {settings.database_path} "
            f"(offers={persisted}, new raw versions={new_versions})"
        )
        if args.review_html:
            print(f"Review: {args.review_html}")
        return 0 if not result.errors else 1

    results = asyncio.run(scan_sources(sources))
    write_report(results, args.output)
    for result in results:
        print(
            f"{result.status:8} {result.company:14} "
            f"jobs={result.jobs_found:<4} latency={result.latency_ms}ms"
        )
        if result.error:
            print(f"         {result.error}")
    healthy = sum(result.status == "ok" for result in results)
    print(f"Report: {args.output} ({healthy}/{len(results)} fully operational)")
    return 0 if healthy == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(run())
