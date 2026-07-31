"""All LLM prompts used in the migration engine.

Centralized here for maintainability.
Each prompt is a function that accepts dynamic parameters
and returns the formatted prompt string.
"""


# ============================================================
# SYSTEM PROMPTS
# ============================================================

SYSTEM_ACTION_MAPPING = (
    "You are an expert RPA migration assistant. "
    "You map UiPath actions to Power Automate Desktop (PAD) Robin script actions. "
    "Return ONLY valid JSON with no extra text."
)

SYSTEM_EXPRESSION_TRANSLATION = (
    "You are an expert at translating UiPath VB.NET expressions to "
    "Power Automate Desktop Robin script expressions. "
    "Return ONLY the translated expression, no explanation."
)

SYSTEM_REPAIR = (
    "You are an expert Power Automate Desktop Robin script developer. "
    "Fix the given Robin script block based on the validation error. "
    "Return ONLY the corrected Robin script block, no explanation."
)

SYSTEM_PARAMETER_INFERENCE = (
    "You are an expert Power Automate Desktop Robin script developer. "
    "You fill in missing parameters for PAD Robin script actions. "
    "Return ONLY valid JSON with no extra text."
)


# ============================================================
# USER PROMPTS
# ============================================================

def action_mapping_prompt(source_action_type, source_properties_json, target_platform="PAD"):
    """Prompt to infer the best target action for an unmapped source action."""
    return f"""Given this UiPath action, find the best matching PAD Robin script action.

Source Action Type: {source_action_type}
Source Properties: {source_properties_json}
Target Platform: {target_platform}

Return a JSON object with these exact keys:
{{
    "target_action": "The PAD Robin action name (e.g., Display.ShowMessageDialog)",
    "confidence": "high or medium or low",
    "reasoning": "Brief explanation of why this mapping is correct",
    "parameter_mapping": {{
        "source_param_name": "target_param_name"
    }}
}}

Rules:
- Only suggest real PAD Robin actions that exist
- If no good mapping exists, set target_action to "UNMAPPED" and confidence to "low"
- Be conservative: prefer "low" confidence over wrong mapping"""


def expression_translation_prompt(source_expression, source_context=""):
    """Prompt to translate a UiPath VB.NET expression to PAD expression syntax."""
    context_line = source_context if source_context else "General use"
    return f"""Translate this UiPath VB.NET expression to PAD Robin script syntax:

Expression: {source_expression}
Context: {context_line}

Rules:
- PAD uses %VariableName% for variable references
- PAD uses + for string concatenation
- PAD does not support VB.NET methods directly
- Convert .ToString() to proper PAD text conversion
- Convert DateTime.Now to current datetime variable
- Return ONLY the translated expression string, nothing else"""


def repair_prompt(failing_block, error_message, full_context=""):
    """Prompt to fix a failing Robin script block."""
    context_section = f"\nSurrounding Context:\n{full_context}" if full_context else ""
    return f"""Fix this Robin script block that failed validation:

Failing Block:
{failing_block}

Validation Error:
{error_message}
{context_section}

Rules:
- Fix ONLY the syntax or parameter error indicated
- Do not change the business logic
- Return ONLY valid Robin script
- Preserve variable names
- Preserve action order"""


def parameter_inference_prompt(action_name, action_skeleton, available_ir_properties):
    """Prompt to infer missing parameters for a PAD action."""
    return f"""Fill in the parameters for this PAD Robin script action.

Action: {action_name}
Action Skeleton: {action_skeleton}
Available Source Properties: {available_ir_properties}

Return a JSON object where keys are parameter names and values are the filled values.

Rules:
- Only use values that logically match the source properties
- Use PAD expression syntax for variable references: %VariableName%
- If a value cannot be determined, use a placeholder: <<PLACEHOLDER_paramname>>
- Do not invent data that is not in the source properties
- Return ONLY valid JSON"""


def bulk_mapping_prompt(unmapped_actions_json, target_platform="PAD"):
    """Prompt to map multiple unmapped actions at once for efficiency."""
    return f"""Map each of these UiPath actions to the best PAD Robin script action.

Unmapped Actions:
{unmapped_actions_json}

Target Platform: {target_platform}

Return a JSON array where each element has:
{{
    "source_action": "original UiPath action type",
    "target_action": "PAD Robin action name",
    "confidence": "high or medium or low",
    "reasoning": "brief explanation"
}}

Rules:
- Only suggest real PAD Robin actions
- Set target_action to "UNMAPPED" if no good match exists
- Be conservative with confidence levels"""


def control_flow_repair_prompt(block_type, failing_block, error_message):
    """Prompt specifically for fixing control flow block structure errors."""
    return f"""Fix this PAD Robin script control flow block:

Block Type: {block_type}
Failing Block:
{failing_block}

Error: {error_message}

Valid Robin control flow syntax:
- IF: IF condition THEN ... ELSE ... END
- LOOP: LOOP condition ... END
- FOR EACH: FOR EACH item IN collection ... END
- SWITCH: SWITCH value ... CASE value ... DEFAULT ... END
- TRY: BEGIN EXCEPTION ... END EXCEPTION

Rules:
- Fix ONLY the structural error
- Preserve all inner actions
- Return ONLY the corrected block"""


def variable_declaration_prompt(variables_used, existing_declarations):
    """Prompt to generate missing variable declarations."""
    return f"""Generate PAD Robin script variable declarations for these variables.

Variables Used In Script:
{variables_used}

Already Declared:
{existing_declarations}

Generate SET statements ONLY for variables that are used but not yet declared.

Rules:
- Use: SET VariableName TO default_value
- Text variables default to ''
- Numeric variables default to 0
- Boolean variables default to False
- List variables default to empty list
- Return ONLY the SET statements, one per line"""