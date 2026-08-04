import json
import logging
import re
import time
import requests
from config import Config
from prompts import (
    SYSTEM_ACTION_MAPPING,
    SYSTEM_EXPRESSION_TRANSLATION,
    SYSTEM_REPAIR,
    SYSTEM_PARAMETER_INFERENCE,
    SYSTEM_ROBIN_FIX, 
    robin_line_fix_prompt,
    action_mapping_prompt,
    expression_translation_prompt,
    repair_prompt,
    parameter_inference_prompt,
    bulk_mapping_prompt,
    variable_declaration_prompt,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Token-efficient payload extractors
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
# Provider: Google Gemini (auto model discovery + circuit breaker)
# ------------------------------------------------------------------

class GeminiProvider:
    """Google Gemini provider.

    - Discovers which models the API key can actually use (1 request)
    - Auto-selects the best available model
    - Never sleeps on 404 (just tries next candidate)
    - Honors exact retry-after time on 429
    - Circuit breaker: after 3 consecutive failures, LLM is disabled
      for the rest of the run (pipeline continues with mapping sheet)
    """

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self):
        self.api_key = Config.GEMINI_API_KEY
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not set in .env")

        self.working_model = None
        self._discovered = []
        self._bad_models = set()
        self._consecutive_failures = 0
        self._circuit_open = False
        logger.info("Gemini provider ready (model will be auto-discovered)")
        
        
    def suggest_robin_fix(self, line, error_message, schema_template):
        """Fix one DLL-rejected Robin line using the official template."""
        user_prompt = robin_line_fix_prompt(line, error_message, schema_template)
        response = self.invoke(user_prompt, system_prompt=SYSTEM_ROBIN_FIX, max_tokens=256)
        if not response:
            return None
        for ln in response.strip().strip("`").splitlines():
            if ln.strip():
                return ln.strip()
        return None

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    def _discover_models(self):
        """Ask Google which models this key can use. 1 request only."""
        try:
            resp = requests.get(
                self.BASE_URL,
                params={"key": self.api_key},
                timeout=30,
            )
            if resp.status_code != 200:
                logger.error(f"Model discovery failed [{resp.status_code}]: {resp.text[:200]}")
                return []

            models = resp.json().get("models", [])
            candidates = []
            for m in models:
                name = m.get("name", "").replace("models/", "")
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" not in methods:
                    continue
                if "embedding" in name.lower() or "image" in name.lower():
                    continue
                candidates.append(name)

            # Rank: configured model first, then flash, stable over preview
            def rank(n):
                score = 0
                if n == Config.GEMINI_MODEL:
                    score -= 100
                if "flash" in n:
                    score -= 10
                if "preview" in n:
                    score += 5
                if "lite" in n:
                    score += 2
                return score

            candidates.sort(key=rank)
            logger.info(f"Model discovery: {len(candidates)} usable model(s): {candidates[:5]}")
            return candidates

        except Exception as e:
            logger.error(f"Model discovery error: {e}")
            return []

    # ------------------------------------------------------------------
    # Invoke
    # ------------------------------------------------------------------

    def invoke(self, prompt, system_prompt=None, max_tokens=None, temperature=None):
        # Circuit breaker
        if self._circuit_open:
            return None

        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature or Config.LLM_TEMPERATURE,
                "maxOutputTokens": max_tokens or Config.LLM_MAX_TOKENS,
                "topP": 0.95,
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        # Build candidate list
        if self.working_model:
            candidates = [self.working_model]
        else:
            if not self._discovered:
                self._discovered = self._discover_models()
            candidates = [m for m in self._discovered if m not in self._bad_models][:3]
            if not candidates:
                candidates = [Config.GEMINI_MODEL]

        for model in candidates:
            url = f"{self.BASE_URL}/{model}:generateContent?key={self.api_key}"
            status, data = self._post(url, payload)

            if status == 200:
                text = self._extract_text(data)
                if text:
                    if model != self.working_model:
                        logger.info(f"Using Gemini model: {model}")
                    self.working_model = model
                    self._consecutive_failures = 0
                    return text
                self._register_failure()
                return None

            if status == 404:
                logger.warning(f"Model '{model}' not available for this key, trying next...")
                self._bad_models.add(model)
                continue

            if status == 429:
                wait = self._parse_retry_after(data)
                logger.warning(f"Gemini quota hit. Waiting {wait:.0f}s (Google's instruction)...")
                time.sleep(wait)
                status2, data2 = self._post(url, payload)
                if status2 == 200:
                    text = self._extract_text(data2)
                    if text:
                        self.working_model = model
                        self._consecutive_failures = 0
                        return text
                self._register_failure()
                return None

            # Any other error
            self._register_failure()
            return None

        # All candidates failed
        logger.error("No usable Gemini model found for this API key.")
        self._register_failure()
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _register_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES and not self._circuit_open:
            self._circuit_open = True
            logger.error(
                "CIRCUIT BREAKER OPEN: Gemini failed 3 times. LLM disabled for this run. "
                "Pipeline continues using mapping sheet + patterns. "
                "Unmapped actions will be flagged for manual review."
            )

    def _post(self, url, payload):
        try:
            resp = requests.post(
                url, json=payload, timeout=120,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                logger.error(f"Gemini API error [{resp.status_code}]: {resp.text[:200]}")
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, {}
        except requests.ConnectionError:
            logger.error("Cannot connect to Gemini API. Check internet.")
            return 0, {}
        except requests.Timeout:
            logger.error("Gemini request timed out.")
            return 0, {}
        except Exception as e:
            logger.error(f"Gemini error: {e}")
            return 0, {}

    @staticmethod
    def _extract_text(data):
        candidates = data.get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return None
        return parts[0].get("text", "") or None

    @staticmethod
    def _parse_retry_after(data):
        msg = data.get("error", {}).get("message", "")
        match = re.search(r"retry in ([\d.]+)s", msg)
        if match:
            return min(float(match.group(1)) + 1, 60)
        return 20


# ------------------------------------------------------------------
# Provider: AWS Bedrock (for office laptop)
# ------------------------------------------------------------------

class BedrockProvider:
    """AWS Bedrock LLM provider."""

    def __init__(self):
        import boto3
        self._client = boto3.client(
            service_name="bedrock-runtime",
            region_name=Config.AWS_REGION,
            aws_access_key_id=Config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY,
            aws_session_token=Config.AWS_SESSION_TOKEN,
        )
        self.model_id = Config.BEDROCK_MODEL_ID
        logger.info(f"Bedrock provider initialized: model={self.model_id}")

    def invoke(self, prompt, system_prompt=None, max_tokens=None, temperature=None):
        from botocore.exceptions import ClientError

        resolved_max = max_tokens or Config.LLM_MAX_TOKENS
        resolved_temp = temperature or Config.LLM_TEMPERATURE
        mid = self.model_id.lower()
        messages = [{"role": "user", "content": prompt}]

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

            if "anthropic" in mid or "claude" in mid:
                content = response_body.get("content", [])
                return content[0].get("text", "") if content else ""
            elif "titan" in mid or "amazon" in mid:
                results = response_body.get("results", [])
                return results[0].get("outputText", "") if results else ""
            else:
                for key in ("content", "results", "generation", "output", "text"):
                    val = response_body.get(key)
                    if val:
                        if isinstance(val, list) and val:
                            return val[0].get("text", str(val[0])) if isinstance(val[0], dict) else str(val[0])
                        if isinstance(val, str):
                            return val
                return str(response_body)

        except ClientError as e:
            logger.error(f"Bedrock error: {e.response['Error']['Message']}")
            return None
        except Exception as e:
            logger.error(f"Bedrock error: {e}")
            return None


# ------------------------------------------------------------------
# Main LLM Client
# ------------------------------------------------------------------

class LLMClient:
    """Unified LLM client. Provider selected by .env LLM_PROVIDER."""

    def __init__(self):
        self._provider = None
        self._initialized = False
        self._total_calls = 0
        self._estimated_tokens = 0

    def _initialize(self):
        if self._initialized:
            return

        provider_name = Config.LLM_PROVIDER.lower()
        providers = {"gemini": GeminiProvider, "bedrock": BedrockProvider}

        if provider_name not in providers:
            raise ValueError(f"Unknown LLM_PROVIDER: {provider_name}. Use: gemini, bedrock")

        try:
            self._provider = providers[provider_name]()
            self._initialized = True
            logger.info(f"LLM provider ready: {provider_name}")
        except Exception as e:
            logger.error(f"Failed to initialize {provider_name}: {e}")
            raise

    def get_usage_stats(self):
        return {
            "provider": Config.LLM_PROVIDER,
            "total_calls": self._total_calls,
            "estimated_input_tokens": self._estimated_tokens,
        }

    def invoke(self, prompt, system_prompt=None, max_tokens=None, temperature=None):
        self._initialize()
        self._total_calls += 1
        self._estimated_tokens += (len(prompt or "") + len(system_prompt or "")) // 4
        logger.debug(f"LLM call #{self._total_calls} via {Config.LLM_PROVIDER}")

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
    
    def suggest_robin_fix(self, line, error_message, schema_template):
        """Fix one DLL-rejected Robin line using the official template."""
        user_prompt = robin_line_fix_prompt(line, error_message, schema_template)
        response = self.invoke(user_prompt, system_prompt=SYSTEM_ROBIN_FIX, max_tokens=256)
        if not response:
            return None
        for ln in response.strip().strip("`").splitlines():
            if ln.strip():
                return ln.strip()
        return None

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