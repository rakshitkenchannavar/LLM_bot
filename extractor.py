import os
import zipfile
import shutil
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class Extractor:
    """Handles input file extraction for .xaml, .zip, and .nupkg formats.

    Responsibilities:
    - Accept .xaml files directly
    - Extract .zip and .nupkg archives
    - Locate all XAML workflow files
    - Identify relevant workflow files (Main.xaml, sequences, etc.)
    """

    # Files and directories to skip during extraction
    SKIP_DIRS = {
        ".git", "__pycache__", "node_modules", ".nuget",
        "lib", "package", "[Content_Types]", "_rels",
    }

    SKIP_FILES = {
        ".nuspec", ".png", ".jpg", ".jpeg", ".gif",
        ".dll", ".exe", ".pdb", ".config",
    }
    SKIP_XAML_PATTERNS = ("testcase", "template")

    def __init__(self, input_path, extract_dir=None):
        """Initialize extractor.

        Args:
            input_path: Path to .xaml, .zip, or .nupkg file
            extract_dir: Optional directory for extraction. Defaults to ./extracted/
        """
        self.input_path = Path(input_path)
        self.extract_dir = Path(extract_dir) if extract_dir else self.input_path.parent / "extracted"
        self.xaml_files = []
        self.project_info = {}

    def extract(self):
        """Main extraction entry point.

        Returns:
            dict: {
                "xaml_files": [list of Path objects],
                "project_info": {metadata dict},
                "source_type": "xaml" | "zip" | "nupkg",
                "extract_dir": Path
            }
        """
        if not self.input_path.exists():
            raise FileNotFoundError(f"Input file not found: {self.input_path}")

        suffix = self.input_path.suffix.lower()

        if suffix == ".xaml":
            return self._handle_xaml()
        elif suffix == ".zip":
            return self._handle_zip()
        elif suffix == ".nupkg":
            return self._handle_nupkg()
        else:
            raise ValueError(f"Unsupported input format: {suffix}. Supported: .xaml, .zip, .nupkg")

    def _handle_xaml(self):
        """Handle direct .xaml input."""
        logger.info(f"Processing single XAML file: {self.input_path}")

        self.xaml_files = [self.input_path]
        self.project_info = {
            "project_name": self.input_path.stem,
            "source_type": "xaml",
            "single_file": True,
        }

        return self._build_result("xaml")

    def _handle_zip(self):
        """Handle .zip archive input."""
        logger.info(f"Extracting ZIP archive: {self.input_path}")
        self._extract_archive()
        self._discover_xaml_files()
        self._detect_project_info()

        return self._build_result("zip")

    def _handle_nupkg(self):
        """Handle .nupkg (NuGet package) input.

        .nupkg files are ZIP archives with NuGet metadata.
        """
        logger.info(f"Extracting NuGet package: {self.input_path}")
        self._extract_archive()
        self._discover_xaml_files()
        self._detect_project_info()

        # Try to extract additional info from .nuspec if present
        self._parse_nuspec()

        return self._build_result("nupkg")

    def _extract_archive(self):
        """Extract a ZIP or NUPKG archive to the extract directory."""
        # Clean previous extraction
        if self.extract_dir.exists():
            shutil.rmtree(self.extract_dir)

        self.extract_dir.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(self.input_path, "r") as zf:
                # Filter out unwanted files during extraction
                members = [
                    m for m in zf.namelist()
                    if not self._should_skip(m)
                ]

                for member in members:
                    zf.extract(member, self.extract_dir)
                    logger.debug(f"Extracted: {member}")

            logger.info(f"Extracted {len(members)} files to {self.extract_dir}")

        except zipfile.BadZipFile:
            raise ValueError(f"File is not a valid ZIP/NUPKG archive: {self.input_path}")

    def _discover_xaml_files(self):
        """Recursively find all .xaml files in the extract directory.

        Skips test/template files (e.g., *TestCase.xaml, *Template.xaml).
        """
        self.xaml_files = []
        skipped = []

        for root, dirs, files in os.walk(self.extract_dir):
            # Skip unwanted directories
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]

            for f in files:
                if f.lower().endswith(".xaml"):
                    if self._should_skip_xaml(f):
                        skipped.append(f)
                        logger.debug(f"Skipping test/template XAML: {f}")
                        continue

                    full_path = Path(root) / f
                    self.xaml_files.append(full_path)
                    logger.debug(f"Found XAML: {full_path}")

        if skipped:
            logger.info(f"Skipped {len(skipped)} test/template XAML file(s): {', '.join(skipped)}")

        if not self.xaml_files:
            logger.warning("No XAML files found in the extracted archive")

        logger.info(f"Discovered {len(self.xaml_files)} XAML files")

    @classmethod
    def _should_skip_xaml(cls, filename):
        """Check if a XAML file should be skipped (test/template files)."""
        stem = Path(filename).stem.lower()
        return any(pattern in stem for pattern in cls.SKIP_XAML_PATTERNS)
    def _detect_project_info(self):
        """Detect UiPath project metadata from project.json if available."""
        self.project_info = {
            "project_name": self.input_path.stem,
            "source_type": self.input_path.suffix.lower().strip("."),
            "single_file": False,
        }

        # Look for project.json (UiPath project descriptor)
        for root, dirs, files in os.walk(self.extract_dir):
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
            for f in files:
                if f.lower() == "project.json":
                    self._parse_project_json(Path(root) / f)
                    return

    def _parse_project_json(self, project_json_path):
        """Parse UiPath project.json for metadata."""
        import json

        try:
            with open(project_json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            self.project_info.update({
                "project_name": data.get("name", self.project_info["project_name"]),
                "project_description": data.get("description", ""),
                "project_version": data.get("projectVersion", ""),
                "main_file": data.get("main", "Main.xaml"),
                "studio_version": data.get("studioVersion", ""),
                "dependencies": data.get("dependencies", {}),
                "target_framework": data.get("targetFramework", ""),
                "expression_language": data.get("expressionLanguage", "VisualBasic"),
            })

            logger.info(f"Parsed project.json: {self.project_info['project_name']}")

        except Exception as e:
            logger.warning(f"Failed to parse project.json: {e}")

    def _parse_nuspec(self):
        """Parse .nuspec file from NuGet package for additional metadata."""
        from lxml import etree

        for root, dirs, files in os.walk(self.extract_dir):
            for f in files:
                if f.lower().endswith(".nuspec"):
                    nuspec_path = Path(root) / f
                    try:
                        tree = etree.parse(str(nuspec_path))
                        root_elem = tree.getroot()
                        ns = {"n": root_elem.nsmap.get(None, "")}

                        metadata = root_elem.find("n:metadata", ns) if ns["n"] else root_elem.find("metadata")
                        if metadata is not None:
                            id_elem = metadata.find("n:id", ns) if ns["n"] else metadata.find("id")
                            ver_elem = metadata.find("n:version", ns) if ns["n"] else metadata.find("version")
                            desc_elem = metadata.find("n:description", ns) if ns["n"] else metadata.find("description")

                            if id_elem is not None and id_elem.text:
                                self.project_info["nuget_id"] = id_elem.text
                            if ver_elem is not None and ver_elem.text:
                                self.project_info["nuget_version"] = ver_elem.text
                            if desc_elem is not None and desc_elem.text:
                                self.project_info.setdefault("project_description", desc_elem.text)

                        logger.info(f"Parsed nuspec: {nuspec_path.name}")

                    except Exception as e:
                        logger.warning(f"Failed to parse nuspec {nuspec_path}: {e}")
                    return

    def _should_skip(self, member_name):
        """Check if a zip member should be skipped during extraction."""
        parts = Path(member_name).parts

        # Skip unwanted directories
        for part in parts:
            if part in self.SKIP_DIRS:
                return True

        # Skip unwanted file extensions
        suffix = Path(member_name).suffix.lower()
        if suffix in self.SKIP_FILES:
            return True

        return False

    def _build_result(self, source_type):
        """Build and return the final extraction result."""

        # Sort XAML files: Main.xaml first, then alphabetical
        sorted_files = self._sort_xaml_files(self.xaml_files)

        result = {
            "xaml_files": sorted_files,
            "project_info": self.project_info,
            "source_type": source_type,
            "extract_dir": self.extract_dir,
            "file_count": len(sorted_files),
        }

        # Log summary
        logger.info(f"Extraction complete:")
        logger.info(f"  Source type : {source_type}")
        logger.info(f"  Project    : {self.project_info.get('project_name', 'unknown')}")
        logger.info(f"  XAML files : {len(sorted_files)}")
        for xf in sorted_files:
            logger.info(f"    - {xf.name}")

        return result

    @staticmethod
    def _sort_xaml_files(xaml_files):
        """Sort XAML files with Main.xaml first, then alphabetically.

        Priority order:
        1. Main.xaml
        2. Other files alphabetically
        """
        main_files = []
        other_files = []

        for f in xaml_files:
            if f.name.lower() == "main.xaml":
                main_files.append(f)
            else:
                other_files.append(f)

        other_files.sort(key=lambda p: p.name.lower())

        return main_files + other_files

    @staticmethod
    def get_relative_path(xaml_path, base_dir):
        """Get the relative path of a XAML file from the base directory.

        Useful for preserving project structure in IR.
        """
        try:
            return xaml_path.relative_to(base_dir)
        except ValueError:
            return xaml_path


def extract_input(input_path, extract_dir=None):
    """Convenience function for extraction.

    Args:
        input_path: Path to .xaml, .zip, or .nupkg
        extract_dir: Optional extraction directory

    Returns:
        dict with xaml_files, project_info, source_type, extract_dir
    """
    extractor = Extractor(input_path, extract_dir)
    return extractor.extract()