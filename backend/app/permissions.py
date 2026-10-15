import re
import urllib.parse
import ipaddress
import logging
from typing import Dict, Any, Optional, Set, List
from pathlib import Path

logger = logging.getLogger(__name__)


class ToolPermissionError(Exception):
    """
    Raised when a tool call violates security permissions or access policies.
    """
    pass


class ToolPermissionValidator:
    """
    Enforces security boundaries on model tool calls before execution:
    1. Read-only enforcement (non-read-only actions blocked in MVP).
    2. Arbitrary executable command prevention.
    3. Filesystem root and directory traversal prevention.
    4. SSRF and private callback URL prevention.
    5. Environment variable injection/leak prevention.
    """

    # Dangerous URL schemes
    BLOCKED_URL_SCHEMES = {
        "file", "gopher", "dict", "ftp", "ldap", "data", "expect", "tftp"
    }

    # Hostnames that resolve to local/internal/metadata services
    BLOCKED_HOSTNAMES = {
        "localhost", "127.0.0.1", "0.0.0.0", "::1",
        "169.254.169.254", "metadata.google.internal",
        "instance-data", "kubernetes.default.svc"
    }

    # Disallowed argument keys for arbitrary execution / env
    DISALLOWED_ARG_KEYS = {
        "command", "cmd", "exec", "shell", "shell_cmd", "bash",
        "powershell", "env", "env_vars", "environment"
    }

    # Shell injection metacharacters
    SHELL_INJECTION_PATTERN = re.compile(
        r"(?:[;&|`$><]|\b(?:rm\s+-rf|curl\s+|wget\s+|chmod\s+|bash\s+-c|powershell\b))",
        re.IGNORECASE
    )

    # Environment variable patterns
    ENV_INJECTION_PATTERN = re.compile(
        r"(?:\$[A-Za-z_][A-Za-z0-9_]*|\$\{[^}]+\}|%[A-Za-z_][A-Za-z0-9_]*%)"
    )

    # Disallowed filesystem root prefixes
    DISALLOWED_ROOT_PREFIXES = (
        "/etc", "/var", "/usr", "/root", "/bin", "/sbin", "/sys", "/proc", "/dev",
        "C:\\Windows", "C:\\Program Files", "C:\\System32", "\\\\",
        "c:/windows", "c:/program files", "c:/system32"
    )

    def __init__(
        self,
        read_only_tools: Optional[Set[str]] = None,
        allowed_workspace_root: Optional[Path] = None,
    ):
        # Default read_only tools for MVP (e.g. echo_echo, echo_add, echo_slow_echo)
        self.read_only_tools = set(read_only_tools) if read_only_tools else set()
        self.allowed_workspace_root = (
            allowed_workspace_root.resolve() if allowed_workspace_root else Path.cwd().resolve()
        )

    def register_read_only_tool(self, tool_name: str) -> None:
        self.read_only_tools.add(tool_name)

    def is_read_only(self, tool_name: str) -> bool:
        # If no specific tools are registered, treat allowlisted echo tools as read-only by default
        if not self.read_only_tools:
            return True
        return tool_name in self.read_only_tools

    def validate_tool_call(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        is_write_confirmed: bool = False,
    ) -> None:
        """
        Validate tool call before execution.
        Raises ToolPermissionError if any check fails.
        """
        # 1. Enforce read-only tool policy
        if not self.is_read_only(tool_name) and not is_write_confirmed:
            raise ToolPermissionError(
                f"Permission denied: Tool '{tool_name}' is not authorized as a read-only tool "
                f"and requires an explicit confirmation token."
            )

        # 2. Inspect argument keys and values
        self._validate_arguments(tool_name, arguments)

    def _validate_arguments(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        for key, value in arguments.items():
            key_lower = key.lower()

            # Check disallowed argument keys
            if key_lower in self.DISALLOWED_ARG_KEYS:
                raise ToolPermissionError(
                    f"Permission denied: Argument key '{key}' permits arbitrary execution "
                    f"or environment access in tool '{tool_name}'."
                )

            # Check argument values recursively
            if isinstance(value, str):
                self._check_string_safety(key, value, tool_name)
            elif isinstance(value, dict):
                self._validate_arguments(tool_name, value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        self._check_string_safety(key, item, tool_name)
                    elif isinstance(item, dict):
                        self._validate_arguments(tool_name, item)

    def _check_string_safety(self, key: str, value: str, tool_name: str) -> None:
        val_trimmed = value.strip()

        # A. Check URL safety (SSRF / internal callbacks)
        if val_trimmed.startswith(("http://", "https://", "file://", "ftp://", "gopher://", "dict://", "data://")):
            self._check_url_safety(val_trimmed, tool_name)

        # B. Check Path safety (Traversal & Filesystem roots)
        if any(indicator in key.lower() for indicator in ("path", "file", "dir", "dest", "src", "target")) or (
            "/" in val_trimmed or "\\" in val_trimmed
        ):
            self._check_path_safety(val_trimmed, tool_name)

        # C. Check Shell / Command injection attempts
        # (Exclude simple mathematical expressions or benign text in echo tools)
        if any(cmd_key in key.lower() for cmd_key in ("command", "cmd", "exec", "script", "code")):
            raise ToolPermissionError(
                f"Permission denied: Command execution parameter '{key}' detected in tool '{tool_name}'."
            )

        # If a parameter value attempts clear shell chaining or dangerous execution
        if re.search(r"(?:;\s*(?:rm|curl|wget|chmod|bash|powershell|cat|nc|sh)|\|\s*(?:bash|sh|powershell)|`[^`]+`|\$\([^)]+\))", val_trimmed, re.IGNORECASE):
            raise ToolPermissionError(
                f"Permission denied: Shell command injection sequence detected in argument '{key}'."
            )

        # D. Check Environment variable access/leak attempts
        if any(env_key in key.lower() for env_key in ("env", "environment", "secret", "token")):
            raise ToolPermissionError(
                f"Permission denied: Environment variable manipulation detected in argument '{key}'."
            )

        if self.ENV_INJECTION_PATTERN.search(val_trimmed) and any(
            var in val_trimmed.upper() for var in ("PATH", "SECRET", "KEY", "TOKEN", "PASSWORD", "DATABASE")
        ):
            raise ToolPermissionError(
                f"Permission denied: Attempted environment variable extraction in argument '{key}'."
            )

    def _check_url_safety(self, url_str: str, tool_name: str) -> None:
        try:
            parsed = urllib.parse.urlparse(url_str)
            scheme = (parsed.scheme or "").lower()

            if scheme in self.BLOCKED_URL_SCHEMES:
                raise ToolPermissionError(
                    f"Permission denied: Blocked dangerous URL scheme '{scheme}:' in tool '{tool_name}'."
                )

            hostname = (parsed.hostname or "").lower()
            if hostname in self.BLOCKED_HOSTNAMES:
                raise ToolPermissionError(
                    f"Permission denied: Blocked internal/private callback URL to '{hostname}'."
                )

            # Check for private / loopback IP address
            try:
                ip = ipaddress.ip_address(hostname)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    raise ToolPermissionError(
                        f"Permission denied: Blocked SSRF attempt to private IP '{hostname}'."
                    )
            except ValueError:
                # Hostname is a domain name, not an IP literal
                pass

        except ToolPermissionError:
            raise
        except Exception as e:
            logger.warning(f"Error parsing URL {url_str}: {e}")

    def _check_path_safety(self, path_str: str, tool_name: str) -> None:
        normalized = path_str.replace("\\", "/")
        normalized_lower = normalized.lower()

        # 1. Directory traversal detection
        if ".." in normalized.split("/"):
            raise ToolPermissionError(
                f"Permission denied: Directory traversal ('..') detected in path '{path_str}'."
            )

        # 2. Filesystem roots and sensitive system directories
        # Check POSIX root, UNC paths, or Windows drive letters
        if (
            normalized_lower.startswith(("/", "\\\\", "//"))
            or re.match(r"^[a-zA-Z]:", normalized)
        ):
            for prefix in self.DISALLOWED_ROOT_PREFIXES:
                p_norm = prefix.replace("\\", "/").lower()
                if normalized_lower.startswith(p_norm):
                    raise ToolPermissionError(
                        f"Permission denied: Access to system root / directory '{path_str}' is forbidden."
                    )
            # If path is an absolute root like "/" or "C:/" or "C:"
            if normalized_lower == "/" or re.match(r"^[a-zA-Z]:/?$", normalized):
                raise ToolPermissionError(
                    f"Permission denied: Root filesystem access '{path_str}' is forbidden."
                )


# Global default validator
tool_permission_validator = ToolPermissionValidator()
