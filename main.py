import sys
import json
import logging
import argparse
from pathlib import Path

from config import Config
from extractor import extract_input
from parser_xaml import parse_xaml, parse_xaml_files
from ir_generator import IRGenerator
from mapping_engine import MappingEngine
from pad_script_generator import PADScriptGenerator
from validator import Validator
from repair_engine import RepairEngine


def setup_logging():
    """Configure logging based on .env LOG_LEVEL."""
    level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger(__name__)
    return logger


def run_pipeline(input_path, target_platform="PAD", output_mode="full"):
    """Run the complete migration pipeline.

    Steps:
    1. Extract input (.xaml, .zip, .nupkg)
    2. Parse XAML into structured tree
    3. Generate normalized IR JSON
    4. Map actions to target platform
    5. Generate Robin script
    6. Validate
    7. Repair if needed
    8. Output final artifacts

    Args:
        input_path: Path to input file
        target_platform: "PAD" or "AA360"
        output_mode: "full" | "script_only" | "debug"

    Returns:
        dict: Pipeline result with all artifacts
    """
    logger = logging.getLogger(__name__)
    Config.ensure_directories()

    result = {
        "status": "started",
        "input_path": str(input_path),
        "target_platform": target_platform,
    }

    # ------------------------------------------------------------------
    # Step 1: Extract
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("STEP 1: EXTRACTION")
    logger.info("=" * 60)

    try:
        extraction = extract_input(input_path)
        xaml_files = extraction["xaml_files"]
        project_info = extraction["project_info"]

        if not xaml_files:
            logger.error("No XAML files found in input")
            result["status"] = "failed"
            result["error"] = "No XAML files found"
            return result

        logger.info(f"Found {len(xaml_files)} XAML file(s)")
        result["extraction"] = {
            "file_count": len(xaml_files),
            "project_info": project_info,
            "files": [str(f) for f in xaml_files],
        }

    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        result["status"] = "failed"
        result["error"] = f"Extraction failed: {e}"
        return result

    # ------------------------------------------------------------------
    # Step 2: Parse XAML
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("STEP 2: XAML PARSING")
    logger.info("=" * 60)

    try:
        parsed_trees = parse_xaml_files(xaml_files)
        successful_parses = [t for t in parsed_trees if not t.get("error")]

        if not successful_parses:
            logger.error("All XAML files failed to parse")
            result["status"] = "failed"
            result["error"] = "All XAML files failed to parse"
            return result

        logger.info(f"Parsed {len(successful_parses)}/{len(parsed_trees)} files successfully")

    except Exception as e:
        logger.error(f"Parsing failed: {e}")
        result["status"] = "failed"
        result["error"] = f"Parsing failed: {e}"
        return result

    # ------------------------------------------------------------------
    # Step 3: Generate IR
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("STEP 3: IR GENERATION")
    logger.info("=" * 60)

    try:
        ir_gen = IRGenerator()

        if len(parsed_trees) == 1:
            ir_data = ir_gen.generate(parsed_trees[0], project_info)
        else:
            ir_data = ir_gen.generate_multiple(parsed_trees, project_info)

        # Save IR
        ir_path = ir_gen.save_ir(ir_data)
        logger.info(f"IR saved to: {ir_path}")

        if output_mode == "full":
            result["ir_data"] = ir_data

    except Exception as e:
        logger.error(f"IR generation failed: {e}")
        result["status"] = "failed"
        result["error"] = f"IR generation failed: {e}"
        return result

    # ------------------------------------------------------------------
    # Step 4: Map Actions
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("STEP 4: ACTION MAPPING")
    logger.info("=" * 60)

    try:
        mapper = MappingEngine()
        mapping_result = mapper.map_all_actions(ir_data)

        # Save mapping result
        mapper.save_mapping_result(mapping_result)

        summary = mapping_result.get("summary", {})
        logger.info(f"Mapping summary: {json.dumps(summary, indent=2)}")

        if output_mode == "full":
            result["mapping_result"] = mapping_result

    except Exception as e:
        logger.error(f"Action mapping failed: {e}")
        result["status"] = "failed"
        result["error"] = f"Action mapping failed: {e}"
        return result

    # ------------------------------------------------------------------
    # Step 5: Generate Robin Script
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("STEP 5: ROBIN SCRIPT GENERATION")
    logger.info("=" * 60)

    try:
        generator = PADScriptGenerator()
        robin_script = generator.generate(ir_data, mapping_result)

        # Save generated script
        script_path = generator.save_script(robin_script)
        logger.info(f"Generated script saved to: {script_path}")

    except Exception as e:
        logger.error(f"Script generation failed: {e}")
        result["status"] = "failed"
        result["error"] = f"Script generation failed: {e}"
        return result

    # ------------------------------------------------------------------
    # Step 6: Validate
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("STEP 6: VALIDATION")
    logger.info("=" * 60)

    try:
        validator = Validator()
        validation_result = validator.validate(script_text=robin_script)

        # Save validation result
        validator.save_validation_result(validation_result)

        if validation_result["is_valid"]:
            logger.info("VALIDATION PASSED - Script is valid")
        else:
            error_count = validation_result.get("error_count", 0)
            logger.warning(f"VALIDATION FAILED - {error_count} error(s) found")

        if output_mode == "full":
            result["validation_result"] = validation_result

    except Exception as e:
        logger.error(f"Validation failed: {e}")
        # Continue to repair even if validation itself errors
        validation_result = {"is_valid": False, "error_count": 1}

    # ------------------------------------------------------------------
    # Step 7: Repair (if needed)
    # ------------------------------------------------------------------
    final_script = robin_script

    if not validation_result.get("is_valid", False):
        logger.info("=" * 60)
        logger.info("STEP 7: REPAIR LOOP")
        logger.info("=" * 60)

        try:
            repair_eng = RepairEngine()
            repair_result = repair_eng.repair(
                script=robin_script,
                ir_data=ir_data,
                mapping_result=mapping_result,
            )

            final_script = repair_result.get("final_script", robin_script)
            is_valid_after_repair = repair_result.get("is_valid", False)
            attempts = repair_result.get("attempts", 0)
            unresolved = repair_result.get("unresolved_errors", [])

            if is_valid_after_repair:
                logger.info(f"REPAIR SUCCEEDED after {attempts} attempt(s)")
            else:
                logger.warning(
                    f"REPAIR INCOMPLETE after {attempts} attempt(s) - "
                    f"{len(unresolved)} unresolved error(s)"
                )

            if output_mode == "full":
                result["repair_result"] = repair_result

        except Exception as e:
            logger.error(f"Repair failed: {e}")
            # Keep the original generated script
            final_script = robin_script

    else:
        logger.info("No repair needed - script is valid")

    # ------------------------------------------------------------------
    # Step 8: Save final output
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("STEP 8: FINAL OUTPUT")
    logger.info("=" * 60)

    try:
        final_path = Config.FINAL_SCRIPT_PATH
        final_path.parent.mkdir(parents=True, exist_ok=True)
        with open(final_path, "w", encoding="utf-8") as f:
            f.write(final_script)
        logger.info(f"Final script saved to: {final_path}")

    except Exception as e:
        logger.error(f"Failed to save final script: {e}")

    # Build final result
    result["status"] = "completed"
    result["final_script_path"] = str(Config.FINAL_SCRIPT_PATH)

    if output_mode == "script_only":
        return {"final_script": final_script}

    if output_mode == "debug":
        result["final_script"] = final_script
        return result

    result["output_files"] = {
        "ir_json": str(Config.IR_OUTPUT_PATH),
        "mapping_result": str(Config.MAPPING_RESULT_PATH),
        "generated_script": str(Config.GENERATED_SCRIPT_PATH),
        "validation_result": str(Config.VALIDATION_RESULT_PATH),
        "final_script": str(Config.FINAL_SCRIPT_PATH),
    }

    logger.info("=" * 60)
    logger.info("MIGRATION COMPLETE")
    logger.info("=" * 60)
    _print_summary(result, mapping_result, validation_result)

    return result


def _print_summary(result, mapping_result, validation_result):
    """Print a readable summary of the migration."""
    logger = logging.getLogger(__name__)

    summary = mapping_result.get("summary", {})
    logger.info(f"  Total actions mapped  : {summary.get('total', 0)}")
    logger.info(f"  High confidence       : {summary.get('high_confidence', 0)}")
    logger.info(f"  Medium confidence     : {summary.get('medium_confidence', 0)}")
    logger.info(f"  Low confidence        : {summary.get('low_confidence', 0)}")
    logger.info(f"  Unmapped              : {summary.get('unmapped_count', 0)}")

    unmapped = summary.get("unmapped_actions", [])
    if unmapped:
        logger.info(f"  Unmapped actions      : {', '.join(unmapped)}")

    is_valid = validation_result.get("is_valid", False)
    error_count = validation_result.get("error_count", 0)
    logger.info(f"  Validation status     : {'PASSED' if is_valid else 'FAILED'}")
    logger.info(f"  Validation errors     : {error_count}")

    logger.info(f"  Output files at       : {Config.OUTPUT_DIR}")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="UiPath to Power Automate Desktop Migration Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --input workflow.xaml
  python main.py --input project.zip --output-mode script_only
  python main.py --input package.nupkg --output-mode debug
  python main.py --input workflow.xaml --validate-only output/generated_script.robin
        """,
    )

    parser.add_argument(
        "--input", "-i",
        required=False,
        help="Path to UiPath input file (.xaml, .zip, or .nupkg)",
    )

    parser.add_argument(
        "--target", "-t",
        default="PAD",
        choices=["PAD", "AA360"],
        help="Target platform (default: PAD)",
    )

    parser.add_argument(
        "--output-mode", "-o",
        default="full",
        choices=["full", "script_only", "debug"],
        help="Output mode: full (all artifacts), script_only (just Robin script), debug (with details)",
    )

    parser.add_argument(
        "--validate-only", "-v",
        required=False,
        help="Only validate an existing Robin script file",
    )

    parser.add_argument(
        "--repair-only", "-r",
        required=False,
        help="Only repair an existing Robin script file",
    )

    parser.add_argument(
        "--ir-only",
        required=False,
        help="Only generate IR from a XAML file",
    )

    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Check configuration and exit",
    )

    args = parser.parse_args()
    logger = setup_logging()

    # ------------------------------------------------------------------
    # Check config
    # ------------------------------------------------------------------
    if args.check_config:
        logger.info("Configuration check:")
        logger.info(json.dumps(Config.summary(), indent=2))
        errors = Config.validate()
        if errors:
            logger.error("Configuration errors:")
            for err in errors:
                logger.error(f"  - {err}")
            sys.exit(1)
        else:
            logger.info("Configuration is valid")
            sys.exit(0)

    # ------------------------------------------------------------------
    # Validate only
    # ------------------------------------------------------------------
    if args.validate_only:
        script_path = args.validate_only
        if not Path(script_path).exists():
            logger.error(f"Script file not found: {script_path}")
            sys.exit(1)

        logger.info(f"Validating: {script_path}")
        validator = Validator()
        result = validator.validate(script_path=script_path)
        validator.save_validation_result(result)

        if result["is_valid"]:
            logger.info("VALIDATION PASSED")
            print(json.dumps(result, indent=2, default=str))
            sys.exit(0)
        else:
            logger.warning(f"VALIDATION FAILED: {result['error_count']} error(s)")
            print(json.dumps(result, indent=2, default=str))
            sys.exit(1)

    # ------------------------------------------------------------------
    # Repair only
    # ------------------------------------------------------------------
    if args.repair_only:
        script_path = args.repair_only
        if not Path(script_path).exists():
            logger.error(f"Script file not found: {script_path}")
            sys.exit(1)

        logger.info(f"Repairing: {script_path}")
        with open(script_path, "r", encoding="utf-8") as f:
            script = f.read()

        repair_eng = RepairEngine()
        result = repair_eng.repair(script)

        final_script = result.get("final_script", script)
        repair_eng.save_final_script(final_script)

        if result["is_valid"]:
            logger.info("REPAIR SUCCEEDED")
        else:
            unresolved = result.get("unresolved_errors", [])
            logger.warning(f"REPAIR INCOMPLETE: {len(unresolved)} unresolved error(s)")

        print(json.dumps({
            "is_valid": result["is_valid"],
            "attempts": result["attempts"],
            "repair_log": result["repair_log"],
            "unresolved_count": len(result.get("unresolved_errors", [])),
        }, indent=2, default=str))

        sys.exit(0 if result["is_valid"] else 1)

    # ------------------------------------------------------------------
    # IR only
    # ------------------------------------------------------------------
    if args.ir_only:
        xaml_path = args.ir_only
        if not Path(xaml_path).exists():
            logger.error(f"XAML file not found: {xaml_path}")
            sys.exit(1)

        logger.info(f"Generating IR for: {xaml_path}")
        parsed = parse_xaml(xaml_path)
        ir_gen = IRGenerator()
        ir_data = ir_gen.generate(parsed)
        ir_path = ir_gen.save_ir(ir_data)

        logger.info(f"IR saved to: {ir_path}")
        print(json.dumps(ir_data, indent=2, default=str))
        sys.exit(0)

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------
    if not args.input:
        parser.print_help()
        sys.exit(1)

    input_path = args.input
    if not Path(input_path).exists():
        logger.error(f"Input file not found: {input_path}")
        sys.exit(1)

    # Validate configuration
    config_errors = Config.validate()
    if config_errors:
        logger.warning("Configuration warnings:")
        for err in config_errors:
            logger.warning(f"  - {err}")
        # Don't exit - LLM might not be needed if all actions are mapped

    # Run pipeline
    result = run_pipeline(
        input_path=input_path,
        target_platform=args.target,
        output_mode=args.output_mode,
    )

    # Output based on mode
    if args.output_mode == "script_only":
        print(result.get("final_script", ""))
    elif args.output_mode == "debug":
        print(json.dumps(result, indent=2, default=str))
    else:
        status = result.get("status", "unknown")
        if status == "completed":
            logger.info("Migration pipeline completed successfully")
            sys.exit(0)
        else:
            logger.error(f"Migration pipeline failed: {result.get('error', 'unknown')}")
            sys.exit(1)


if __name__ == "__main__":
    main()