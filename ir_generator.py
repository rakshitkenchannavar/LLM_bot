import json
import logging
from pathlib import Path
from config import Config

logger = logging.getLogger(__name__)


class IRGenerator:
    """Converts parsed XAML tree into normalized Intermediate Representation (IR) JSON.

    The IR is action-centric and preserves:
    - workflow_name, source_file
    - action_id, parent_id, order
    - action_type, display_name, container_type
    - arguments, properties, variables_defined, variables_used
    - expressions, selectors, target_app
    - children, exception_handling, annotations
    """

    def __init__(self):
        self.action_counter = 0

    def generate(self, parsed_tree, project_info=None):
        """Generate IR JSON from a single parsed XAML tree.

        Args:
            parsed_tree: Output from XAMLParser.parse()
            project_info: Optional project metadata dict

        Returns:
            dict: Normalized IR JSON
        """
        if not parsed_tree:
            raise ValueError("Parsed tree is empty or None")

        if parsed_tree.get("error"):
            logger.warning(f"Skipping errored parse: {parsed_tree.get('source_file')}")
            return self._error_ir(parsed_tree)

        self.action_counter = 0

        # Flatten the activity tree into ordered action list
        actions = []
        activity_tree = parsed_tree.get("activity_tree")
        if activity_tree:
            self._flatten_tree(activity_tree, actions, parent_id=None, order=0)

        # Build the IR
        ir = {
            "workflow_name": parsed_tree.get("workflow_name", "Unknown"),
            "source_file": parsed_tree.get("source_file", ""),
            "project_info": project_info or {},
            "variables": self._normalize_variables(parsed_tree.get("variables", [])),
            "arguments": self._normalize_arguments(parsed_tree.get("arguments", [])),
            "actions": actions,
            "total_actions": len(actions),
            "action_summary": self._build_action_summary(actions),
        }

        logger.info(
            f"IR generated for {ir['workflow_name']}: "
            f"{ir['total_actions']} actions, "
            f"{len(ir['variables'])} variables, "
            f"{len(ir['arguments'])} arguments"
        )

        return ir

    def generate_multiple(self, parsed_trees, project_info=None):
        """Generate IR JSON for multiple parsed XAML trees.

        Args:
            parsed_trees: List of parsed tree dicts
            project_info: Optional project metadata

        Returns:
            dict: Combined IR with all workflows
        """
        workflows = []
        total_actions = 0

        for tree in parsed_trees:
            try:
                ir = self.generate(tree, project_info)
                workflows.append(ir)
                total_actions += ir.get("total_actions", 0)
            except Exception as e:
                logger.error(f"IR generation failed for {tree.get('source_file', 'unknown')}: {e}")
                workflows.append(self._error_ir(tree, str(e)))

        combined_ir = {
            "project_info": project_info or {},
            "workflows": workflows,
            "total_workflows": len(workflows),
            "total_actions": total_actions,
        }

        logger.info(
            f"Combined IR: {len(workflows)} workflows, {total_actions} total actions"
        )

        return combined_ir

    # ------------------------------------------------------------------
    # Tree flattening
    # ------------------------------------------------------------------

    def _flatten_tree(self, node, actions, parent_id, order):
        """Recursively flatten activity tree into ordered action list.

        Each action gets a unique sequential ID and preserves parent-child relationship.
        Children are stored both inline (for tree structure) and by reference (parent_id).

        Args:
            node: Activity tree node dict
            actions: List to append flattened actions to
            parent_id: Parent action ID
            order: Execution order within parent
        """
        if node is None:
            return

        # Handle list of nodes
        if isinstance(node, list):
            for idx, child in enumerate(node):
                self._flatten_tree(child, actions, parent_id, idx)
            return

        # Skip nodes without meaningful action type
        action_type = node.get("action_type", "")
        if not action_type:
            # Still process children
            children = node.get("children", [])
            for idx, child in enumerate(children):
                self._flatten_tree(child, actions, parent_id, idx)
            return

        # Assign action ID if not present
        action_id = node.get("action_id")
        if not action_id:
            self.action_counter += 1
            action_id = f"ir_{self.action_counter:04d}"

        # Build normalized action entry
        action = self._normalize_action(node, action_id, parent_id, order)

        # Process children
        children = node.get("children", [])
        child_ids = []
        child_actions = []

        for idx, child in enumerate(children):
            if child is None:
                continue

            child_id = child.get("action_id")
            if not child_id:
                self.action_counter += 1
                child_id = f"ir_{self.action_counter:04d}"
                child["action_id"] = child_id

            child_ids.append(child_id)
            self._flatten_tree(child, actions, action_id, idx)

            # Build inline child summary for tree readability
            child_actions.append({
                "action_id": child_id,
                "action_type": child.get("action_type", ""),
                "display_name": child.get("display_name", ""),
            })

        action["child_ids"] = child_ids
        action["children_summary"] = child_actions

        actions.append(action)

    def _normalize_action(self, node, action_id, parent_id, order):
        """Normalize a single action node into IR format.

        Args:
            node: Raw parsed activity node
            action_id: Assigned action ID
            parent_id: Parent action ID
            order: Execution order

        Returns:
            dict: Normalized action
        """
        properties = node.get("properties", {})
        expressions = node.get("expressions", {})
        selector = node.get("selector")

        # Detect target application from selector or properties
        target_app = self._detect_target_app(properties, selector)

        # Normalize expressions - separate from properties
        clean_properties = {}
        action_expressions = dict(expressions)

        for key, value in properties.items():
            if isinstance(value, str) and self._is_expression(value):
                action_expressions[key] = value
            else:
                clean_properties[key] = value

        action = {
            "action_id": action_id,
            "parent_id": parent_id,
            "order": order,
            "action_type": node.get("action_type", "Unknown"),
            "display_name": node.get("display_name", ""),
            "container_type": node.get("container_type"),
            "properties": clean_properties,
            "arguments": self._extract_action_arguments(properties),
            "variables_defined": node.get("variables_defined", []),
            "variables_used": node.get("variables_used", []),
            "expressions": action_expressions,
            "selector": selector,
            "target_app": target_app,
            "exception_handling": node.get("exception_handling"),
            "annotation": node.get("annotation"),
            "child_ids": [],
            "children_summary": [],
        }

        return action

    # ------------------------------------------------------------------
    # Normalization helpers
    # ------------------------------------------------------------------

    def _normalize_variables(self, variables):
        """Normalize variable definitions to consistent IR format."""
        normalized = []
        seen_names = set()

        for var in variables:
            if not var or not var.get("name"):
                continue

            name = var["name"]
            if name in seen_names:
                continue
            seen_names.add(name)

            normalized.append({
                "name": name,
                "type": self._map_variable_type(var.get("type", "Object")),
                "default_value": var.get("default_value", ""),
                "scope": var.get("scope", "local"),
                "direction": var.get("direction", "local"),
            })

        return normalized

    def _normalize_arguments(self, arguments):
        """Normalize argument definitions to consistent IR format."""
        normalized = []
        seen_names = set()

        for arg in arguments:
            if not arg or not arg.get("name"):
                continue

            name = arg["name"]
            if name in seen_names:
                continue
            seen_names.add(name)

            normalized.append({
                "name": name,
                "type": self._map_variable_type(arg.get("type", "String")),
                "direction": arg.get("direction", "In"),
                "default_value": arg.get("default_value", ""),
            })

        return normalized

    @staticmethod
    def _map_variable_type(uipath_type):
        """Map UiPath .NET types to simplified IR types."""
        type_mapping = {
            "String": "Text",
            "Int32": "Number",
            "Int64": "Number",
            "Double": "Number",
            "Decimal": "Number",
            "Boolean": "Boolean",
            "DateTime": "DateTime",
            "TimeSpan": "TimeSpan",
            "DataTable": "DataTable",
            "DataRow": "DataRow",
            "Object": "General",
            "Array": "List",
            "List": "List",
            "Dictionary": "Dictionary",
            "Exception": "Error",
            "SystemException": "Error",
            "BusinessRuleException": "Error",
            "SecureString": "SecureText",
        }
        return type_mapping.get(uipath_type, "General")

    @staticmethod
    def _extract_action_arguments(properties):
        """Extract argument-like properties from action properties.

        Some UiPath actions pass arguments inline as properties.
        """
        arg_keys = [
            "To", "Value", "From", "Input", "Output", "Result",
            "FileName", "FilePath", "SheetName", "Range",
            "WorkbookPath", "Text", "Condition", "Expression",
        ]
        arguments = {}
        for key in arg_keys:
            if key in properties:
                arguments[key] = properties[key]
        return arguments

    @staticmethod
    def _detect_target_app(properties, selector):
        """Detect the target application from selector or properties."""
        if not selector:
            # Check properties for app hints
            for key in ("app", "Application", "BrowserType", "ApplicationPath"):
                if key in properties:
                    return properties[key]
            return None

        selector_lower = selector.lower() if isinstance(selector, str) else ""

        # Common application patterns in selectors
        app_patterns = {
            "chrome": "Chrome",
            "firefox": "Firefox",
            "iexplore": "InternetExplorer",
            "msedge": "Edge",
            "excel": "Excel",
            "outlook": "Outlook",
            "notepad": "Notepad",
            "saplogon": "SAP",
            "explorer": "FileExplorer",
            "cmd": "CommandPrompt",
            "powershell": "PowerShell",
        }

        for pattern, app_name in app_patterns.items():
            if pattern in selector_lower:
                return app_name

        return "UnknownApp"

    @staticmethod
    def _is_expression(value):
        """Check if a property value is an expression rather than a literal."""
        if not isinstance(value, str):
            return False

        expression_indicators = [
            "(", ")", ".", "+", "&", "=",
            "New ", "CType", "Convert.",
            "String.Format", "DateTime.",
            ".ToString", ".Count", ".Length",
            "If(", "IIf(",
        ]

        for indicator in expression_indicators:
            if indicator in value:
                return True

        return False

    @staticmethod
    def _build_action_summary(actions):
        """Build a summary of action types and counts."""
        summary = {}
        for action in actions:
            action_type = action.get("action_type", "Unknown")
            summary[action_type] = summary.get(action_type, 0) + 1
        return summary

    @staticmethod
    def _error_ir(parsed_tree, error_msg=None):
        """Build an error IR entry for failed parsing."""
        return {
            "workflow_name": parsed_tree.get("workflow_name", "Unknown"),
            "source_file": parsed_tree.get("source_file", ""),
            "project_info": {},
            "variables": [],
            "arguments": [],
            "actions": [],
            "total_actions": 0,
            "action_summary": {},
            "error": error_msg or parsed_tree.get("error", "Unknown error"),
        }

    # ------------------------------------------------------------------
    # Save / Load
    # ------------------------------------------------------------------

    @staticmethod
    def save_ir(ir_data, output_path=None):
        """Save IR JSON to file.

        Args:
            ir_data: IR dict to save
            output_path: Optional path override. Defaults to Config.IR_OUTPUT_PATH
        """
        path = Path(output_path) if output_path else Config.IR_OUTPUT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(ir_data, f, indent=2, ensure_ascii=False, default=str)

        logger.info(f"IR JSON saved to: {path}")
        return path

    @staticmethod
    def load_ir(input_path=None):
        """Load IR JSON from file.

        Args:
            input_path: Optional path override. Defaults to Config.IR_OUTPUT_PATH

        Returns:
            dict: Loaded IR data
        """
        path = Path(input_path) if input_path else Config.IR_OUTPUT_PATH

        if not path.exists():
            raise FileNotFoundError(f"IR file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        logger.info(f"IR JSON loaded from: {path}")
        return data


def generate_ir(parsed_tree, project_info=None):
    """Convenience function to generate IR from a single parsed tree.

    Args:
        parsed_tree: Output from XAMLParser.parse()
        project_info: Optional project metadata

    Returns:
        dict: Normalized IR JSON
    """
    generator = IRGenerator()
    return generator.generate(parsed_tree, project_info)


def generate_ir_multiple(parsed_trees, project_info=None):
    """Convenience function to generate IR from multiple parsed trees.

    Args:
        parsed_trees: List of parsed tree dicts
        project_info: Optional project metadata

    Returns:
        dict: Combined IR JSON
    """
    generator = IRGenerator()
    return generator.generate_multiple(parsed_trees, project_info)