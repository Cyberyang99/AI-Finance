"""外部文档摄入模块 — PDF/DOCX/XLSX/PPTX → CoT 三段式 + 用户论点."""

from .base import ingest_file, SUPPORTED_EXT
from .cot_extractor import extract_cot
from .user_note import (
    save_user_note, load_user_notes, auto_structure, auto_structure_from_doc,
    save_note_12d, inherit_sector_tags, append_to_today_note,
)

__all__ = ["ingest_file", "SUPPORTED_EXT", "extract_cot",
           "save_user_note", "load_user_notes", "auto_structure", "auto_structure_from_doc",
           "save_note_12d", "inherit_sector_tags", "append_to_today_note"]
