import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")


class Config:
    """Central configuration loaded from .env file (AWS Bedrock)."""

    LLM_PROVIDER = "bedrock"

    # AWS Bedrock
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_SESSION_TOKEN = os.getenv("AWS_SESSION_TOKEN", "")
    AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
    BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-sonnet-20240229-v1:0")

    # Common LLM Settings
    LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))

    # Migration Settings
    TARGET_PLATFORM = os.getenv("TARGET_PLATFORM", "PAD")
    MAX_RETRY_COUNT = int(os.getenv("MAX_RETRY_COUNT", "3"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # Paths
    PROJECT_ROOT = Path(__file__).parent
    OUTPUT_DIR = PROJECT_ROOT / os.getenv("OUTPUT_DIR", "output")
    SCHEMA_DIR = PROJECT_ROOT / os.getenv("SCHEMA_DIR", "schemas")
    VALIDATOR_PATH = PROJECT_ROOT / os.getenv("VALIDATOR_PATH", "validators/PADValidator.ps1")

    # Schema files
    PAD_SCHEMA_PATH = SCHEMA_DIR / "pad_llm_schema.json"
    MAPPING_SHEET_PATH = SCHEMA_DIR / "mapping_sheet.csv"

    # Output files
    IR_OUTPUT_PATH = OUTPUT_DIR / "ir_output.json"
    MAPPING_RESULT_PATH = OUTPUT_DIR / "mapping_result.json"
    GENERATED_SCRIPT_PATH = OUTPUT_DIR / "generated_script.robin"
    VALIDATION_RESULT_PATH = OUTPUT_DIR / "validation_result.json"
    FINAL_SCRIPT_PATH = OUTPUT_DIR / "final_script.robin"

    @classmethod
    def validate(cls):
        """Validate required configuration."""
        errors = []

        if not cls.AWS_ACCESS_KEY_ID:
            errors.append("AWS_ACCESS_KEY_ID is not set in .env")
        if not cls.AWS_SECRET_ACCESS_KEY:
            errors.append("AWS_SECRET_ACCESS_KEY is not set in .env")
        # Session token optional (long-term IAM keys have none)

        if cls.TARGET_PLATFORM not in ("PAD", "AA360"):
            errors.append(f"TARGET_PLATFORM must be PAD or AA360, got: {cls.TARGET_PLATFORM}")

        if not cls.PAD_SCHEMA_PATH.exists():
            errors.append(f"PAD schema not found at: {cls.PAD_SCHEMA_PATH}")

        if not cls.MAPPING_SHEET_PATH.exists():
            errors.append(f"Mapping sheet not found at: {cls.MAPPING_SHEET_PATH}")

        return errors

    @classmethod
    def ensure_directories(cls):
        cls.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        cls.SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
        cls.VALIDATOR_PATH.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def summary(cls):
        return {
            "llm_provider": cls.LLM_PROVIDER,
            "aws_region": cls.AWS_REGION,
            "aws_key_set": bool(cls.AWS_ACCESS_KEY_ID),
            "aws_secret_set": bool(cls.AWS_SECRET_ACCESS_KEY),
            "aws_token_set": bool(cls.AWS_SESSION_TOKEN),
            "bedrock_model": cls.BEDROCK_MODEL_ID,
            "max_tokens": cls.LLM_MAX_TOKENS,
            "temperature": cls.LLM_TEMPERATURE,
            "target_platform": cls.TARGET_PLATFORM,
            "max_retry": cls.MAX_RETRY_COUNT,
            "log_level": cls.LOG_LEVEL,
            "output_dir": str(cls.OUTPUT_DIR),
            "schema_dir": str(cls.SCHEMA_DIR),
        }