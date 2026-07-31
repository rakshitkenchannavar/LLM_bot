import streamlit as st
import json
import logging
import tempfile
import shutil
import zipfile
from pathlib import Path
from datetime import datetime

from config import Config
from extractor import extract_input
from parser_xaml import parse_xaml, parse_xaml_files
from ir_generator import IRGenerator
from mapping_engine import MappingEngine
from pad_script_generator import PADScriptGenerator
from validator import Validator
from repair_engine import RepairEngine

# ------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Page config
# ------------------------------------------------------------------

st.set_page_config(
    page_title="UiPath → PAD Migration Engine",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ------------------------------------------------------------------
# Custom CSS
# ------------------------------------------------------------------

st.markdown("""
<style>
    .main-header {
        font-size: 2rem;
        font-weight: 700;
        color: #1E3A5F;
        margin-bottom: 0.5rem;
    }
    .sub-header {
        font-size: 1rem;
        color: #666;
        margin-bottom: 2rem;
    }
    .status-pass {
        background-color: #d4edda;
        border: 1px solid #c3e6cb;
        border-radius: 8px;
        padding: 12px 16px;
        color: #155724;
        font-weight: 600;
    }
    .status-fail {
        background-color: #f8d7da;
        border: 1px solid #f5c6cb;
        border-radius: 8px;
        padding: 12px 16px;
        color: #721c24;
        font-weight: 600;
    }
    .status-info {
        background-color: #d1ecf1;
        border: 1px solid #bee5eb;
        border-radius: 8px;
        padding: 12px 16px;
        color: #0c5460;
        font-weight: 600;
    }
    .file-card {
        background-color: #f8f9fa;
        border: 1px solid #dee2e6;
        border-radius: 8px;
        padding: 16px;
        margin-bottom: 12px;
    }
    .metric-card {
        background-color: #fff;
        border: 1px solid #e0e0e0;
        border-radius: 8px;
        padding: 16px;
        text-align: center;
        box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    }
    .stDownloadButton > button {
        width: 100%;
    }
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------
# Session state initialization
# ------------------------------------------------------------------

def init_session_state():
    defaults = {
        "migration_results": None,
        "pipeline_log": [],
        "current_step": 0,
        "total_steps": 8,
        "is_running": False,
        "uploaded_file_name": None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


init_session_state()


# ------------------------------------------------------------------
# Pipeline runner (per XAML file → per .robin output)
# ------------------------------------------------------------------

def run_migration_per_file(input_path, progress_bar, status_text):
    """Run migration pipeline and produce one .robin per .xaml file.

    Returns:
        dict: {
            "status": str,
            "robin_files": {xaml_name: {"script": str, "validation": dict, ...}},
            "ir_files": {xaml_name: dict},
            "mapping_files": {xaml_name: dict},
            "summary": dict,
            "log": [str],
        }
    """
    pipeline_log = []
    robin_files = {}
    ir_files = {}
    mapping_files = {}

    def log(msg):
        pipeline_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        logger.info(msg)

    Config.ensure_directories()

    # ------------------------------------------------------------------
    # Step 1: Extract
    # ------------------------------------------------------------------
    progress_bar.progress(5, text="Step 1/8: Extracting input...")
    status_text.info("📦 Extracting input files...")
    log("STEP 1: Extraction started")

    try:
        extraction = extract_input(input_path)
        xaml_files = extraction["xaml_files"]
        project_info = extraction["project_info"]

        if not xaml_files:
            log("ERROR: No XAML files found")
            return {"status": "failed", "error": "No XAML files found", "log": pipeline_log}

        log(f"Found {len(xaml_files)} XAML file(s)")
        for xf in xaml_files:
            log(f"  - {xf.name}")

    except Exception as e:
        log(f"ERROR: Extraction failed: {e}")
        return {"status": "failed", "error": str(e), "log": pipeline_log}

    # ------------------------------------------------------------------
    # Process each XAML file independently
    # ------------------------------------------------------------------
    total_files = len(xaml_files)
    total_actions_all = 0
    total_high = 0
    total_medium = 0
    total_low = 0
    total_unmapped = 0
    total_valid = 0
    total_invalid = 0

    for file_idx, xaml_path in enumerate(xaml_files):
        file_name = xaml_path.stem
        robin_key = f"{file_name}.robin"
        file_progress_base = 10 + int((file_idx / total_files) * 85)

        log(f"--- Processing: {xaml_path.name} ({file_idx + 1}/{total_files}) ---")

        # ------------------------------------------------------------------
        # Step 2: Parse XAML
        # ------------------------------------------------------------------
        step_progress = file_progress_base + int((1 / 6) * (85 / total_files))
        progress_bar.progress(min(step_progress, 95), text=f"Step 2/8: Parsing {xaml_path.name}...")
        status_text.info(f"📄 Parsing: {xaml_path.name}")
        log(f"Parsing {xaml_path.name}")

        try:
            parsed_tree = parse_xaml(xaml_path)
            if parsed_tree.get("error"):
                log(f"ERROR: Parse failed for {xaml_path.name}: {parsed_tree['error']}")
                robin_files[robin_key] = {
                    "script": f"# ERROR: Failed to parse {xaml_path.name}\n# {parsed_tree['error']}",
                    "validation": {"is_valid": False},
                    "status": "parse_error",
                }
                total_invalid += 1
                continue

            log(f"Parsed: {parsed_tree.get('total_actions', 0)} actions, "
                f"{len(parsed_tree.get('variables', []))} variables")

        except Exception as e:
            log(f"ERROR: Parse exception for {xaml_path.name}: {e}")
            robin_files[robin_key] = {
                "script": f"# ERROR: Failed to parse {xaml_path.name}\n# {e}",
                "validation": {"is_valid": False},
                "status": "parse_error",
            }
            total_invalid += 1
            continue

        # ------------------------------------------------------------------
        # Step 3: Generate IR
        # ------------------------------------------------------------------
        step_progress = file_progress_base + int((2 / 6) * (85 / total_files))
        progress_bar.progress(min(step_progress, 95), text=f"Step 3/8: Generating IR for {file_name}...")
        status_text.info(f"🔧 Generating IR: {file_name}")
        log(f"Generating IR for {file_name}")

        try:
            ir_gen = IRGenerator()
            ir_data = ir_gen.generate(parsed_tree, project_info)
            ir_files[file_name] = ir_data
            log(f"IR generated: {ir_data.get('total_actions', 0)} actions")

        except Exception as e:
            log(f"ERROR: IR generation failed for {file_name}: {e}")
            robin_files[robin_key] = {
                "script": f"# ERROR: IR generation failed for {file_name}\n# {e}",
                "validation": {"is_valid": False},
                "status": "ir_error",
            }
            total_invalid += 1
            continue

        # ------------------------------------------------------------------
        # Step 4: Map Actions
        # ------------------------------------------------------------------
        step_progress = file_progress_base + int((3 / 6) * (85 / total_files))
        progress_bar.progress(min(step_progress, 95), text=f"Step 4/8: Mapping actions for {file_name}...")
        status_text.info(f"🗺️ Mapping actions: {file_name}")
        log(f"Mapping actions for {file_name}")

        try:
            mapper = MappingEngine()
            mapping_result = mapper.map_all_actions(ir_data)
            mapping_files[file_name] = mapping_result

            summary = mapping_result.get("summary", {})
            total_actions_all += summary.get("total", 0)
            total_high += summary.get("high_confidence", 0)
            total_medium += summary.get("medium_confidence", 0)
            total_low += summary.get("low_confidence", 0)
            total_unmapped += summary.get("unmapped_count", 0)

            log(f"Mapped: {summary.get('total', 0)} actions "
                f"(H:{summary.get('high_confidence', 0)} "
                f"M:{summary.get('medium_confidence', 0)} "
                f"L:{summary.get('low_confidence', 0)} "
                f"U:{summary.get('unmapped_count', 0)})")

        except Exception as e:
            log(f"ERROR: Mapping failed for {file_name}: {e}")
            robin_files[robin_key] = {
                "script": f"# ERROR: Action mapping failed for {file_name}\n# {e}",
                "validation": {"is_valid": False},
                "status": "mapping_error",
            }
            total_invalid += 1
            continue

        # ------------------------------------------------------------------
        # Step 5: Generate Robin Script
        # ------------------------------------------------------------------
        step_progress = file_progress_base + int((4 / 6) * (85 / total_files))
        progress_bar.progress(min(step_progress, 95), text=f"Step 5/8: Generating Robin script for {file_name}...")
        status_text.info(f"📝 Generating Robin script: {file_name}")
        log(f"Generating Robin script for {file_name}")

        try:
            generator = PADScriptGenerator()
            robin_script = generator.generate(ir_data, mapping_result)
            log(f"Robin script generated: {len(robin_script.splitlines())} lines")

        except Exception as e:
            log(f"ERROR: Script generation failed for {file_name}: {e}")
            robin_files[robin_key] = {
                "script": f"# ERROR: Script generation failed for {file_name}\n# {e}",
                "validation": {"is_valid": False},
                "status": "generation_error",
            }
            total_invalid += 1
            continue

        # ------------------------------------------------------------------
        # Step 6: Validate
        # ------------------------------------------------------------------
        step_progress = file_progress_base + int((5 / 6) * (85 / total_files))
        progress_bar.progress(min(step_progress, 95), text=f"Step 6/8: Validating {file_name}...")
        status_text.info(f"✅ Validating: {file_name}")
        log(f"Validating {file_name}")

        try:
            validator = Validator()
            validation_result = validator.validate(script_text=robin_script)
            is_valid = validation_result.get("is_valid", False)
            error_count = validation_result.get("error_count", 0)
            log(f"Validation: {'PASS' if is_valid else 'FAIL'} ({error_count} errors)")

        except Exception as e:
            log(f"ERROR: Validation failed for {file_name}: {e}")
            validation_result = {"is_valid": False, "error_count": 1}

        # ------------------------------------------------------------------
        # Step 7: Repair (if needed)
        # ------------------------------------------------------------------
        final_script = robin_script
        repair_result = None

        if not validation_result.get("is_valid", False):
            progress_bar.progress(min(step_progress + 2, 95), text=f"Step 7/8: Repairing {file_name}...")
            status_text.info(f"🔧 Repairing: {file_name}")
            log(f"Repair loop for {file_name}")

            try:
                repair_eng = RepairEngine()
                repair_result = repair_eng.repair(
                    script=robin_script,
                    ir_data=ir_data,
                    mapping_result=mapping_result,
                )
                final_script = repair_result.get("final_script", robin_script)
                is_valid_after = repair_result.get("is_valid", False)
                attempts = repair_result.get("attempts", 0)

                if is_valid_after:
                    log(f"Repair SUCCEEDED after {attempts} attempt(s)")
                    validation_result["is_valid"] = True
                else:
                    unresolved = len(repair_result.get("unresolved_errors", []))
                    log(f"Repair INCOMPLETE after {attempts} attempt(s) - {unresolved} unresolved")

            except Exception as e:
                log(f"ERROR: Repair failed for {file_name}: {e}")

        if validation_result.get("is_valid", False):
            total_valid += 1
        else:
            total_invalid += 1

        # Store result
        robin_files[robin_key] = {
            "script": final_script,
            "validation": validation_result,
            "repair_result": repair_result,
            "mapping_summary": mapping_result.get("summary", {}),
            "status": "valid" if validation_result.get("is_valid", False) else "has_errors",
            "line_count": len(final_script.splitlines()),
        }

    # ------------------------------------------------------------------
    # Step 8: Final summary
    # ------------------------------------------------------------------
    progress_bar.progress(100, text="Step 8/8: Complete!")
    status_text.success("✅ Migration complete!")
    log("MIGRATION COMPLETE")

    overall_summary = {
        "total_files": total_files,
        "valid_scripts": total_valid,
        "invalid_scripts": total_invalid,
        "total_actions": total_actions_all,
        "high_confidence": total_high,
        "medium_confidence": total_medium,
        "low_confidence": total_low,
        "unmapped": total_unmapped,
    }

    return {
        "status": "completed",
        "robin_files": robin_files,
        "ir_files": ir_files,
        "mapping_files": mapping_files,
        "summary": overall_summary,
        "log": pipeline_log,
    }


# ------------------------------------------------------------------
# UI Components
# ------------------------------------------------------------------

def render_header():
    st.markdown('<div class="main-header">🔄 UiPath → PAD Migration Engine</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sub-header">Convert UiPath workflows (.xaml, .zip, .nupkg) to '
        'Power Automate Desktop Robin scripts</div>',
        unsafe_allow_html=True,
    )


def render_sidebar():
    with st.sidebar:
        st.header("⚙️ Settings")

        st.subheader("Configuration Status")
        config_errors = Config.validate()
        if config_errors:
            for err in config_errors:
                st.warning(f"⚠️ {err}")
        else:
            st.success("✅ Configuration valid")

        st.divider()

        st.subheader("Config Summary")
        summary = Config.summary()
        st.text(f"Provider   : {summary.get('llm_provider', 'unknown')}")

        if summary.get('llm_provider') == 'gemini':
            st.text(f"Gemini Key : {'✅ Set' if summary.get('gemini_key_set') else '❌ Missing'}")
            st.text(f"Model      : {summary.get('gemini_model', '')}")
        elif summary.get('llm_provider') == 'bedrock':
            st.text(f"AWS Key    : {'✅ Set' if summary.get('aws_key_set') else '❌ Missing'}")
            st.text(f"AWS Secret : {'✅ Set' if summary.get('aws_secret_set') else '❌ Missing'}")
            st.text(f"AWS Token  : {'✅ Set' if summary.get('aws_token_set') else '❌ Missing'}")
            st.text(f"Model      : {summary.get('bedrock_model', '')}")

        st.text(f"Max Tokens : {summary.get('max_tokens', '')}")
        st.text(f"Platform   : {summary.get('target_platform', '')}")
        st.text(f"Max Retry  : {summary.get('max_retry', '')}")

        st.divider()

        st.subheader("About")
        st.markdown("""
        **Migration Priority:**
        1. Mapping sheet (deterministic)
        2. Pattern inference (deterministic)
        3. LLM inference (last resort)

        **Validation:**
        - Semantic validation (schema-based)
        - PAD validator (DLL-based, Windows only)
        """)


def render_upload_section():
    st.subheader("📤 Upload UiPath Workflow")

    uploaded_file = st.file_uploader(
        "Upload .xaml, .zip, or .nupkg file",
        type=["xaml", "zip", "nupkg"],
        help="Upload a UiPath workflow file or project archive",
    )

    return uploaded_file


def render_metrics(summary):
    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        st.metric("📁 Total Files", summary.get("total_files", 0))
    with col2:
        st.metric("✅ Valid Scripts", summary.get("valid_scripts", 0))
    with col3:
        st.metric("🟢 High Confidence", summary.get("high_confidence", 0))
    with col4:
        st.metric("🟡 Medium Confidence", summary.get("medium_confidence", 0))
    with col5:
        st.metric("🔴 Unmapped", summary.get("unmapped", 0))


def render_robin_files(robin_files):
    st.subheader("📄 Generated Robin Scripts")

    # Download all as ZIP
    if len(robin_files) > 0:
        zip_buffer = create_zip_download(robin_files)
        st.download_button(
            label="📦 Download All Scripts (.zip)",
            data=zip_buffer,
            file_name="pad_robin_scripts.zip",
            mime="application/zip",
            use_container_width=True,
        )

    st.divider()

    # Individual files
    for file_name, file_data in robin_files.items():
        script = file_data.get("script", "")
        status = file_data.get("status", "unknown")
        validation = file_data.get("validation", {})
        is_valid = validation.get("is_valid", False)
        line_count = file_data.get("line_count", len(script.splitlines()))
        mapping_summary = file_data.get("mapping_summary", {})

        # File card
        with st.expander(f"{'✅' if is_valid else '⚠️'} {file_name} ({line_count} lines)", expanded=False):

            # Status row
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                if is_valid:
                    st.markdown('<div class="status-pass">✅ Valid</div>', unsafe_allow_html=True)
                else:
                    error_count = validation.get("error_count", 0)
                    st.markdown(
                        f'<div class="status-fail">⚠️ {error_count} error(s)</div>',
                        unsafe_allow_html=True,
                    )
            with col2:
                st.metric("Lines", line_count)
            with col3:
                st.metric("Actions", mapping_summary.get("total", 0))
            with col4:
                high = mapping_summary.get("high_confidence", 0)
                total = mapping_summary.get("total", 1) or 1
                pct = int((high / total) * 100)
                st.metric("Confidence", f"{pct}%")

            # Download individual file
            st.download_button(
                label=f"⬇️ Download {file_name}",
                data=script,
                file_name=file_name,
                mime="text/plain",
                key=f"download_{file_name}",
                use_container_width=True,
            )

            # Script preview
            st.code(script, language="text", line_numbers=True)

            # Validation errors (if any)
            if not is_valid:
                errors = validation.get("combined_errors", [])
                if errors:
                    st.subheader("🔍 Validation Errors")
                    for err in errors:
                        st.error(
                            f"**Line {err.get('line', '?')}** [{err.get('error_type', 'unknown')}]: "
                            f"{err.get('message', 'Unknown error')}"
                        )

            # Repair log (if any)
            repair_result = file_data.get("repair_result")
            if repair_result:
                repair_log = repair_result.get("repair_log", [])
                if repair_log:
                    st.subheader("🔧 Repair Log")
                    for entry in repair_log:
                        if isinstance(entry, dict):
                            status_icon = "✅" if entry.get("status") == "fixed" else "⏭️"
                            st.text(
                                f"{status_icon} Line {entry.get('line', '?')}: "
                                f"[{entry.get('error_type', '')}] {entry.get('status', '')}"
                            )


def render_pipeline_log(log_entries):
    with st.expander("📋 Pipeline Log", expanded=False):
        log_text = "\n".join(log_entries)
        st.code(log_text, language="text")


def render_ir_viewer(ir_files):
    if not ir_files:
        return

    with st.expander("🔍 IR JSON Viewer", expanded=False):
        selected_file = st.selectbox(
            "Select workflow",
            options=list(ir_files.keys()),
            key="ir_viewer_select",
        )
        if selected_file:
            ir_data = ir_files[selected_file]
            st.json(ir_data)


def render_mapping_viewer(mapping_files):
    if not mapping_files:
        return

    with st.expander("🗺️ Mapping Viewer", expanded=False):
        selected_file = st.selectbox(
            "Select workflow",
            options=list(mapping_files.keys()),
            key="mapping_viewer_select",
        )
        if selected_file:
            mapping_data = mapping_files[selected_file]
            summary = mapping_data.get("summary", {})

            st.subheader("Summary")
            st.json(summary)

            st.subheader("Action Mappings")
            mappings = mapping_data.get("mappings", [])
            for m in mappings:
                confidence = m.get("confidence", "low")
                icon = "🟢" if confidence == "high" else "🟡" if confidence == "medium" else "🔴"
                st.text(
                    f"{icon} {m.get('source_action', '?')} → {m.get('target_action', '?')} "
                    f"[{m.get('mapping_source', '?')}]"
                )


def create_zip_download(robin_files):
    """Create a ZIP file containing all Robin scripts.

    Structure:
        output/
        ├── Main.robin
        ├── OtherWorkflow.robin
        └── ...
    """
    import io

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_name, file_data in robin_files.items():
            script = file_data.get("script", "")
            zf.writestr(f"output/{file_name}", script)

    zip_buffer.seek(0)
    return zip_buffer


# ------------------------------------------------------------------
# Main App
# ------------------------------------------------------------------

def main():
    render_header()
    render_sidebar()

    # Upload section
    uploaded_file = render_upload_section()

    if uploaded_file is not None:
        # Show file info
        file_size_kb = uploaded_file.size / 1024
        st.markdown(
            f'<div class="status-info">'
            f'📁 <strong>{uploaded_file.name}</strong> '
            f'({file_size_kb:.1f} KB, type: {uploaded_file.type or uploaded_file.name.split(".")[-1]})'
            f'</div>',
            unsafe_allow_html=True,
        )

        st.divider()

        # Migrate button
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            migrate_clicked = st.button(
                "🚀 Start Migration",
                use_container_width=True,
                type="primary",
                disabled=st.session_state.get("is_running", False),
            )

        if migrate_clicked:
            st.session_state.is_running = True
            st.session_state.migration_results = None

            # Save uploaded file to temp directory
            temp_dir = Path(tempfile.mkdtemp())
            temp_file_path = temp_dir / uploaded_file.name

            with open(temp_file_path, "wb") as f:
                f.write(uploaded_file.getbuffer())

            # Progress UI
            st.divider()
            progress_bar = st.progress(0, text="Starting migration...")
            status_text = st.empty()

            try:
                # Run migration
                results = run_migration_per_file(
                    input_path=str(temp_file_path),
                    progress_bar=progress_bar,
                    status_text=status_text,
                )

                st.session_state.migration_results = results
                st.session_state.is_running = False

            except Exception as e:
                st.error(f"❌ Migration failed: {e}")
                logger.exception("Migration failed")
                st.session_state.is_running = False

            finally:
                # Cleanup temp files
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Display results
    # ------------------------------------------------------------------

    results = st.session_state.get("migration_results")

    if results:
        st.divider()

        if results.get("status") == "failed":
            st.error(f"❌ Migration failed: {results.get('error', 'Unknown error')}")
            if results.get("log"):
                render_pipeline_log(results["log"])
            return

        # Summary metrics
        st.subheader("📊 Migration Summary")
        summary = results.get("summary", {})
        render_metrics(summary)

        st.divider()

        # Robin script files
        robin_files = results.get("robin_files", {})
        if robin_files:
            render_robin_files(robin_files)

        st.divider()

        # Advanced viewers
        col1, col2 = st.columns(2)
        with col1:
            render_ir_viewer(results.get("ir_files", {}))
        with col2:
            render_mapping_viewer(results.get("mapping_files", {}))

        # Pipeline log
        render_pipeline_log(results.get("log", []))


if __name__ == "__main__":
    main()