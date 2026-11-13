"""Public MemoryItem bounds shared by storage and frozen Run context."""

MEMORY_KINDS = frozenset({"fact", "preference", "decision", "note"})
MAX_TITLE = 160
MAX_CONTENT_BYTES = 16 * 1024
MAX_TAGS = 16
MAX_TAG_BYTES = 64
MAX_SOURCE = 256
