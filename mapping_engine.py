import csv
import json
import logging
import re
from pathlib import Path
from config import Config
from llm_client import get_llm_client

logger = logging.getLogger(__name__)


class MappingEngine:
    """Maps UiPath actions to target platform actions.

    Priority order:
    1. Exact match from mapping_sheet.csv (deterministic)
    2. Pattern-based inference (deterministic)
    3. LLM inference for unmapped actions (non-deterministic, last resort)

    Each mapped action gets:
    - source_action: original UiPath action type
    - target_action: mapped PAD Robin action
    - confidence: high | medium | low
    - mapping_source: sheet | pattern | llm
    - parameter_mapping: source param -> target param
    - notes: any relevant notes
    """

    def __init__(self):
        self.mapping_sheet = {}
        self.pad_schema = []
        self.pad_schema_index = {}
        self._load_mapping_sheet()
        self._load_pad_schema()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_mapping_sheet(self):
        """Load the UiPath -> PAD mapping sheet from CSV.

        CSV columns:
        uipath_activity, uipath_type, power_automate_desktop_action,
        aa360_action, difficulty, notes, complex_handling

        Builds a dict keyed by uipath_activity.
        """
        path = Config.MAPPING_SHEET_PATH

        if not path.exists():
            logger.warning(f"Mapping sheet not found at {path}")
            self.mapping_sheet = {}
            return

        try:
            self.mapping_sheet = {}

            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)

                for row in reader:
                    uipath_activity = (row.get("uipath_activity") or "").strip()
                    if not uipath_activity:
                        continue

                    uipath_type = (row.get("uipath_type") or "").strip()
                    pad_action = (row.get("power_automate_desktop_action") or "").strip()
                    aa360_action = (row.get("aa360_action") or "").strip()
                    difficulty = (row.get("difficulty") or "Medium").strip()
                    notes = (row.get("notes") or "").strip()
                    complex_handling = (row.get("complex_handling") or "").strip()

                    # Map difficulty to confidence
                    confidence = self._difficulty_to_confidence(difficulty)

                    self.mapping_sheet[uipath_activity] = {
                        "target_action": pad_action,
                        "aa360_action": aa360_action,
                        "uipath_type": uipath_type,
                        "confidence": confidence,
                        "difficulty": difficulty,
                        "notes": notes,
                        "complex_handling": complex_handling,
                        "parameter_mapping": {},  # CSV doesn't carry param mapping
                    }

            logger.info(f"Mapping sheet loaded (CSV): {len(self.mapping_sheet)} entries")

        except Exception as e:
            logger.error(f"Failed to load mapping sheet CSV: {e}")
            self.mapping_sheet = {}

    def _load_pad_schema(self):
        """Load the PAD Robin action schema from pad_1lm_schema.json."""
        path = Config.PAD_SCHEMA_PATH

        if not path.exists():
            logger.error(f"PAD schema not found at {path}")
            self.pad_schema = []
            self.pad_schema_index = {}
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                self.pad_schema = json.load(f)

            # Build index by ActionId
            self.pad_schema_index = {}
            self._action_id_lower = {}

            for entry in self.pad_schema:
                action_id = entry.get("ActionId", "")
                if action_id:
                    self.pad_schema_index[action_id] = entry
                    self._action_id_lower[action_id.lower()] = entry

            logger.info(f"PAD schema loaded: {len(self.pad_schema_index)} actions indexed")

        except Exception as e:
            logger.error(f"Failed to load PAD schema: {e}")
            self.pad_schema = []
            self.pad_schema_index = {}

    @staticmethod
    def _difficulty_to_confidence(difficulty):
        """Convert CSV difficulty level to confidence level.

        Low difficulty    = high confidence (easy to map)
        Medium difficulty = medium confidence
        High difficulty   = low confidence (hard to map)
        """
        mapping = {
            "low": "high",
            "medium": "medium",
            "high": "low",
            "very high": "low",
        }
        return mapping.get(difficulty.lower(), "medium")

    # ------------------------------------------------------------------
    # Main mapping interface
    # ------------------------------------------------------------------

    def map_action(self, ir_action):
        """Map a single IR action to its target platform equivalent.

        Args:
            ir_action: Single action dict from IR JSON

        Returns:
            dict: Mapping result
        """
        source_type = ir_action.get("action_type", "")
        display_name = ir_action.get("display_name", "")
        properties = ir_action.get("properties", {})

        # Structural containers: ALWAYS deterministic, CSV can never override
        structural = {
            "StateMachine": "BLOCK:StateMachine",
            "State": "BLOCK:State",
            "Transition": "BLOCK:Transition",
            "Sequence": "BLOCK:Sequence",
            "Flowchart": "BLOCK:Flowchart",
        }
        if source_type in structural:
            return self._build_mapping_result(
                ir_action=ir_action,
                target_action=structural[source_type],
                confidence="high",
                mapping_source="pattern",
                parameter_mapping={},
                notes="Structural container",
                robin_skeleton=None,
            )

        # Orchestrator-only activities: no PAD equivalent -> comment
        orchestrator_only = {
            "ShouldStop": "Orchestrator stop signal - no PAD equivalent",
            "AddLogFields": "Orchestrator log fields - no PAD equivalent",
            "GetAsset": "Orchestrator asset - replace with PAD variable/credential",
            "GetCredential": "Orchestrator credential - replace with PAD credential",
            "GetTransactionItem": "Orchestrator queue transaction - no PAD equivalent; replace with work queue source (Excel/DB/API)",
            "GetQueueItems": "Orchestrator queue items - no PAD equivalent; replace with work queue source",
            "AddQueueItem": "Orchestrator add queue item - no PAD equivalent",
            "GetQueueItem": "Orchestrator queue transaction - no PAD equivalent; replace with work queue source (Excel/DB/API)",
            "GetRobotAsset": "Orchestrator asset - no PAD equivalent; replace with PAD variable/credential/secure file",
            "SetTransactionStatus": "Orchestrator set-transaction-status - no PAD equivalent; log the status instead",
            "RemoveLogFields": "Orchestrator log fields - no PAD equivalent",
        }
        if source_type in orchestrator_only:
            return self._build_mapping_result(
                ir_action=ir_action,
                target_action="COMMENT",
                confidence="medium",
                mapping_source="pattern",
                parameter_mapping={},
                notes=orchestrator_only[source_type],
                robin_skeleton=None,
            )

        
        # Step 1: Check mapping sheet for exact match
        mapping = self._lookup_mapping_sheet(
            source_type,
            display_name,
        )

        if mapping:
            normalized_source = re.sub(
                r"[^a-z0-9]",
                "",
                source_type.lower(),
            )

            text_entry_source_types = {
                "typeinto",
                "settext",
                "typetext",
                "entertext",
            }

            # UiPath TypeInto/SetText must use a normal UI/web text-entry
            # action. Do not use generic DisplayName matching because SAP
            # contains an action with a similar display name.
            if normalized_source in text_entry_source_types:
                target = self._resolve_standard_text_entry_action()

                if not target:
                    target = "UNMAPPED"

                parameter_mapping = dict(
                    mapping.get("parameter_mapping", {})
                )

                if target != "UNMAPPED":
                    schema_entry = self._get_robin_skeleton(target)
                    schema_input_names = {
                        str(item.get("Name") or "")
                        for item in (
                            schema_entry.get("Inputs", [])
                            if schema_entry
                            else []
                        )
                    }

                    # Map UiPath's text property to the exact text parameter
                    # exposed by the selected PAD action.
                    for target_text_parameter in (
                        "Text",
                        "TextToWrite",
                        "TextValue",
                        "Value",
                    ):
                        if target_text_parameter in schema_input_names:
                            parameter_mapping["Text"] = (
                                target_text_parameter
                            )
                            break

                notes = (
                    mapping.get("notes", "")
                    + " | Forced to non-SAP UI/web text-entry action."
                ).strip(" |")

            else:
                target = self.resolve_pad_action_id(
                    mapping["target_action"]
                )
                parameter_mapping = mapping.get(
                    "parameter_mapping",
                    {},
                )
                notes = mapping.get("notes", "")

            # Final safety rule: a normal UiPath activity cannot become an
            # SAP action unless the IR explicitly confirms an SAP target.
            if target and target.lower().startswith("sap."):
                target_app = str(
                    ir_action.get("target_app") or ""
                ).lower()

                selector = str(
                    ir_action.get("selector") or ""
                ).lower()

                is_confirmed_sap = (
                    target_app == "sap"
                    or "saplogon" in selector
                    or "sap gui" in selector
                    or "sap.exe" in selector
                )

                if not is_confirmed_sap:
                    logger.warning(
                        "Rejected SAP mapping '%s' for non-SAP source "
                        "activity '%s'",
                        target,
                        source_type,
                    )

                    if normalized_source in text_entry_source_types:
                        target = (
                            self._resolve_standard_text_entry_action()
                            or "UNMAPPED"
                        )
                    else:
                        target = "UNMAPPED"

            logger.debug(
                "Sheet match: %s -> %s",
                source_type,
                target,
            )

            return self._build_mapping_result(
                ir_action=ir_action,
                target_action=target,
                confidence=mapping.get(
                    "confidence",
                    "high",
                ),
                mapping_source="sheet",
                parameter_mapping=parameter_mapping,
                notes=notes,
                robin_skeleton=self._get_robin_skeleton(target),
            )

        # Step 2: Try pattern-based inference
        pattern_result = self._pattern_inference(source_type, properties, display_name)
        if pattern_result:
            target = pattern_result["target_action"]
            logger.debug(f"Pattern match: {source_type} -> {target}")
            return self._build_mapping_result(
                ir_action=ir_action,
                target_action=target,
                confidence=pattern_result.get("confidence", "medium"),
                mapping_source="pattern",
                parameter_mapping=pattern_result.get("parameter_mapping", {}),
                notes=pattern_result.get("notes", "Pattern-based inference"),
                robin_skeleton=self._get_robin_skeleton(target),
            )

        # Step 3: LLM inference as last resort
        llm_result = self._llm_inference(source_type, properties)
        target = llm_result.get("target_action", "UNMAPPED")
        logger.debug(
            f"LLM inference: {source_type} -> {target} "
            f"(confidence: {llm_result.get('confidence', 'low')})"
        )
        return self._build_mapping_result(
            ir_action=ir_action,
            target_action=target,
            confidence=llm_result.get("confidence", "low"),
            mapping_source="llm",
            parameter_mapping=llm_result.get("parameter_mapping", {}),
            notes=llm_result.get("reasoning", "LLM inference"),
            robin_skeleton=self._get_robin_skeleton(target),
        )

    def map_all_actions(self, ir_data):
        """Map all actions from an IR JSON.

        Args:
            ir_data: Full IR JSON dict (single workflow or combined)

        Returns:
            dict: {mappings, summary, unmapped}
        """
        # Handle combined IR (multiple workflows)
        if "workflows" in ir_data:
            all_mappings = []
            for workflow in ir_data["workflows"]:
                actions = workflow.get("actions", [])
                for action in actions:
                    mapping = self.map_action(action)
                    mapping["workflow_name"] = workflow.get("workflow_name", "")
                    all_mappings.append(mapping)
        else:
            actions = ir_data.get("actions", [])
            all_mappings = []
            for action in actions:
                mapping = self.map_action(action)
                mapping["workflow_name"] = ir_data.get("workflow_name", "")
                all_mappings.append(mapping)

        summary = self._build_mapping_summary(all_mappings)

        result = {
            "mappings": all_mappings,
            "summary": summary,
            "unmapped": summary.get("unmapped_actions", []),
        }

        logger.info(
            f"Mapping complete: {summary['total']} actions, "
            f"{summary['high_confidence']} high, "
            f"{summary['medium_confidence']} medium, "
            f"{summary['low_confidence']} low, "
            f"{summary['unmapped_count']} unmapped"
        )

        return result

    # ------------------------------------------------------------------
    # Step 1: Mapping sheet lookup
    # ------------------------------------------------------------------

    def _lookup_mapping_sheet(self, source_type, display_name=""):
        """Look up a source action in the CSV mapping sheet.

        Tries in order:
        1. Exact match by action_type
        2. Case-insensitive match by action_type
        3. Match by display_name
        4. Match by base type (strip generic params)
        5. Partial/contains match

        Returns:
            dict or None
        """
        if not self.mapping_sheet:
            return None

        # 1. Exact match
        if source_type in self.mapping_sheet:
            entry = self.mapping_sheet[source_type]
            if entry.get("target_action"):
                return entry

        # 2. Case-insensitive match
        source_lower = source_type.lower()
        for key, value in self.mapping_sheet.items():
            if key.lower() == source_lower and value.get("target_action"):
                return value

        # 3. Match by display_name
        if display_name:
            display_lower = display_name.lower()
            for key, value in self.mapping_sheet.items():
                if key.lower() == display_lower and value.get("target_action"):
                    return value

        # 4. Strip generic type arguments: "ForEach<String>" -> "ForEach"
        base_type = source_type.split("<")[0].split("(")[0].strip()
        if base_type != source_type:
            if base_type in self.mapping_sheet:
                entry = self.mapping_sheet[base_type]
                if entry.get("target_action"):
                    return entry
            for key, value in self.mapping_sheet.items():
                if key.lower() == base_type.lower() and value.get("target_action"):
                    return value

        # 5. Partial match: source_type contains or is contained in a key
        for key, value in self.mapping_sheet.items():
            key_lower = key.lower()
            if (source_lower in key_lower or key_lower in source_lower) and value.get("target_action"):
                return value

        return None

    # ------------------------------------------------------------------
    # Step 2: Pattern-based inference
    # ------------------------------------------------------------------

    def _pattern_inference(self, source_type, properties, display_name):
        """Infer target action using deterministic patterns.

        Handles common UiPath actions that follow predictable naming.

        Returns:
            dict or None
        """
        source_lower = source_type.lower()

        # Control flow patterns
        control_flow_map = {
            "sequence": {
                "target_action": "BLOCK:Sequence",
                "confidence": "high",
                "notes": "Sequence container",
            },
            "flowchart": {
                "target_action": "BLOCK:Flowchart",
                "confidence": "high",
                "notes": "Flowchart container",
            },
            "if": {
                "target_action": "Conditionals.If",
                "confidence": "high",
                "parameter_mapping": {"Condition": "Condition"},
            },
            "switch": {
                "target_action": "Conditionals.Switch",
                "confidence": "high",
                "parameter_mapping": {"Expression": "Value"},
            },
            "while": {
                "target_action": "Loops.Loop",
                "confidence": "high",
                "parameter_mapping": {"Condition": "Condition"},
            },
            "dowhile": {
                "target_action": "Loops.Loop",
                "confidence": "high",
                "parameter_mapping": {"Condition": "Condition"},
                "notes": "DoWhile converted to Loop",
            },
            "foreach": {
                "target_action": "Loops.ForEach",
                "confidence": "high",
                "parameter_mapping": {"Values": "List", "CurrentItem": "CurrentItem"},
            },
                        "foreachrow": {
                "target_action": "Loops.ForEach",
                "confidence": "high",
                "parameter_mapping": {"DataTable": "List", "CurrentRow": "CurrentItem"},
            },
            "flowdecision": {
                "target_action": "Conditionals.If",
                "confidence": "high",
                "parameter_mapping": {"Condition": "Condition"},
            },
            "flowstep": {
                "target_action": "BLOCK:FlowStep",
                "confidence": "high",
                "notes": "Flowchart step - sequential passthrough",
            },
        }

        for pattern, result in control_flow_map.items():
            if source_lower == pattern or source_lower.startswith(pattern):
                return result

        # Block markers
        block_patterns = {
            "block_then": {"target_action": "BLOCK:Then", "confidence": "high"},
            "block_else": {"target_action": "BLOCK:Else", "confidence": "high"},
            "block_try": {"target_action": "BLOCK:Try", "confidence": "high"},
            "block_finally": {"target_action": "BLOCK:Finally", "confidence": "high"},
            "block_body": {"target_action": "BLOCK:Body", "confidence": "high"},
            "block_action": {"target_action": "BLOCK:Action", "confidence": "high"},
            "container": {"target_action": "BLOCK:Container", "confidence": "high"},
        }

        for pattern, result in block_patterns.items():
            if source_lower == pattern:
                return result

        # Exception handling
        exception_map = {
            "trycatch": {
                "target_action": "ErrorHandling.BeginException",
                "confidence": "high",
            },
            "catch": {
                "target_action": "ErrorHandling.BeginException",
                "confidence": "high",
                "notes": "Catch block within TryCatch",
            },
            "throw": {
                "target_action": "ErrorHandling.ThrowError",
                "confidence": "high",
                "parameter_mapping": {"Exception": "Message"},
            },
            "rethrow": {
                "target_action": "ErrorHandling.ThrowError",
                "confidence": "medium",
                "notes": "Rethrow mapped to ThrowError",
            },
            "retryscope": {
                "target_action": "ErrorHandling.BeginException",
                "confidence": "medium",
                "notes": "RetryScope has no direct PAD equivalent",
            },
        }

        for pattern, result in exception_map.items():
            if source_lower == pattern:
                return result

        # Assignment
        if source_lower == "assign":
            return {
                "target_action": "Variables.SetVariable",
                "confidence": "high",
                "parameter_mapping": {"To": "Name", "Value": "Value"},
            }

        if source_lower == "multipleassign":
            return {
                "target_action": "Variables.SetVariable",
                "confidence": "high",
                "notes": "MultipleAssign split into multiple SetVariable",
            }

        # Dialogs
        if source_lower == "logmessage":
            return {
                "target_action": "Display.ShowMessageDialog",
                "confidence": "medium",
                "parameter_mapping": {"Message": "Message"},
                "notes": "LogMessage mapped to ShowMessageDialog",
            }

        if source_lower == "messagebox":
            return {
                "target_action": "Display.ShowMessageDialog",
                "confidence": "high",
                "parameter_mapping": {"Text": "Message", "Caption": "Title"},
            }

        if source_lower == "inputdialog":
            return {
                "target_action": "Display.InputDialog",
                "confidence": "high",
                "parameter_mapping": {"Title": "Title", "Label": "Message"},
            }

        # Comment
        if source_lower == "comment":
            return {
                "target_action": "COMMENT",
                "confidence": "high",
                "parameter_mapping": {"Text": "Comment"},
            }

        # Delay
        if source_lower == "delay":
            return {
                "target_action": "System.Wait",
                "confidence": "high",
                "parameter_mapping": {"Duration": "Duration"},
            }

        # Invoke
        if source_lower in ("invokeworkflowfile", "invokeworkflow"):
            return {
                "target_action": "Flow.RunSubflow",
                "confidence": "high",
                "parameter_mapping": {"WorkflowFileName": "SubflowName"},
            }
        
        # State machine containers (PAD has no state machine - flatten with comments)
        if source_lower == "statemachine":
            return {
                "target_action": "BLOCK:StateMachine",
                "confidence": "high",
                "notes": "StateMachine flattened; transitions documented as comments",
            }
        if source_lower == "state":
            return {"target_action": "BLOCK:State", "confidence": "high"}
        if source_lower == "transition":
            return {"target_action": "BLOCK:Transition", "confidence": "high"}

        # Terminate workflow
        if source_lower == "terminateworkflow":
            return {
                "target_action": "ErrorHandling.ThrowError",
                "confidence": "medium",
                "parameter_mapping": {"Exception": "Message"},
                "notes": "TerminateWorkflow ends execution; mapped to ThrowError",
            }
            
        # File / image operations
        if source_lower == "createdirectory":
            return {
                "target_action": "Folder.Create",
                "confidence": "medium",
                "parameter_mapping": {"Path": "Folder"},
            }
        if source_lower == "saveimage":
            return {
                "target_action": "COMMENT",
                "confidence": "medium",
                "notes": "SaveImage - PAD: pass a file path to Workstation.TakeScreenshot or save %Screenshot% via a script",
            }
        # No pattern match
        return None

    # ------------------------------------------------------------------
    # Step 3: LLM inference
    # ------------------------------------------------------------------

    def _llm_inference(self, source_type, properties):
        """Use LLM to infer target action. Last resort only."""
        try:
            client = get_llm_client()
            result = client.infer_action_mapping(source_type, properties, "PAD")
            return result
        except Exception as e:
            logger.error(f"LLM inference failed for {source_type}: {e}")
            return {
                "target_action": "UNMAPPED",
                "confidence": "low",
                "reasoning": f"LLM inference failed: {e}",
                "parameter_mapping": {},
            }

    # ------------------------------------------------------------------
    # Robin skeleton lookup from pad_1lm_schema.json
    # ------------------------------------------------------------------

    def _get_robin_skeleton(self, target_action):
        """Look up Robin script skeleton from PAD schema.

        The pad_1lm_schema.json is the ONLY authoritative source.

        Tries:
        1. Exact ActionId match
        2. Case-insensitive match
        3. Suffix/partial match

        Returns:
            dict: Schema entry or None
        """
        if not target_action:
            return None
        if target_action.startswith("BLOCK:") or target_action in ("UNMAPPED", "COMMENT"):
            return None
        if not self.pad_schema_index:
            return None

        # 1. Exact match
        if target_action in self.pad_schema_index:
            return self.pad_schema_index[target_action]

        # 2. Case-insensitive match
        lower = target_action.lower()
        if lower in self._action_id_lower:
            return self._action_id_lower[lower]

        # 3. Partial match: find ActionId that contains the target
        for action_id, entry in self.pad_schema_index.items():
            if target_action in action_id or action_id in target_action:
                return entry

        # 4. Suffix match: match last segment
        target_suffix = target_action.split(".")[-1].lower()
        for action_id, entry in self.pad_schema_index.items():
            if action_id.split(".")[-1].lower() == target_suffix:
                return entry

        logger.debug(f"No Robin skeleton found for: {target_action}")
        return None

    def _resolve_standard_text_entry_action(self):
        """Resolve the normal UI/web Populate Text Field action.

        SAP actions are explicitly excluded. This method is used for UiPath
        TypeInto and SetText activities unless the source is confirmed as SAP.
        """

        # First try trusted ActionIds commonly used by PAD schemas.
        preferred_action_ids = (
            "UIAutomation.PopulateTextField",
            "WebAutomation.PopulateTextField",
            "UIAutomation.PopulateTextFieldInWindow",
            "WebAutomation.PopulateTextFieldOnWebPage",
        )

        for action_id in preferred_action_ids:
            if action_id in self.pad_schema_index:
                return action_id

            action_lower = action_id.lower()
            if action_lower in self._action_id_lower:
                return self._action_id_lower[action_lower]["ActionId"]

        # Fall back to schema search, but explicitly reject SAP actions.
        candidates = []

        for entry in self.pad_schema:
            action_id = (entry.get("ActionId") or "").strip()
            display_name = (entry.get("DisplayName") or "").strip()

            if not action_id:
                continue

            action_lower = action_id.lower()
            display_lower = display_name.lower()
            searchable = f"{action_lower} {display_lower}"

            # UiPath TypeInto for a normal browser/window must never map to SAP.
            if action_lower.startswith("sap."):
                continue

            is_text_entry_action = (
                "populate" in searchable
                and "text" in searchable
                and "field" in searchable
            )

            if not is_text_entry_action:
                continue

            # Deterministic namespace priority.
            if action_lower.startswith("uiautomation."):
                score = 100
            elif action_lower.startswith("webautomation."):
                score = 90
            elif "window" in searchable:
                score = 80
            elif "web" in searchable:
                score = 70
            else:
                score = 50

            candidates.append((score, action_id))

        if not candidates:
            logger.error(
                "No non-SAP Populate Text Field action was found in PAD schema"
            )
            return None

        candidates.sort(
            key=lambda item: (-item[0], item[1].lower())
        )

        selected_action = candidates[0][1]

        logger.info(
            "Resolved standard text-entry action to: %s",
            selected_action,
        )

        return selected_action
    
    # ------------------------------------------------------------------
    # Resolve PAD action from CSV mapping value
    # ------------------------------------------------------------------

    def resolve_pad_action_id(self, csv_pad_action):
        """Resolve a CSV PAD action name to exact ActionId in schema.

        Strategy order:
        1. Exact ActionId match
        2. Case-insensitive ActionId match
        3. Exact DisplayName match
        4. Curated known-mappings dict
        5. Token-overlap fuzzy match (handles any human-readable name)
        6. Partial substring match
        """
        if not csv_pad_action:
            return ""

        # 1. Direct schema match
        if csv_pad_action in self.pad_schema_index:
            return csv_pad_action

        csv_lower = csv_pad_action.lower().strip()

        # 2. Case-insensitive ActionId match
        if csv_lower in self._action_id_lower:
            return self._action_id_lower[csv_lower]["ActionId"]

        # 3. Curated mappings must be checked before DisplayName matching.
        # Multiple PAD modules can expose the same DisplayName, including SAP.
        known = self._get_known_mappings()

        if csv_lower in known:
            known_action = known[csv_lower]

            synthetic_targets = {
                "COMMENT",
                "UNMAPPED",
                "ErrorHandling.BeginException",
            }

            if (
                known_action.startswith("BLOCK:")
                or known_action in synthetic_targets
            ):
                return known_action

            if known_action in self.pad_schema_index:
                return known_action

            known_lower = known_action.lower()

            if known_lower in self._action_id_lower:
                return self._action_id_lower[
                    known_lower
                ]["ActionId"]

            # If the hardcoded ActionId differs in this schema version,
            # dynamically resolve the normal non-SAP action.
            if csv_lower in {
                "type into",
                "typeinto",
                "set text",
                "populate text field",
                "populate text field in window",
                "populate text field on window",
            }:
                text_action = (
                    self._resolve_standard_text_entry_action()
                )

                if text_action:
                    return text_action

        # 4. Exact DisplayName match, only when unambiguous.
        display_matches = []

        for entry in self.pad_schema:
            display = (
                entry.get("DisplayName") or ""
            ).strip().lower()

            if display and display == csv_lower:
                display_matches.append(entry)

        if len(display_matches) == 1:
            return display_matches[0]["ActionId"]

        if len(display_matches) > 1:
            non_sap_matches = [
                entry
                for entry in display_matches
                if not (
                    entry.get("ActionId", "")
                    .lower()
                    .startswith("sap.")
                )
            ]

            if len(non_sap_matches) == 1:
                return non_sap_matches[0]["ActionId"]

            logger.warning(
                "Ambiguous PAD DisplayName '%s'. Candidates: %s",
                csv_pad_action,
                [
                    entry.get("ActionId", "")
                    for entry in display_matches
                ],
            )

        # 5. Token-overlap fuzzy match
        csv_tokens = {t for t in re.split(r"[^a-z0-9]+", csv_lower) if len(t) > 2}
        if csv_tokens:
            best_score = 0.0
            best_id = None
            for entry in self.pad_schema:
                display = (entry.get("DisplayName") or "").lower()
                action_id = entry.get("ActionId", "").lower().replace(".", " ")
                target_tokens = {t for t in re.split(r"[^a-z0-9]+", display + " " + action_id) if len(t) > 2}
                if not target_tokens:
                    continue
                overlap = len(csv_tokens & target_tokens)
                if overlap == 0:
                    continue
                score = overlap / len(csv_tokens)
                if score > best_score:
                    best_score = score
                    best_id = entry["ActionId"]
            if best_score >= 0.75 and best_id:
                logger.debug(f"Fuzzy resolved '{csv_pad_action}' -> {best_id} (score {best_score:.2f})")
                return best_id

        # 6. Partial substring match on DisplayName
        for entry in self.pad_schema:
            display = (entry.get("DisplayName") or "").lower()
            if display and (csv_lower in display or display in csv_lower):
                return entry["ActionId"]

        logger.debug(f"Could not resolve CSV PAD action: '{csv_pad_action}'")
        return csv_pad_action

    @staticmethod
    def _get_known_mappings():
        """Curated CSV-name -> schema ActionId mappings."""
        return {
            # Containers / control flow
            "subflow": "BLOCK:Subflow",
            "step": "BLOCK:Subflow",
            "if": "Conditionals.If",
            "else": "Conditionals.Else",
            "else if": "Conditionals.ElseIf",
            "switch": "Conditionals.Switch",
            "case": "Conditionals.Case",
            "default case": "Conditionals.DefaultCase",
            "for each": "Loops.ForEach",
            "loop": "Loops.Loop",
            "loop condition": "Loops.Loop",
            "while loop": "Loops.Loop",
            "exit loop": "Loops.ExitLoop",
            "next loop": "Loops.NextLoop",
            # Variables
            "set variable": "Variables.SetVariable",
            "increase variable": "Variables.IncreaseVariable",
            "decrease variable": "Variables.DecreaseVariable",
            "generate random number": "Variables.GenerateRandomNumber",
            "create new list": "Variables.CreateNewList",
            "create new data table": "Variables.CreateNewDatatable",
            "add row to data table": "Variables.AddRowToDataTable",
            "delete row from data table": "Variables.DeleteRowFromDataTable",
            # Display / dialogs
            "display message": "Display.ShowMessageDialog",
            "display message dialog": "Display.ShowMessageDialog",
            "display message box": "Display.ShowMessageDialog",
            "show message": "Display.ShowMessageDialog",
            "message box": "Display.ShowMessageDialog",
            "log message": "Display.ShowMessageDialog",
            "display input dialog": "Display.InputDialog",
            "input dialog": "Display.InputDialog",
            "display select file dialog": "Display.SelectFileDialog",
            "display select folder dialog": "Display.SelectFolderDialog",
            # Excel
            "launch excel": "Excel.LaunchExcel",
            "close excel": "Excel.CloseExcel",
            "read range": "Excel.ReadFromExcel",
            "read from excel": "Excel.ReadFromExcel",
            "read from excel worksheet": "Excel.ReadFromExcel",
            "write range": "Excel.WriteToExcel",
            "write to excel": "Excel.WriteToExcel",
            "write to excel worksheet": "Excel.WriteToExcel",
            "read cell": "Excel.ReadFromExcel",
            "write cell": "Excel.WriteToExcel",
            "append range": "Excel.WriteToExcel",
            "run excel macro": "Excel.RunMacro",
            "save excel": "Excel.SaveExcel",
            # File
            "read text file": "File.ReadTextFromFile",
            "read text from file": "File.ReadTextFromFile",
            "write text file": "File.WriteTextToFile",
            "write text to file": "File.WriteTextToFile",
            "copy file": "File.Copy",
            "copy files": "File.Copy",
            "move file": "File.Move",
            "move files": "File.Move",
            "delete file": "File.Delete",
            "delete files": "File.Delete",
            "if file exists": "File.IfFileExists",
            "get files in folder": "Folder.GetFiles",
            "create folder": "Folder.Create",
            "delete folder": "Folder.Delete",
            # Web
            "launch chrome": "WebAutomation.LaunchChrome",
            "launch firefox": "WebAutomation.LaunchFirefox",
            "launch edge": "WebAutomation.LaunchEdge",
            "open browser": "WebAutomation.LaunchChrome",
            "go to web page": "WebAutomation.GoToWebPage",
            "navigate to": "WebAutomation.GoToWebPage",
            "close web browser": "WebAutomation.CloseWebBrowser",
            "close browser": "WebAutomation.CloseWebBrowser",
            # UI
            "click": "MouseAndKeyboard.Click",
            "click ui element in window": "UIAutomation.Click",
            "type into": "UIAutomation.PopulateTextField",
            "typeinto": "UIAutomation.PopulateTextField",
            "set text": "UIAutomation.PopulateTextField",
            "populate text field": "UIAutomation.PopulateTextField",
            "populate text field in window": "UIAutomation.PopulateTextField",
            "populate text field on window": "UIAutomation.PopulateTextField",
            "get text": "UIAutomation.GetDetailsOfUiElement",
            "get details of ui element in window": "UIAutomation.GetDetailsOfUiElement",
            "send hotkey": "MouseAndKeyboard.SendKeys",
            "send keys": "MouseAndKeyboard.SendKeys",
            # System
            "delay": "System.Wait",
            "wait": "System.Wait",
            "run application": "System.RunApplication",
            "run powershell script": "Scripting.RunPowershellScript",
            # Email
            "send smtp mail message": "Email.SendEmailThroughSmtp",
            "send email": "Email.SendEmailThroughSmtp",
            # Error handling
            "try catch": "ErrorHandling.BeginException",
            "begin error handling": "ErrorHandling.BeginException",
            "for each row": "Loops.ForEach",
            "begin exception block": "ErrorHandling.BeginException",
            "throw": "ErrorHandling.ThrowError",
            "throw error": "ErrorHandling.ThrowError",
            "throw": "ErrorHandling.ThrowError",
            "throw exception": "ErrorHandling.ThrowError",
            "begin error handling": "ErrorHandling.BeginException",
            "run desktop flow": "Flow.RunSubflow",
            "invoke workflow file": "Flow.RunSubflow",
            "terminate flow": "ErrorHandling.ThrowError",
            "manual review required": "COMMENT",
            "rethrow": "ErrorHandling.ThrowError",
            # Invoke
            "invoke workflow file": "Flow.RunSubflow",
            # Comment
            "comment": "COMMENT",
        }

    # ------------------------------------------------------------------
    # Result building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_mapping_result(ir_action, target_action, confidence,
                               mapping_source, parameter_mapping,
                               notes, robin_skeleton):
        """Build a standardized mapping result dict."""
        return {
            "action_id": ir_action.get("action_id", ""),
            "source_action": ir_action.get("action_type", ""),
            "display_name": ir_action.get("display_name", ""),
            "target_action": target_action,
            "confidence": confidence,
            "mapping_source": mapping_source,
            "parameter_mapping": parameter_mapping,
            "robin_skeleton": robin_skeleton,
            "notes": notes,
            "source_properties": ir_action.get("properties", {}),
            "source_expressions": ir_action.get("expressions", {}),
            "source_selector": ir_action.get("selector"),
            "source_variables_used": ir_action.get("variables_used", []),
            "source_exception_handling": ir_action.get("exception_handling"),
            "container_type": ir_action.get("container_type"),
            "child_ids": ir_action.get("child_ids", []),
            "parent_id": ir_action.get("parent_id"),
            "order": ir_action.get("order", 0),
        }

    @staticmethod
    def _build_mapping_summary(mappings):
        """Build summary statistics for all mappings."""
        total = len(mappings)
        high = sum(1 for m in mappings if m["confidence"] == "high")
        medium = sum(1 for m in mappings if m["confidence"] == "medium")
        low = sum(1 for m in mappings if m["confidence"] == "low")
        unmapped = [
            m["source_action"] for m in mappings
            if m["target_action"] == "UNMAPPED"
        ]
        by_source = {}
        for m in mappings:
            src = m.get("mapping_source", "unknown")
            by_source[src] = by_source.get(src, 0) + 1

        return {
            "total": total,
            "high_confidence": high,
            "medium_confidence": medium,
            "low_confidence": low,
            "unmapped_count": len(unmapped),
            "unmapped_actions": list(set(unmapped)),
            "by_mapping_source": by_source,
        }

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    @staticmethod
    def save_mapping_result(mapping_result, output_path=None):
        """Save mapping result to file."""
        path = Path(output_path) if output_path else Config.MAPPING_RESULT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(mapping_result, f, indent=2, ensure_ascii=False, default=str)

        logger.info(f"Mapping result saved to: {path}")
        return path


def map_actions(ir_data):
    """Convenience function to map all actions from IR data."""
    engine = MappingEngine()
    return engine.map_all_actions(ir_data)