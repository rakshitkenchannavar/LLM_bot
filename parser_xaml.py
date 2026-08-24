import logging
import re
from pathlib import Path
from lxml import etree

logger = logging.getLogger(__name__)


# UiPath XAML namespace constants
NAMESPACES = {
    "x": "http://schemas.microsoft.com/winfx/2006/xaml",
    "sap": "http://schemas.microsoft.com/netfx/2009/xaml/activities/presentation",
    "sap2010": "http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation",
    "ui": "http://schemas.uipath.com/workflow/activities",
    "scg": "clr-namespace:System.Collections.Generic;assembly=mscorlib",
    "s": "clr-namespace:System;assembly=mscorlib",
    "mca": "clr-namespace:Microsoft.CSharp.Activities;assembly=Microsoft.CSharp.Activities",
}

# Known UiPath activity type patterns
ACTIVITY_PATTERNS = {
    # Control flow
    "Sequence": "Sequence",
    "Flowchart": "Flowchart",
    "FlowDecision": "FlowDecision",
    "FlowStep": "FlowStep",
    "If": "If",
    "Else": "Else",
    "ElseIf": "ElseIf",
    "Switch": "Switch",
    "ForEach": "ForEach",
    "While": "While",
    "DoWhile": "DoWhile",
    "Parallel": "Parallel",
    "Pick": "Pick",
    "PickBranch": "PickBranch",

    # Exception handling
    "TryCatch": "TryCatch",
    "Try": "Try",
    "Catch": "Catch",
    "Catches": "Catches",
    "Finally": "Finally",
    "Throw": "Throw",
    "Rethrow": "Rethrow",
    "RetryScope": "RetryScope",

    # Assignment and variables
    "Assign": "Assign",
    "MultipleAssign": "MultipleAssign",
    "AddToCollection": "AddToCollection",
    "RemoveFromCollection": "RemoveFromCollection",

    # UI Automation
    "Click": "Click",
    "DoubleClick": "DoubleClick",
    "Hover": "Hover",
    "TypeInto": "TypeInto",
    "SendHotkey": "SendHotkey",
    "GetText": "GetText",
    "GetAttribute": "GetAttribute",
    "SetText": "SetText",
    "Check": "Check",
    "SelectItem": "SelectItem",
    "ElementExists": "ElementExists",
    "FindElement": "FindElement",
    "WaitElementVanish": "WaitElementVanish",
    "HighlightElement": "HighlightElement",

    # Browser
    "OpenBrowser": "OpenBrowser",
    "CloseBrowser": "CloseBrowser",
    "NavigateTo": "NavigateTo",
    "AttachBrowser": "AttachBrowser",
    "GoBack": "GoBack",
    "GoForward": "GoForward",

    # Excel
    "ExcelApplicationScope": "ExcelApplicationScope",
    "ReadRange": "ReadRange",
    "WriteRange": "WriteRange",
    "ReadCell": "ReadCell",
    "WriteCell": "WriteCell",
    "AppendRange": "AppendRange",
    "GetWorkbookSheet": "GetWorkbookSheet",
    "InsertDeleteRows": "InsertDeleteRows",
    "InsertDeleteColumns": "InsertDeleteColumns",

    # File operations
    "ReadTextFile": "ReadTextFile",
    "WriteTextFile": "WriteTextFile",
    "AppendLine": "AppendLine",
    "PathExists": "PathExists",
    "CopyFile": "CopyFile",
    "MoveFile": "MoveFile",
    "DeleteFile": "DeleteFile",
    "CreateDirectory": "CreateDirectory",
    "GetFiles": "GetFiles",
    "CreateFile": "CreateFile",

    # Data
    "BuildDataTable": "BuildDataTable",
    "AddDataRow": "AddDataRow",
    "RemoveDataRow": "RemoveDataRow",
    "FilterDataTable": "FilterDataTable",
    "SortDataTable": "SortDataTable",
    "MergeDataTable": "MergeDataTable",
    "LookupDataTable": "LookupDataTable",
    "OutputDataTable": "OutputDataTable",
    "ForEachRow": "ForEachRow",

    # Dialogs
    "MessageBox": "MessageBox",
    "InputDialog": "InputDialog",
    "LogMessage": "LogMessage",
    "WriteLine": "WriteLine",

    # Invoke
    "InvokeWorkflowFile": "InvokeWorkflowFile",
    "InvokeCode": "InvokeCode",
    "InvokeMethod": "InvokeMethod",
    "InvokePowerShell": "InvokePowerShell",

    # Email
    "SendSmtpMailMessage": "SendSmtpMailMessage",
    "GetImapMailMessages": "GetImapMailMessages",
    "GetPop3MailMessages": "GetPop3MailMessages",
    "SendOutlookMailMessage": "SendOutlookMailMessage",
    "GetOutlookMailMessages": "GetOutlookMailMessages",

    # Orchestrator
    "GetQueueItems": "GetQueueItems",
    "AddQueueItem": "AddQueueItem",
    "GetTransactionItem": "GetTransactionItem",
    "SetTransactionStatus": "SetTransactionStatus",
    "GetAsset": "GetAsset",
    "GetCredential": "GetCredential",

    # Misc
    "Delay": "Delay",
    "Comment": "Comment",
    "TerminateWorkflow": "TerminateWorkflow",
    "ShouldStop": "ShouldStop",
    
    # State machine
    "StateMachine": "StateMachine",
    "State": "State",
    "Transition": "Transition",

}


class XAMLParser:
    """Parses UiPath XAML workflow files into structured tree representation.

    Extracts:
    - Activities and their hierarchy
    - Variables and arguments
    - Properties and expressions
    - Selectors
    - Annotations
    - Exception handling blocks
    """

    def __init__(self):
        self.action_counter = 0
        self.variables = []
        self.arguments = []

    def parse(self, xaml_path):
        """Parse a single XAML file into a structured tree.

        Args:
            xaml_path: Path to the .xaml file

        Returns:
            dict: Structured tree representation
        """
        xaml_path = Path(xaml_path)
        if not xaml_path.exists():
            raise FileNotFoundError(f"XAML file not found: {xaml_path}")

        logger.info(f"Parsing XAML: {xaml_path.name}")

        self.action_counter = 0
        self.variables = []
        self.arguments = []

        try:
            # recover=True tolerates invalid namespace URIs (e.g., clr-namespace
            # with full assembly-qualified names containing spaces/commas)
            xml_parser = etree.XMLParser(recover=True)
            tree = etree.parse(str(xaml_path), xml_parser)
            root = tree.getroot()
        except etree.XMLSyntaxError as e:
            raise ValueError(f"Invalid XAML syntax in {xaml_path}: {e}")

        # Extract global variables and arguments first
        self._extract_variables(root)
        self._extract_arguments(root)

        # Parse the activity tree
        activity_tree = self._parse_element(root, parent_id=None, order=0)

        result = {
            "workflow_name": xaml_path.stem,
            "source_file": str(xaml_path),
            "variables": self.variables,
            "arguments": self.arguments,
            "activity_tree": activity_tree,
            "total_actions": self.action_counter,
        }

        logger.info(
            f"Parsed {xaml_path.name}: "
            f"{self.action_counter} actions, "
            f"{len(self.variables)} variables, "
            f"{len(self.arguments)} arguments"
        )

        return result

    def parse_multiple(self, xaml_paths):
        """Parse multiple XAML files.

        Args:
            xaml_paths: List of Path objects

        Returns:
            list: List of parsed tree dicts
        """
        results = []
        for path in xaml_paths:
            try:
                parsed = self.parse(path)
                results.append(parsed)
            except Exception as e:
                logger.error(f"Failed to parse {path}: {e}")
                results.append({
                    "workflow_name": Path(path).stem,
                    "source_file": str(path),
                    "error": str(e),
                    "variables": [],
                    "arguments": [],
                    "activity_tree": None,
                    "total_actions": 0,
                })
        return results

    # ------------------------------------------------------------------
    # Variable and argument extraction
    # ------------------------------------------------------------------

    def _extract_variables(self, root):
        """Extract all variable definitions from the XAML."""
        self.variables = []

        for var_elem in root.iter():
            tag = self._clean_tag(var_elem.tag)

            if tag == "Variable":
                var_info = self._parse_variable(var_elem)
                if var_info:
                    self.variables.append(var_info)

            # Also check for Variable elements within Variable.Default
            if tag == "Variables":
                for child in var_elem:
                    child_tag = self._clean_tag(child.tag)
                    if child_tag == "Variable":
                        var_info = self._parse_variable(child)
                        if var_info:
                            self.variables.append(var_info)

    def _parse_variable(self, var_elem):
        """Parse a single Variable element."""
        name = var_elem.get("Name", "")
        if not name:
            x_name = var_elem.get(f"{{{NAMESPACES['x']}}}Name", "")
            name = x_name if x_name else ""

        if not name:
            return None

        var_type_raw = var_elem.get("TypeArguments", "")
        if not var_type_raw:
            var_type_raw = var_elem.get(f"{{{NAMESPACES['x']}}}TypeArguments", "")

        var_type = self._simplify_type(var_type_raw)

        # Keep the full UiPath type text so generic parameters such as
        # Dictionary(String, Object) survive into the generated comment.
        full_type = str(var_type_raw or "").strip()

        if full_type:
            full_type = re.sub(
                r"clr-namespace:[^;]+;assembly=[^\s,)]+",
                "",
                full_type,
            )
            full_type = re.sub(r"\s+", " ", full_type).strip()
            full_type = full_type.replace("x:", "").replace("s:", "")
            full_type = full_type.replace("scg:", "")

        default_value = var_elem.get("Default", "")

        # Check for default value in child element
        if not default_value:
            for child in var_elem:
                child_tag = self._clean_tag(child.tag)
                if "Default" in child_tag:
                    default_value = self._get_element_value(child)
                    break

        return {
            "name": name,
            "type": var_type,
            "source_type": full_type or var_type,
            "default_value": default_value,
            "scope": "local",
        }

    def _extract_arguments(self, root):
        """Extract workflow arguments (In/Out/InOut)."""
        self.arguments = []

        # Look for x:Members which define arguments in UiPath
        for member in root.iter():
            tag = self._clean_tag(member.tag)

            if tag == "Property" or tag == "Member":
                arg_info = self._parse_argument(member)
                if arg_info:
                    self.arguments.append(arg_info)

        # Also look for arguments defined in XAML attributes
        root_attribs = dict(root.attrib)
        for key, value in root_attribs.items():
            clean_key = self._clean_tag(key)
            if clean_key.startswith("Argument_") or clean_key.startswith("arg_"):
                self.arguments.append({
                    "name": clean_key,
                    "direction": "In",
                    "type": "String",
                    "default_value": value,
                })

    def _parse_argument(self, arg_elem):
        """Parse a single argument/property element."""
        name = arg_elem.get("Name", "")
        if not name:
            return None

        arg_type_raw = arg_elem.get("Type", "")
        direction = "In"

        if "InOutArgument" in arg_type_raw:
            direction = "InOut"
        elif "OutArgument" in arg_type_raw:
            direction = "Out"
        elif "InArgument" in arg_type_raw:
            direction = "In"

        arg_type = self._simplify_type(arg_type_raw)

        # Strip the InArgument/OutArgument wrapper to expose the real type.
        full_type = str(arg_type_raw or "").strip()

        wrapper_match = re.match(
            r"(?:In|Out|InOut)Argument\((.+)\)\s*$",
            full_type,
        )

        if wrapper_match:
            full_type = wrapper_match.group(1).strip()

        if full_type:
            full_type = re.sub(
                r"clr-namespace:[^;]+;assembly=[^\s,)]+",
                "",
                full_type,
            )
            full_type = re.sub(r"\s+", " ", full_type).strip()
            full_type = full_type.replace("x:", "").replace("s:", "")
            full_type = full_type.replace("scg:", "")

        return {
            "name": name,
            "direction": direction,
            "type": arg_type,
            "source_type": full_type or arg_type,
            "default_value": arg_elem.get("Default", ""),
        }

    # ------------------------------------------------------------------
    # Activity tree parsing
    # ------------------------------------------------------------------

    def _parse_element(self, element, parent_id, order):
        """Recursively parse an XML element into an activity node.

        Args:
            element: lxml Element
            parent_id: ID of parent activity
            order: Execution order within parent

        Returns:
            dict: Activity node with children
        """
        tag = self._clean_tag(element.tag)
        activity_type = self._resolve_activity_type(tag, element)

        # Skip non-activity structural elements
        if self._is_structural_only(tag):
            children = self._parse_children(element, parent_id, order)
            if len(children) == 1:
                return children[0]
            if children:
                return {
                    "action_id": None,
                    "action_type": "Container",
                    "display_name": tag,
                    "container_type": tag,
                    "children": children,
                }
            return None

        self.action_counter += 1
        action_id = f"act_{self.action_counter:04d}"

        # Extract display name
        display_name = self._get_display_name(element, tag)

        # Extract properties
        properties = self._extract_properties(element)

        # Extract selector if present
        selector = self._extract_selector(element)

        # Extract expressions
        expressions = self._extract_expressions(element, properties)

        # Extract annotation
        annotation = self._extract_annotation(element)

        # Extract local variables defined in this scope
        local_variables = self._extract_local_variables(element)

        # Determine container type
        container_type = self._determine_container_type(activity_type)

        # Extract exception handling info
        exception_handling = self._extract_exception_handling(element, activity_type)

        # Build the activity node
        node = {
            "action_id": action_id,
            "parent_id": parent_id,
            "order": order,
            "action_type": activity_type,
            "display_name": display_name,
            "container_type": container_type,
            "properties": properties,
            "selector": selector,
            "expressions": expressions,
            "annotation": annotation,
            "variables_defined": local_variables,
            "variables_used": self._detect_variables_used(properties, expressions),
            "exception_handling": exception_handling,
            "children": [],
        }

        # Parse children based on container type to preserve visual execution order
        if activity_type == "Flowchart":
            node["children"] = self._parse_flowchart_graph(element, action_id)
        elif activity_type == "StateMachine":
            node["children"] = self._parse_state_machine_graph(element, action_id)
        else:
            node["children"] = self._parse_children(element, action_id, 0)

        return node

    def _parse_children(self, element, parent_id, start_order):
        """Parse all child activities of an element.

        Handles UiPath-specific child containers like:
        - Sequence.Activities
        - If.Then, If.Else
        - TryCatch.Try, TryCatch.Catches, TryCatch.Finally
        - ForEach.Body
        """
        children = []
        order = start_order

        for child in element:
            child_tag = self._clean_tag(child.tag)

            # Skip variable and argument definition elements
            if child_tag in ("Variables", "Variable", "Members", "Member", "Property"):
                continue
            
            # Skip designer/property-bag entries (anything with x:Key, e.g. av:Point x:Key="ShapeLocation")
            if child.get(f"{{{NAMESPACES['x']}}}Key") is not None or child.get("Key") is not None:
                continue

            # Handle UiPath container properties
            if self._is_activity_container_property(child_tag):
                container_children = self._parse_container_property(child, parent_id, child_tag, order)
                children.extend(container_children)
                order += len(container_children)
                continue

            # >>> NEW: Skip dotted property elements (Assign.To, Assign.Value, etc.)
            # Their values are already captured in _extract_properties.
            # Structural dotted tags (RetryScope.ActivityBody) still recurse.
            if "." in child_tag and not self._is_structural_only(child_tag):
                continue
            # <<< END NEW

            # Parse regular child activity
            parsed = self._parse_element(child, parent_id, order)
            if parsed is not None:
                children.append(parsed)
                order += 1

        return children

    def _parse_container_property(self, element, parent_id, container_tag, start_order):
        """Parse a UiPath activity container property like If.Then, TryCatch.Try, etc."""
        results = []
        order = start_order

        # Wrap in a labeled container for control flow clarity
        wrapper_type = container_tag.split(".")[-1] if "." in container_tag else container_tag

        inner_children = []
        for child in element:
            parsed = self._parse_element(child, parent_id, order)
            if parsed is not None:
                inner_children.append(parsed)
                order += 1

        if inner_children:
            # For specific blocks, keep the wrapper label
            if wrapper_type in ("Then", "Else", "Try", "Finally", "Body", "Action", "True", "False"):
                wrapper = {
                    "action_id": None,
                    "action_type": f"Block_{wrapper_type}",
                    "display_name": wrapper_type,
                    "container_type": wrapper_type,
                    "children": inner_children,
                }
                results.append(wrapper)
            else:
                results.extend(inner_children)

        return results
    
    def _parse_flowchart_graph(self, element, parent_id):
        """Trace flowchart execution flow following graph references (Next, True, False).
        
        This overrides simple sequential sibling traversal, guaranteeing execution
        order matches visual connections.
        """
        nodes_by_ref = {}
        start_node_ref = None

        # 1. Map all visual flowchart nodes by their internal reference ID
        for child in element.iter():
            ref_name = child.get(f"{{{NAMESPACES['x']}}}Name") or child.get("Name")
            if ref_name:
                nodes_by_ref[ref_name] = child

        # 2. Locate flowchart start node
        for child in element:
            tag = self._clean_tag(child.tag)
            if "StartNode" in tag or tag.endswith(".StartNode"):
                for start_child in child:
                    start_node_ref = start_child.get("Ref") or start_child.get(f"{{{NAMESPACES['x']}}}Key")
                    if not start_node_ref and len(start_child) > 0:
                        # Direct embedded element
                        parsed_start = self._parse_element(start_child, parent_id, 0)
                        if parsed_start:
                            return [parsed_start]

        if not start_node_ref and nodes_by_ref:
            # Fallback to first visual key
            start_node_ref = list(nodes_by_ref.keys())[0]

        # 3. Traverse the execution graph following execution connections
        visited = set()
        execution_sequence = []
        current_ref = start_node_ref
        order = 0

        while current_ref and current_ref not in visited:
            visited.add(current_ref)
            node = nodes_by_ref.get(current_ref)
            if node is None:
                break

            parsed_node = self._parse_element(node, parent_id, order)
            if parsed_node:
                execution_sequence.append(parsed_node)
                order += 1

            # Resolve next execution steps (edges)
            next_ref = None
            for edge in node:
                edge_tag = self._clean_tag(edge.tag)
                if edge_tag.endswith(".Next") or edge_tag == "Next":
                    next_ref = edge.get("Ref") or (edge[0].get("Ref") if len(edge) > 0 else None)
                elif edge_tag.endswith(".True") or edge_tag == "True":
                    next_ref = edge.get("Ref")
                elif edge_tag.endswith(".False") or edge_tag == "False":
                    next_ref = edge.get("Ref")

            current_ref = next_ref

        # Fallback to simple traversal if graph tracing produced no execution sequence
        if not execution_sequence:
            return self._parse_children(element, parent_id, 0)

        return execution_sequence

    def _parse_state_machine_graph(self, element, parent_id):
        """Extract states and their transition mappings from a StateMachine container."""
        states = []
        initial_state_ref = None
        
        # Parse states and extract visual transitions
        for child in element:
            tag = self._clean_tag(child.tag)
            if tag == "State":
                state_node = self._parse_element(child, parent_id, len(states))
                if state_node:
                    states.append(state_node)
            elif "InitialState" in tag or tag.endswith(".InitialState"):
                for init_child in child:
                    initial_state_ref = init_child.get("Ref")

        # Package the state-machine metadata cleanly for the script generator
        state_machine_container = {
            "action_id": f"sm_{self.action_counter:04d}",
            "parent_id": parent_id,
            "order": 0,
            "action_type": "StateMachine",
            "display_name": element.get("DisplayName", "Process State Machine"),
            "container_type": "StateMachine",
            "properties": {
                "InitialState": initial_state_ref or (states[0]["action_id"] if states else ""),
            },
            "children": states,
        }
        return [state_machine_container]

    # ------------------------------------------------------------------
    # Property extraction
    # ------------------------------------------------------------------

    def _extract_properties(self, element):
        """Extract all relevant properties from an activity element."""
        properties = {}

        # Direct attributes
        for key, value in element.attrib.items():
            clean_key = self._clean_tag(key)
            # Skip internal XAML attributes
            if clean_key in ("TypeArguments", "Key", "FieldIdentifier"):
                continue
            if clean_key.startswith("{"):
                continue
            properties[clean_key] = value

        # InvokeWorkflowFile argument bindings live in a child
        # dictionary and are lost by generic property extraction.
        if "InvokeWorkflowFile" in self._clean_tag(element.tag):
            invoke_arguments = {}

            for child in element:
                if "Arguments" not in self._clean_tag(child.tag):
                    continue

                for argument in child:
                    key = (
                        argument.get(
                            f"{{{NAMESPACES['x']}}}Key"
                        )
                        or argument.get("Key")
                        or ""
                    )

                    if not key:
                        continue

                    direction = self._clean_tag(argument.tag)

                    value = (
                        (argument.text or "").strip()
                        or argument.get("ExpressionText", "")
                    )

                    invoke_arguments[key] = {
                        "direction": direction,
                        "value": value,
                    }

            if invoke_arguments:
                properties["InvokeArguments"] = invoke_arguments
        
        # Child property elements (e.g., Assign.To, Assign.Value)
        for child in element:
            child_tag = self._clean_tag(child.tag)

            # Check if this is a property element (contains "." indicating Activity.Property)
            if "." in child_tag:
                prop_name = child_tag.split(".")[-1]

                # Get value from text content or child elements
                value = self._get_element_value(child)
                if value:
                    properties[prop_name] = value
                else:
                    # Check for nested activity (like Assign.Value containing an expression)
                    for sub_child in child:
                        sub_value = self._get_element_value(sub_child)
                        if sub_value:
                            properties[prop_name] = sub_value
                            break

        return properties

    def _extract_selector(self, element):
        """Extract UI selector from an activity element."""
        selector = None

        # Check direct Selector attribute
        selector = element.get("Selector", "")

        # Check for selector in child elements
        if not selector:
            for child in element:
                child_tag = self._clean_tag(child.tag)
                if "Selector" in child_tag or "Target" in child_tag:
                    selector = self._get_element_value(child)
                    if selector:
                        break

                    # Check deeper for selector
                    for sub in child:
                        sub_tag = self._clean_tag(sub.tag)
                        if "Selector" in sub_tag:
                            selector = self._get_element_value(sub)
                            if selector:
                                break

        return selector if selector else None

    def _extract_expressions(self, element, properties):
        """Extract VB.NET or C# expressions from the activity."""
        expressions = {}

        # Check for expression attributes
        for key, value in element.attrib.items():
            clean_key = self._clean_tag(key)
            if self._looks_like_expression(value):
                expressions[clean_key] = value

        # Check properties for expressions
        for key, value in properties.items():
            if isinstance(value, str) and self._looks_like_expression(value):
                expressions[key] = value

        # Check for CSharpValue or VisualBasicValue elements
        for desc in element.iter():
            desc_tag = self._clean_tag(desc.tag)
            if "CSharpValue" in desc_tag or "VisualBasicValue" in desc_tag:
                expr_text = desc.get("ExpressionText", "") or (desc.text if desc.text else "")
                if expr_text:
                    expressions[f"expression_{len(expressions)}"] = expr_text
            if "CSharpReference" in desc_tag or "VisualBasicReference" in desc_tag:
                ref_text = desc.get("ExpressionText", "") or (desc.text if desc.text else "")
                if ref_text:
                    expressions[f"reference_{len(expressions)}"] = ref_text

        return expressions

    def _extract_annotation(self, element):
        """Extract annotation/comment from an activity."""
        # sap2010:Annotation.AnnotationText
        annotation = element.get(
            f"{{{NAMESPACES['sap2010']}}}Annotation.AnnotationText", ""
        )
        if not annotation:
            annotation = element.get("AnnotationText", "")
        return annotation if annotation else None

    def _extract_local_variables(self, element):
        """Extract variables defined within this activity's scope."""
        local_vars = []
        for child in element:
            child_tag = self._clean_tag(child.tag)
            if child_tag == "Variables" or "Variables" in child_tag:
                for var_child in child:
                    var_info = self._parse_variable(var_child)
                    if var_info:
                        local_vars.append(var_info)
            elif child_tag == "Variable":
                var_info = self._parse_variable(child)
                if var_info:
                    local_vars.append(var_info)
        return local_vars

    def _extract_exception_handling(self, element, activity_type):
        """Extract exception handling metadata."""
        if activity_type not in ("TryCatch", "RetryScope", "Catch", "Throw", "Rethrow"):
            return None

        eh_info = {"type": activity_type}

        if activity_type == "TryCatch":
            # Extract catch types
            catches = []
            for child in element.iter():
                child_tag = self._clean_tag(child.tag)
                if child_tag == "Catch":
                    catch_type = child.get("TypeArguments", "")
                    if not catch_type:
                        catch_type = child.get(f"{{{NAMESPACES['x']}}}TypeArguments", "")
                    catches.append(self._simplify_type(catch_type))
            eh_info["catch_types"] = catches

        elif activity_type == "RetryScope":
            eh_info["retry_count"] = element.get("NumberOfRetries", "3")
            eh_info["retry_interval"] = element.get("RetryInterval", "00:00:05")

        elif activity_type == "Throw":
            eh_info["exception_expression"] = element.get("Exception", "")

        return eh_info

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_tag(tag):
        """Remove namespace from an XML tag."""
        if "}" in tag:
            return tag.split("}")[-1]
        return tag

    @staticmethod
    def _get_display_name(element, fallback_tag):
        """Get the DisplayName attribute or use tag as fallback."""
        display_name = element.get("DisplayName", "")
        if not display_name:
            display_name = element.get(
                f"{{{NAMESPACES['sap2010']}}}WorkflowViewState.IdRef", ""
            )
        return display_name if display_name else fallback_tag

    @staticmethod
    def _simplify_type(type_string):
        """Simplify a .NET type string to a readable form."""
        if not type_string:
            return "Object"

        type_map = {
            "String": "String",
            "Int32": "Int32",
            "Int64": "Int64",
            "Boolean": "Boolean",
            "Double": "Double",
            "Decimal": "Decimal",
            "DateTime": "DateTime",
            "TimeSpan": "TimeSpan",
            "Object": "Object",
            "DataTable": "DataTable",
            "DataRow": "DataRow",
            "Array": "Array",
            "Exception": "Exception",
            "SystemException": "SystemException",
            "BusinessRuleException": "BusinessRuleException",
        }

        for key, value in type_map.items():
            if key in type_string:
                return value

        if "List" in type_string or "Collection" in type_string:
            return "List"

        if "Dictionary" in type_string:
            return "Dictionary"

        # Return last part of namespace
        parts = type_string.replace("clr-namespace:", "").split(".")
        return parts[-1] if parts else "Object"

    @staticmethod
    def _get_element_value(element):
        """Get the text value of an element, checking text and tail."""
        if element.text and element.text.strip():
            return element.text.strip()

        # Check for nested text elements
        for child in element:
            if child.text and child.text.strip():
                return child.text.strip()
            # Check expression text attribute
            expr = child.get("ExpressionText", "")
            if expr:
                return expr

        return ""

    @staticmethod
    def _looks_like_expression(value):
        """Check if a string looks like a VB.NET or C# expression."""
        if not value or not isinstance(value, str):
            return False

        indicators = [
            ".", "(", ")", "+", "&", "=", "<>",
            "New ", "CType", "CStr", "CInt", "CDbl",
            "String.Format", "DateTime", "Convert.",
            "ToString", ".Count", ".Length", ".Contains",
            "If(", "IIf(", "Not ", "And ", "Or ",
        ]

        for indicator in indicators:
            if indicator in value:
                return True

        return False

    @staticmethod
    def _resolve_activity_type(tag, element):
        """Resolve the UiPath activity type from the tag name."""
        # Check direct match
        if tag in ACTIVITY_PATTERNS:
            return ACTIVITY_PATTERNS[tag]

        # Check with common prefixes removed
        for prefix in ("ui:", "sa:", ""):
            clean = tag.replace(prefix, "")
            if clean in ACTIVITY_PATTERNS:
                return ACTIVITY_PATTERNS[clean]

        # Check TypeArguments for generic activities
        type_args = element.get(f"{{{NAMESPACES['x']}}}TypeArguments", "")
        if type_args:
            return f"{tag}<{XAMLParser._simplify_type(type_args)}>"

        return tag

    # XAML metadata / designer noise tags - never real activities
    STRUCTURAL_TAGS = {
        "Activity", "WorkflowService", "ConfigurationElement",
        "TextExpression", "TextExpressionImports",
        "AssemblyReference", "Import", "Imports",
        "Members", "WorkflowViewState", "ViewStateData",
        "ViewStateManager", "Literal", "ArgumentValue",
        "AnnotationText",
        # Primitive type elements (namespace lists, references)
        "Null", "String", "Boolean", "Int32", "Int64", "Double",
        "Decimal", "Object", "DateTime", "TimeSpan", "Guid",
        "Byte", "Char", "Single", "Type", "Uri",
        "List", "Dictionary", "Array", "Collection", "KeyedCollection",
        # Workflow argument/delegate wrappers (NOT activities)
        "ActivityAction", "ActivityFunc",
        "DelegateInArgument", "DelegateOutArgument",
        "OutArgument", "InArgument", "InOutArgument",
        # Expression holders
        "VisualBasicValue", "VisualBasicReference",
        "CSharpValue", "CSharpReference",
        # Designer / metadata elements
        "VisualBasic.Settings",
        "TextExpression.NamespacesForImplementation",
        "TextExpression.ReferencesForImplementation",
        "WorkflowViewState.IdRef",
        "VirtualizedContainerService.HintSize",
        "WorkflowViewStateService.ViewState",
        "WorkflowItemPresenter",
        "DynamicActivityProperty",
        "RetryScope.ActivityBody",
        
        # Designer canvas shapes / coordinates (av: namespace)
        "Point", "Size", "PointCollection", "Rectangle",
        "Reference", "Double", "Single",
    }

    STRUCTURAL_PREFIXES = (
        "VirtualizedContainerService.",
        "WorkflowViewState",
        "TextExpression.",
        "VisualBasic.",
        "Annotation.",
        "WorkflowItemPresenter",
        "ActivityAction",
        "ActivityFunc",
        "DelegateInArgument",
        "DelegateOutArgument",
        "OutArgument",
        "InArgument",
        "InOutArgument",
    )

    @staticmethod
    def _is_structural_only(tag):
        """Check if a tag is purely structural with no activity meaning."""
        if tag in XAMLParser.STRUCTURAL_TAGS:
            return True
        return tag.startswith(XAMLParser.STRUCTURAL_PREFIXES)

    @staticmethod
    def _is_activity_container_property(tag):
        """Check if a tag represents an activity container property."""
        container_patterns = [
            ".Then", ".Else", ".Body", ".Action",
            ".Try", ".Catches", ".Finally",
            ".Activities", ".Branches", ".Cases",
            ".Default", ".Condition",
            ".Entry", ".Transitions", ".To",
            ".True", ".False", ".Next", ".StartNode",
        ]
        for pattern in container_patterns:
            if tag.endswith(pattern) or pattern in tag:
                return True
        return False

    @staticmethod
    def _determine_container_type(activity_type):
        """Determine if an activity is a container and what type."""
        containers = {
            "Sequence": "Sequence",
            "Flowchart": "Flowchart",
            "If": "Conditional",
            "Switch": "Switch",
            "ForEach": "Loop",
            "While": "Loop",
            "DoWhile": "Loop",
            "TryCatch": "ExceptionHandler",
            "RetryScope": "RetryHandler",
            "Parallel": "Parallel",
            "Pick": "Pick",
            "ExcelApplicationScope": "Scope",
            "AttachBrowser": "Scope",
            "OpenBrowser": "Scope",
        }
        return containers.get(activity_type, None)

    @staticmethod
    def _detect_variables_used(properties, expressions):
        """Detect variable names referenced in properties and expressions."""
        variables_used = set()
        vb_var_pattern = re.compile(r'\b([A-Za-z_]\w*)\b')

        all_values = list(properties.values()) + list(expressions.values())

        for value in all_values:
            if not isinstance(value, str):
                continue
            matches = vb_var_pattern.findall(value)
            for match in matches:
                # Filter out common keywords and literals
                if match.lower() not in (
                    "true", "false", "nothing", "null", "new",
                    "string", "integer", "boolean", "double", "decimal",
                    "object", "datetime", "if", "then", "else",
                    "and", "or", "not", "mod", "is", "isnot",
                    "cstr", "cint", "cdbl", "ctype", "typeof",
                    "throw", "catch", "try", "finally",
                    "for", "each", "in", "next", "while", "do",
                    "dim", "as", "byval", "byref",
                    "format", "convert", "tostring", "length",
                    "count", "contains", "trim", "replace",
                    "split", "join", "substring", "indexof",
                    "now", "today", "year", "month", "day",
                ):
                    variables_used.add(match)

        return list(variables_used)


def parse_xaml(xaml_path):
    """Convenience function to parse a single XAML file.

    Args:
        xaml_path: Path to .xaml file

    Returns:
        dict: Parsed tree structure
    """
    parser = XAMLParser()
    return parser.parse(xaml_path)


def parse_xaml_files(xaml_paths):
    """Convenience function to parse multiple XAML files.

    Args:
        xaml_paths: List of paths

    Returns:
        list: List of parsed tree structures
    """
    parser = XAMLParser()
    return parser.parse_multiple(xaml_paths)