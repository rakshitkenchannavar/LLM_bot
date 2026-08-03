import json
import logging
import boto3
from botocore.exceptions import ClientError
from config import Config
from prompts import (
    SYSTEM_ACTION_MAPPING,
    SYSTEM_EXPRESSION_TRANSLATION,
    SYSTEM_REPAIR,
    SYSTEM_PARAMETER_INFERENCE,
    action_mapping_prompt,
    expression_translation_prompt,
    repair_prompt,
    parameter_inference_prompt,
    bulk_mapping_prompt,
    variable_declaration_prompt,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Token-efficient payload extractors (identical logic)
# ------------------------------------------------------------------

def extract_mapping_payload(source_action_type, source_properties):
    """Extract ONLY fields needed for action mapping."""
    skip_keys = {
        "DisplayName", "IdRef", "sap2010", "sap",
        "ContinueOnError", "IsExpanded", "WorkflowViewState",
        "Annotation", "FieldIdentifier",
    }
    minimal = {}
    for key, value in source_properties.items():
        if key in skip_keys:
            continue
        if not value or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, str) and len(value) > 200:
            minimal[key] = value[:200] + "...[truncated]"
        else:
            minimal[key] = value
    return minimal


def extract_parameter_payload(ir_action, parameter_mapping):
    """Extract ONLY source properties relevant to target parameters."""
    properties = ir_action.get("properties", {})
    expressions = ir_action.get("expressions", {})
    relevant = {}

    for src_key in parameter_mapping.keys():
        if src_key in properties:
            relevant[src_key] = properties[src_key]
        elif src_key in expressions:
            relevant[src_key] = expressions[src_key]

    common_keys = [
        "To", "Value", "From", "Input", "Output", "Result",
        "FileName", "FilePath", "Text", "Condition", "Expression",
        "Url", "Path", "SheetName", "Range",
    ]
    for key in common_keys:
        if key in properties and key not in relevant:
            relevant[key] = properties[key]
        elif key in expressions and key not in relevant:
            relevant[key] = expressions[key]

    return relevant


def extract_bulk_mapping_payload(unmapped_actions):
    """Extract minimal payload for bulk mapping."""
    minimal_list = []
    for action in unmapped_actions:
        props = action.get("properties", {})
        minimal_props = extract_mapping_payload(action.get("action_type", ""), props)
        if len(minimal_props) > 5:
            minimal_props = dict(list(minimal_props.items())[:5])
        minimal_list.append({
            "source_action": action.get("action_type", ""),
            "display_name": action.get("display_name", ""),
            "properties": minimal_props,
        })
    return minimal_list


# ------------------------------------------------------------------
# Provider: AWS Bedrock
# ------------------------------------------------------------------

class BedrockProvider:
    """AWS Bedrock LLM provider with circuit breaker."""

    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self):
        if not Config.AWS_ACCESS_KEY_ID or not Config.AWS_SECRET_ACCESS_KEY:
            raise ValueError("AWS credentials not set in .env")

        kwargs = {
            "service_name": "bedrock-runtime",
            "region_name": Config.AWS_REGION,
            "aws_access_key_id": Config.AWS_ACCESS_KEY_ID,
            "aws_secret_access_key": Config.AWS_SECRET_ACCESS_KEY,
        }
        if Config.AWS_SESSION_TOKEN:
            kwargs["aws_session_token"] = Config.AWS_SESSION_TOKEN

        self._client = boto3.client(**kwargs)
        self.model_id = Config.BEDROCK_MODEL_ID
        self._consecutive_failures = 0
        self._circuit_open = False
        logger.info(f"Bedrock provider initialized: model={self.model_id}")

    def invoke(self, prompt, system_prompt=None, max_tokens=None, temperature=None):
        if self._circuit_open:
            return None

        resolved_max = max_tokens or Config.LLM_MAX_TOKENS
        resolved_temp = temperature or Config.LLM_TEMPERATURE
        mid = self.model_id.lower()
        messages = [{"role": "user", "content": prompt}]

        # Build request body per model family
        if "anthropic" in mid or "claude" in mid:
            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": resolved_max,
                "temperature": resolved_temp,
                "messages": messages,
            }
            if system_prompt:
                body["system"] = system_prompt
        elif "titan" in mid or "amazon" in mid:
            combined = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
            body = {
                "inputText": combined,
                "textGenerationConfig": {
                    "maxTokenCount": resolved_max,
                    "temperature": resolved_temp,
                },
            }
        elif "meta" in mid or "llama" in mid:
            if system_prompt:
                combined = f"<s>[INST] <<SYS>>\n{system_prompt}\n<</SYS>>\n\n{prompt} [/INST]"
            else:
                combined = f"<s>[INST] {prompt} [/INST]"
            body = {"prompt": combined, "max_gen_len": resolved_max, "temperature": resolved_temp}
        else:
            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": resolved_max,
                "temperature": resolved_temp,
                "messages": messages,
            }
            if system_prompt:
                body["system"] = system_prompt

        try:
            response = self._client.invoke_model(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )
            response_body = json.loads(response["body"].read())
            text = self._extract_text(mid, response_body)
            if text:
                self._consecutive_failures = 0
            else:
                self._register_failure()
            return text

        except ClientError as e:
            logger.error(f"Bedrock API error: {e.response['Error']['Message']}")
            self._register_failure()
            return None
        except Exception as e:
            logger.error(f"Bedrock error: {e}")
            self._register_failure()
            return None

    def _register_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES and not self._circuit_open:
            self._circuit_open = True
            logger.error(
                "CIRCUIT BREAKER OPEN: Bedrock failed 3 times. LLM disabled for this run. "
                "Pipeline continues using mapping sheet + patterns. "
                "Check AWS credentials / model access and restart."
            )

    @staticmethod
    def _extract_text(mid, response_body):
        if "anthropic" in mid or "claude" in mid:
            content = response_body.get("content", [])
            return content[0].get("text", "") if content else ""
        if "titan" in mid or "amazon" in mid:
            results = response_body.get("results", [])
            return results[0].get("outputText", "") if results else ""
        if "meta" in mid or "llama" in mid:
            return response_body.get("generation", "")
        for key in ("content", "results", "generation", "output", "text"):
            val = response_body.get(key)
            if val:
                if isinstance(val, list) and val:
                    return val[0].get("text", str(val[0])) if isinstance(val[0], dict) else str(val[0])
                if isinstance(val, str):
                    return val
        return ""


# ------------------------------------------------------------------
# Main LLM Client (provider-agnostic interface)
# ------------------------------------------------------------------

class LLMClient:
    """LLM client using AWS Bedrock.

    Used ONLY for unmapped action inference, expression translation,
    and repair suggestions. All other logic is deterministic.
    """

    def __init__(self):
        self._provider = None
        self._initialized = False
        self._total_calls = 0
        self._estimated_tokens = 0

    def _initialize(self):
        if self._initialized:
            return
        try:
            self._provider = BedrockProvider()
            self._initialized = True
            logger.info("LLM provider ready: bedrock")
        except Exception as e:
            logger.error(f"Failed to initialize Bedrock: {e}")
            raise

    def get_usage_stats(self):
        return {
            "provider": "bedrock",
            "model": Config.BEDROCK_MODEL_ID,
            "total_calls": self._total_calls,
            "estimated_input_tokens": self._estimated_tokens,
        }

    def invoke(self, prompt, system_prompt=None, max_tokens=None, temperature=None):
        self._initialize()
        self._total_calls += 1
        self._estimated_tokens += (len(prompt or "") + len(system_prompt or "")) // 4
        logger.debug(f"LLM call #{self._total_calls} via bedrock")

        result = self._provider.invoke(prompt, system_prompt, max_tokens, temperature)
        if result:
            logger.debug(f"LLM response: {len(result)} chars")
        return result

    # ------------------------------------------------------------------
    # High-level inference methods (IDENTICAL logic)
    # ------------------------------------------------------------------

    def infer_action_mapping(self, source_action_type, source_properties, target_platform="PAD"):
        minimal_props = extract_mapping_payload(source_action_type, source_properties)
        props_json = json.dumps(minimal_props, indent=2, default=str)

        user_prompt = action_mapping_prompt(source_action_type, props_json, target_platform)
        response = self.invoke(user_prompt, system_prompt=SYSTEM_ACTION_MAPPING)

        if not response:
            return self._unmapped_fallback("LLM invocation failed")

        try:
            result = self._parse_json_from_response(response)
            for key in ("target_action", "confidence", "reasoning", "parameter_mapping"):
                if key not in result:
                    result[key] = {} if key == "parameter_mapping" else ("low" if key == "confidence" else "UNMAPPED")
            return result
        except Exception as e:
            logger.error(f"Failed to parse mapping response: {e}")
            return self._unmapped_fallback(f"Parse error: {e}")

    def infer_bulk_mapping(self, unmapped_actions, target_platform="PAD"):
        minimal_actions = extract_bulk_mapping_payload(unmapped_actions)
        actions_json = json.dumps(minimal_actions, indent=2, default=str)

        user_prompt = bulk_mapping_prompt(actions_json, target_platform)
        response = self.invoke(user_prompt, system_prompt=SYSTEM_ACTION_MAPPING)

        if not response:
            return [self._unmapped_fallback("Bulk LLM failed") for _ in unmapped_actions]

        try:
            result = self._parse_json_from_response(response)
            return result if isinstance(result, list) else [self._unmapped_fallback("Unexpected format")]
        except Exception as e:
            return [self._unmapped_fallback(f"Parse error: {e}") for _ in unmapped_actions]

    def infer_expression_translation(self, source_expression, source_context=""):
        if not source_expression or len(source_expression.strip()) < 3:
            return source_expression

        user_prompt = expression_translation_prompt(source_expression, source_context)
        response = self.invoke(user_prompt, system_prompt=SYSTEM_EXPRESSION_TRANSLATION, max_tokens=256)

        if not response:
            return source_expression

        cleaned = response.strip().strip('"').strip("'").strip("`")
        return cleaned if cleaned else source_expression

    def suggest_repair(self, failing_block, error_message, full_context=""):
        user_prompt = repair_prompt(failing_block, error_message, full_context)
        response = self.invoke(user_prompt, system_prompt=SYSTEM_REPAIR, max_tokens=512)

        if not response:
            return failing_block

        return response.strip()

    def infer_parameters(self, action_name, action_skeleton, ir_action, parameter_mapping=None):
        relevant_props = extract_parameter_payload(ir_action, parameter_mapping or {})
        props_json = json.dumps(relevant_props, indent=2, default=str)

        user_prompt = parameter_inference_prompt(action_name, action_skeleton, props_json)
        response = self.invoke(user_prompt, system_prompt=SYSTEM_PARAMETER_INFERENCE, max_tokens=512)

        if not response:
            return {}

        try:
            return self._parse_json_from_response(response)
        except Exception:
            return {}

    def infer_variable_declarations(self, variables_used, existing_declarations):
        limited_vars = variables_used[:50]
        limited_existing = existing_declarations[:50]

        user_prompt = variable_declaration_prompt(
            json.dumps(limited_vars, default=str),
            json.dumps(limited_existing, default=str),
        )
        response = self.invoke(user_prompt, system_prompt=SYSTEM_PARAMETER_INFERENCE, max_tokens=512)

        return response.strip() if response else ""

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _unmapped_fallback(reason):
        return {
            "target_action": "UNMAPPED",
            "confidence": "low",
            "reasoning": reason,
            "parameter_mapping": {},
        }

    @staticmethod
    def _parse_json_from_response(response_text):
        text = response_text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            return json.loads(text[start:end].strip())

        if "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            return json.loads(text[start:end].strip())

        brace_start = text.find("{")
        brace_end = text.rfind("}") + 1
        if brace_start != -1 and brace_end > brace_start:
            return json.loads(text[brace_start:brace_end])

        bracket_start = text.find("[")
        bracket_end = text.rfind("]") + 1
        if bracket_start != -1 and bracket_end > bracket_start:
            return json.loads(text[bracket_start:bracket_end])

        raise ValueError(f"Could not extract JSON: {text[:200]}")


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

_llm_client = None


def get_llm_client():
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client