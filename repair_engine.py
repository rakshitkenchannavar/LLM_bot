import re
import json
import logging
from pathlib import Path
from config import Config
from validator import Validator
from llm_client import get_llm_client

logger = logging.getLogger(__name__)


class RepairEngine:
    """Targeted repair engine for failing Robin scripts.

    Repair strategy:
    1. Parse each validation error
    2. Classify error type
    3. Apply deterministic fix if possible
    4. Fall back to LLM-based fix if deterministic fix unavailable
    5. Revalidate after each repair pass
    6. Repeat until valid or retry limit reached

    Core principle: Prefer partial targeted repair over full regeneration.
    """

    def __init__(self):
        self.validator = Validator()
        self.max_retries = Config.MAX_RETRY_COUNT
        self.repair_log = []

    def repair(self, script, ir_data=None, mapping_result=None):
        """Run the repair loop on a Robin script.

        Args:
            script: Robin script string
            ir_data: Optional IR JSON for context
            mapping_result: Optional mapping result for context

        Returns:
            dict: {
                "final_script": str,
                "is_valid": bool,
                "repair_log": [list of repair actions],
                "attempts": int,
                "validation_result": dict,
                "unresolved_errors": [list of remaining errors]
            }
        """
        self.repair_log = []
        current_script = script
        attempt = 0

        logger.info(f"Starting repair loop (max retries: {self.max_retries})")

        while attempt < self.max_retries:
            attempt += 1
            logger.info(f"Repair attempt {attempt}/{self.max_retries}")

            # Validate current script
            validation = self.validator.validate(script_text=current_script)

            if validation["is_valid"]:
                logger.info(f"Script is valid after {attempt} attempt(s)")
                return self._build_result(
                    final_script=current_script,
                    is_valid=True,
                    attempts=attempt,
                    validation=validation,
                    unresolved=[],
                )

            errors = validation.get("combined_errors", [])
            if not errors:
                logger.warning("Validation failed but no errors reported")
                break

            logger.info(f"Found {len(errors)} errors, attempting repair")

            # Apply repairs
            repaired_script, repairs_applied = self._apply_repairs(
                script=current_script,
                errors=errors,
                ir_data=ir_data,
                mapping_result=mapping_result,
            )

            if not repairs_applied:
                logger.warning("No repairs could be applied, stopping")
                break

            # Check if script actually changed
            if repaired_script == current_script:
                logger.warning("Repairs produced no changes, stopping")
                break

            current_script = repaired_script
            self.repair_log.append({
                "attempt": attempt,
                "errors_found": len(errors),
                "repairs_applied": repairs_applied,
            })

        # Final validation
        final_validation = self.validator.validate(script_text=current_script)
        unresolved = final_validation.get("combined_errors", [])

        if unresolved:
            logger.warning(f"Repair loop ended with {len(unresolved)} unresolved errors")

        return self._build_result(
            final_script=current_script,
            is_valid=final_validation["is_valid"],
            attempts=attempt,
            validation=final_validation,
            unresolved=unresolved,
        )

    # ------------------------------------------------------------------
    # Repair application
    # ------------------------------------------------------------------

    def _apply_repairs(self, script, errors, ir_data=None, mapping_result=None):
        """Apply targeted repairs for each error.

        Processes errors from bottom to top (highest line first)
        to preserve line numbers during editing.

        Args:
            script: Current Robin script
            errors: List of classified errors
            ir_data: Optional IR data for context
            mapping_result: Optional mapping result for context

        Returns:
            tuple: (repaired_script, number_of_repairs_applied)
        """
        lines = script.split("\n")
        repairs_applied = 0

        # Sort errors by line number descending (repair from bottom up)
        sorted_errors = sorted(errors, key=lambda e: e.get("line", 0), reverse=True)

        for error in sorted_errors:
            error_type = error.get("error_type", "")
            line_num = error.get("line", 0)
            message = error.get("message", "")

            logger.debug(f"Attempting repair for line {line_num}: [{error_type}] {message}")

            # Dispatch to appropriate repair handler
            handler = self._get_repair_handler(error_type)
            if handler:
                repaired_lines = handler(
                    lines=lines,
                    error=error,
                    ir_data=ir_data,
                    mapping_result=mapping_result,
                )
                if repaired_lines is not None and repaired_lines != lines:
                    lines = repaired_lines
                    repairs_applied += 1
                    self.repair_log.append({
                        "line": line_num,
                        "error_type": error_type,
                        "message": message,
                        "status": "fixed",
                    })
                    logger.debug(f"Repair applied for line {line_num}")
                else:
                    self.repair_log.append({
                        "line": line_num,
                        "error_type": error_type,
                        "message": message,
                        "status": "skipped",
                    })
            else:
                self.repair_log.append({
                    "line": line_num,
                    "error_type": error_type,
                    "message": message,
                    "status": "no_handler",
                })

        repaired_script = "\n".join(lines)
        return repaired_script, repairs_applied

    def _get_repair_handler(self, error_type):
        """Get the repair handler function for an error type."""
        handlers = {
            "unknown_action": self._repair_unknown_action,
            "unknown_argument": self._repair_unknown_argument,
            "unknown_output": self._repair_unknown_output,
            "syntax_error": self._repair_syntax_error,
            "missing_required": self._repair_missing_required,
            "invalid_expression": self._repair_invalid_expression,
            "invalid_block": self._repair_invalid_block,
            "variable_not_defined": self._repair_variable_not_defined,
            "type_mismatch": self._repair_type_mismatch,
        }
        return handlers.get(error_type)

    # ------------------------------------------------------------------
    # Deterministic repair handlers
    # ------------------------------------------------------------------

    def _repair_unknown_action(self, lines, error, ir_data=None, mapping_result=None):
        """Fix unknown action ID errors.

        Strategy:
        1. Check if error message suggests a correct action (Did you mean...?)
        2. If suggestion exists, replace action ID
        3. Otherwise, comment out the line and flag for manual review
        """
        line_num = error.get("line", 0)
        message = error.get("message", "")
        if line_num <= 0 or line_num > len(lines):
            return None

        idx = line_num - 1
        original_line = lines[idx]

        # Extract suggestion from error message
        suggested = self._extract_suggestion(message)

        if suggested:
            # Extract current action ID from line
            match = re.match(r'^(\s*)([A-Za-z0-9_.]+)(.*)', original_line)
            if match:
                indent = match.group(1)
                old_action = match.group(2)
                rest = match.group(3)

                # Get suggested template for parameter correction
                suggested_template = self._extract_suggested_template(message)
                if suggested_template:
                    # Use the template directly with proper indentation
                    lines[idx] = f"{indent}{suggested_template}"
                else:
                    lines[idx] = f"{indent}{suggested}{rest}"

                logger.debug(f"Replaced action '{old_action}' with '{suggested}'")
                return lines

        # No suggestion - comment out the line
        indent = re.match(r'^(\s*)', original_line).group(1)
        lines[idx] = f"{indent}# TODO [UNMAPPED ACTION]: {original_line.strip()}"
        return lines

    def _repair_unknown_argument(self, lines, error, ir_data=None, mapping_result=None):
        """Fix unknown argument errors.

        Strategy:
        1. Extract the invalid argument name
        2. Extract valid syntax from error message
        3. Remove or replace the invalid argument
        """
        line_num = error.get("line", 0)
        message = error.get("message", "")
        if line_num <= 0 or line_num > len(lines):
            return None

        idx = line_num - 1
        original_line = lines[idx]

        # Extract invalid argument name
        arg_match = re.search(r"Unknown argument\(s\):\s*'(\w+)'", message)
        if not arg_match:
            return self._repair_with_llm(lines, error)

        invalid_arg = arg_match.group(1)

        # Extract valid syntax template from error hint
        template = self._extract_suggested_template(message)
        if template:
            # Replace entire line with corrected template (preserving indent)
            indent = re.match(r'^(\s*)', original_line).group(1)
            lines[idx] = f"{indent}{template}"
            return lines

        # Try to remove just the invalid argument
        # Pattern: InvalidArg: value (where value can be quoted, variable, etc.)
        pattern = rf'\s*{re.escape(invalid_arg)}:\s*(?:\'[^\']*\'|"[^"]*"|%[^%]*%|\S+)'
        cleaned = re.sub(pattern, '', original_line)

        if cleaned.strip() != original_line.strip():
            lines[idx] = cleaned
            return lines

        return self._repair_with_llm(lines, error)

    def _repair_unknown_output(self, lines, error, ir_data=None, mapping_result=None):
        """Fix unknown output argument errors.

        Strategy:
        1. Extract the invalid output name
        2. Remove or fix the output parameter
        """
        line_num = error.get("line", 0)
        message = error.get("message", "")
        if line_num <= 0 or line_num > len(lines):
            return None

        idx = line_num - 1
        original_line = lines[idx]

        # Extract invalid output name
        out_match = re.search(r"Unknown argument\(s\):\s*'(\w+)'.*Output", message)
        if not out_match:
            return self._repair_with_llm(lines, error)

        invalid_out = out_match.group(1)

        # Never delete an output capture. Removing "Instance=> Var" or
        # "ExcelData=> Var" silently breaks the runtime handle that every
        # downstream action depends on.
        protected_outputs = {
            "Instance",
            "ExcelData",
            "CellValue",
            "BrowserInstance",
            "Result",
        }

        if invalid_out in protected_outputs:
            logger.warning(
                "Refusing to remove output capture '%s=>' - it carries a "
                "runtime handle the flow depends on",
                invalid_out,
            )
            return None

        pattern = rf'\s*{re.escape(invalid_out)}=>\s*\S+'
        cleaned = re.sub(pattern, '', original_line)

        if cleaned.strip() != original_line.strip():
            lines[idx] = cleaned
            return lines

        return self._repair_with_llm(lines, error)

    def _repair_syntax_error(self, lines, error, ir_data=None, mapping_result=None):
        """Fix Robin syntax errors.

        Strategy:
        1. Try common deterministic fixes first
        2. Fall back to LLM for complex syntax issues
        """
        line_num = error.get("line", 0)
        message = error.get("message", "")
        if line_num <= 0 or line_num > len(lines):
            return None

        idx = line_num - 1
        original_line = lines[idx]

        # Fix: Unclosed string literals
        if "string" in message.lower() or "quote" in message.lower():
            fixed = self._fix_unclosed_strings(original_line)
            if fixed != original_line:
                lines[idx] = fixed
                return lines

        # Fix: Missing END for blocks
        if "end" in message.lower() or "expected" in message.lower():
            fixed_lines = self._fix_missing_block_end(lines, idx)
            if fixed_lines:
                return fixed_lines

        # Fix: Extra or misplaced keywords
        if "unexpected" in message.lower():
            fixed = self._fix_unexpected_token(original_line, message)
            if fixed != original_line:
                lines[idx] = fixed
                return lines

        return self._repair_with_llm(lines, error)

    def _repair_missing_required(self, lines, error, ir_data=None, mapping_result=None):
        """Fix missing required parameter errors."""
        return self._repair_with_llm(lines, error)

    def _repair_invalid_expression(self, lines, error, ir_data=None, mapping_result=None):
        """Fix invalid expression errors.

        Strategy:
        1. Try to fix common expression issues
        2. Fall back to LLM for complex expressions
        """
        line_num = error.get("line", 0)
        if line_num <= 0 or line_num > len(lines):
            return None

        idx = line_num - 1
        original_line = lines[idx]

        # Fix: Variable references without %
        fixed = self._fix_variable_references(original_line)
        if fixed != original_line:
            lines[idx] = fixed
            return lines

        return self._repair_with_llm(lines, error)

    def _repair_invalid_block(
        self,
        lines,
        error,
        ir_data=None,
        mapping_result=None,
    ):
        """Repair safe block-balance problems only.

        Duplicate ON BLOCK ERROR declarations cannot safely be repaired by
        changing one line because the surrounding END ownership would also need
        to be reconstructed. The generator must prevent those structures.
        """
        message = (error.get("message") or "").lower()

        if (
            "error block statement was previously defined" in message
            or "block statement was previously defined" in message
        ):
            logger.warning(
                "Duplicate ON BLOCK ERROR detected. Skipping unsafe "
                "line-level repair; regenerate using the corrected "
                "PADScriptGenerator._generate_try_catch()."
            )
            return None

        return self._fix_all_block_structures(lines)

    def _repair_variable_not_defined(self, lines, error, ir_data=None, mapping_result=None):
        """Fix variable not defined errors.

        Strategy:
        1. Extract variable name from error
        2. Add SET declaration at the top of the script
        """
        message = error.get("message", "")

        # Extract variable name
        var_match = re.search(r"variable\s+'?(\w+)'?", message, re.IGNORECASE)
        if not var_match:
            return None

        var_name = var_match.group(1)

        # Find insertion point (after existing SET statements or comments at top)
        insert_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("SET ") or stripped == "":
                insert_idx = i + 1
            else:
                break

        # Add variable declaration
        lines.insert(insert_idx, f"SET {var_name} TO ''")
        logger.debug(f"Added variable declaration for '{var_name}' at line {insert_idx + 1}")
        return lines

    def _repair_type_mismatch(self, lines, error, ir_data=None, mapping_result=None):
        """Fix type mismatch errors."""
        return self._repair_with_llm(lines, error)

    # ------------------------------------------------------------------
    # Deterministic fix helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_suggestion(message):
        """Extract suggested action ID from error message."""
        match = re.search(r"Did you mean '([^']+)'\?", message)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_suggested_template(message):
        """Extract suggested Robin syntax template from error message."""
        match = re.search(r"Syntax:\s*(.+?)(?:\s*$)", message)
        if match:
            template = match.group(1).strip()
            if template:
                return template
        return None

    @staticmethod
    def _fix_unclosed_strings(line):
        """Fix unclosed single-quoted strings in a line."""
        indent = re.match(r'^(\s*)', line).group(1)
        content = line.strip()

        # Count single quotes (excluding escaped)
        quotes = content.count("'") - content.count("\\'")
        if quotes % 2 != 0:
            # Add closing quote at end
            content += "'"

        return f"{indent}{content}"

    @staticmethod
    def _fix_missing_block_end(lines, error_idx):
        """Fix missing END for block structures."""
        # Count openers and closers
        openers = 0
        closers = 0

        block_keywords = {"IF", "LOOP", "FOR", "SWITCH", "BEGIN"}
        end_keywords = {"END", "END EXCEPTION"}

        for line in lines:
            stripped = line.strip().upper()
            for kw in block_keywords:
                if stripped.startswith(kw + " ") or stripped == kw:
                    openers += 1
                    break
            if stripped == "END" or stripped.startswith("END "):
                closers += 1

        if openers > closers:
            # Add missing END(s) before the error line or at end
            missing = openers - closers
            indent = ""
            if error_idx < len(lines):
                indent = re.match(r'^(\s*)', lines[error_idx]).group(1)

            for _ in range(missing):
                lines.append(f"{indent}END")

            return lines

        return None

    @staticmethod
    def _fix_unexpected_token(line, message):
        """Fix unexpected token errors by removing or replacing."""
        indent = re.match(r'^(\s*)', line).group(1)
        content = line.strip()

        # Remove double spaces
        content = re.sub(r'\s{2,}', ' ', content)

        return f"{indent}{content}"

    @staticmethod
    def _fix_variable_references(line):
        """Fix variable references that should be wrapped in %%."""
        # This is conservative - only fix obvious cases
        indent = re.match(r'^(\s*)', line).group(1)
        content = line.strip()

        # Fix: VarName that should be %VarName% in value positions
        # Pattern: after TO keyword or after parameter: keyword
        content = re.sub(
            r'(:\s+)(Var\w+)(\s|$)',
            lambda m: f"{m.group(1)}%{m.group(2)}%{m.group(3)}",
            content
        )

        return f"{indent}{content}"

    def _fix_all_block_structures(self, lines):
        """Scan entire script and fix all block structure issues."""
        block_stack = []
        fixed_lines = list(lines)
        insertions = []

        for i, line in enumerate(lines):
            stripped = line.strip().upper()
            indent = re.match(r'^(\s*)', line).group(1)

            if stripped.startswith("IF ") and "THEN" in stripped:
                block_stack.append(("IF", i, indent))
            elif stripped.startswith("LOOP ") or stripped == "LOOP":
                block_stack.append(("LOOP", i, indent))
            elif stripped.startswith("FOR EACH "):
                block_stack.append(("FOR", i, indent))
            elif stripped.startswith("SWITCH "):
                block_stack.append(("SWITCH", i, indent))
            elif stripped == "BEGIN EXCEPTION":
                block_stack.append(("EXCEPTION", i, indent))
            elif stripped == "END EXCEPTION":
                # Find matching EXCEPTION
                for j in range(len(block_stack) - 1, -1, -1):
                    if block_stack[j][0] == "EXCEPTION":
                        block_stack.pop(j)
                        break
            elif stripped == "END":
                if block_stack:
                    block_stack.pop()
            elif stripped == "ELSE":
                pass  # ELSE is part of IF block, don't change stack
            elif stripped == "EXCEPTION":
                pass  # EXCEPTION is part of BEGIN EXCEPTION block

        # Add missing ENDs
        for block_type, line_idx, indent in reversed(block_stack):
            if block_type == "EXCEPTION":
                insertions.append((len(fixed_lines), f"{indent}END EXCEPTION"))
            else:
                insertions.append((len(fixed_lines), f"{indent}END"))

        # Apply insertions
        for pos, line_text in sorted(insertions, key=lambda x: x[0], reverse=True):
            fixed_lines.insert(pos, line_text)

        if insertions:
            logger.debug(f"Added {len(insertions)} missing END statements")
            return fixed_lines

        return None

    # ------------------------------------------------------------------
    # LLM-based repair fallback
    # ------------------------------------------------------------------

    def _repair_with_llm(self, lines, error):
        """Use LLM to repair a failing script block.

        This is the last resort when deterministic fixes don't apply.

        Args:
            lines: List of script lines
            error: Classified error dict

        Returns:
            list: Repaired lines, or None if repair fails
        """
        line_num = error.get("line", 0)
        message = error.get("message", "")
        failing_block = error.get("failing_block", "")

        if line_num <= 0 or line_num > len(lines):
            return None

        idx = line_num - 1
        original_line = lines[idx]

        # Get surrounding context (5 lines each direction)
        start = max(0, idx - 5)
        end = min(len(lines), idx + 6)
        context = "\n".join(lines[start:end])

        try:
            client = get_llm_client()
            fixed_block = client.suggest_repair(
                failing_block=original_line.strip(),
                error_message=message,
                full_context=context,
            )

            if not fixed_block or fixed_block == original_line.strip():
                return None

            # Clean LLM response
            fixed_block = self._clean_llm_response(fixed_block)

            if not fixed_block:
                return None

            # Preserve original indentation
            indent = re.match(r'^(\s*)', original_line).group(1)

            # Handle multi-line repairs
            fixed_lines = fixed_block.split("\n")
            new_lines = list(lines)

            # Remove original line
            new_lines.pop(idx)

            # Insert fixed lines
            for j, fl in enumerate(fixed_lines):
                fl_stripped = fl.strip()
                if fl_stripped:
                    new_lines.insert(idx + j, f"{indent}{fl_stripped}")

            logger.debug(f"LLM repair applied at line {line_num}")
            return new_lines

        except Exception as e:
            logger.error(f"LLM repair failed: {e}")
            return None

    @staticmethod
    def _clean_llm_response(response):
        """Clean LLM repair response by removing markdown, explanations, etc."""
        text = response.strip()

        # Remove markdown code blocks
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                code = parts[1]
                # Remove language identifier
                if code.startswith("robin") or code.startswith("powershell"):
                    code = code.split("\n", 1)[-1]
                return code.strip()

        # Remove lines that look like explanations
        cleaned_lines = []
        for line in text.split("\n"):
            stripped = line.strip()
            # Skip explanation lines
            if stripped.startswith("Here") or stripped.startswith("The ") or \
               stripped.startswith("I ") or stripped.startswith("This ") or \
               stripped.startswith("Note:") or stripped.startswith("Explanation:"):
                continue
            cleaned_lines.append(line)

        return "\n".join(cleaned_lines).strip()

    # ------------------------------------------------------------------
    # Result building
    # ------------------------------------------------------------------

    def _build_result(self, final_script, is_valid, attempts, validation, unresolved):
        """Build the repair result dict."""
        return {
            "final_script": final_script,
            "is_valid": is_valid,
            "repair_log": self.repair_log,
            "attempts": attempts,
            "validation_result": validation,
            "unresolved_errors": unresolved,
        }

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    @staticmethod
    def save_final_script(script, output_path=None):
        """Save the final repaired script."""
        path = Path(output_path) if output_path else Config.FINAL_SCRIPT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            f.write(script)

        logger.info(f"Final script saved to: {path}")
        return path


def repair_script(script, ir_data=None, mapping_result=None):
    """Convenience function to repair a Robin script.

    Args:
        script: Robin script string
        ir_data: Optional IR JSON
        mapping_result: Optional mapping result

    Returns:
        dict: Repair result
    """
    engine = RepairEngine()
    return engine.repair(script, ir_data, mapping_result)