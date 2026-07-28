"""Schema version constants for persisted GensokyoAI data."""

CONFIG_SCHEMA_VERSION = 1
SESSION_SCHEMA_VERSION = 2
MEMORY_SCHEMA_VERSION = 2
SESSION_EXPORT_SCHEMA_VERSION = 1
CHARACTER_PACKAGE_SCHEMA_VERSION = 1
WORLD_SESSION_SCHEMA_VERSION = 1

GENSOKYOAI_CREATED_BY = "GensokyoAI"

SESSION_FILE_FORMAT = "gensokyoai.session.file"
MEMORY_STORE_FORMAT = "gensokyoai.memory.topic_store"
SESSION_EXPORT_FORMAT = "gensokyoai.session.export"
CHARACTER_PACKAGE_FORMAT = "gensokyoai.character.package"
WORLD_SESSION_FILE_FORMAT = "gensokyoai.world.session.file"


def schema_versions_payload() -> dict[str, int | None]:
    """Return all public schema versions for Runtime version negotiation."""

    return {
        "config": CONFIG_SCHEMA_VERSION,
        "session": SESSION_SCHEMA_VERSION,
        "memory": MEMORY_SCHEMA_VERSION,
        "session_export": SESSION_EXPORT_SCHEMA_VERSION,
        "character_package": CHARACTER_PACKAGE_SCHEMA_VERSION,
    }
