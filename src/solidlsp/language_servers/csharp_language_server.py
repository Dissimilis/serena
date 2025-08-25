"""
CSharp Language Server using csharp-ls (Roslyn-based LSP server)
"""
import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import cast
from overrides import override
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_logger import LanguageServerLogger
from solidlsp.ls_utils import PathUtils
from solidlsp.lsp_protocol_handler.lsp_types import InitializeParams
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo
from solidlsp.settings import SolidLSPSettings

def breadth_first_file_scan(root_dir):
    """
    Perform a breadth-first scan of files in the given directory.
    Yields file paths in breadth-first order.
    """
    queue = [root_dir]
    while queue:
        current_dir = queue.pop(0)
        try:
            for item in os.listdir(current_dir):
                if item.startswith("."):
                    continue
                item_path = os.path.join(current_dir, item)
                if os.path.isdir(item_path):
                    queue.append(item_path)
                elif os.path.isfile(item_path):
                    yield item_path
        except (PermissionError, OSError):
            # Skip directories we can't access
            pass

def find_solution_or_project_file(root_dir) -> str | None:
    """
    Find the first .sln file in breadth-first order.
    If no .sln file is found, look for a .csproj file.
    """
    sln_file = None
    csproj_file = None
    for filename in breadth_first_file_scan(root_dir):
        if filename.endswith(".sln") and sln_file is None:
            sln_file = filename
        elif filename.endswith(".csproj") and csproj_file is None:
            csproj_file = filename
        # If we found a .sln file, return it immediately
        if sln_file:
            return sln_file
    # If no .sln file was found, return the first .csproj file
    return csproj_file

class CSharpLanguageServer(SolidLanguageServer):
    """
    Provides C# specific instantiation of the LanguageServer class using csharp-ls.
    This is a lightweight Roslyn-based language server.
    """
    def __init__(
        self, config: LanguageServerConfig, logger: LanguageServerLogger, repository_root_path: str, solidlsp_settings: SolidLSPSettings
    ):
        """
        Creates a CSharpLanguageServer instance. This class is not meant to be instantiated directly.
        Use LanguageServer.create() instead.
        """
        csharp_ls_path = self._ensure_server_installed(logger, config, solidlsp_settings)
        # Find solution or project file
        self.solution_or_project = find_solution_or_project_file(repository_root_path)
        # Create log directory (optional; csharp-ls may not use it, but keep for consistency)
        log_dir = Path(self.ls_resources_dir(solidlsp_settings)) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # Build command
        cmd = [csharp_ls_path, "--logLevel trace"]
        if self.solution_or_project:
            logger.log(f"Found solution/project file: {self.solution_or_project}", logging.INFO)
        else:
            logger.log("No .sln or .csproj file found, language server will attempt auto-discovery", logging.WARNING)
        logger.log(f"Language server command: {' '.join(cmd)}", logging.DEBUG)
        super().__init__(
            config,
            logger,
            repository_root_path,
            ProcessLaunchInfo(cmd=cmd, cwd=repository_root_path),
            "csharp",
            solidlsp_settings,
        )
        self.initialization_complete = threading.Event()

    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        return super().is_ignored_dirname(dirname) or dirname in ["bin", "obj", "packages", ".vs"]

    @classmethod
    def _ensure_server_installed(
        cls, logger: LanguageServerLogger, config: LanguageServerConfig, solidlsp_settings: SolidLSPSettings
    ) -> str:
        """
        Ensure csharp-ls is available in PATH.
        Returns the path to csharp-ls executable.
        """
        csharp_ls_path = shutil.which("csharp-ls")
        if not csharp_ls_path:
            raise SolidLSPException("csharp-ls not found in PATH. Ensure it's installed globally via 'dotnet tool install --global csharp-ls'.")
        logger.log(f"Found csharp-ls at {csharp_ls_path}", logging.INFO)
        return csharp_ls_path

    def _get_initialize_params(self) -> InitializeParams:
        """
        Returns the initialize params for csharp-ls.
        """
        root_uri = PathUtils.path_to_uri(self.repository_root_path)
        root_name = os.path.basename(self.repository_root_path)
        init_params = cast(
            InitializeParams,
            {
                "workspaceFolders": [{"uri": root_uri, "name": root_name}],
                "processId": os.getpid(),
                "rootPath": self.repository_root_path,
                "rootUri": root_uri,
                "capabilities": {
                    "window": {
                        "workDoneProgress": True,
                        "showMessage": {"messageActionItem": {"additionalPropertiesSupport": True}},
                        "showDocument": {"support": True},
                    },
                    "workspace": {
                        "applyEdit": True,
                        "workspaceEdit": {"documentChanges": True},
                        "didChangeConfiguration": {"dynamicRegistration": True},
                        "didChangeWatchedFiles": {"dynamicRegistration": True},
                        "symbol": {
                            "dynamicRegistration": True,
                            "symbolKind": {"valueSet": list(range(1, 27))},
                        },
                        "executeCommand": {"dynamicRegistration": True},
                        "configuration": True,
                        "workspaceFolders": True,
                        "workDoneProgress": True,
                    },
                    "textDocument": {
                        "synchronization": {"dynamicRegistration": True, "willSave": True, "willSaveWaitUntil": True, "didSave": True},
                        "hover": {"dynamicRegistration": True, "contentFormat": ["markdown", "plaintext"]},
                        "signatureHelp": {
                            "dynamicRegistration": True,
                            "signatureInformation": {
                                "documentationFormat": ["markdown", "plaintext"],
                                "parameterInformation": {"labelOffsetSupport": True},
                            },
                        },
                        "definition": {"dynamicRegistration": True},
                        "references": {"dynamicRegistration": True},
                        "documentSymbol": {
                            "dynamicRegistration": True,
                            "symbolKind": {"valueSet": list(range(1, 27))},
                            "hierarchicalDocumentSymbolSupport": True,
                        },
                    },
                },
            },
        )
        # Add csharp-ls specific initialization options
        if self.solution_or_project:
            init_params["initializationOptions"] = {
                "csharp": {
                    "solution": self.solution_or_project,
                    "applyFormattingOptions": True  # Optional: Use client formatting (overrides .editorconfig if True)
                }
            }
        return init_params

    def _start_server(self):
        def do_nothing(params):
            return

        def window_log_message(msg):
            """Log messages from the language server."""
            message_text = msg.get("message", "")
            level = msg.get("type", 4)  # Default to Log level
            # Map LSP message types to Python logging levels
            level_map = {1: logging.ERROR, 2: logging.WARNING, 3: logging.INFO, 4: logging.DEBUG}
            self.logger.log(f"LSP: {message_text}", level_map.get(level, logging.DEBUG))

        def handle_progress(params):
            """Handle progress notifications from the language server."""
            token = params.get("token", "")
            value = params.get("value", {})
            # Log raw progress for debugging
            self.logger.log(f"Progress notification received: {params}", logging.DEBUG)
            kind = value.get("kind")
            if kind == "begin":
                title = value.get("title", "Operation in progress")
                message = value.get("message", "")
                percentage = value.get("percentage")
                if percentage is not None:
                    self.logger.log(f"Progress [{token}]: {title} - {message} ({percentage}%)", logging.INFO)
                else:
                    self.logger.log(f"Progress [{token}]: {title} - {message}", logging.INFO)
            elif kind == "report":
                message = value.get("message", "")
                percentage = value.get("percentage")
                if percentage is not None:
                    self.logger.log(f"Progress [{token}]: {message} ({percentage}%)", logging.INFO)
                elif message:
                    self.logger.log(f"Progress [{token}]: {message}", logging.INFO)
            elif kind == "end":
                message = value.get("message", "Operation completed")
                self.logger.log(f"Progress [{token}]: {message}", logging.INFO)

        def handle_workspace_configuration(params):
            """Handle workspace/configuration requests from the server."""
            items = params.get("items", [])
            result = []
            for item in items:
                section = item.get("section", "")
                # Provide csharp-ls specific values
                if section == "csharp.solution":
                    result.append(self.solution_or_project if self.solution_or_project else None)
                elif section.startswith(("dotnet", "csharp")):
                    # Default configuration for C# settings
                    if "enable" in section or "show" in section or "suppress" in section or "navigate" in section:
                        # Boolean settings
                        result.append(False)
                    elif "scope" in section:
                        # Scope settings - use appropriate enum values
                        if "analyzer_diagnostics_scope" in section:
                            result.append("openFiles")  # BackgroundAnalysisScope
                        elif "compiler_diagnostics_scope" in section:
                            result.append("openFiles")  # CompilerDiagnosticsScope
                        else:
                            result.append("openFiles")
                    elif section == "dotnet_member_insertion_location":
                        # ImplementTypeInsertionBehavior enum
                        result.append("with_other_members_of_the_same_kind")
                    elif section == "dotnet_property_generation_behavior":
                        # ImplementTypePropertyGenerationBehavior enum
                        result.append("prefer_throwing_properties")
                    elif "location" in section or "behavior" in section:
                        # Other enum settings - return null to avoid parsing errors
                        result.append(None)
                    else:
                        # Default for other dotnet/csharp settings
                        result.append(None)
                elif section == "tab_width" or section == "indent_size":
                    # Tab and indent settings
                    result.append(4)
                elif section == "insert_final_newline":
                    # Editor settings
                    result.append(True)
                else:
                    # Unknown configuration - return null
                    result.append(None)
            return result

        def handle_work_done_progress_create(params):
            """Handle work done progress create requests."""
            # Just acknowledge the request
            return

        def handle_register_capability(params):
            """Handle client/registerCapability requests."""
            # Just acknowledge the request - we don't need to track these for now
            return

        def handle_project_needs_restore(params):
            return

        # Set up notification handlers
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", handle_progress)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)
        self.server.on_request("workspace/configuration", handle_workspace_configuration)
        self.server.on_request("window/workDoneProgress/create", handle_work_done_progress_create)
        self.server.on_request("client/registerCapability", handle_register_capability)
        self.server.on_request("workspace/_roslyn_projectNeedsRestore", handle_project_needs_restore)
        self.logger.log("Starting csharp-ls process", logging.INFO)
        try:
            self.server.start()
        except Exception as e:
            self.logger.log(f"Failed to start language server process: {e}", logging.ERROR)
            raise SolidLSPException(f"Failed to start C# language server: {e}")
        # Send initialization
        initialize_params = self._get_initialize_params()
        self.logger.log("Sending initialize request to language server", logging.INFO)
        try:
            init_response = self.server.send.initialize(initialize_params)
            self.logger.log(f"Received initialize response: {init_response}", logging.DEBUG)
        except Exception as e:
            raise SolidLSPException(f"Failed to initialize C# language server for {self.repository_root_path}: {e}") from e
        # Verify required capabilities
        capabilities = init_response.get("capabilities", {})
        required_capabilities = [
            "textDocumentSync",
            "definitionProvider",
            "referencesProvider",
            "documentSymbolProvider",
        ]
        missing = [cap for cap in required_capabilities if cap not in capabilities]
        if missing:
            raise RuntimeError(
                f"Language server is missing required capabilities: {', '.join(missing)}. "
                "Initialization failed. Please ensure csharp-ls is installed and compatible."
            )
        # Complete initialization
        self.server.notify.initialized({})
        self.initialization_complete.set()
        self.completions_available.set()
        self.logger.log(
            "csharp-ls initialized and ready\n"
            "Waiting for language server to index project files...\n"
            "This may take a while for large projects",
            logging.INFO,
        )

    @override
    def _get_wait_time_for_cross_file_referencing(self) -> float:
        return 1
