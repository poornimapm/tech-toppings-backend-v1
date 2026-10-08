"""Platform layer: capabilities shared by all modules (auth, users, module registry, ...).

Modules may import only what this package re-exports. Platform code may import ``app.core``
but never a module (enforced by import-linter).
"""

from beanie import Document

# Beanie document models owned by the platform; registered with the database at startup.
PLATFORM_DOCUMENTS: tuple[type[Document], ...] = ()

__all__ = ["PLATFORM_DOCUMENTS"]
