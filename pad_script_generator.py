import json
import re
import logging
from pathlib import Path
from config import Config
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
        self.generated_variables = set()
        self.script_lines = []
        self.indent_level = 0
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
        """Generate complete PAD Robin script from IR and mapping result."""
        self.script_lines = []
        self.generated_variables = set()
        self.indent_level = 0

        mapping_lookup = {}
        for m in mapping_result.get("mappings", []):
            mapping_lookup[m["action_id"]] = m

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
        script = "\n".join(self.script_lines)
        script = self._lint_script(script)
        logger.info(f"Robin script generated: {len(self.script_lines)} lines")
        return script

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
            self._add_comment(f"Argument ({direction}): {name}")
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

        mapping = mapping_lookup.get(action_id)

        if not mapping:
            self._add_comment(f"UNMAPPED: {action_type} - {display_name} (no mapping found)")
            self._generate_children(action, mapping_lookup, all_actions)
            return

        target_action = mapping.get("target_action", "")

        # Sequence/Flowchart map to "Subflow" in CSV but are passthrough in Robin
        if target_action in ("Subflow", "Step", "BLOCK:Subflow", "BLOCK:Step") \
                or action.get("container_type") in ("Sequence", "Flowchart"):
            self._generate_children(action, mapping_lookup, all_actions)
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
        display_name = action.get("display_name", "")

        self._add_comment(f"{display_name}")
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
            self._add_comment("No actions in Then block")

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
        display_name = action.get("display_name", "")

        self._add_comment(f"{display_name}")
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
        """Generate FOR EACH or LOOP WHILE block."""
        target_action = mapping.get("target_action", "")
        display_name = action.get("display_name", "")

        self._add_comment(f"{display_name}")

        if target_action == "Loops.ForEach":
            item_var = self._resolve_param(action, mapping, "CurrentItem", "CurrentItem")
            list_var = self._resolve_param(action, mapping, "List", "ItemList")

            # Fallback: read collection directly from source properties
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

            self._ensure_variable(item_var)
            self._add_line(f"FOR EACH {item_var} IN {self._var_ref(list_var)}")
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

    def _generate_try_catch(self, action, mapping, mapping_lookup, all_actions):
        """Generate BEGIN EXCEPTION / END EXCEPTION block."""
        display_name = action.get("display_name", "")

        self._add_comment(f"{display_name}")
        self._add_line("BEGIN EXCEPTION")
        self.indent_level += 1

        child_ids = action.get("child_ids", [])
        try_children = []
        catch_children = []
        finally_children = []

        def _passthrough(node, bucket):
            for sub_id in node.get("child_ids", []):
                sub = self._get_action_by_id(sub_id, all_actions)
                if sub:
                    bucket.append(sub)

        def _classify(node):
            for cid in node.get("child_ids", []):
                child = self._get_action_by_id(cid, all_actions)
                if not child:
                    continue
                ct = child.get("action_type", "")
                if ct in ("TryCatch", "RetryScope"):
                    try_children.append(child)          # nested block stays intact
                elif ct == "Block_Try":
                    _passthrough(child, try_children)
                elif ct == "Block_Finally":
                    _passthrough(child, finally_children)
                elif ct == "Catch" or ct.startswith("Catch"):
                    _passthrough(child, catch_children)
                elif "ActivityBody" in ct or "ActivityAction" in ct or ct.startswith("Block_"):
                    _classify(child)                     # transparent wrapper - look inside
                else:
                    try_children.append(child)

        _classify(action)

        if try_children:
            for child in try_children:
                self._generate_action(child, mapping_lookup, all_actions)
        else:
            self._add_comment("Empty try block")

        self.indent_level -= 1
        self._add_line("ON ERROR")
        self.indent_level += 1

        if catch_children:
            for child in catch_children:
                self._generate_action(child, mapping_lookup, all_actions)
        else:
            self._add_comment("Exception caught - add error handling logic")

        self.indent_level -= 1
        self._add_line("END EXCEPTION")

        if finally_children:
            self._add_comment("Finally block (executed after try-catch)")
            for child in finally_children:
                self._generate_action(child, mapping_lookup, all_actions)

    def _generate_block(self, action, mapping, mapping_lookup, all_actions):
        """Generate content for structural blocks."""
        block_type = mapping.get("target_action", "BLOCK:Unknown").replace("BLOCK:", "")
        display_name = action.get("display_name", "")

        if block_type == "StateMachine":
            self._add_comment(f"STATE MACHINE: {display_name}")
            self._add_comment("NOTE: PAD has no state machine. Each STATE below is emitted with its full logic;")
            self._add_comment("TRANSITION comments show the original conditions. Re-wire the flow manually")
            self._add_comment("(typically as a LOOP WHILE with a State variable) when importing into PAD.")
            self._generate_children(action, mapping_lookup, all_actions)
            return

        if block_type == "State":
            self._add_line("")
            self._add_comment(f"===== STATE: {display_name} =====")
            self._generate_children(action, mapping_lookup, all_actions)
            return

        if block_type == "Transition":
            props = action.get("properties", {})
            cond = props.get("Condition", "")
            cond_str = self._translate_expression(cond) if cond else "always"
            self._add_comment(f"--- TRANSITION: {display_name} | Condition: {cond_str} ---")
            self._generate_children(action, mapping_lookup, all_actions)
            return

        # Default: Sequence / Flowchart / Container / Body / Action / Then / Else
        self._generate_children(action, mapping_lookup, all_actions)
        
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
            self._add_comment(f"{display_name}")
            self._add_comment(f"TODO [REVIEW]: No PAD action found for '{target_action}' - manual migration required")
            return False

        template = schema_entry.get("RobinSyntaxTemplate", "")
        if not template:
            self._add_comment(f"{display_name}")
            self._add_comment(f"WARNING: Empty RobinSyntaxTemplate for '{target_action}'")
            return False

        filled_line = self._fill_template(template, schema_entry, action, mapping)

        annotation = action.get("annotation")
        if annotation:
            self._add_comment(annotation)

        self._add_comment(f"{display_name}")
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
        """Generate SET variable assignment."""
        display_name = action.get("display_name", "")
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
        # Safety: a bare identifier must be a %variable% reference
        if re.fullmatch(r'[A-Za-z_]\w*', value):
            value = f"%{value}%"
        # Safety: untranslatable .NET value -> placeholder + MANUAL FIX comment
        if self._is_untranslatable(value):
            original = properties.get("Value", "") or expressions.get("Value", "")
            self._add_comment(f"MANUAL FIX VALUE: {original}")
            value = "'MANUAL_Fix'"
        self._ensure_variable(var_name)

        self._add_comment(f"{display_name}")
        self._add_line(f"SET {var_name} TO {value}")

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
        self._add_comment(f"{display_name}")

        if entry:
            template = entry.get("RobinSyntaxTemplate", "")
            filled = template.replace("''", f"'{subflow_name}'", 1)
            self._add_line(filled)
        else:
            self._add_comment(
                f"SUBFLOW CALL: {subflow_name} - add a 'Run desktop flow' action manually"
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

        self._add_comment(f"{display_name}")
        self._add_comment(f"THROW (no Robin equivalent - raise manually if needed): {message}")

    def _generate_comment_action(self, action, mapping):
        """Generate a comment line (includes notes so unmapped actions stay visible)."""
        properties = action.get("properties", {})
        text = properties.get("Text", "") or action.get("display_name", "Comment")
        notes = mapping.get("notes", "")
        if notes:
            self._add_comment(f"{text} - {notes}")
        else:
            self._add_comment(text)

    def _generate_unmapped(self, action, mapping):
        """Generate placeholder for unmapped action."""
        source_action = mapping.get("source_action", "")
        display_name = action.get("display_name", "")
        notes = mapping.get("notes", "")
        confidence = mapping.get("confidence", "low")

        self._add_comment(f"TODO [UNMAPPED]: {source_action} - {display_name}")
        self._add_comment(f"  Confidence: {confidence}")
        if notes:
            self._add_comment(f"  Notes: {notes}")
        self._add_comment(f"  Manual migration required for this action")

    # ------------------------------------------------------------------
    # Template filling
    # ------------------------------------------------------------------

    def _fill_template(self, template, schema_entry, action, mapping):
        """Fill a RobinSyntaxTemplate with actual values from IR."""
        inputs = schema_entry.get("Inputs", [])
        outputs = schema_entry.get("Outputs", [])
        param_mapping = mapping.get("parameter_mapping", {})
        source_props = action.get("properties", {})
        source_exprs = action.get("expressions", {})

        reverse_mapping = {}
        for src_key, tgt_key in param_mapping.items():
            reverse_mapping[tgt_key] = src_key

        action_name, template_params = self._parse_template(template)

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
            # PAD named parameters accept literals or %var% only:
            # extract inline concatenations into a temp variable
            if isinstance(resolved_value, str) and "+" in resolved_value:
                tmp = re.sub(r'[^A-Za-z0-9_]', '_', f"Tmp_{param_name}_{len(self.script_lines)}")
                self._add_line(f"SET {tmp} TO {resolved_value}")
                resolved_value = f"%{tmp}%"
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

        return expr.strip() if expr.strip() else "''"
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

        # Simple identifier - treat as variable
        if re.match(r'^[A-Za-z_]\w*$', value):
            return f"%{value}%"

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
                self._add_comment(f"MANUAL FIX CONDITION: {condition}")
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

        return "<<PLACEHOLDER_Condition>>"
    
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

        return "<<PLACEHOLDER_SwitchValue>>"

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
        stripped = re.sub(r"'[^']*'", "", text or "")
        return bool(re.search(r"MANUAL_|\bGetType\b|\w+\(|\w+\.\w+", stripped))

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

    @staticmethod
    def _var_ref(name):
        """Wrap a variable name in PAD variable reference syntax."""
        if not name:
            return "''"
        name = name.strip().strip("%")
        return f"%{name}%"

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
        """Add a comment line to the script (official Robin comment syntax)."""
        indent = self.INDENT * self.indent_level
        self.script_lines.append(f"{indent}// {text}")

    
    LINT_STRUCTURAL_RE = re.compile(
        r"^("
        r"IF\s+.+\s+THEN"
        r"|ELSE(\s+IF\s+.+\s+THEN)?"
        r"|END(\s+EXCEPTION)?"
        r"|BEGIN\s+EXCEPTION"
        r"|ON\s+ERROR"
        r"|LOOP(\s+WHILE\s+.+)?"
        r"|FOR\s+EACH\s+\S+\s+IN\s+.+"
        r"|SWITCH\s+.+"
        r"|CASE\s+.+"
        r"|DEFAULT"
        r"|WAIT\s+.+"
        r"|SET\s+[A-Za-z_]\w*\s+TO\s+.+"
        r"|EXIT\s+LOOP"
        r"|NEXT\s+LOOP"
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

        missing = sorted(v for v in referenced if v not in self.generated_variables)
        if not missing:
            return

        block = ["// Auto-declared variables referenced by migrated actions"]
        block += [f"SET {self._safe_var(v)} TO ''" for v in missing]

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
            if self._is_valid_robin_line(stripped):
                out.append(line)
            else:
                indent = line[: len(line) - len(line.lstrip())]
                out.append(f"{indent}// [AUTO-NEUTRALIZED - verify manually] {stripped}")
                logger.warning(f"Lint neutralized invalid line: {stripped[:80]}")
        return "\n".join(out)

    def _is_valid_robin_line(self, stripped):
        if self.LINT_STRUCTURAL_RE.match(stripped):
            return True
        m = re.match(r"^([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)", stripped)
        return bool(m and m.group(1) in self.pad_schema_index)
    
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