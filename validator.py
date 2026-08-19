import json
import re
import subprocess
import logging
import platform
import difflib
from pathlib import Path
from config import Config

logger = logging.getLogger(__name__)


# ================================================================
# SEMANTIC VALIDATION (from your semantic_validation.py)
# ================================================================

STRUCTURAL_RE = re.compile(
    r"^("
    r"IF\s+.+\s+THEN"
    r"|ELSE(\s+IF\s+.+\s+THEN)?"
    r"|END(\s+EXCEPTION)?"
    r"|BEGIN\s+EXCEPTION"
    r"|ON\s+ERROR"
    r"|LOOP\s+WHILE\s+.+"
    r"|LOOP\s+FOREACH\s+\S+\s+IN\s+.+"
    r"|LOOP"
    r"|SWITCH\s+.+"
    r"|CASE\s+.+"
    r"|DEFAULT"
    r"|WAIT\s+.+"
    r"|SET\s+[A-Za-z_]\w*\s+TO\s+.+"
    r"|EXIT\s+LOOP"
    r"|NEXT\s+LOOP"
    r"|GOTO\s+\S+"
    r"|LABEL\s+\S+"
    r"|ON\s+BLOCK\s+ERROR"
    r"|BLOCK"
    r")$"
)


def strip_strings(line):
    """Remove quoted strings and variable markers to avoid false matches."""
    line = re.sub(r"'''(.*?)'''", "''", line)
    line = re.sub(r"'(.*?)'", "''", line)
    line = re.sub(r'"(.*?)"', '""', line)
    line = re.sub(r"%(.*?)%", "%%", line)
    return line


def validate_robin_semantics(script_path, schema_path=None):
    """Validate Robin script against pad_llm_schema.json using a real grammar.

    A line is valid if it is either:
    - a comment/blank, or
    - a structural Robin keyword form (SET/IF/ON ERROR/BEGIN EXCEPTION/...), or
    - a known schema ActionId with valid parameter names.
    No keyword skip-list: invalid constructs like bare THROW are now caught.
    """
    if schema_path is None:
        schema_path = str(Config.PAD_SCHEMA_PATH)

    with open(schema_path, "r", encoding="utf-8") as f:
        schema_list = json.load(f)

    schema_map = {item["ActionId"]: item for item in schema_list}
    errors = []

    try:
        with open(script_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        return {"errors": [{"line": 0, "message": str(e)}], "isValid": False}

    for idx, line in enumerate(lines):
        line_num = idx + 1
        orig_line = line.strip()

        if not orig_line or orig_line.startswith("#") or orig_line.startswith("//"):
            continue
        if orig_line.startswith("#region") or orig_line.startswith("#endregion"):
            continue

        # Valid Robin structural keyword line
        if STRUCTURAL_RE.match(orig_line):
            continue

        match = re.match(r"^([A-Za-z0-9_.]+)", orig_line)
        if not match:
            errors.append({
                "message": f"Unrecognized Robin syntax: '{orig_line[:80]}'",
                "text": None, "stopLine": line_num, "stopColumn": 0,
                "startLine": line_num, "startColumn": 0,
            })
            continue

        action_id = match.group(1)

        # variable property assignment like dt.RowsCount
        if "." in action_id and not action_id[0].isupper():
            continue

        if action_id not in schema_map:
            suggestion = ""
            prefix_matches = [k for k in schema_map if k.startswith(action_id + ".") or action_id in k]
            if prefix_matches:
                s = schema_map[prefix_matches[0]]
                suggestion = f" Did you mean '{prefix_matches[0]}'? Syntax: {s.get('RobinSyntaxTemplate', '')}"
            else:
                close = difflib.get_close_matches(action_id, schema_map.keys(), n=1, cutoff=0.5)
                if close:
                    s = schema_map[close[0]]
                    suggestion = f" Did you mean '{close[0]}'? Syntax: {s.get('RobinSyntaxTemplate', '')}"
            errors.append({
                "message": f"Module or action '{action_id}' wasn't found.{suggestion}",
                "text": None, "stopLine": line_num, "stopColumn": 0,
                "startLine": line_num, "startColumn": 0,
            })
            continue

        schema = schema_map[action_id]
        valid_inputs = {inp["Name"] for inp in schema.get("Inputs", [])}

        declared_outputs = schema.get("Outputs", []) or []
        valid_outputs = set()
        for out in declared_outputs:
            valid_outputs.add(out.get("Name", "") if isinstance(out, dict) else out)
        clean_line = strip_strings(orig_line)

        inputs_used = re.findall(r"(?:^|\s)([A-Za-z0-9_]+):", clean_line)
        for inp in inputs_used:
            if inp not in valid_inputs:
                hint = f" Valid syntax: {schema.get('RobinSyntaxTemplate', '')}"
                errors.append({
                    "message": f"Unknown argument(s): '{inp}'.{hint}",
                    "text": None, "stopLine": line_num, "stopColumn": 0,
                    "startLine": line_num, "startColumn": 0,
                })

        # Output validation only applies when the schema actually records
        # outputs. Every Excel action has an empty Outputs list even though
        # the PAD designer emits captures like "Instance=> Var" - flagging
        # those produces false errors and can trigger a repair that deletes
        # the capture.
        if declared_outputs:
            outputs_used = re.findall(
                r"(?:^|\s)([A-Za-z0-9_]+)=>",
                clean_line,
            )

            for out in outputs_used:
                if out not in valid_outputs:
                    errors.append({
                        "message": (
                            f"Unknown argument(s): '{out}' (Output)."
                        ),
                        "text": None,
                        "stopLine": line_num,
                        "stopColumn": 0,
                        "startLine": line_num,
                        "startColumn": 0,
                    })

    return {"errors": errors, "isValid": len(errors) == 0}
# ================================================================
# PAD DLL VALIDATOR (calls PADValidator.ps1)
# ================================================================

def run_pad_validator(script_path, validator_ps1_path=None):
    """Run PADValidator.ps1 against the script.

    Only works on Windows with PAD installed.

    Args:
        script_path: Path to .robin script file
        validator_ps1_path: Path to PADValidator.ps1

    Returns:
        dict: {"errors": [...], "isValid": bool, "pad_not_available": bool}
    """
    is_windows = platform.system() == "Windows"

    if not is_windows:
        logger.info("PAD validator skipped: not running on Windows")
        return {
            "errors": [],
            "isValid": True,
            "pad_not_available": True,
            "skip_reason": "Not running on Windows",
        }

    ps1_path = Path(validator_ps1_path) if validator_ps1_path else Config.VALIDATOR_PATH
    if not ps1_path.exists():
        logger.warning(f"PADValidator.ps1 not found at: {ps1_path}")
        return {
            "errors": [],
            "isValid": True,
            "pad_not_available": True,
            "skip_reason": f"PADValidator.ps1 not found at {ps1_path}",
        }

    try:
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(ps1_path),
            "-ScriptFile", str(script_path),
        ]

        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
        )

        stdout = process.stdout.strip()
        stderr = process.stderr.strip()

        if stderr and not stdout:
            logger.warning(f"PAD validator stderr: {stderr}")
            return {
                "errors": [{"message": f"PAD validator error: {stderr}"}],
                "isValid": False,
            }

        if not stdout:
            return {
                "errors": [],
                "isValid": True,
                "pad_not_available": True,
                "skip_reason": "PAD validator returned empty output",
            }

        result = json.loads(stdout)
        logger.debug(
            f"PAD validation: {'PASS' if result.get('isValid') else 'FAIL'} "
            f"({len(result.get('errors', []))} errors)"
        )
        return result

    except subprocess.TimeoutExpired:
        logger.error("PAD validator timed out")
        return {
            "errors": [{"message": "PAD validator timed out after 60 seconds"}],
            "isValid": False,
        }
    except json.JSONDecodeError as e:
        logger.error(f"PAD validator output not valid JSON: {e}")
        return {
            "errors": [{"message": f"PAD validator output parse error: {e}"}],
            "isValid": False,
        }
    except FileNotFoundError:
        logger.warning("PowerShell not found")
        return {
            "errors": [],
            "isValid": True,
            "pad_not_available": True,
            "skip_reason": "PowerShell not found on system",
        }
    except Exception as e:
        logger.error(f"PAD validator execution failed: {e}")
        return {
            "errors": [{"message": f"PAD validator error: {e}"}],
            "isValid": False,
        }


# ================================================================
# COMBINED VALIDATOR (wrapper)
# ================================================================

class Validator:
    """Combined validator: semantic + PAD DLL.

    Runs both validators, combines results, classifies errors
    for the repair engine.
    """

    ERROR_TYPES = {
        "unknown_action": "Action ID not found in schema",
        "unknown_argument": "Parameter name not valid for action",
        "unknown_output": "Output name not valid for action",
        "syntax_error": "Robin syntax error from PAD parser",
        "missing_required": "Required parameter missing",
        "invalid_expression": "Expression syntax invalid",
        "invalid_block": "Block structure invalid",
        "variable_not_defined": "Variable referenced but not declared",
        "type_mismatch": "Parameter type mismatch",
        "pad_not_available": "PAD validator not available",
        "internal_error": "Internal validation error",
    }

    def __init__(self):
        self.schema_path = str(Config.PAD_SCHEMA_PATH)

    def validate(self, script_text=None, script_path=None):
        """Run both validators and return combined result.

        Args:
            script_text: Robin script as string
            script_path: Path to .robin script file

        Returns:
            dict: {
                "is_valid": bool,
                "semantic_result": dict,
                "pad_result": dict,
                "combined_errors": [classified error list],
                "error_count": int,
                "error_summary": dict
            }
        """
        # Ensure we have a file path
        if script_text and not script_path:
            script_path = str(Config.OUTPUT_DIR / "_validation_temp.robin")
            Path(script_path).parent.mkdir(parents=True, exist_ok=True)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_text)

        if not script_path or not Path(script_path).exists():
            return self._error_result("No script provided for validation")

        # Load script lines for context
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                script_lines = f.readlines()
        except Exception:
            script_lines = []

        # Step 1: Semantic validation
        try:
            semantic_result = validate_robin_semantics(script_path, self.schema_path)
            logger.debug(
                f"Semantic: {'PASS' if semantic_result['isValid'] else 'FAIL'} "
                f"({len(semantic_result.get('errors', []))} errors)"
            )
        except Exception as e:
            logger.error(f"Semantic validation failed: {e}")
            semantic_result = {
                "errors": [{"line": 0, "message": f"Semantic validator error: {e}"}],
                "isValid": False,
            }

        # Step 2: PAD validator
        pad_result = run_pad_validator(script_path)

        # Combine and classify
        combined_errors = self._combine_and_classify(semantic_result, pad_result, script_lines)
        error_summary = self._build_error_summary(combined_errors)

        is_valid = semantic_result.get("isValid", False) and pad_result.get("isValid", False)
        if pad_result.get("pad_not_available", False):
            is_valid = semantic_result.get("isValid", False)

        result = {
            "is_valid": is_valid,
            "semantic_result": semantic_result,
            "pad_result": pad_result,
            "combined_errors": combined_errors,
            "error_count": len(combined_errors),
            "error_summary": error_summary,
        }

        if is_valid:
            logger.info("Validation PASSED")
        else:
            logger.warning(f"Validation FAILED: {len(combined_errors)} errors ({error_summary})")

        return result

    # ------------------------------------------------------------------
    # Error combination and classification
    # ------------------------------------------------------------------

    def _combine_and_classify(self, semantic_result, pad_result, script_lines):
        """Combine errors from both validators and classify each."""
        combined = []

        for err in semantic_result.get("errors", []):
            combined.append(self._classify_error(err, "semantic", script_lines))

        for err in pad_result.get("errors", []):
            combined.append(self._classify_error(err, "pad", script_lines))

        return self._deduplicate_errors(combined)

    def _classify_error(self, error, source, script_lines):
        """Classify a single validation error."""
        message = error.get("message", "")
        line_num = error.get("startLine") or error.get("stopLine") or error.get("line", 0)
        column = error.get("startColumn", 0)
        text = error.get("text", "")

        error_type = self._determine_error_type(message)
        failing_block = self._get_failing_block(script_lines, line_num)
        fix_type = self._suggest_fix_type(error_type)

        return {
            "source": source,
            "error_type": error_type,
            "message": message,
            "line": line_num,
            "column": column,
            "text": text,
            "failing_block": failing_block,
            "suggested_fix_type": fix_type,
        }

    @staticmethod
    def _determine_error_type(message):
        """Determine error type from message content."""
        msg_lower = (message or "").lower()

        # Specific PAD block errors must be detected before generic syntax
        # classification.
        if (
            "error block statement was previously defined" in msg_lower
            or "block statement was previously defined" in msg_lower
            or "invalid block" in msg_lower
            or "block structure" in msg_lower
            or (
                "block" in msg_lower
                and "previously defined" in msg_lower
            )
        ):
            return "invalid_block"

        if "wasn't found" in msg_lower or "not found" in msg_lower:
            if "module" in msg_lower or "action" in msg_lower:
                return "unknown_action"

        if "unknown argument" in msg_lower:
            if "output" in msg_lower:
                return "unknown_output"
            return "unknown_argument"

        if "variable" in msg_lower and (
            "not defined" in msg_lower
            or "undefined" in msg_lower
            or "does not exist" in msg_lower
        ):
            return "variable_not_defined"

        if "type" in msg_lower and "mismatch" in msg_lower:
            return "type_mismatch"

        if "required" in msg_lower or "missing parameter" in msg_lower:
            return "missing_required"

        if (
            "block" in msg_lower
            or "end mismatch" in msg_lower
            or "unexpected end" in msg_lower
        ):
            return "invalid_block"

        if "expression" in msg_lower or "invalid expression" in msg_lower:
            return "invalid_expression"

        if (
            "syntax" in msg_lower
            or "unexpected" in msg_lower
            or "expected" in msg_lower
        ):
            return "syntax_error"

        return "syntax_error"

    @staticmethod
    def _suggest_fix_type(error_type):
        """Suggest fix type for repair engine."""
        fix_map = {
            "unknown_action": "replace_action_id",
            "unknown_argument": "fix_parameter_name",
            "unknown_output": "fix_output_name",
            "syntax_error": "fix_syntax",
            "missing_required": "add_parameter",
            "invalid_expression": "fix_expression",
            "invalid_block": "fix_block_structure",
            "variable_not_defined": "add_variable_declaration",
            "type_mismatch": "fix_parameter_type",
            "pad_not_available": "none",
            "internal_error": "manual_review",
        }
        return fix_map.get(error_type, "manual_review")

    @staticmethod
    def _get_failing_block(script_lines, line_num, context=2):
        """Extract failing block with surrounding context."""
        if not script_lines or line_num <= 0:
            return ""

        idx = line_num - 1
        start = max(0, idx - context)
        end = min(len(script_lines), idx + context + 1)

        block_lines = []
        for i in range(start, end):
            marker = ">>>" if i == idx else "   "
            block_lines.append(f"{marker} {i + 1:4d} | {script_lines[i].rstrip()}")

        return "\n".join(block_lines)

    @staticmethod
    def _deduplicate_errors(errors):
        """Remove duplicate errors."""
        seen = set()
        unique = []
        for err in errors:
            key = (err.get("line", 0), err.get("message", "")[:80])
            if key not in seen:
                seen.add(key)
                unique.append(err)
        return unique

    @staticmethod
    def _build_error_summary(errors):
        """Build summary counts by error type."""
        summary = {}
        for err in errors:
            etype = err.get("error_type", "unknown")
            summary[etype] = summary.get(etype, 0) + 1
        return summary

    @staticmethod
    def _error_result(message):
        """Build error result when validation cannot run."""
        return {
            "is_valid": False,
            "semantic_result": {"errors": [{"message": message}], "isValid": False},
            "pad_result": {"errors": [], "isValid": True, "pad_not_available": True},
            "combined_errors": [{
                "source": "internal",
                "error_type": "internal_error",
                "message": message,
                "line": 0,
                "column": 0,
                "text": "",
                "failing_block": "",
                "suggested_fix_type": "manual_review",
            }],
            "error_count": 1,
            "error_summary": {"internal_error": 1},
        }

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    @staticmethod
    def save_validation_result(result, output_path=None):
        """Save validation result to JSON file."""
        path = Path(output_path) if output_path else Config.VALIDATION_RESULT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)

        logger.info(f"Validation result saved to: {path}")
        return path


# ================================================================
# Convenience functions
# ================================================================

def validate_script(script_text=None, script_path=None):
    """Convenience function to validate a Robin script."""
    validator = Validator()
    return validator.validate(script_text=script_text, script_path=script_path)