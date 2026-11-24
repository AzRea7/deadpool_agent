"""Allowlist: which paths may be deleted automatically, which services may be touched, and what
may never be deleted by anyone, approved or not.
"""

import re
from dataclasses import dataclass, field

# Roots that Tier 0 deletes may touch, per platform. Deletes never leave these roots.
AUTO_DELETE_ROOTS = {
    "windows": (
        "C:\\inetpub\\logs\\LogFiles",
        "F:\\iislogs",
        "C:\\Windows\\Temp",
        "C:\\ProgramData\\Microsoft\\Windows\\WER",
    ),
    "linux": (
        "/var/log/apache2",
        "/data/apache-logs",
        "/var/log/journal",
        "/var/cache/apt",
        "/tmp",
    ),
}

SERVICES = {
    "windows": {"pools": ("DefaultAppPool",), "services": ("WAS", "W3SVC")},
    "linux": {"pools": (), "services": ("apache2", "httpd")},
}

# Never deletable, even by an approved Tier 2 delete_paths. Matched case-insensitively.
NEVER = [
    r"wwwroot",
    r"^/var/www(/|$)",
    r"uploads?",
    r"\.(mdf|ldf|db|sqlite3?)$",
    r"^/var/lib/(mysql|postgresql|docker)(/|$)",
    r"^c:\\users\\",
    r"^/home/",
    r"^/root(/|$)",
    r"^c:\\windows\\(system32|winsxs)",
    r"^/(etc|boot|usr|bin|sbin|lib)(/|$)",
    r"^/srv/",
]


class AllowlistError(ValueError):
    pass


@dataclass(frozen=True)
class Allowlist:
    delete_roots: dict[str, tuple[str, ...]] = field(default_factory=lambda: AUTO_DELETE_ROOTS)
    services: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=lambda: SERVICES)
    never: tuple[str, ...] = tuple(NEVER)

    def check_never(self, path: str) -> None:
        if ".." in path.replace("\\", "/").split("/"):
            raise AllowlistError(f"{path!r}: '..' is not allowed")
        for pattern in self.never:
            if re.search(pattern, path, re.IGNORECASE):
                raise AllowlistError(f"{path!r} matches never-delete rule {pattern!r}")

    def under_auto_root(self, platform: str, path: str) -> bool:
        sep = "\\" if platform == "windows" else "/"
        for root in self.delete_roots.get(platform, ()):
            r, p = (root.lower(), path.lower()) if platform == "windows" else (root, path)
            if p == r or p.startswith(r.rstrip(sep) + sep):
                return True
        return False

    def check_service(self, platform: str, kind: str, name: str) -> None:
        allowed = self.services.get(platform, {}).get(kind, ())
        if name not in allowed:
            raise AllowlistError(f"{kind[:-1]} {name!r} is not on the {platform} allowlist")


DEFAULT = Allowlist()
