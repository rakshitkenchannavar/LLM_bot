'''log message -> write text to file
SET variable: top of file, once ✅ (you got this right)
Write text to file actions: NOT at the end — each one sits exactly where the original UiPath Log Message was in the flow. That's essential: a log saying "Started Process" must execute at the start, an error log must execute inside the error branch. Order = source order.
Manual review comment: once per file, next to wherever the first log action happened to be — not at the end.'''


import json
import re
import logging
from pathlib import Path
from config import Config
from validator import run_pad_validator
import tempfile
import os
from llm_client import get_llm_client

logger = logging.getLogger(__name__)


class PADScriptGenerator:
    """Generates PAD Robin script from IR JSON and mapping results.

    Uses pad_llm_schema.json as the authoritative source for Robin script skeletons.

    Schema format per action:
    {
        "ActionId": "Module.Action.SubAction",
        "DisplayName": "...",
        "Description": "...",
        "RobinSyntaxTemplate": "Module.Action Param1: value1 Param2: value2",
        "Inputs": [{"Name": "...", "Type": "...", "EnumValues": [...]}],
        "Outputs": [{"Name": "...", "Type": "..."}]
    }
    """

    # Robin indentation unit
    INDENT = "    "

    def __init__(self):
        self.pad_schema = []
        self.pad_schema_index = {}
        self._manual_review_comments = {}
        self.generated_variables = set()
        self.script_lines = []
        self.indent_level = 0
        self.var_grammar = "bare"

        # Tracks whether generation is currently inside an ON BLOCK ERROR
        # handler. PAD does not allow another active ON BLOCK ERROR declaration
        # inside the same error-handler context.
        self._error_handler_depth = 0

        self._load_pad_schema()

    def _load_pad_schema(self):
        """Load pad_llm_schema.json and build lookup index by ActionId."""
        path = Config.PAD_SCHEMA_PATH
        if not path.exists():
            raise FileNotFoundError(f"PAD schema not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            self.pad_schema = json.load(f)

        self.pad_schema_index = {}
        self._action_id_lower = {}
        self._action_suffix_index = {}

        for entry in self.pad_schema:
            action_id = entry.get("ActionId", "")
            if action_id:
                self.pad_schema_index[action_id] = entry
                self._action_id_lower[action_id.lower()] = entry

                suffix = action_id.split(".")[-1].lower()
                if suffix not in self._action_suffix_index:
                    self._action_suffix_index[suffix] = []
                self._action_suffix_index[suffix].append(entry)

        logger.info(f"PAD schema loaded: {len(self.pad_schema_index)} actions indexed")

    # ------------------------------------------------------------------
    # Schema lookup
    # ------------------------------------------------------------------

    def lookup_schema(self, target_action):
        """Look up a PAD action in the schema by ActionId."""
        if not target_action:
            return None

        if target_action in self.pad_schema_index:
            return self.pad_schema_index[target_action]

        lower = target_action.lower()
        if lower in self._action_id_lower:
            return self._action_id_lower[lower]

        suffix = target_action.split(".")[-1].lower()
        candidates = self._action_suffix_index.get(suffix, [])
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            for candidate in candidates:
                cid = candidate["ActionId"].lower()
                if lower in cid or cid in lower:
                    return candidate
            logger.warning(
                f"Multiple schema matches for '{target_action}', "
                f"using first: {candidates[0]['ActionId']}"
            )
            return candidates[0]

        return None

    # ------------------------------------------------------------------
    # Main generation interface
    # ------------------------------------------------------------------

    def generate(self, ir_data, mapping_result):
        """Build the script strictly from schema templates, then let PAD's own
        Robin parser (PADValidator.ps1) choose the variable grammar that is
        valid on this machine. No guessing, no manual probes."""
        mapping_lookup = {m["action_id"]: m for m in mapping_result.get("mappings", [])}

        best_script, best_grammar = None, None
        for grammar in ("bare", "pct", "interp"):
            self.var_grammar = grammar
            self.script_lines = []
            self.generated_variables = set()
            self.indent_level = 0
            self._emitted = set()
            self._manual_review_comments = {}
            self._error_handler_depth = 0
            self._ui_context_emitted = set()

            if "workflows" in ir_data:
                for idx, workflow in enumerate(ir_data["workflows"]):
                    if workflow.get("error"):
                        self._add_comment(f"ERROR: Skipped workflow '{workflow.get('workflow_name')}' - {workflow['error']}")
                        continue
                    if idx > 0:
                        self._add_line("")
                    self._generate_workflow(workflow, mapping_lookup)
            else:
                self._generate_workflow(ir_data, mapping_lookup)

            self._declare_missing_variables()
            script = self._lint_script("\n".join(self.script_lines))
            script = self._remove_empty_structures(script)
            self._check_block_balance(script)
            if best_script is None:
                best_script, best_grammar = script, grammar

            # Ask PAD's own parser which grammar is valid
            tmp = os.path.join(tempfile.gettempdir(), "_grammar_probe.robin")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(script)
                res = run_pad_validator(tmp)
                if res.get("pad_not_available"):
                    logger.warning("PAD parser unavailable; keeping grammar: bare")
                    break
                if res.get("isValid"):
                    best_script, best_grammar = script, grammar
                    logger.info(f"PAD parser accepted variable grammar: {grammar}")
                    break
                logger.info(f"PAD parser rejected grammar '{grammar}' - trying next")
            except Exception as e:
                logger.warning(f"PAD parser check failed ({e}); keeping first build")
                break

        best_script = self._ensure_pasteable(best_script)
        logger.info(f"Robin script generated with grammar: {best_grammar}")
        return best_script

    def _generate_workflow(self, workflow_ir, mapping_lookup):
        """Generate Robin script for a single workflow."""
        workflow_name = workflow_ir.get("workflow_name", "Main")

        self._add_comment(f"Workflow: {workflow_name}")
        self._add_comment(f"Source: {workflow_ir.get('source_file', 'Unknown')}")
        self._add_comment(f"Migrated from UiPath to Power Automate Desktop")
        self._add_line("")

        self._generate_variable_declarations(workflow_ir)

        actions = workflow_ir.get("actions", [])
        action_tree = self._build_action_tree(actions)

        for action in action_tree:
            self._generate_action(action, mapping_lookup, actions)
    
    # ------------------------------------------------------------------
    # Variable declarations
    # ------------------------------------------------------------------

    def _generate_variable_declarations(self, workflow_ir):
        """Generate SET statements for all workflow variables."""
        variables = workflow_ir.get("variables", [])
        arguments = workflow_ir.get("arguments", [])

        if not variables and not arguments:
            return

        self._add_comment("Variable Declarations")

        for var in variables:
            name = var.get("name", "")
            if not name:
                continue
            default = var.get("default_value", "")
            var_type = var.get("type", "General")
            default_value = self._get_default_for_type(var_type, default)
            safe = self._safe_var(name)

            # Prefer the exact UiPath type over the simplified IR type so
            # Dictionary(String, Object) and Double are not reduced to
            # Text or Number.
            source_type = (
                var.get("source_type")
                or var.get("type")
                or ""
            )

            if source_type and source_type not in ("General", "Object"):
                self._add_line(
                    f"# Variable Type: {source_type}"
                )

            self._add_line(f"SET {safe} TO {default_value}")
            self.generated_variables.add(safe)

        for arg in arguments:
            name = arg.get("name", "")
            if not name or name in self.generated_variables:
                continue
            default = arg.get("default_value", "")
            arg_type = arg.get("type", "General")
            default_value = self._get_default_for_type(arg_type, default)
            direction = arg.get("direction", "In")

            arg_source_type = (
                arg.get("source_type")
                or arg.get("type")
                or ""
            )

            if arg_source_type and arg_source_type not in (
                "General",
                "Object",
            ):
                self._add_line(
                    f"# Argument ({direction}) Type: {arg_source_type}"
                )

            self._add_line(f"SET {name} TO {default_value}")
            self.generated_variables.add(name)

        self._add_line("")

    # ------------------------------------------------------------------
    # Action tree building
    # ------------------------------------------------------------------

    def _build_action_tree(self, actions):
        """Build ordered action tree from flat action list."""
        roots = []
        for action in actions:
            parent = action.get("parent_id")
            if parent is None:
                roots.append(action)
        roots.sort(key=lambda a: a.get("order", 0))
        return roots

    def _get_action_by_id(self, action_id, all_actions):
        """Find an action by its ID from the flat list."""
        for action in all_actions:
            if action.get("action_id") == action_id:
                return action
        return None

    # ------------------------------------------------------------------
    # Action generation dispatcher
    # ------------------------------------------------------------------

    def _generate_action(self, action, mapping_lookup, all_actions):
        """Generate Robin script for a single action."""
        action_id = action.get("action_id", "")
        action_type = action.get("action_type", "")
        display_name = action.get("display_name", "")

        # Each activity node is generated at most once (kills duplicate blocks)
        if action_id:
            if action_id in self._emitted:
                return
            self._emitted.add(action_id)

        mapping = mapping_lookup.get(action_id)

        if not mapping:
            self._add_manual_review(
                action=action,
                reason="No target mapping was found",
                required_work=(
                    "Create the corresponding PAD action manually."
                ),
            )
            self._generate_children(action, mapping_lookup, all_actions)
            return

        target_action = mapping.get("target_action", "")

        # Sequence/Flowchart map to "Subflow" in CSV but are passthrough in Robin
        if target_action in ("Subflow", "Step", "BLOCK:Subflow", "BLOCK:Step") \
                or action.get("container_type") in ("Sequence", "Flowchart"):
            self._generate_children(action, mapping_lookup, all_actions)
            return
        
        # UiPath LogMessage and WriteLine must write silently to a file.
        source_base_type = (
            str(action_type)
            .split("<", 1)[0]
            .split("(", 1)[0]
            .strip()
        )

        normalized_source_type = re.sub(
            r"[^a-z0-9]",
            "",
            source_base_type.lower(),
        )

        if normalized_source_type in {
            "logmessage",
            "writeline",
        }:
            self._generate_file_log_action(
                action,
                mapping,
            )
            return

        # UiPath Excel scopes become launch + children + close in PAD.
        if action_type in (
            "ExcelApplicationScope",
            "ExcelProcessScope",
        ):
            self._generate_excel_scope(
                action,
                mapping,
                mapping_lookup,
                all_actions,
            )
            return

        # Excel read/write activities need worksheet activation and
        # range coordinates, which the generic template path cannot
        # supply.
        if action_type in (
            "ReadRange",
            "ReadCell",
            "ReadColumn",
            "WriteRange",
            "WriteCell",
            "AppendRange",
            "SaveWorkbook",
        ):
            if self._generate_excel_activity(action, mapping):
                self._generate_children(
                    action,
                    mapping_lookup,
                    all_actions,
                )
                return

        # Source-type guarantee: Throw/Rethrow always become THROW ERROR
        if action_type in ("Throw", "Rethrow"):
            self._generate_throw(action, mapping)
            return
        
        # Source-type guarantee: TryCatch/RetryScope always become exception blocks
        if action_type in ("TryCatch", "RetryScope"):
            self._generate_try_catch(action, mapping, mapping_lookup, all_actions)
            return
        
        # Source-type guarantee: subflow invocations always use schema-backed call
        target_lower = target_action.lower()
        if action_type == "InvokeWorkflowFile" or target_action == "Flow.RunSubflow" \
                or (target_action.startswith("Flow.") and "run" in target_lower):
            self._generate_subflow_call(action, mapping)
            return

        # Dispatch based on target action type
        if target_action == "UNMAPPED":
            self._generate_unmapped(action, mapping)
            self._generate_children(action, mapping_lookup, all_actions)

        elif target_action == "COMMENT":
            self._generate_comment_action(action, mapping)

        elif target_action.startswith("BLOCK:"):
            self._generate_block(action, mapping, mapping_lookup, all_actions)

        elif target_action == "Conditionals.If":
            self._generate_if(action, mapping, mapping_lookup, all_actions)

        elif target_action == "Conditionals.Switch":
            self._generate_switch(action, mapping, mapping_lookup, all_actions)

        elif target_action in ("Loops.ForEach", "Loops.Loop"):
            self._generate_loop(action, mapping, mapping_lookup, all_actions)

        elif target_action == "ErrorHandling.BeginException":
            if action.get("action_type") in ("TryCatch", "RetryScope"):
                self._generate_try_catch(action, mapping, mapping_lookup, all_actions)
            else:
                # Inner Try/Catch/Finally wrappers: pass through children
                self._generate_children(action, mapping_lookup, all_actions)

        elif target_action in ("ErrorHandling.ThrowError", "Throw"):
            self._generate_throw(action, mapping)

        elif target_action == "Flow.RunSubflow":
            self._generate_subflow_call(action, mapping)

        elif target_action == "Variables.SetVariable":
            self._generate_set_variable(action, mapping)

        elif target_action == "System.Wait":
            self._generate_wait(action, mapping)

        else:
            # Standard action - use schema skeleton
            generated = self._generate_standard_action(action, mapping)
            if not generated and action.get("container_type"):
                # Safety: an unknown container must not swallow its subtree
                self._generate_children(action, mapping_lookup, all_actions)

        # Generate children for non-container actions
        container = action.get("container_type")
        if not container and target_action not in (
            "Conditionals.If", "Conditionals.Switch",
            "Loops.ForEach", "Loops.Loop",
            "ErrorHandling.BeginException",
        ) and not target_action.startswith("BLOCK:"):
            self._generate_children(action, mapping_lookup, all_actions)

    def _generate_children(self, action, mapping_lookup, all_actions):
        """Generate Robin script for all children of an action."""
        child_ids = action.get("child_ids", [])
        for child_id in child_ids:
            child_action = self._get_action_by_id(child_id, all_actions)
            if child_action:
                self._generate_action(child_action, mapping_lookup, all_actions)

    # ------------------------------------------------------------------
    # Control flow generators
    # ------------------------------------------------------------------

    def _generate_if(self, action, mapping, mapping_lookup, all_actions):
        """Generate IF/ELSE/END block."""
        condition = self._resolve_condition(action, mapping)

        self._add_line('IF $"' + condition + '" THEN')
        self.indent_level += 1

        child_ids = action.get("child_ids", [])
        then_children = []
        else_children = []
        current_block = "then"

        for child_id in child_ids:
            child = self._get_action_by_id(child_id, all_actions)
            if not child:
                continue

            child_type = child.get("action_type", "")

            if child_type in ("Block_Then", "Block_True"):
                current_block = "then"
                for sub_id in child.get("child_ids", []):
                    sub = self._get_action_by_id(sub_id, all_actions)
                    if sub:
                        then_children.append(sub)
                continue

            if child_type in ("Block_Else", "Block_False"):
                current_block = "else"
                for sub_id in child.get("child_ids", []):
                    sub = self._get_action_by_id(sub_id, all_actions)
                    if sub:
                        else_children.append(sub)
                continue

            if current_block == "then":
                then_children.append(child)
            else:
                else_children.append(child)

        if then_children:
            for child in then_children:
                self._generate_action(child, mapping_lookup, all_actions)
        else:
            self._add_comment("Empty Then block")

        if else_children:
            self.indent_level -= 1
            self._add_line("ELSE")
            self.indent_level += 1
            for child in else_children:
                self._generate_action(child, mapping_lookup, all_actions)

        self.indent_level -= 1
        self._add_line("END")
        
    def _generate_switch(self, action, mapping, mapping_lookup, all_actions):
        """Generate SWITCH/CASE/DEFAULT/END block."""
        value = self._resolve_switch_value(action, mapping)

        self._add_line(f"SWITCH {value}")
        self.indent_level += 1

        child_ids = action.get("child_ids", [])
        for child_id in child_ids:
            child = self._get_action_by_id(child_id, all_actions)
            if not child:
                continue

            child_type = child.get("action_type", "")
            if "Default" in child_type:
                self._add_line("DEFAULT")
                self.indent_level += 1
                for sub_id in child.get("child_ids", []):
                    sub = self._get_action_by_id(sub_id, all_actions)
                    if sub:
                        self._generate_action(sub, mapping_lookup, all_actions)
                self.indent_level -= 1
            else:
                case_value = child.get("properties", {}).get("Value", child.get("display_name", ""))
                self._add_line(f"CASE {case_value}")
                self.indent_level += 1
                for sub_id in child.get("child_ids", []):
                    sub = self._get_action_by_id(sub_id, all_actions)
                    if sub:
                        self._generate_action(sub, mapping_lookup, all_actions)
                self.indent_level -= 1

        self.indent_level -= 1
        self._add_line("END")

    def _generate_loop(self, action, mapping, mapping_lookup, all_actions):
        """Generate loops using the schema's real Robin keywords."""
        target_action = mapping.get("target_action", "")

        if target_action == "Loops.ForEach":
            item_var = self._resolve_param(
                action, mapping, "CurrentItem", "CurrentItem"
            )

            # Nested loops must not share the same item variable.
            if item_var == "CurrentItem":
                item_var = re.sub(
                    r"[^A-Za-z0-9_]",
                    "_",
                    f"CurrentItem_{action.get('action_id', 'x')}",
                )
            list_var = self._resolve_param(action, mapping, "List", "ItemList")

            if list_var == "ItemList":
                props = action.get("properties", {})
                exprs = action.get("expressions", {})
                raw = (props.get("Values") or props.get("DataTable") or
                       props.get("Collection") or exprs.get("Values") or
                       exprs.get("DataTable") or "")
                if raw:
                    raw = raw.strip()
                    if raw.startswith("[") and raw.endswith("]"):
                        raw = raw[1:-1].strip()
                    if re.match(r'^[A-Za-z_]\w*$', raw):
                        list_var = raw

            for v in (item_var, list_var):
                if v not in self.generated_variables:
                    self._add_line(f"SET {v} TO ''")
                    self.generated_variables.add(v)

            self._add_line(f"LOOP FOREACH {item_var} IN {self._var_ref(list_var)}")
        else:
            condition = self._resolve_condition(action, mapping)
            self._add_line(f"LOOP WHILE {condition}")

        self.indent_level += 1

        child_ids = action.get("child_ids", [])
        body_generated = False
        for child_id in child_ids:
            child = self._get_action_by_id(child_id, all_actions)
            if not child:
                continue

            child_type = child.get("action_type", "")
            if child_type in ("Block_Body", "Block_Action"):
                for sub_id in child.get("child_ids", []):
                    sub = self._get_action_by_id(sub_id, all_actions)
                    if sub:
                        self._generate_action(sub, mapping_lookup, all_actions)
                        body_generated = True
            else:
                self._generate_action(child, mapping_lookup, all_actions)
                body_generated = True

        if not body_generated:
            self._add_comment("Empty loop body")

        self.indent_level -= 1
        self._add_line("END")
        
    def _generate_try_catch(
        self,
        action,
        mapping,
        mapping_lookup,
        all_actions,
    ):
        """Generate UiPath TryCatch using flag-based PAD BLOCK grammar.

        Designer-verified: ON BLOCK ERROR accepts only flat statements.
        The handler raises a flag; catch logic runs afterwards in a normal
        IF, which supports arbitrary nesting.
        """
        try_children = []
        catch_children = []
        finally_children = []

        def add_children(node, destination):
            for child_id in node.get("child_ids", []):
                child = self._get_action_by_id(child_id, all_actions)
                if child:
                    destination.append(child)

        def classify_children(node):
            for child_id in node.get("child_ids", []):
                child = self._get_action_by_id(child_id, all_actions)
                if not child:
                    continue

                child_type = child.get("action_type", "")
                child_container = child.get("container_type", "")

                if child_type == "Block_Try" or child_container == "Try":
                    add_children(child, try_children)
                    continue

                if (
                    child_type == "Block_Finally"
                    or child_container == "Finally"
                ):
                    add_children(child, finally_children)
                    continue

                if (
                    child_type == "Catch"
                    or child_type.startswith("Catch<")
                    or child_container == "Catch"
                ):
                    add_children(child, catch_children)
                    continue

                if (
                    child_type.startswith("Block_")
                    or child_type == "Container"
                    or "ActivityBody" in child_type
                    or "ActivityAction" in child_type
                ):
                    classify_children(child)
                    continue

                try_children.append(child)

        classify_children(action)

        # Record the entry indent so it can be restored no matter what
        # the child generators do.
        entry_indent = self.indent_level

        error_flag = re.sub(
            r"[^A-Za-z0-9_]",
            "_",
            f"BlockError_{action.get('action_id') or 'x'}",
        )

        self._add_line(f"SET {error_flag} TO False")
        self.generated_variables.add(error_flag)

        # ---- BLOCK ------------------------------------------------
        self._add_line("BLOCK")
        self.indent_level = entry_indent + 1

        # ---- ON BLOCK ERROR (exactly one flat statement) ----------
        self._add_line("ON BLOCK ERROR")
        self.indent_level = entry_indent + 2
        self._add_line(f"SET {error_flag} TO True")
        self.indent_level = entry_indent + 1
        self._add_line("END")

        # ---- protected body ---------------------------------------
        body_start = len(self.script_lines)

        for child in try_children:
            self._generate_action(
                child,
                mapping_lookup,
                all_actions,
            )

        # Child generators must not leak indent changes.
        self.indent_level = entry_indent + 1

        executable_body = [
            line
            for line in self.script_lines[body_start:]
            if line.strip() and not line.strip().startswith("#")
        ]

        if not executable_body:
            placeholder_var = f"{error_flag}_Placeholder"

            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "Protected block body has no executable PAD action "
                    "because its source activities require manual "
                    "migration; the catch logic below is unreachable "
                    "until real actions are added here"
                ),
                suggested_pad_action="Block / On block error",
                source_data={
                    "TryActionCount": len(try_children),
                    "CatchActionCount": len(catch_children),
                    "PlaceholderVariable": placeholder_var,
                },
                required_work=(
                    "Replace the placeholder SET below with the migrated "
                    "business actions (see the review comments above)."
                ),
            )

            self._add_line(
                f"SET {placeholder_var} TO "
                f"$'''Replace with migrated actions'''"
            )
            self.generated_variables.add(placeholder_var)

        # ---- close BLOCK (always) ---------------------------------
        self.indent_level = entry_indent
        self._add_line("END")

        # ---- catch logic outside the block ------------------------
        if catch_children:
            self._add_line(
                f'IF $"%{error_flag}% = True" THEN'
            )
            self.indent_level = entry_indent + 1

            for child in catch_children:
                self._generate_action(
                    child,
                    mapping_lookup,
                    all_actions,
                )

            self.indent_level = entry_indent
            self._add_line("END")

        # ---- finally ----------------------------------------------
        if finally_children:
            for child in finally_children:
                self._generate_action(
                    child,
                    mapping_lookup,
                    all_actions,
                )

        # Guarantee the caller sees the same indent it started with.
        self.indent_level = entry_indent
                
    def _generate_block(
        self,
        action,
        mapping,
        mapping_lookup,
        all_actions,
    ):
        """Generate structural UiPath containers.

        Structural blocks do not emit unsupported Robin actions. Their
        children are preserved in source execution order.
        """
        target_action = mapping.get("target_action", "BLOCK:Unknown")
        block_type = target_action.replace("BLOCK:", "", 1)
        display_name = (
            action.get("display_name")
            or action.get("action_type")
            or block_type
        )

        if block_type == "StateMachine":
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "PAD has no state-machine container; states were "
                    "flattened into sequential execution"
                ),
                suggested_pad_action=(
                    "Loop with CurrentState variable and If branches"
                ),
                required_work=(
                    "Re-wire states as a CurrentState loop. States "
                    "currently run sequentially, which is NOT the "
                    "source behavior."
                ),
            )
            self._generate_children(
                action,
                mapping_lookup,
                all_actions,
            )
            return

        if block_type == "State":
            self._add_line("")
            self._add_comment(
                f"===== STATE: {display_name} ====="
            )
            self._generate_children(
                action,
                mapping_lookup,
                all_actions,
            )
            return

        if block_type == "Transition":
            properties = action.get("properties", {})
            expressions = action.get("expressions", {})

            condition = (
                properties.get("Condition")
                or expressions.get("Condition")
                or expressions.get("expression_0")
                or ""
            )

            if condition:
                translated_condition = self._translate_expression(
                    condition
                )
            else:
                translated_condition = "always"

            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "State transition requires manual state-variable "
                    "routing"
                ),
                suggested_pad_action="If condition plus Set CurrentState",
                source_data={
                    "Condition": condition or "unconditional",
                    "TranslatedCondition": translated_condition,
                },
                required_work=(
                    "Set CurrentState to the target state when this "
                    "condition is true."
                ),
            )
            self._generate_children(
                action,
                mapping_lookup,
                all_actions,
            )
            return

        if block_type == "FlowStep":
            self._generate_children(
                action,
                mapping_lookup,
                all_actions,
            )
            return

        # Sequence, Flowchart, Then, Else, Body, Action, Container,
        # Subflow and other structural wrappers are passthrough blocks.
        self._generate_children(
            action,
            mapping_lookup,
            all_actions,
        )
            
    def _extract_file_log_message(self, action):
        """Extract the original UiPath LogMessage or WriteLine content."""
        properties = action.get("properties", {}) or {}
        expressions = action.get("expressions", {}) or {}

        source_type = re.sub(
            r"[^a-z0-9]",
            "",
            str(action.get("action_type") or "")
            .split("<", 1)[0]
            .lower(),
        )

        if source_type == "logmessage":
            candidate_keys = (
                "Message",
                "Text",
                "Value",
                "Expression",
            )
        else:
            candidate_keys = (
                "Text",
                "Message",
                "Value",
                "Expression",
            )

        for key in candidate_keys:
            value = properties.get(key)

            if value not in ("", None):
                return value

            value = expressions.get(key)

            if value not in ("", None):
                return value

        for key in (
            "expression_0",
            "expression_1",
        ):
            value = expressions.get(key)

            if value not in ("", None):
                return value

        return ""

    @staticmethod
    def _extract_file_log_level(action):
        """Extract UiPath LogMessage level when available."""
        properties = action.get("properties", {}) or {}
        expressions = action.get("expressions", {}) or {}

        return (
            properties.get("Level")
            or properties.get("LogLevel")
            or expressions.get("Level")
            or expressions.get("LogLevel")
            or ""
        )
    
    def _generate_file_log_action(self, action, mapping):
        """Generate PAD Write text to file for LogMessage and WriteLine.

        This affects only UiPath logging activities. It preserves the source
        message expression and generates:
        - AppendNewLine: True
        - IfFileExists: Append
        - Encoding: Unicode
        """
        target_action = mapping.get(
            "target_action",
            "",
        )

        if target_action != "File.WriteText":
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "The logging activity was not mapped to the PAD "
                    "Write text to file action"
                ),
                suggested_pad_action="File.WriteText",
                source_data={
                    "OriginalMessage": (
                        self._extract_file_log_message(action)
                    ),
                },
                required_work=(
                    "Create Write text to file manually with "
                    "AppendNewLine=True, IfFileExists=Append, and "
                    "Unicode or UTF-8 encoding."
                ),
            )
            return

        schema_entry = self.lookup_schema(
            "File.WriteText"
        )

        if not schema_entry:
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "File.WriteText was not found in the authoritative "
                    "PAD schema"
                ),
                suggested_pad_action="Write text to file",
                source_data={
                    "OriginalMessage": (
                        self._extract_file_log_message(action)
                    ),
                },
                required_work=(
                    "Create Write text to file manually with append mode."
                ),
            )
            return

        input_names = {
            str(item.get("Name") or "")
            for item in schema_entry.get("Inputs", [])
            if isinstance(item, dict)
        }

        required_inputs = {
            "AppendNewLine",
            "Encoding",
            "File",
            "IfFileExists",
            "TextToWrite",
        }

        missing_inputs = required_inputs - input_names

        if missing_inputs:
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "File.WriteText does not expose all required logging "
                    "parameters in the PAD schema"
                ),
                suggested_pad_action="File.WriteText",
                source_data={
                    "MissingParameters": sorted(
                        missing_inputs
                    ),
                    "OriginalMessage": (
                        self._extract_file_log_message(action)
                    ),
                },
                required_work=(
                    "Configure the missing logging parameters manually."
                ),
            )
            return

        source_message = self._extract_file_log_message(
            action
        )

        if source_message in ("", None):
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "The source logging activity does not contain a "
                    "message expression"
                ),
                suggested_pad_action="File.WriteText",
                required_work=(
                    "Set TextToWrite using the original UiPath logging "
                    "requirement."
                ),
            )
            return

        # UiPath logging does not contain a physical destination file.
        # Use one configurable variable for the generated workflow.
        log_path_variable = "MigrationLogFilePath"

        if log_path_variable not in self.generated_variables:
            # Declare at flow top, never inside the current branch, so all
            # branches referencing %MigrationLogFilePath% see it assigned.
            self.script_lines.insert(
                0,
                "SET MigrationLogFilePath TO "
                "$'''C:\\Temp\\MigrationLog.txt'''",
            )

            # The insert shifted every previously recorded manual-review
            # line index down by one - keep them in sync.
            for review_entry in self._manual_review_comments.values():
                review_entry["line_index"] += 1

            self.generated_variables.add(
                log_path_variable
            )
            self._add_manual_review(
                action={
                    "action_id": "migration_log_configuration",
                    "action_type": "LogConfiguration",
                    "display_name": "Configure migration log file",
                    "properties": {},
                    "expressions": {},
                    "selector": None,
                },
                reason=(
                    "UiPath LogMessage and WriteLine do not define a "
                    "physical PAD log-file destination"
                ),
                suggested_pad_action="Set variable",
                source_data={
                    "Variable": "MigrationLogFilePath",
                    "TemporaryPath": (
                        r"C:\Temp\MigrationLog.txt"
                    ),
                },
                required_work=(
                    "Change MigrationLogFilePath to the required "
                    "production log-file location."
                ),
            )

        raw_message = str(source_message).strip()

        # Only treat the message as an expression when it actually looks
        # like one. Plain literal text must never have its words wrapped
        # as %variables%.
        looks_like_expression = bool(
            re.search(
                r'["\[\]%+()]|\.ToString|\bNew\s|\bvbCrLf\b',
                raw_message,
            )
        )

        if looks_like_expression:
            translated_message = self._translate_expression(
                raw_message
            )
        else:
            # Preserve the literal exactly; single quotes are dropped to
            # keep the Robin literal safe.
            safe_literal = raw_message.replace("'", "")
            translated_message = f"'{safe_literal}'"

        # Bare identifiers joined by '+' mean variable wrapping failed;
        # emitting them writes literal text like "A + B" to the log.
        if re.search(
            r"[A-Za-z_]\w*\s*\+\s*[A-Za-z_]\w*",
            translated_message,
        ):
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "Log message concatenation could not be resolved "
                    "to PAD variables"
                ),
                source_data={"OriginalMessage": source_message},
                required_work=(
                    "Rebuild the message text using PAD variables."
                ),
            )
            translated_message = "'MANUAL_Fix'"
        source_level = self._extract_file_log_level(
            action
        )

        normalized_action_type = re.sub(
            r"[^a-z0-9]",
            "",
            str(action.get("action_type") or "")
            .split("<", 1)[0]
            .lower(),
        )

        # Preserve the separate UiPath LogMessage level in the file entry.
        # Never build a '+' concatenation for the prefix - splice it inside
        # the final literal instead, because _pad_set_value treats any
        # quote-delimited string as one literal.
        if (
            normalized_action_type == "logmessage"
            and source_level not in ("", None)
        ):
            clean_level = re.sub(
                r"\s+",
                " ",
                str(source_level).strip(),
            )
            level_prefix = f"[{clean_level}] "
        else:
            level_prefix = ""

        text_to_write = self._pad_set_value(
            translated_message
        )

        if level_prefix:
            if (
                text_to_write.startswith("$'''")
                and text_to_write.endswith("'''")
            ):
                inner = text_to_write[4:-3]
                text_to_write = (
                    "$'''" + level_prefix + inner + "'''"
                )
            elif (
                len(text_to_write) >= 2
                and text_to_write.startswith("'")
                and text_to_write.endswith("'")
            ):
                inner = text_to_write[1:-1]
                text_to_write = (
                    "$'''" + level_prefix + inner + "'''"
                )
            elif re.fullmatch(r"[A-Za-z_]\w*", text_to_write):
                # Bare variable name returned by _pad_set_value.
                text_to_write = (
                    "$'''" + level_prefix
                    + "%" + text_to_write + "%'''"
                )
            else:
                # Any other expression form - wrap as interpolated text.
                text_to_write = (
                    "$'''" + level_prefix + text_to_write + "'''"
                )

        # Final guard: bare identifiers joined by '+' survived translation
        # and would be written to the log as literal text.
        if re.search(
            r"[A-Za-z_]\w*\s*\+\s*[A-Za-z_]\w*",
            text_to_write,
        ):
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "Log message concatenation could not be resolved "
                    "to PAD variables"
                ),
                source_data={"OriginalMessage": source_message},
                required_work=(
                    "Rebuild the message text using PAD variables."
                ),
            )
            text_to_write = "$'''MANUAL_Fix'''"

        # Exact ActionId, parameters and enum values verified from
        # pad_llm_schema.json.
        self._add_line(
            "File.WriteText "
            "AppendNewLine: True "
            "Encoding: File.FileEncoding.Unicode "
            "File: $'''%MigrationLogFilePath%''' "
            "IfFileExists: File.IfFileExists.Append "
            f"TextToWrite: {text_to_write}"
        )
    
    @staticmethod
    def _parse_excel_range(range_text):
        """Parse A1 notation into start/end column and row.

        'A1:D50' -> A,1,D,50   'B7' -> B,7,B,7   '' -> None
        """
        text = str(range_text or "").strip().strip("'\"[] ")

        if not text:
            return None

        match = re.fullmatch(
            r"([A-Za-z]{1,3})(\d{1,7})"
            r"(?::([A-Za-z]{1,3})(\d{1,7}))?",
            text,
        )

        if not match:
            return None

        return {
            "start_column": match.group(1).upper(),
            "start_row": int(match.group(2)),
            "end_column": (
                match.group(3) or match.group(1)
            ).upper(),
            "end_row": int(
                match.group(4) or match.group(2)
            ),
            "is_single_cell": match.group(3) is None,
        }

    def _emit_worksheet_activation(
        self,
        action,
        mapping,
        instance_var,
        sheet_name,
    ):
        """Activate the target worksheet before a read or write.

        PAD read/write actions operate on the active sheet, so the
        UiPath SheetName must become a separate activation action.
        """
        if not sheet_name:
            return

        activate_id = (
            "Excel.SetActiveWorksheet.ActivateWorksheetByName"
        )

        if activate_id not in self.pad_schema_index:
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "No worksheet activation action in the PAD schema; "
                    "the read/write below uses the active sheet"
                ),
                suggested_pad_action="Set active worksheet",
                source_data={"SheetName": sheet_name},
                required_work=(
                    "Activate the correct worksheet before this action."
                ),
            )
            return

        sheet_value = self._pad_set_value(
            self._translate_expression(str(sheet_name))
        )

        if "MANUAL_Fix" in sheet_value:
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "Worksheet name expression could not be translated"
                ),
                suggested_pad_action=activate_id,
                source_data={"SheetName": sheet_name},
                required_work=(
                    "Set the worksheet name on the activation action."
                ),
            )

        self._add_line(
            f"{activate_id} "
            f"Instance: {instance_var} "
            f"Name: {sheet_value}"
        )

    def _resolve_excel_output_variable(
        self,
        action,
        fallback_prefix,
    ):
        """Pick the variable that should receive an Excel read result."""
        properties = action.get("properties", {}) or {}
        expressions = action.get("expressions", {}) or {}

        target = (
            properties.get("DataTable")
            or expressions.get("DataTable")
            or properties.get("Result")
            or expressions.get("Result")
            or properties.get("Output")
            or expressions.get("Output")
            or properties.get("Value")
            or ""
        )

        if target:
            cleaned = self._clean_variable_name(target)

            if cleaned:
                self._ensure_variable(cleaned)
                return cleaned

        generated = re.sub(
            r"[^A-Za-z0-9_]",
            "_",
            f"{fallback_prefix}_{action.get('action_id', 'x')}",
        )

        self._ensure_variable(generated)
        return generated
    
    def _generate_excel_scope(
        self,
        action,
        mapping,
        mapping_lookup,
        all_actions,
    ):
        """Open Excel, run scoped children, then close it.

        UiPath's Excel scope becomes three PAD actions. The instance
        capture syntax (Instance=> Var) is designer-verified, even though
        the schema records no Outputs for these actions.
        """
        properties = action.get("properties", {}) or {}
        expressions = action.get("expressions", {}) or {}

        workbook_path = (
            properties.get("WorkbookPath")
            or expressions.get("WorkbookPath")
            or ""
        )

        if not self.lookup_schema("Excel.LaunchExcel.LaunchAndOpen"):
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "No Excel launch action found in the PAD schema"
                ),
                suggested_pad_action="Launch Excel",
                source_data={"WorkbookPath": workbook_path},
                required_work=(
                    "Open the workbook manually before the child "
                    "actions below."
                ),
            )
            self._generate_children(
                action,
                mapping_lookup,
                all_actions,
            )
            return

        instance_var = re.sub(
            r"[^A-Za-z0-9_]",
            "_",
            f"ExcelInstance_{action.get('action_id', 'x')}",
        )

        if workbook_path:
            translated_path = self._translate_expression(
                workbook_path
            )
            path_value = self._pad_set_value(translated_path)

            self._add_line(
                "# OriginalExpression: "
                + re.sub(r"\s+", " ", str(workbook_path))[:200]
            )
        else:
            path_value = "$'''MANUAL_Fix'''"

        if "MANUAL_Fix" in path_value:
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "Workbook path could not be translated to PAD"
                ),
                suggested_pad_action=(
                    "Excel.LaunchExcel.LaunchAndOpen"
                ),
                source_data={"WorkbookPath": workbook_path},
                required_work=(
                    "Set the workbook path on the Launch Excel action."
                ),
            )

        # Designer-verified: Instance=> captures the workbook handle.
        self._add_line(
            "Excel.LaunchExcel.LaunchAndOpen "
            "LoadAddInsAndMacros: False "
            "UseMachineLocale: False "
            f"Path: {path_value} "
            "ReadOnly: False "
            "Visible: False "
            f"Instance=> {instance_var}"
        )
        self.generated_variables.add(instance_var)

        previous_instance = getattr(
            self,
            "_active_excel_instance",
            None,
        )
        self._active_excel_instance = instance_var

        try:
            self._generate_children(
                action,
                mapping_lookup,
                all_actions,
            )
        finally:
            self._active_excel_instance = previous_instance

        if self.lookup_schema("Excel.CloseExcel.Close"):
            self._add_line(
                f"Excel.CloseExcel.Close Instance: {instance_var}"
            )

    def _generate_excel_activity(self, action, mapping):
        """Generate a PAD Excel read/write/save with sheet and range.

        Returns True when an action was emitted, False to fall back to
        the generic schema path.
        """
        instance_var = getattr(
            self,
            "_active_excel_instance",
            None,
        )

        if not instance_var:
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "Excel activity is not inside a workbook scope, so "
                    "no PAD Excel instance is available"
                ),
                suggested_pad_action="Launch Excel",
                required_work=(
                    "Add a Launch Excel action and reference its "
                    "instance in this action."
                ),
            )
            return False

        properties = action.get("properties", {}) or {}
        expressions = action.get("expressions", {}) or {}

        action_type = re.sub(
            r"[^a-z]",
            "",
            str(action.get("action_type") or "").lower(),
        )

        sheet_name = (
            properties.get("SheetName")
            or expressions.get("SheetName")
            or ""
        )

        self._emit_worksheet_activation(
            action=action,
            mapping=mapping,
            instance_var=instance_var,
            sheet_name=sheet_name,
        )

        cell_range = (
            properties.get("Range")
            or expressions.get("Range")
            or ""
        )

        parsed_range = self._parse_excel_range(cell_range)

        if cell_range and not parsed_range:
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "Excel range is a dynamic expression and could not "
                    "be converted to fixed coordinates"
                ),
                suggested_pad_action="Excel read/write with coordinates",
                source_data={"Range": cell_range},
                required_work=(
                    "Set the start and end coordinates manually."
                ),
            )

        add_headers = str(
            properties.get("AddHeaders")
            or properties.get("FirstLineIsHeader")
            or "False"
        ).strip().lower() in ("true", "1")

        header_flag = "True" if add_headers else "False"

        contents_mode = (
            "GetCellContentsMode: "
            "Excel.GetCellContentsMode.TypedValues"
        )

        # ----------------------------------------------------------
        # READ
        # ----------------------------------------------------------
        if action_type in ("readrange", "readcell", "readcolumn"):
            output_var = self._resolve_excel_output_variable(
                action,
                "ExcelData",
            )

            if parsed_range and parsed_range["is_single_cell"]:
                self._add_line(
                    "Excel.ReadFromExcel.ReadCell "
                    f"Instance: {instance_var} "
                    f"{contents_mode} "
                    f"StartColumn: $'''{parsed_range['start_column']}''' "
                    f"StartRow: {parsed_range['start_row']} "
                    f"CellValue=> {output_var}"
                )
                return True

            if parsed_range:
                self._add_line(
                    "Excel.ReadFromExcel.ReadCells "
                    f"EndColumn: $'''{parsed_range['end_column']}''' "
                    f"EndRow: {parsed_range['end_row']} "
                    f"FirstLineIsHeader: {header_flag} "
                    f"Instance: {instance_var} "
                    f"{contents_mode} "
                    f"StartColumn: $'''{parsed_range['start_column']}''' "
                    f"StartRow: {parsed_range['start_row']} "
                    f"ExcelData=> {output_var}"
                )
                return True

            self._add_line(
                "Excel.ReadFromExcel.ReadAllCells "
                f"FirstLineIsHeader: {header_flag} "
                f"Instance: {instance_var} "
                f"{contents_mode} "
                f"ExcelData=> {output_var}"
            )
            return True

        # ----------------------------------------------------------
        # WRITE
        # ----------------------------------------------------------
        if action_type in ("writerange", "writecell"):
            source_value = (
                properties.get("DataTable")
                or expressions.get("DataTable")
                or properties.get("Text")
                or expressions.get("Text")
                or properties.get("Value")
                or expressions.get("Value")
                or ""
            )

            if source_value:
                self._add_line(
                    "# OriginalExpression: "
                    + re.sub(
                        r"\s+",
                        " ",
                        str(source_value),
                    )[:200]
                )

                write_value = self._pad_set_value(
                    self._translate_expression(str(source_value))
                )
            else:
                write_value = "$'''MANUAL_Fix'''"

            if "MANUAL_Fix" in write_value:
                self._add_manual_review(
                    action=action,
                    mapping=mapping,
                    reason=(
                        "Excel write value could not be translated"
                    ),
                    suggested_pad_action="Excel.WriteToExcel",
                    source_data={"Value": source_value},
                    required_work=(
                        "Set the value written to Excel manually."
                    ),
                )

            if parsed_range:
                self._add_line(
                    "Excel.WriteToExcel.WriteCell "
                    f"Column: $'''{parsed_range['start_column']}''' "
                    f"Instance: {instance_var} "
                    f"Row: {parsed_range['start_row']} "
                    f"Value: {write_value}"
                )
            else:
                self._add_line(
                    "Excel.WriteToExcel.Write "
                    f"Instance: {instance_var} "
                    f"Value: {write_value}"
                )

            return True

        # ----------------------------------------------------------
        # APPEND
        # ----------------------------------------------------------
        if action_type == "appendrange":
            source_value = (
                properties.get("DataTable")
                or expressions.get("DataTable")
                or ""
            )

            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "UiPath AppendRange has no single PAD equivalent; "
                    "the first free row must be located before writing"
                ),
                suggested_pad_action=(
                    "Excel.GetFirstFreeRowOnColumn then "
                    "Excel.WriteToExcel.WriteCell"
                ),
                source_data={
                    "DataTable": source_value,
                    "SheetName": sheet_name,
                },
                required_work=(
                    "Add Get first free row on column, then write the "
                    "data at that row. Verify the output variable name "
                    "of the first-free-row action in the designer."
                ),
            )

            if source_value:
                write_value = self._pad_set_value(
                    self._translate_expression(str(source_value))
                )
            else:
                write_value = "$'''MANUAL_Fix'''"

            self._add_line(
                "Excel.WriteToExcel.Write "
                f"Instance: {instance_var} "
                f"Value: {write_value}"
            )
            return True

        # ----------------------------------------------------------
        # SAVE
        # ----------------------------------------------------------
        if action_type in ("saveworkbook", "savedocument", "save"):
            if "Excel.SaveExcel.Save" in self.pad_schema_index:
                self._add_line(
                    f"Excel.SaveExcel.Save Instance: {instance_var}"
                )
                return True

        return False
            
    # ------------------------------------------------------------------
    # Standard action generators
    # ------------------------------------------------------------------

    def _generate_standard_action(self, action, mapping):
        """Generate a standard PAD action using schema skeleton.

        Returns:
            True if a real Robin action line was emitted, False otherwise.
        """
        target_action = mapping.get("target_action", "")
        display_name = action.get("display_name", "")

        schema_entry = self.lookup_schema(target_action)

        if not schema_entry:
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=f"PAD schema action '{target_action}' was not found",
                suggested_pad_action=target_action,
                required_work="Select the closest PAD action and configure it manually.",
            )
            return False

        template = schema_entry.get("RobinSyntaxTemplate", "")
        if not template:
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=f"PAD schema action '{target_action}' has no Robin syntax template",
                suggested_pad_action=target_action,
                required_work="Create and configure this PAD action manually.",
            )
            return False

        filled_line = self._fill_template(template, schema_entry, action, mapping)

        self._add_line(filled_line)
        return True
    
    def _generate_from_mapping_only(self, action, mapping):
        """Generate action from mapping data when no schema entry exists."""
        target_action = mapping.get("target_action", "")
        param_mapping = mapping.get("parameter_mapping", {})
        source_props = mapping.get("source_properties", {})
        source_exprs = mapping.get("source_expressions", {})

        parts = [target_action]
        for source_key, target_key in param_mapping.items():
            value = source_props.get(source_key, "") or source_exprs.get(source_key, "")
            if value:
                formatted = self._format_value(value)
                parts.append(f"{target_key}: {formatted}")
            else:
                parts.append(f"{target_key}: <<PLACEHOLDER_{target_key}>>")

        self._add_line(" ".join(parts))

    def _generate_set_variable(self, action, mapping):
        """Generate SET variable assignment in PAD's proven grammar."""
        properties = action.get("properties", {})
        expressions = action.get("expressions", {})

        var_name = (
            properties.get("To", "") or
            expressions.get("To", "") or
            properties.get("Result", "") or
            expressions.get("Result", "") or
            properties.get("reference_0", "") or
            expressions.get("reference_0", "")
        )
        value = (
            properties.get("Value", "") or
            expressions.get("Value", "") or
            expressions.get("expression_0", "") or
            "''"
        )

        var_name = self._clean_variable_name(var_name)
        if not var_name:
            var_name = "UnnamedVariable"

        value = self._translate_expression(value)
        if re.fullmatch(r'[A-Za-z_]\w*', value):
            value = f"%{value}%"

        # 'CatchException'/'SysException' are renamed source exception
        # OBJECTS, not real PAD variables. The designer cannot resolve
        # them during paste, which breaks the whole file.
        if value.strip("%") in ("CatchException", "SysException"):
            self._add_manual_review(
                action=action,
                mapping=mapping,
                reason=(
                    "Source assigned the caught .NET exception object; "
                    "PAD has no equivalent object assignment"
                ),
                suggested_pad_action=(
                    "Capture error details in the Block's "
                    "'On block error' configuration"
                ),
                source_data={
                    "TargetVariable": var_name,
                    "SourceObject": value.strip("%"),
                },
                required_work=(
                    "Use the block error settings to capture the error "
                    "message into a variable, then use that variable."
                ),
            )
            value = "''"

        if self._is_untranslatable(value) or self._has_forbidden_vb(value):
            original = (
                properties.get("Value", "")
                or expressions.get("Value", "")
                or expressions.get("expression_0", "")
            )

            # If the source value is a plain quoted literal, no manual
            # work is needed - use it directly.
            literal_match = re.fullmatch(
                r"\[?\s*[\"']([^\"']*)[\"']\s*\]?",
                str(original).strip(),
            )

            if literal_match:
                value = f"'{literal_match.group(1)}'"
            else:
                self._add_manual_review(
                    action=action,
                    mapping=mapping,
                    reason=(
                        "Assignment expression could not be translated "
                        "to PAD"
                    ),
                    suggested_pad_action="Set variable",
                    source_data={
                        "TargetVariable": var_name,
                        "RequiredValue": original,
                    },
                    required_work=(
                        f"Replace MANUAL_Fix with the PAD equivalent of: "
                        f"{original}"
                    ),
                )
                value = "'MANUAL_Fix'"
        self._ensure_variable(var_name)

        self._add_line(f"SET {var_name} TO {self._pad_set_value(value)}")
        
    def _generate_wait(self, action, mapping):
        """Generate WAIT action."""
        display_name = action.get("display_name", "")
        properties = action.get("properties", {})

        duration = properties.get("Duration", "")
        seconds = self._parse_duration_to_seconds(duration)

        self._add_comment(f"{display_name}")
        self._add_line(f"WAIT {seconds}")

    def _generate_subflow_call(self, action, mapping):
        """Generate a schema-backed subflow call (never a bare keyword)."""
        display_name = action.get("display_name", "")
        properties = action.get("properties", {})
        expressions = action.get("expressions", {})

        workflow_file = (
            properties.get("WorkflowFileName", "") or
            expressions.get("WorkflowFileName", "")
        )
        subflow_name = Path(workflow_file).stem if workflow_file else display_name
        subflow_name = self._clean_subflow_name(subflow_name)

        entry = self._find_flow_run_schema()

        if entry:
            template = entry.get("RobinSyntaxTemplate", "")
            if template:
                filled = template.replace(
                    "''", f"'{subflow_name}'", 1
                )
                self._add_line(filled)
                return

        self._add_manual_review(
            action=action,
            mapping=mapping,
            reason=(
                "No schema-backed PAD subflow call could be generated"
            ),
            suggested_pad_action="Run desktop flow / Run subflow",
            source_data={
                "SubflowName": subflow_name,
                "WorkflowFileName": workflow_file,
                "Inputs": [
                    name
                    for name, info in (
                        properties.get("InvokeArguments", {}) or {}
                    ).items()
                    if "InArgument" in info.get("direction", "")
                ],
                "Outputs": [
                    name
                    for name, info in (
                        properties.get("InvokeArguments", {}) or {}
                    ).items()
                    if "OutArgument" in info.get("direction", "")
                ],
                "InOuts": [
                    name
                    for name, info in (
                        properties.get("InvokeArguments", {}) or {}
                    ).items()
                    if "InOutArgument" in info.get("direction", "")
                ],
            },
            required_work=(
                f"Create a call to '{subflow_name}' and map all "
                "input/output/in-out arguments."
            ),
        )

    def _find_flow_run_schema(self):
        """Locate the schema action that runs another desktop flow."""
        for cand in ("Flow.RunDesktopFlow", "Flow.RunSubflow"):
            if cand in self.pad_schema_index:
                return self.pad_schema_index[cand]
        for aid, entry in self.pad_schema_index.items():
            if aid.lower().startswith("flow.") and "run" in aid.lower():
                return entry
        for entry in self.pad_schema:
            if (entry.get("DisplayName") or "").lower() == "run desktop flow":
                return entry
        return None

    def _generate_throw(self, action, mapping):
        """Generate THROW ERROR."""
        display_name = action.get("display_name", "")
        properties = action.get("properties", {})
        expressions = action.get("expressions", {})

        message = (
            properties.get("Exception", "") or
            expressions.get("Exception", "") or
            expressions.get("expression_0", "")
        )

        if message:
            message = self._translate_expression(message)
            unbalanced = message.count("(") != message.count(")")
            if (message.startswith("new ") or "Exception(" in message
                    or message in ("'Exception'", "Exception")
                    or "MANUAL_" in message
                    or unbalanced or not message.strip()):
                message = f"'{display_name}'"
        else:
            message = f"'{display_name}'"

        self._add_manual_review(
            action=action,
            mapping=mapping,
            reason="Throw/Rethrow has no direct Robin equivalent",
            suggested_pad_action="Throw error / Terminate flow",
            source_data={"ExceptionMessage": message},
            required_work=(
                "Create the PAD error action and preserve the original "
                "termination behavior."
            ),
        )
    def _generate_comment_action(self, action, mapping):
        """Generate a comment line (includes notes so unmapped actions stay visible)."""
        properties = action.get("properties", {})
        text = properties.get("Text", "") or action.get("display_name", "Comment")
        notes = mapping.get("notes", "")

        manual_markers = (
            "no pad equivalent",
            "orchestrator",
            "replace with",
            "manual",
            "unsupported",
            "queue",
            "transaction",
            "credential",
            "asset",
        )

        searchable = (
            f"{mapping.get('source_action', '')} {notes}"
        ).lower()

        # Ordinary UiPath Comment activities are intentionally omitted.
        if not any(m in searchable for m in manual_markers):
            return

        self._add_manual_review(
            action=action,
            mapping=mapping,
            reason=notes or "No direct PAD equivalent is available",
            required_work=(
                "Choose a replacement strategy (work queue, Excel, "
                "database, API) and build the PAD action."
            ),
        )

    def _generate_unmapped(self, action, mapping):
        """Generate placeholder for unmapped action."""
        source_action = mapping.get("source_action", "")
        display_name = action.get("display_name", "")
        notes = mapping.get("notes", "")
        confidence = mapping.get("confidence", "low")

        self._add_manual_review(
            action=action,
            mapping=mapping,
            reason="No supported PAD action mapping is available",
            details=f"Confidence={confidence}; Notes={notes}",
            required_work=(
                "Select and configure the closest PAD action manually."
            ),
        )
    
    def _emit_ui_context_comments(self, action):
        """Preserve the UiPath selector and browser source in the output.

        Lets the developer identify the target element and browser
        without opening the original XAML.
        """
        action_id = action.get("action_id")

        if not hasattr(self, "_ui_context_emitted"):
            self._ui_context_emitted = set()

        if action_id in self._ui_context_emitted:
            return

        self._ui_context_emitted.add(action_id)

        selector = action.get("selector")

        if selector:
            self._add_line("# UI ELEMENT CAPTURE REQUIRED")
            self._add_line(
                "# Original Selector: "
                + re.sub(r"\s+", " ", str(selector))[:250]
            )

        searchable = (
            str(action.get("_inherited_context") or "")
            + " "
            + str(action.get("target_app") or "")
        ).lower()

        browser_markers = (
            "openbrowser",
            "attachbrowser",
            "browser",
            "chrome",
            "msedge",
            "firefox",
            "iexplore",
        )

        if any(marker in searchable for marker in browser_markers):
            properties = action.get("properties", {}) or {}

            self._add_line("# BROWSER MAPPING REQUIRED")
            self._add_line(
                "# Source Activity: "
                + str(action.get("action_type") or "Browser scope")
            )
            self._add_line(
                "# Browser Type: "
                + str(
                    properties.get("BrowserType")
                    or action.get("target_app")
                    or "Unknown"
                )
            )

            url = (
                properties.get("Url")
                or properties.get("BrowserUrl")
                or ""
            )

            if url:
                self._add_line(
                    "# URL: "
                    + re.sub(r"\s+", " ", str(url))[:200]
                )
        
    def _derive_meaningful_variable_name(
        self,
        action,
        param_name,
    ):
        """Build a business-meaningful variable name for a parameter.

        Priority: UiPath DisplayName -> target parameter -> action id.
        A generic Tmp_* name is only produced when nothing usable exists.
        """
        display_name = str(
            action.get("display_name") or ""
        ).strip()

        # Words that describe the UiPath activity, not the business value.
        noise_words = {
            "type", "into", "click", "set", "get", "assign",
            "enter", "input", "write", "read", "select",
            "the", "a", "an", "to", "in", "on", "for", "of",
            "with", "and", "or", "value", "field", "text",
            "box", "button", "element", "ui", "web", "page",
            "window", "screen", "activity", "step", "action",
        }

        words = [
            word
            for word in re.split(r"[^A-Za-z0-9]+", display_name)
            if word and word.lower() not in noise_words
        ]

        if words:
            base = "".join(
                word[:1].upper() + word[1:]
                for word in words[:4]
            )
        else:
            base = ""

        if not base:
            # Fall back to the parameter's own name.
            base = str(param_name or "").strip()

        if not base:
            base = "Value"

        # Append the parameter role when it adds meaning.
        param_clean = re.sub(
            r"[^A-Za-z0-9]",
            "",
            str(param_name or ""),
        )

        if (
            param_clean
            and param_clean.lower() not in base.lower()
            and param_clean.lower() not in ("text", "value")
        ):
            base = f"{base}{param_clean}"

        candidate = re.sub(r"[^A-Za-z0-9_]", "", base)

        if not candidate or not candidate[0].isalpha():
            candidate = f"Var{candidate}"

        candidate = candidate[:60]

        # Guarantee uniqueness without losing readability.
        if candidate in self.generated_variables:
            action_id = re.sub(
                r"[^A-Za-z0-9_]",
                "_",
                str(action.get("action_id") or "x"),
            )
            candidate = f"{candidate}_{action_id}"

        return candidate
    
    # ------------------------------------------------------------------
    # Template filling
    # ------------------------------------------------------------------

    def _fill_template(self, template, schema_entry, action, mapping):
        """Fill a RobinSyntaxTemplate using PAD's proven parameter grammar."""
        inputs = schema_entry.get("Inputs", [])
        outputs = schema_entry.get("Outputs", [])
        param_mapping = mapping.get("parameter_mapping", {})
        source_props = action.get("properties", {})
        source_exprs = action.get("expressions", {})

        reverse_mapping = {}
        for src_key, tgt_key in param_mapping.items():
            reverse_mapping[tgt_key] = src_key

        action_name, template_params = self._parse_template(template)

        # Terminate/kill-process actions must never run with a default
        # ProcessId of 0 - that would target an invalid/system process.
        action_id_lower = str(
            schema_entry.get("ActionId") or ""
        ).lower()

        is_process_kill_action = (
            "terminateprocess" in action_id_lower
            or "killprocess" in action_id_lower
        )

        filled_parts = [action_name]
        for param_name, default_value in template_params:
            resolved_value = self._resolve_template_param(
                param_name=param_name,
                default_value=default_value,
                reverse_mapping=reverse_mapping,
                source_props=source_props,
                source_exprs=source_exprs,
                inputs=inputs,
            )

            # Excel child actions must reference the enclosing scope's
            # instance rather than an empty placeholder variable.
            if param_name.lower() == "instance":
                active_instance = getattr(
                    self,
                    "_active_excel_instance",
                    None,
                )

                if active_instance:
                    resolved_value = active_instance

            if (
                is_process_kill_action
                and param_name.lower() in ("processid", "processname")
                and str(resolved_value).strip("'\"$ ") in ("0", "")
            ):
                self._add_manual_review(
                    action=action,
                    mapping=mapping,
                    reason=(
                        "Process Name Required - the source did not "
                        "provide a resolvable process identifier"
                    ),
                    suggested_pad_action=schema_entry.get(
                        "ActionId", ""
                    ),
                    required_work=(
                        "Set the target process name/ID manually before "
                        "running this action."
                    ),
                )
                resolved_value = "$'''MANUAL_Fix'''"

            if isinstance(resolved_value, str):

                if "+" in resolved_value:
                    merged = self._pad_set_value(resolved_value)

                    if (
                        merged.startswith("$'''")
                        and "MANUAL_Fix" not in merged
                    ):
                        resolved_value = merged
                    else:
                        value_var = (
                            self._derive_meaningful_variable_name(
                                action,
                                param_name,
                            )
                        )

                        source_expression = (
                            action.get("properties", {}).get(param_name)
                            or action.get("expressions", {}).get(
                                param_name
                            )
                            or resolved_value
                        )

                        if "MANUAL_Fix" in merged:
                            hint = ""
                            expression_text = str(source_expression)

                            if ".Replace(" in expression_text:
                                hint = (
                                    " Add a Text.Replace action before "
                                    "this one and use its Result."
                                )
                            elif "String.Format(" in expression_text:
                                hint = (
                                    " Rebuild as interpolated PAD text "
                                    "using %Variable% placeholders."
                                )
                            elif re.search(
                                r"row\s*\(",
                                expression_text,
                                re.IGNORECASE,
                            ):
                                hint = (
                                    " Read the column value into a "
                                    "variable first."
                                )

                            self._add_manual_review(
                                action=action,
                                mapping=mapping,
                                reason=(
                                    f"Parameter '{param_name}' could not "
                                    "be translated to PAD"
                                ),
                                suggested_pad_action=schema_entry.get(
                                    "ActionId",
                                    "",
                                ),
                                source_data={
                                    "Variable": value_var,
                                    "OriginalExpression": (
                                        source_expression
                                    ),
                                },
                                required_work=(
                                    f"Set {value_var} to the PAD "
                                    f"equivalent of the original "
                                    f"expression.{hint}"
                                ),
                            )

                        compact_expression = re.sub(
                            r"\s+",
                            " ",
                            str(source_expression),
                        )[:200]

                        self._add_line(
                            f"# OriginalExpression: "
                            f"{compact_expression}"
                        )

                        self._add_line(
                            f"SET {value_var} TO {merged}"
                        )

                        self.generated_variables.add(value_var)
                        resolved_value = f"$'''%{value_var}%'''"
                else:
                    m = re.fullmatch(
                        r"%([A-Za-z_]\w*)%",
                        resolved_value,
                    )

                    if m:
                        resolved_value = f"$'''%{m.group(1)}%'''"
                    elif re.fullmatch(r"Var\w+", resolved_value):
                        param_lower = param_name.lower()

                        needs_context = any(
                            token in param_lower
                            for token in (
                                "element",
                                "field",
                                "control",
                                "window",
                                "browser",
                            )
                        )

                        if needs_context:
                            self._emit_ui_context_comments(action)

                        if resolved_value not in self.generated_variables:
                            self._add_line(
                                f"SET {resolved_value} TO ''"
                            )
                            self.generated_variables.add(resolved_value)

                # LLM fill for unresolved schema placeholders.
                if resolved_value.startswith("<<PLACEHOLDER"):
                    llm_val = self._llm_fill_param(
                        param_name,
                        schema_entry,
                        action,
                    )

                    if llm_val:
                        resolved_value = llm_val

                # A placeholder must never survive - it would make the
                # lint gate neutralize the whole action into a comment.
                if resolved_value.startswith("<<PLACEHOLDER"):
                    input_type = ""

                    for input_definition in inputs:
                        if input_definition.get("Name") == param_name:
                            input_type = str(
                                input_definition.get("Type") or ""
                            )
                            break

                    type_lower = input_type.lower()
                    name_lower = param_name.lower()

                    is_element = (
                        "element" in type_lower
                        or "element" in name_lower
                        or "control" in type_lower
                    )

                    if is_element:
                        self._emit_ui_context_comments(action)

                        element_var = re.sub(
                            r"[^A-Za-z0-9_]",
                            "_",
                            f"UIElement_{action.get('action_id', 'x')}",
                        )

                        if element_var not in self.generated_variables:
                            self._add_line(
                                f"SET {element_var} TO ''"
                            )
                            self.generated_variables.add(element_var)

                        self._add_manual_review(
                            action=action,
                            mapping=mapping,
                            reason=(
                                "UiPath selectors cannot be converted to "
                                "PAD UI elements"
                            ),
                            suggested_pad_action=schema_entry.get(
                                "ActionId",
                                "",
                            ),
                            source_data={
                                "Parameter": param_name,
                                "UiPathSelector": action.get("selector"),
                                "ElementVariable": element_var,
                            },
                            required_work=(
                                "Capture the target UI element in the PAD "
                                "designer and attach it to this action."
                            ),
                        )

                        resolved_value = element_var

                    elif "bool" in type_lower:
                        resolved_value = "False"
                    elif (
                        "int" in type_lower
                        or "double" in type_lower
                        or "decimal" in type_lower
                    ):
                        resolved_value = "0"
                    else:
                        resolved_value = "''"

                # Value parameters are exposed as a named variable so the
                # developer sets the value in one place, exactly as UiPath
                # assigned it before the activity.
                is_value_param = param_name.lower() in (
                    "text",
                    "texttowrite",
                    "textvalue",
                    "value",
                    "inputtext",
                    "content",
                )

                already_variable = bool(
                    re.fullmatch(
                        r"\$'''%[A-Za-z_]\w*%'''",
                        resolved_value,
                    )
                )

                needs_named_variable = (
                    is_value_param
                    and not already_variable
                    and resolved_value not in ("''", "")
                )

                if needs_named_variable:
                    value_var = (
                        self._derive_meaningful_variable_name(
                            action,
                            param_name,
                        )
                    )

                    source_expression = (
                        action.get("properties", {}).get(param_name)
                        or action.get("expressions", {}).get(param_name)
                        or action.get("properties", {}).get("Text")
                        or action.get("expressions", {}).get("Text")
                        or ""
                    )

                    if source_expression:
                        compact_expression = re.sub(
                            r"\s+",
                            " ",
                            str(source_expression),
                        )[:200]

                        self._add_line(
                            f"# OriginalExpression: "
                            f"{compact_expression}"
                        )

                    assignment_value = self._pad_set_value(
                        resolved_value
                    )

                    self._add_line(
                        f"SET {value_var} TO {assignment_value}"
                    )

                    self.generated_variables.add(value_var)
                    resolved_value = f"$'''%{value_var}%'''"

            # PAD designer emits text as $'''...''' - match it for non-empty strings
            if isinstance(resolved_value, str) and len(resolved_value) >= 2 \
                    and resolved_value.startswith("'") and resolved_value.endswith("'") \
                    and not resolved_value.startswith("$"):
                inner = resolved_value[1:-1]
                if inner:
                    resolved_value = "$'''" + inner + "'''"

            # Final per-parameter gates (issues: VB leakage, key tokens,
            # concatenation inside $'''...''').
            if isinstance(resolved_value, str):
                # Normalize any surviving [k(enter)]/[k(%enter%)] tokens.
                resolved_value = self._normalize_key_tokens(resolved_value)

                # Mixed text + variables must be interpolated, never bare.
                if (
                    "%" in resolved_value
                    and not resolved_value.startswith("$'''")
                    and not re.fullmatch(r"%[A-Za-z_]\w*%", resolved_value)
                    and not re.fullmatch(r"[A-Za-z_.]+", resolved_value)
                ):
                    resolved_value = self._pad_set_value(resolved_value)
                    if re.fullmatch(r"[A-Za-z_]\w*", resolved_value):
                        resolved_value = f"$'''%{resolved_value}%'''"
                    elif not resolved_value.startswith("$"):
                        resolved_value = "$'''" + resolved_value.strip("'") + "'''"

                # Concatenation leaked inside an interpolated literal:
                # $'''<td>' + %x% + '</td>''' -> re-merge through
                # _pad_set_value.
                if (
                    resolved_value.startswith("$'''")
                    and "' + " in resolved_value or " + '" in resolved_value
                ) and resolved_value.startswith("$'''"):
                    remerged = self._pad_set_value(
                        resolved_value[4:-3]
                    )
                    if remerged.startswith("$'''"):
                        resolved_value = remerged

                # Hard gate: any remaining VB leakage in this parameter
                # makes the whole action unpastable - use MANUAL_Fix and
                # record the original for the developer.
                if self._has_forbidden_vb(resolved_value):
                    self._add_manual_review(
                        action=action,
                        mapping=mapping,
                        reason=(
                            f"Parameter '{param_name}' contains "
                            "untranslated .NET syntax"
                        ),
                        suggested_pad_action=schema_entry.get(
                            "ActionId", ""
                        ),
                        source_data={
                            "Parameter": param_name,
                            "OriginalValue": resolved_value,
                        },
                        required_work=(
                            "Rebuild this value with PAD text actions "
                            "and set it on the generated action."
                        ),
                    )
                    resolved_value = "$'''MANUAL_Fix'''"

            filled_parts.append(f"{param_name}: {resolved_value}")

        for output in outputs:
            out_name = output.get("Name", "")
            if isinstance(out_name, dict):
                out_name = out_name.get("Name", "")
            if out_name:
                if not any(p[0] == out_name for p in template_params):
                    var_name = self._resolve_output_variable(out_name, action, reverse_mapping)
                    self._ensure_variable(var_name)
                    filled_parts.append(f"{out_name}=> {var_name}")

        return " ".join(filled_parts)
    
    def _llm_fill_param(self, param_name, schema_entry, action):
        """Fill a schema placeholder via LLM when IR has no value for it."""
        try:
            client = get_llm_client()
            result = client.infer_parameters(
                schema_entry.get("ActionId", ""),
                schema_entry.get("RobinSyntaxTemplate", ""),
                action,
            )
            val = result.get(param_name)
            if val and not str(val).startswith("<<"):
                return self._format_value(str(val))
        except Exception as e:
            logger.debug(f"LLM param fill failed for {param_name}: {e}")
        return None

    def _parse_template(self, template):
        """Parse RobinSyntaxTemplate into action name and parameter list."""
        parts = template.strip().split()

        if not parts:
            return "", []

        action_name = parts[0]
        params = []

        i = 1
        while i < len(parts):
            token = parts[i]

            if token.endswith(":"):
                param_name = token[:-1]
                if i + 1 < len(parts):
                    value = parts[i + 1]
                    if value.endswith(":"):
                        params.append((param_name, "''"))
                    else:
                        params.append((param_name, value))
                        i += 1
                else:
                    params.append((param_name, "''"))

            elif "=>" in token:
                if i + 1 < len(parts):
                    i += 1
            else:
                if ":" in token and not token.startswith("'"):
                    colon_idx = token.index(":")
                    param_name = token[:colon_idx]
                    value = token[colon_idx + 1:] if colon_idx + 1 < len(token) else "''"
                    params.append((param_name, value))

            i += 1

        return action_name, params

    def _resolve_template_param(self, param_name, default_value, reverse_mapping,
                                 source_props, source_exprs, inputs):
        """Resolve the value for a single template parameter."""
        source_key = reverse_mapping.get(param_name)
        if source_key:
            value = source_props.get(source_key) or source_exprs.get(source_key)
            if value:
                return self._format_value(value)

        value = source_props.get(param_name)
        if value:
            return self._format_value(value)

        param_lower = param_name.lower()
        for key, val in source_props.items():
            if key.lower() == param_lower:
                return self._format_value(val)

        value = source_exprs.get(param_name)
        if value:
            return self._translate_expression(value)

        for key, val in source_exprs.items():
            if key.lower() == param_lower:
                return self._translate_expression(val)

        if default_value and not self._is_placeholder_default(default_value):
            return default_value

        for inp in inputs:
            if inp.get("Name") == param_name:
                return self._get_schema_default(inp, default_value)

        if default_value:
            return default_value

        return f"<<PLACEHOLDER_{param_name}>>"

    def _resolve_output_variable(self, output_name, action, reverse_mapping):
        """Resolve output variable name."""
        source_key = reverse_mapping.get(output_name)
        if source_key:
            props = action.get("properties", {})
            value = props.get(source_key, "")
            if value:
                return self._clean_variable_name(value)
        return output_name

    # ------------------------------------------------------------------
    # Expression and value helpers
    # ------------------------------------------------------------------

    def _translate_expression(self, expression):
        """Translate a UiPath VB.NET expression to PAD Robin syntax.

        Pipeline: protect strings -> VB operators -> function rewrites ->
        Config() vars -> wrap identifiers -> restore strings.
        """
        if not expression or not isinstance(expression, str):
            return "''"

        expr = expression.strip()

        # Strip VB expression brackets
        if expr.startswith("[") and expr.endswith("]"):
            expr = expr[1:-1].strip()

        if not expr:
            return "''"

        # ---- 1. Protect quoted string literals so words inside are never touched
        stashed = []

        def _stash(match):
            stashed.append(match.group(1))
            return f"\x00{len(stashed) - 1}\x00"

        # Normalize and protect keyboard tokens FIRST so key names are
        # never wrapped as %variables% by the identifier pass.
        expr = self._normalize_key_tokens(expr)

        expr = self._normalize_key_tokens(expr)

        expr = re.sub(r'"([^"]*)"', _stash, expr)
        expr = re.sub(r"'([^']*)'", _stash, expr)

        expr = re.sub(r'"([^"]*)"', _stash, expr)
        expr = re.sub(r"'([^']*)'", _stash, expr)

        # ---- 2. VB operators and keywords
        expr = re.sub(r'\bisNot\b', '<>', expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bNothing\b', '""', expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bIs\b', '=', expr)
        expr = re.sub(r'\bis\b', '=', expr)
        expr = re.sub(r'\bAndAlso\b', 'AND', expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bOrElse\b', 'OR', expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bAnd\b', 'AND', expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bOr\b', 'OR', expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bNot\b', 'NOT', expr)

        # ---- 3. Common function rewrites
        expr = re.sub(r'NOT\s*String\.IsNullOrWhiteSpace\(([^()]*)\)', r'(\1 <> "")', expr, flags=re.IGNORECASE)
        expr = re.sub(r'String\.IsNullOrWhiteSpace\(([^()]*)\)', r'(\1 = "")', expr, flags=re.IGNORECASE)
        
        # Placeholder helper (stashed so values are never %wrapped%)
        def _stash_literal(text):
            stashed.append(text)
            return f"\x00{len(stashed) - 1}\x00"

        # String.IsNullOrEmpty / IsNullOrWhiteSpace -> PAD comparisons
        expr = re.sub(r'NOT\s+String\.IsNullOrEmpty\(([^()]*)\)', r'(\1 <> "")', expr, flags=re.IGNORECASE)
        expr = re.sub(r'String\.IsNullOrEmpty\(([^()]*)\)', r'(\1 = "")', expr, flags=re.IGNORECASE)
        expr = re.sub(r'NOT\s+String\.IsNullOrWhiteSpace\(([^()]*)\)', r'(\1 <> "")', expr, flags=re.IGNORECASE)
        expr = re.sub(r'String\.IsNullOrWhiteSpace\(([^()]*)\)', r'(\1 = "")', expr, flags=re.IGNORECASE)

        # Now.ToString("format") WITH format arg FIRST, then bare forms
        expr = re.sub(r'\bnow\.ToString\(\s*(?:\x00\d+\x00|"[^"]*")\s*\)', lambda m: _stash_literal("MANUAL_CurrentDateTime"), expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bnow\.ToString\b', lambda m: _stash_literal("MANUAL_CurrentDateTime"), expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bnow\b', lambda m: _stash_literal("MANUAL_CurrentDateTime"), expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bstring\.Empty\b', lambda m: _stash_literal(""), expr, flags=re.IGNORECASE)

        # Path.Combine(a, b) -> (a + '\' + b)
        expr = re.sub(r'Path\.Combine\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)', r"(\1 + '\\' + \2)", expr, flags=re.IGNORECASE)

        # New X(...) object constructors -> placeholder
        expr = re.sub(r'\bNew\s+\w+(?:\.\w+)?\((?:[^()]|\([^()]*\))*\)', lambda m: _stash_literal("MANUAL_Object"), expr, flags=re.IGNORECASE)
        # Config("Key") -> %Config_Key%   (MUST run before wrapper stripping,
        # because Cint(Config(...)) has nested parentheses)
        def _config_var(match):
            idx = int(match.group(1))
            key = re.sub(r'[^A-Za-z0-9_]', '_', stashed[idx])
            return f"%Config_{key}%"

        expr = re.sub(r'\b(?:in_|out_|io_)?Config\(\s*\x00(\d+)\x00\s*\)(?:\.ToString)?', _config_var, expr, flags=re.IGNORECASE)
        # Strip type-conversion wrappers (repeat for nesting)
        for _ in range(6):
            new_expr = re.sub(
                r'\b(?:Cint|CStr|CDbl|CType|Convert\.ToBoolean|Convert\.ToInt32|Convert\.ToString|Integer\.Parse)\(([^()]*)\)',
                r'\1', expr, flags=re.IGNORECASE)
            if new_expr == expr:
                break
            expr = new_expr

       
        # new Exception(...) -> placeholder (handles one level of nested parens)
        expr = re.sub(r'new\s+\w*Exception\((?:[^()]|\([^()]*\))*\)', "'Exception'", expr, flags=re.IGNORECASE)

        # Property accessors with no PAD equivalent
        expr = re.sub(r'\.Message\b', '_Message', expr)
        expr = re.sub(r'\.Source\b', '_Source', expr)
        expr = re.sub(r'\.ToString\b', '', expr, flags=re.IGNORECASE)
        expr = re.sub(r'\.Trim\b', '', expr, flags=re.IGNORECASE)
        
        # Normalize spacing around comparison operators (PAD is strict)
        expr = re.sub(r'\s*(>=|<=|<>|>|<|=)\s*', r' \1 ', expr)
        expr = re.sub(r'  +', ' ', expr)
        # Normalize spacing around + (concatenation) as well
        expr = re.sub(r'\s*\+\s*', ' + ', expr)

        # ---- 4. Wrap bare identifiers as %var%
        keywords = {
            "true", "false", "and", "or", "not", "mod", "new", "nothing",
            "null", "then", "else", "if", "end", "string", "integer",
            "boolean", "double", "decimal", "object", "datetime", "convert",
            "math", "text", "screen", "to",
        }

        def _wrap(match):
            word = match.group(1)

            if word.lower() in keywords:
                return word

            # Excel cell references (H35, AB12) are navigation literals,
            # never variables. Wrapping them creates phantom %H35%
            # references and forces an empty SET H35 declaration.
            if re.fullmatch(r"[A-Z]{1,3}\d{1,7}", word):
                return word

            return f"%{self._safe_var(word)}%"

        expr = re.sub(r'(?<![.\w%\x00])([A-Za-z_]\w*)(?![.\w(])', _wrap, expr)

        # ---- 5. Restore stashed strings
        expr = re.sub(r'\x00(\d+)\x00', lambda m: f"'{stashed[int(m.group(1))]}'", expr)

        # Screen resolution has no PAD equivalent (after wrapping, so the
        # placeholder text is not turned into %variables%)
        expr = re.sub(r'Screen\.PrimaryScreen\.Bounds\.(Width|Height)', r"'MANUAL_Screen_\1'", expr)

        # Clean leftovers
        expr = re.sub(r'\(\s*\)', '', expr)

        # Robin text literals use single quotes - never emit double quotes
        expr = expr.replace('""', "''")

        # Final key-token normalization (covers tokens restored from
        # stashed strings, e.g. "...[k(enter)]" inside quoted literals).
        expr = self._normalize_key_tokens(expr)

        return expr.strip() if expr.strip() else "''"
    
    def _pad_set_value(self, value):
        """PAD's proven SET grammar: literals as-is; single %Var% -> bare name;
        expressions -> $'''...''' with %x% inside."""
        v = (value or "").strip()

        m = re.fullmatch(r"%([A-Za-z_]\w*)%", v)
        if m:
            return m.group(1)

        # Non-empty SINGLE quoted literal -> designer-style $'''...'''
        # Must be exactly one 'literal' - a concatenation like
        # 'a' + %x% + 'b' also starts/ends with a quote and must NOT
        # take this branch.
        if re.fullmatch(r"'[^']*'", v):
            inner = v[1:-1]
            if inner:
                return "$'''" + inner + "'''"
            return v

        if "%" not in v and "+" not in v:
            return v

        parts = re.split(r"\s*\+\s*", v)
        has_literal = any(
            re.fullmatch(r"'[^']*'|\"[^\"]*\"", p.strip()) for p in parts
        )

        if has_literal:
            out = []
            leaked = False
            for p in parts:
                p = p.strip()
                if re.fullmatch(r"'[^']*'|\"[^\"]*\"", p):
                    out.append(p[1:-1])
                elif re.fullmatch(r"%[A-Za-z_]\w*%", p):
                    out.append(p)
                elif re.fullmatch(r"[A-Z]{1,3}\d{1,7}", p):
                    # Excel cell reference - literal text, not a variable.
                    out.append(p)
                elif re.fullmatch(r"[A-Za-z_]\w*", p):
                    out.append(f"%{p}%")
                elif p:
                    # Unresolved VB (.Replace, calls, member access).
                    # Merging it would leak raw VB into the literal.
                    leaked = True
                    break

            if leaked or self._has_forbidden_vb("".join(out)):
                return "$'''MANUAL_Fix'''"

            # Join with no separator - literals carry their own spacing
            # ('<td>' + %x% must become <td>%x%, not <td> %x%).
            inner = "".join(out)
        else:
            numeric_parts = [p.strip() for p in parts if p.strip()]

            # Pure variable/number arithmetic must remain a numeric
            # expression. The PAD parser on this machine requires BARE
            # variable names in SET arithmetic (confirmed by DLL test:
            # %var% + 1 -> syntax error at the % character; var + 1 -> valid).
            if numeric_parts and all(
                re.fullmatch(r"%[A-Za-z_]\w*%|\d+(?:\.\d+)?", p)
                for p in numeric_parts
            ):
                bare_parts = [
                    p.strip("%") if p.startswith("%") else p
                    for p in numeric_parts
                ]
                return " + ".join(bare_parts)

            inner = "+".join(numeric_parts)

        return "$'''" + inner + "'''"
    
    
    def _format_value(self, value):
        """Format a value for Robin script parameter."""
        if not value:
            return "''"

        if not isinstance(value, str):
            return str(value)

        value = value.strip()

        # Already quoted
        if (value.startswith("'") and value.endswith("'")) or \
           (value.startswith('"') and value.endswith('"')):
            return f"'{value[1:-1]}'"

        # Number
        try:
            float(value)
            return value
        except ValueError:
            pass

        # Boolean
        if value.lower() in ("true", "false"):
            return value.capitalize()

        # Enum value (contains dots like File.TextFileEncoding.UTF8)
        if "." in value and " " not in value and not value.startswith("%"):
            return value

        # Variable reference
        if value.startswith("%") and value.endswith("%"):
            return value

        # A bare identifier is only a variable reference when the source
        # actually declared it. UiPath literals like
        # "HX_POST_VERIFICATION_BU_BPS" or Excel cell refs like "H35"
        # must stay literal text, not become phantom %variables%.
        if re.match(r'^[A-Za-z_]\w*$', value):
            known_variable = (
                value in self.generated_variables
                or self._safe_var(value) in self.generated_variables
            )

            # Excel-style cell references (H35, AB12) are always literals.
            is_cell_reference = bool(
                re.fullmatch(r"[A-Z]{1,3}\d{1,7}", value)
            )

            if known_variable and not is_cell_reference:
                return f"%{value}%"

            return f"'{value}'"

        # Expression
        if any(c in value for c in ('+', '&', '(', ')', '.')):
            return self._translate_expression(value)

        # VB expression brackets: ["..."] or [expr]
        if value.startswith("[") and value.endswith("]"):
            return self._translate_expression(value[1:-1])

        # Default: wrap as string
        return f"'{value}'"

    def _resolve_condition(self, action, mapping):
        """Resolve IF/WHILE condition from action data.

        If the VB condition has no Robin equivalent (e.g. Directory.Exists),
        emit IF True with a MANUAL FIX comment so the paste stays valid.
        """
        properties = action.get("properties", {})
        expressions = action.get("expressions", {})

        condition = (
            properties.get("Condition", "") or
            expressions.get("Condition", "") or
            properties.get("condition", "") or
            expressions.get("expression_0", "")
        )

        if condition:
            translated = self._translate_expression(condition)
            # Still contains .NET calls / placeholders -> not valid Robin
            if self._is_untranslatable(translated):
                self._add_manual_review(
                    action=action,
                    mapping=mapping,
                    reason=(
                        "Condition could not be translated; temporary "
                        "True inserted"
                    ),
                    source_data={"OriginalCondition": condition},
                    required_work=(
                        "Recreate this condition in PAD syntax and "
                        "replace True."
                    ),
                )
                return "True"
            # Strip redundant outer parentheses (PAD paste is strict)
            while len(translated) > 2 and translated.startswith("(") and translated.endswith(")"):
                inner = translated[1:-1]
                if inner.count("(") == inner.count(")"):
                    translated = inner.strip()
                else:
                    break
            # Bare %var% used as boolean -> explicit comparison,
            # but NEVER touch variables that are operands of >, <, =
            def _bool_var(m):
                before = translated[:m.start()].rstrip()
                after = translated[m.end():].lstrip()
                if before.endswith(('>', '<', '=')) or after.startswith(('>', '<', '=')):
                    return m.group(0)
                return f"{m.group(0)} = True"
            translated = re.sub(r'%[A-Za-z_]\w*%', _bool_var, translated)
            return translated

        # No condition found in the source action - never emit a
        # placeholder into an executable IF.
        self._add_manual_review(
            action=action,
            mapping=mapping,
            reason="Condition Required - no condition could be extracted from the source activity",
            suggested_pad_action="PAD If condition",
            required_work=(
                "Determine the intended condition from the source XAML "
                "and replace the temporary True condition."
            ),
        )
        return "True"
    
    def _resolve_switch_value(self, action, mapping):
        """Resolve SWITCH value expression."""
        properties = action.get("properties", {})
        expressions = action.get("expressions", {})

        value = (
            properties.get("Expression", "") or
            expressions.get("Expression", "") or
            properties.get("Value", "") or
            expressions.get("expression_0", "")
        )

        if value:
            return self._translate_expression(value)

        self._add_manual_review(
            action=action,
            mapping=mapping,
            reason="Switch value required - none could be extracted from the source activity",
            suggested_pad_action="PAD Switch",
            required_work="Set the switch expression from the source XAML.",
        )
        return "''"

    def _resolve_param(self, action, mapping, target_param, fallback):
        """Resolve a single parameter value from action and mapping."""
        param_mapping = mapping.get("parameter_mapping", {})
        properties = action.get("properties", {})
        expressions = action.get("expressions", {})

        for src_key, tgt_key in param_mapping.items():
            if tgt_key == target_param:
                value = properties.get(src_key) or expressions.get(src_key)
                if value:
                    return self._clean_variable_name(value) if "Item" in target_param or "Name" in target_param else value

        value = properties.get(target_param) or expressions.get(target_param)
        if value:
            return value

        return fallback

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    @staticmethod
    def _is_placeholder_default(value):
        """Check if a template default is a placeholder."""
        if not value:
            return True
        if value == "''":
            return True
        if value.startswith("Var"):
            return True
        return False

    @staticmethod
    def _get_schema_default(input_def, template_default):
        """Get an appropriate default value based on schema input type."""
        input_type = input_def.get("Type", "String")
        enum_values = input_def.get("EnumValues")

        if enum_values:
            return template_default if template_default else enum_values[0]

        type_defaults = {
            "String": "''",
            "Boolean": "False",
            "Int32": "0",
            "Int64": "0",
            "Double": "0.0",
        }

        if input_type in type_defaults:
            return type_defaults[input_type]

        if template_default:
            return template_default

        return "''"
    
    @staticmethod
    def _is_untranslatable(text):
        """True if a translated expression still contains .NET constructs
        (function calls, member access, GetType, bare placeholders)."""
        # Check MANUAL_ markers BEFORE stripping quotes - a quoted
        # 'MANUAL_CurrentDateTime' literal is still untranslatable.
        if "MANUAL_" in (text or ""):
            return True
        stripped = re.sub(r"'[^']*'", "", text or "")
        return bool(re.search(r"\bGetType\b|\w+\(|\w+\.\w+", stripped))
    
    # Patterns that must never appear in emitted Robin values.
    FORBIDDEN_VB_PATTERNS = (
        r"\.Replace\s*\(",
        r"\.Substring\s*\(",
        r"\.Split\s*\(",
        r"\.Trim\s*\(",
        r"\.ToUpper\b",
        r"\.ToLower\b",
        r"String\.Format\s*\(",
        r"String\.Join\s*\(",
        r"Environment\.NewLine",
        r"DateTime\.",
        r"CurrentDateTime\.",
        r"vbCrLf|vbLf|vbTab",
        r"row\s*\(\s*\"",
        r"\.Rows\s*\(",
        r"\.Item\s*\(",
        r"'\s*\+\s*|\s*\+\s*'",
        r"\[k\(%[^%]+%\)\]",
        # Untranslatable placeholders that must never be executable:
        r"MANUAL_CurrentDateTime",
        r"MANUAL_Object",
        r"MANUAL_Screen_",
        r"\.DayOfWeek\b",
        r"\.AddDays\b|\.AddMonths\b|\.AddYears\b",
        r"<<PLACEHOLDER_[^>]*>>",
    )

    @classmethod
    def _has_forbidden_vb(cls, text):
        """True if the value still contains VB/.NET leakage."""
        if not text:
            return False
        return any(
            re.search(p, text, flags=re.IGNORECASE)
            for p in cls.FORBIDDEN_VB_PATTERNS
        )
        
    _KEY_TOKEN_MAP = {
        "enter": "ENTER", "tab": "TAB", "space": "SPACE",
        "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
        "esc": "ESC", "escape": "ESC", "backspace": "BACKSPACE",
        "delete": "DELETE", "del": "DELETE", "home": "HOME",
        "end": "END", "pageup": "PAGEUP", "pagedown": "PAGEDOWN",
        "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4", "f5": "F5",
        "f6": "F6", "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10",
        "f11": "F11", "f12": "F12",
    }

    @classmethod
    def _normalize_key_tokens(cls, text):
        """Normalize [k(enter)] to [k(ENTER)] and repair [k(%enter%)]."""
        if not text or "[k(" not in text.lower():
            return text

        def fix(match):
            key = match.group(1).strip().strip("%").lower()
            mapped = cls._KEY_TOKEN_MAP.get(key)
            if mapped:
                return f"[k({mapped})]"
            return f"[k({match.group(1).strip().strip('%')})]"

        return re.sub(
            r"\[\s*k\s*\(\s*([^)\]]+?)\s*\)\s*\]",
            fix,
            text,
            flags=re.IGNORECASE,
        )

    @staticmethod
    def _get_default_for_type(var_type, existing_default=""):
        """Get default value for variable declaration based on type."""
        if existing_default:
            return existing_default

        defaults = {
            "Text": "''",
            "Number": "0",
            "Boolean": "False",
            "DateTime": "''",
            "TimeSpan": "0",
            "DataTable": "''",
            "List": "[]",
            "Dictionary": "{}",
            "General": "''",
            "Error": "''",
            "SecureText": "''",
        }
        return defaults.get(var_type, "''")

    @staticmethod
    def _safe_var(name):
        """Rename variables that collide with PAD reserved error-context names
        and canonicalize their case so references always match declarations."""
        lower = (name or "").lower()
        if lower == "exception":
            return "CatchException"
        if lower.startswith("exception_"):
            return "CatchException" + name[len("exception"):]
        if lower == "systemexception":
            return "SysException"
        if lower.startswith("systemexception_"):
            return "SysException" + name[len("systemexception"):]
        return name
    
    @staticmethod
    def _clean_variable_name(name):
        """Clean a variable name for Robin script."""
        if not name:
            return ""
        name = name.strip().strip("%").strip()
        # Strip VB brackets
        if name.startswith("[") and name.endswith("]"):
            name = name[1:-1].strip()
        # Remove quotes
        name = name.strip("'").strip('"')
        # Replace invalid chars
        name = re.sub(r'[^A-Za-z0-9_]', '_', name)
        # Collapse multiple underscores and strip edges (PAD naming safety)
        name = re.sub(r'_+', '_', name).strip('_')
        # Ensure starts with letter
        if name and not name[0].isalpha():
            name = f"Var_{name}"
        # Never collide with PAD reserved error-context names
        return PADScriptGenerator._safe_var(name)

    @staticmethod
    def _clean_subflow_name(name):
        """Clean a subflow name for Robin CALL statement."""
        if not name:
            return "UnnamedSubflow"
        name = re.sub(r'[^A-Za-z0-9_]', '_', name)
        if name and not name[0].isalpha():
            name = f"Sub_{name}"
        return name

    @staticmethod
    def _parse_duration_to_seconds(duration):
        """Parse a .NET TimeSpan string to seconds."""
        if not duration:
            return "1"

        match = re.match(r'(\d+):(\d+):(\d+)', str(duration))
        if match:
            hours, minutes, seconds = int(match.group(1)), int(match.group(2)), int(match.group(3))
            total = hours * 3600 + minutes * 60 + seconds
            return str(total) if total > 0 else "1"

        try:
            val = int(float(str(duration)))
            return str(val) if val > 0 else "1"
        except (ValueError, TypeError):
            pass

        return "1"

    def _var_ref(self, name):
        """Variable reference in the grammar currently being validated."""
        g = self.var_grammar
        if g == "pct":
            return f"%{name}%"
        if g == "interp":
            return f"$'''%{name}%'''"
        return name

    def _ensure_variable(self, var_name):
        """Track a variable as generated."""
        if var_name:
            clean = self._clean_variable_name(var_name)
            self.generated_variables.add(clean)

    # ------------------------------------------------------------------
    # Script line management
    # ------------------------------------------------------------------

    def _add_line(self, line):
        """Add a line to the script with proper indentation."""
        if line == "":
            self.script_lines.append("")
        else:
            indent = self.INDENT * self.indent_level
            self.script_lines.append(f"{indent}{line}")

    def _add_comment(self, text):
        """Suppress ordinary informational comments.

        Comments required for incomplete migration must be emitted through
        _add_manual_review().
        """
        return

    @staticmethod
    def _compact_manual_value(value, max_length=350):
        """Convert source information into safe single-line text."""
        if value is None:
            return ""

        if isinstance(value, (dict, list, tuple, set)):
            try:
                text = json.dumps(
                    value,
                    ensure_ascii=False,
                    default=str,
                )
            except Exception:
                text = str(value)
        else:
            text = str(value)

        text = re.sub(r"\s+", " ", text).strip()
        text = text.replace("|", "/")

        if len(text) > max_length:
            text = text[:max_length] + "...[truncated]"

        return text

    def _add_manual_review(
        self,
        action,
        reason,
        details=None,
        mapping=None,
        suggested_pad_action=None,
        required_work=None,
        source_data=None,
        issue_key=None,
    ):
        """Emit one structured manual-review comment per source action."""
        action = action or {}
        mapping = mapping or {}

        action_id = self._compact_manual_value(
            action.get("action_id")
            or f"manual_{len(self._manual_review_comments) + 1}"
        )

        action_type = self._compact_manual_value(
            action.get("action_type")
            or mapping.get("source_action")
            or "UnknownAction"
        )

        display_name = self._compact_manual_value(
            action.get("display_name")
            or action_type
        )

        suggested_target = self._compact_manual_value(
            suggested_pad_action
            or mapping.get("target_action")
            or "Manual PAD action selection required"
        )

        reason_text = self._compact_manual_value(
            reason or "Manual implementation required"
        )

        details_text = self._compact_manual_value(details)
        required_work_text = self._compact_manual_value(required_work)

        if source_data is None:
            source_data = {}

            properties = action.get("properties", {}) or {}
            expressions = action.get("expressions", {}) or {}
            selector = action.get("selector")

            relevant_keys = (
                "Text",
                "Value",
                "Condition",
                "Expression",
                "WorkflowFileName",
                "FileName",
                "FilePath",
                "Path",
                "Url",
                "To",
                "From",
                "Result",
                "Exception",
                "Message",
                "SheetName",
                "Range",
            )

            for key in relevant_keys:
                if key in properties and properties[key] not in ("", None):
                    source_data[key] = properties[key]
                elif key in expressions and expressions[key] not in ("", None):
                    source_data[key] = expressions[key]

            if selector:
                source_data["UiPathSelector"] = selector

        source_data_text = self._compact_manual_value(source_data)

        issue_parts = [reason_text]

        if details_text:
            issue_parts.append(details_text)

        if required_work_text:
            issue_parts.append(
                f"RequiredWork={required_work_text}"
            )

        issue_text = "; ".join(
            part for part in issue_parts if part
        )

        existing = self._manual_review_comments.get(action_id)

        if existing:
            if issue_text not in existing["issues"]:
                existing["issues"].append(issue_text)

            combined_issues = "; ".join(existing["issues"])

            parts = [
                "[MANUAL REVIEW]",
                f"SourceId={action_id}",
                f"UiPathAction={action_type}",
                f"Name={display_name}",
                f"SuggestedPAD={suggested_target}",
                f"Reason={combined_issues}",
            ]

            if source_data_text:
                parts.append(f"SourceData={source_data_text}")

            line_index = existing["line_index"]

            if 0 <= line_index < len(self.script_lines):
                indent = self.INDENT * existing["indent_level"]
                self.script_lines[line_index] = (
                    f"{indent}# " + " | ".join(parts)
                )
            else:
                indent = self.INDENT * self.indent_level
                new_index = len(self.script_lines)
                self.script_lines.append(
                    f"{indent}# " + " | ".join(parts)
                )
                existing["line_index"] = new_index
                existing["indent_level"] = self.indent_level

            return

        parts = [
            "[MANUAL REVIEW]",
            f"SourceId={action_id}",
            f"UiPathAction={action_type}",
            f"Name={display_name}",
            f"SuggestedPAD={suggested_target}",
            f"Reason={issue_text}",
        ]

        if source_data_text:
            parts.append(f"SourceData={source_data_text}")

        indent = self.INDENT * self.indent_level
        line_index = len(self.script_lines)

        self.script_lines.append(
            f"{indent}# " + " | ".join(parts)
        )

        self._manual_review_comments[action_id] = {
            "line_index": line_index,
            "indent_level": self.indent_level,
            "issues": [issue_text],
        }

    LINT_STRUCTURAL_RE = re.compile(
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
        r"|ON\s+BLOCK\s+ERROR"
        r"|BLOCK"
        r")$"
    )
    
    def _declare_missing_variables(self):
        """PERMANENT FIX: PAD paste aborts on %variables% that are not declared
        in the flow. Declare every referenced variable at the top so the
        paste always succeeds, for any file, any size."""
        referenced = set()
        for line in self.script_lines:
            for m in re.finditer(r"%([A-Za-z_]\w*)%", line):
                referenced.add(m.group(1))

        # Excel cell references are literals, not variables - declaring
        # them creates phantom empty variables the developer must ignore.
        missing = sorted(
            v
            for v in referenced
            if v not in self.generated_variables
            and not re.fullmatch(r"[A-Z]{1,3}\d{1,7}", v)
        )

        if not missing:
            return

        # Detect variables used numerically (comparisons or arithmetic) so
        # they are declared as 0 instead of '' - empty text compared with
        # > 0 breaks conditions at runtime.
        script_text = "\n".join(self.script_lines)
        numeric_vars = set()

        for variable_name in missing:
            var_ref = re.escape(f"%{variable_name}%")

            numeric_usage = re.search(
                rf"{var_ref}\s*(?:>=|<=|>|<)\s*"
                rf"|(?:>=|<=|>|<)\s*{var_ref}"
                rf"|{var_ref}\s*\+\s*\d"
                rf"|\d\s*\+\s*{var_ref}",
                script_text,
            )

            if numeric_usage:
                numeric_vars.add(variable_name)

        block = [
            f"SET {self._safe_var(v)} TO "
            f"{'0' if v in numeric_vars else chr(39) + chr(39)}"
            for v in missing
        ]

        # Insert after the header (first blank line); all SETs precede use
        insert_at = 0
        for i, line in enumerate(self.script_lines):
            if line.strip() == "":
                insert_at = i
                break

        self.script_lines[insert_at:insert_at] = block
        self.generated_variables.update(missing)
        logger.info(f"Auto-declared {len(missing)} referenced variable(s)")

    def _lint_script(self, script):
        """PERMANENT SAFETY NET: neutralize any line that is not provably
        valid Robin (structural keyword form or known schema ActionId).
        Guarantees one bad action can never break the whole file."""
        out = []
        for line in script.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                out.append(line)
                continue
            # Robin strings are single-quoted only - never emit double quotes
            line = line.replace('""', "''")
            stripped = line.strip()

            # Documentation comments intentionally carry the original
            # VB expression - they must never be neutralized.
            if stripped.startswith("# OriginalExpression:"):
                out.append(line)
                continue

            # A schema-backed action must survive even if one parameter
            # value still needs manual work. Neutralizing the whole line
            # would delete the action the developer needs.
            action_match = re.match(
                r"^([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)",
                stripped,
            )

            is_known_action = bool(
                action_match
                and action_match.group(1) in self.pad_schema_index
            )

            if is_known_action and self._has_forbidden_vb(stripped):
                logger.warning(
                    "Action retained despite unresolved value: %s",
                    stripped[:80],
                )
                out.append(line)
                continue

            if self._has_forbidden_vb(stripped):
                indent = line[: len(line) - len(line.lstrip())]
                compact = re.sub(r"\s+", " ", stripped)[:300]
                out.append(
                    f"{indent}# [MANUAL REVIEW] "
                    f"SourceId=generated_line | UiPathAction=Unknown | "
                    f"Name=VB expression leakage | "
                    f"SuggestedPAD=Rebuild value with PAD text actions | "
                    f"Reason=Line contains untranslated .NET syntax | "
                    f"SourceData=Original: {compact}"
                )
                logger.warning(
                    "VB leakage neutralized: %s", stripped[:80]
                )
                continue

            if self._is_valid_robin_line(stripped):
                out.append(line)
            else:
                indent = line[: len(line) - len(line.lstrip())]
                out.append(f"{indent}# [AUTO-NEUTRALIZED - verify manually] {stripped}")
                logger.warning(f"Lint neutralized invalid line: {stripped[:80]}")
        return "\n".join(out)

    @staticmethod
    def _remove_empty_structures(script):
        """Iteratively drop IF/ELSE structures with no executable body.

        Empty branches are a designer-paste risk and carry no behavior.
        Iteration matters: removing an inner empty IF can leave an outer
        structure empty on the next pass.
        """
        lines = script.split("\n")
        changed = True

        def is_noise(text):
            # Comments carry manual-review information and must never be
            # deleted. Only blank lines count as noise.
            return not text

        while changed:
            changed = False
            output = []
            index = 0

            while index < len(lines):
                stripped = lines[index].strip()

                # IF ... THEN followed (ignoring noise) directly by END
                if (
                    stripped.startswith("IF ")
                    and stripped.endswith(" THEN")
                ):
                    lookahead = index + 1
                    while (
                        lookahead < len(lines)
                        and is_noise(lines[lookahead].strip())
                    ):
                        lookahead += 1

                    if (
                        lookahead < len(lines)
                        and lines[lookahead].strip() == "END"
                    ):
                        index = lookahead + 1
                        changed = True
                        continue

                # ELSE followed directly by END -> drop the ELSE only
                if stripped == "ELSE":
                    lookahead = index + 1
                    while (
                        lookahead < len(lines)
                        and is_noise(lines[lookahead].strip())
                    ):
                        lookahead += 1

                    if (
                        lookahead < len(lines)
                        and lines[lookahead].strip() == "END"
                    ):
                        index += 1
                        changed = True
                        continue

                output.append(lines[index])
                index += 1

            lines = output

        return "\n".join(lines)
    
    @staticmethod
    def _check_block_balance(script):
        """Log an error if openers and END statements do not match."""
        openers = 0
        closers = 0

        for line in script.split("\n"):
            stripped = line.strip()

            if stripped.startswith("#") or not stripped:
                continue

            if (
                stripped == "BLOCK"
                or stripped == "ON BLOCK ERROR"
                or stripped.startswith("LOOP")
                or stripped.startswith("SWITCH ")
                or (
                    stripped.startswith("IF ")
                    and stripped.endswith(" THEN")
                )
            ):
                openers += 1
            elif stripped == "END":
                closers += 1

        if openers != closers:
            logger.error(
                "BLOCK BALANCE ERROR: %d openers vs %d END statements "
                "- the script will not paste",
                openers,
                closers,
            )
            return False

        return True

    def _is_valid_robin_line(self, stripped):
        if self.LINT_STRUCTURAL_RE.match(stripped):
            return True
        m = re.match(r"^([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)", stripped)
        return bool(m and m.group(1) in self.pad_schema_index)
    
    def _llm_fix_line(self, line, error_msg):
        """Ask the LLM to fix one DLL-rejected line, giving it the official
        schema template so it corrects syntax instead of guessing."""
        try:
            m = re.match(r"^([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)", line.strip())
            template = ""
            if m and m.group(1) in self.pad_schema_index:
                template = self.pad_schema_index[m.group(1)].get("RobinSyntaxTemplate", "")
            client = get_llm_client()
            return client.suggest_robin_fix(line, error_msg, template)
        except Exception:
            return None

    def _ensure_pasteable(self, script):
        """Validate with the PAD parser and apply targeted safe repairs.

        Non-structural failing lines may be corrected using the LLM and the
        official schema template. Structural errors are returned unchanged
        for block-level repair because changing one BLOCK, ON BLOCK ERROR,
        IF, ELSE, or END line can corrupt the complete control-flow tree.
        """
        if not script:
            return script or ""

        max_attempts = 3

        for attempt in range(max_attempts):
            temp_path = os.path.join(
                tempfile.gettempdir(),
                "_paste_check.robin",
            )

            try:
                with open(
                    temp_path,
                    "w",
                    encoding="utf-8",
                ) as file_handle:
                    file_handle.write(script)

                result = run_pad_validator(temp_path)

            except Exception as exc:
                logger.warning(
                    "PAD pasteability check failed: %s",
                    exc,
                )
                return script

            if result.get("pad_not_available"):
                logger.warning(
                    "PAD parser is unavailable. Returning the script "
                    "without PAD DLL certification."
                )
                return script

            if result.get("isValid"):
                logger.info(
                    "PAD DLL certified the script as pasteable after "
                    "%d validation pass(es)",
                    attempt + 1,
                )
                return script

            errors = result.get("errors") or []

            if not errors:
                logger.warning(
                    "PAD parser reported invalid output without errors."
                )
                return script

            lines = script.split("\n")
            fixed_any = False
            structural_error_found = False

            for error in errors:
                message = str(error.get("message") or "")
                message_lower = message.lower()

                structural_markers = (
                    "error block statement was previously defined",
                    "block statement was previously defined",
                    "block structure",
                    "unexpected end",
                    "expected end",
                    "end of block",
                    "missing end",
                    "unclosed block",
                )

                if any(
                    marker in message_lower
                    for marker in structural_markers
                ):
                    structural_error_found = True
                    logger.warning(
                        "PAD structural error cannot safely be repaired "
                        "one line at a time: %s",
                        message,
                    )
                    continue

                line_number = (
                    error.get("startLine")
                    or error.get("stopLine")
                    or 0
                )

                try:
                    line_number = int(line_number)
                except (TypeError, ValueError):
                    line_number = 0

                line_index = line_number - 1

                if not 0 <= line_index < len(lines):
                    logger.warning(
                        "PAD error has no usable line number: %s",
                        message,
                    )
                    continue

                original_line = lines[line_index]
                stripped_line = original_line.strip()

                if (
                    not stripped_line
                    or stripped_line.startswith("#")
                    or stripped_line.startswith("//")
                ):
                    continue

                # Deterministic repair first: rejected lines whose only
                # defect is a malformed keyboard token.
                corrected_line = None
                if "[k(" in original_line.lower():
                    normalized = self._normalize_key_tokens(
                        original_line
                    )
                    if normalized.strip() != original_line.strip():
                        corrected_line = normalized.strip()

                if not corrected_line:
                    corrected_line = self._llm_fix_line(
                        original_line,
                        message,
                    )

                indent = original_line[
                    :len(original_line) - len(original_line.lstrip())
                ]

                if (
                    corrected_line
                    and corrected_line.strip()
                    and corrected_line.strip() != stripped_line
                ):
                    lines[line_index] = (
                        f"{indent}{corrected_line.strip()}"
                    )
                    fixed_any = True

                    logger.info(
                        "Applied targeted PAD correction at line %d",
                        line_number,
                    )
                else:
                    # Preserve traceability. This makes the script
                    # syntactically safer but requires manual review.
                    lines[line_index] = (
                        f"{indent}# [MANUAL - DLL rejected] "
                        f"{stripped_line}"
                    )
                    fixed_any = True

                    logger.warning(
                        "Neutralized PAD-rejected line %d: %s",
                        line_number,
                        stripped_line,
                    )

            # Structural errors must be handled by RepairEngine or by fixing
            # the corresponding generator. Do not partially alter the script.
            if structural_error_found:
                logger.warning(
                    "Structural PAD validation errors remain. Returning "
                    "the intact script for block-level repair."
                )
                return script

            if not fixed_any:
                logger.warning(
                    "No safe targeted PAD corrections were available."
                )
                return script

            repaired_script = "\n".join(lines)

            if repaired_script == script:
                logger.warning(
                    "PAD correction pass produced no script changes."
                )
                return script

            script = repaired_script

        # Always return the latest script when the retry limit is reached.
        return script
                        
    
    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_script(self, script, output_path=None):
        """Save Robin script to file."""
        path = Path(output_path) if output_path else Config.GENERATED_SCRIPT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            f.write(script)

        logger.info(f"Robin script saved to: {path}")
        return path


def generate_pad_script(ir_data, mapping_result):
    """Convenience function to generate PAD Robin script."""
    generator = PADScriptGenerator()
    return generator.generate(ir_data, mapping_result)