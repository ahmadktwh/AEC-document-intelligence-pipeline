# ==============================================================================
# PROPRIETARY AND CONFIDENTIAL - STRICTLY RESTRICTED USE ONLY
# ------------------------------------------------------------------------------
# Copyright (c) 2026 Mujeeb Ahmad. All rights reserved.
# Unauthorized commercial reproduction, distribution, replication, or hosting
# of this proprietary software, in whole or in part, is strictly prohibited.
# Licensed solely for authorized technical review and demonstration purposes.
# ==============================================================================

"""
extractor_worker.py — Isolated Extraction with Instructor + Vision Fallback
"""

import os
import re
import time
import json
import base64
import threading
import boto3
import logging
import fitz  # PyMuPDF for page-to-image conversion
import instructor
from botocore.exceptions import ClientError
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal
from enum import Enum
from src.utils.gcp_helper import setup_gcp_credentials
from src.database.token_ledger import token_ledger_db
from src.utils.rate_limiter import GlobalRateLimiter
from src.core.vault import PromptVault

# Standardized logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ExtractorWorker")

# Module-level shared rate limiter — one instance, used everywhere
_GEMINI_LIMITER = GlobalRateLimiter("GLOBAL_GEMINI_LOCK", rpm_limit=5, token_limit=20000)

_OUTPUT_LEAK_PATTERNS = re.compile(
    r'('
    r'\b(field extraction guide|status rules|critical rules|return complete json|'
    r'response_schema|pydantic|schema validation|system prompt|developer instruction|'
    r'you are a|your task|must be exactly|never invent|copy evidence_excerpt|'
    r'extraction_method=|source_document=|page_reference=)\b'
    r')',
    re.IGNORECASE
)


def _sanitize_llm_string(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "N/S"
    if _OUTPUT_LEAK_PATTERNS.search(text):
        logger.warning(f"Validator: detected prompt/schema leakage in '{field_name}'. Sanitizing.")
        if field_name == "evidence_excerpt":
            return "[SANITIZED: prompt/schema leakage removed; verify source evidence]"
        return "N/S"
    return text


# ─── STRICT PYDANTIC SCHEMA ───────────────────────────────────────────────────

RowStatus = Literal["confirmed", "verification_needed", "reference_only", "excluded"]


class SpecDetailRow(BaseModel):
    """
    A single, first-class specification row for the Spec Detail Ledger.
    CRITICAL: This represents ONE product only. Never merge multiple tags.
    """

    finish_tag: str = Field(
        ...,
        description="The EXACT finish tag being extracted (e.g., PL-1, LVT-2). "
                    "MUST match the assigned target tag exactly. NEVER 'N/S'."
    )
    area_scope: str = Field(
        ...,
        description="The room, area or scope where this tag is applied "
                    "(e.g., 'Suite C & D', 'Lobby'). Use 'N/S' only if genuinely absent."
    )
    manufacturer: str = Field(
        ...,
        description="Brand name only (e.g., 'Armstrong', 'Mohawk'). Use 'N/S' if not found."
    )
    model_series: str = Field(
        ...,
        description="Product line or series name. Use 'N/S' if not found."
    )
    finish_color: str = Field(
        ...,
        description="Exact color name AND code if present. Use 'N/S' if not found. DO NOT guess."
    )
    size: str = Field(default="N/S", description="Physical dimensions (e.g., '12x24', '6x36 plank')")
    product_criteria: str = Field(default="N/S", description="Technical specs (e.g., '20 mil wear layer')")
    installation_criteria: str = Field(default="N/S", description="Installation method")
    standards_codes: str = Field(default="N/S", description="Standards referenced (e.g., 'ASTM F1700')")
    csi_division: str = Field(default="N/S", description="CSI division (e.g., '09')")
    csi_section: str = Field(default="N/S", description="CSI section number (e.g., '09 65 00')")
    finish_type: str = Field(
        default="N/S",
        description=(
            "Surface finish sheen or texture if specified: "
            "e.g. 'MATTE', 'SEMI GLOSS', 'GLOSS', 'SATIN', 'EGGSHELL', "
            "'TEXTURED', 'SMOOTH', 'POLISHED'. Extract from TYPE/SIZE or NOTES column."
        )
    )
    special_remarks: str = Field(
        default="N/S",
        description=(
            "ANY notes, basis-of-design callouts, approved alternates, TBD placeholders, "
            "'match existing', 'see spec section XX', fire rating notes, ADA notes, "
            "grout pairing notes (e.g. 'w/ CT-1'), or any data that does not fit other fields. "
            "Copy verbatim. Do NOT discard this data."
        )
    )
    source_document: str = Field(..., description="Source PDF filename")
    page_reference: str = Field(..., description="Exact page number")
    evidence_id: str = Field(..., description="Unique chunk ID for traceability")
    evidence_excerpt: str = Field(..., description="EXACT verbatim text from source. Do NOT paraphrase.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence 0.0-1.0 all fields correct")
    status: RowStatus = Field(default="confirmed")
    extraction_method: str = Field(default="text")

    @field_validator(
        'area_scope', 'manufacturer', 'model_series', 'finish_color', 'size',
        'product_criteria', 'installation_criteria', 'standards_codes',
        'csi_division', 'csi_section', 'finish_type', 'special_remarks',
        'source_document', 'page_reference', 'evidence_id', 'evidence_excerpt',
        mode='before'
    )
    @classmethod
    def scrub_prompt_leakage(cls, v, info):
        return _sanitize_llm_string(v, info.field_name)

    # FIX-4: Strengthened validator — rejects N/S, long hallucinations, wrong tags
    # target_tag is injected as a class variable before instantiation
    @field_validator('finish_tag')
    @classmethod
    def normalize_tag(cls, v):
        v = str(v).strip().upper()

        # Get injected target for enforcement
        target = getattr(cls, '_target_tag', None)
        target_upper = target.strip().upper() if target else None

        # Rule 1: Never allow N/S as a finish_tag — always fall back to target
        if v in ('N/S', 'NS', 'N/A', 'NA', 'NONE', 'NULL', ''):
            logger.warning(f"Validator: LLM returned finish_tag='{v}'. Replacing with target '{target_upper}'.")
            return target_upper if target_upper else v

        # Rule 2: Reject hallucinated descriptions longer than 20 chars
        # e.g. "MARBLE THRESHOLDS", "VINYL COMPOSITION TILE"
        if len(v) > 20:
            logger.warning(f"Validator: finish_tag='{v}' too long (hallucination). Attempting extraction.")
            match = re.match(r'^([A-Z]{1,6}[-]?[A-Z]{0,2}\d{0,4}[A-Z]{0,2})', v)
            if match:
                extracted = match.group(1)
                logger.warning(f"Validator: Extracted '{extracted}' from long tag.")
                return extracted
            return target_upper if target_upper else v[:20]

        return v

    @field_validator('extraction_method')
    @classmethod
    def sanitize_extraction_method(cls, v):
        value = str(v or "text").strip().lower()
        allowed = {
            "text", "vision", "pinecone_direct", "dry_run",
            "text_unverified", "vision_unverified"
        }
        if value in allowed:
            return value
        if "pinecone" in value:
            return "pinecone_direct"
        if "vision" in value:
            return "vision"
        if "dry" in value:
            return "dry_run"
        return "text"


# ─── EVIDENCE TYPE DETECTOR ───────────────────────────────────────────────────

class EvidenceType(str, Enum):
    FLAT_TEXT = "flat_text"
    TABLE_CHUNK = "table_chunk"


def detect_evidence_type(chunk_text: str, metadata: dict = None) -> EvidenceType:
    if metadata:
        if metadata.get("extraction_method") in ("pdfplumber_table", "vision_scanned", "table_row_nl"):
            return EvidenceType.TABLE_CHUNK
        if metadata.get("chunk_type") in ("table_row", "overflow", "router_fallback"):
            return EvidenceType.TABLE_CHUNK

    if chunk_text.count('|') >= 2:
        return EvidenceType.TABLE_CHUNK

    tag_pattern = re.compile(r'[A-Z]{1,5}-\d{1,3}', re.IGNORECASE)
    found_tags = tag_pattern.findall(chunk_text)
    if len(found_tags) >= 3 and len(chunk_text) < 600:
        return EvidenceType.TABLE_CHUNK

    kv_patterns = [
        r'manufacturer[:\s]', r'model[:\s]', r'color[:\s]',
        r'finish[:\s]', r'size[:\s]', r'series[:\s]', r'product[:\s]'
    ]
    kv_hits = sum(1 for p in kv_patterns if re.search(p, chunk_text, re.IGNORECASE))
    if kv_hits >= 2:
        return EvidenceType.TABLE_CHUNK

    return EvidenceType.FLAT_TEXT


# ─── VISION LAYER ────────────────────────────────────────────────────────────

def extract_page_as_image_bytes(pdf_path: str, page_num: int, dpi: int = 150) -> bytes:
    """
    Returns raw PNG bytes for Gemini Vision.
    DPI=150 is sufficient for text reading and produces ~60% smaller images than DPI=200.

    Raises RuntimeError if:
      - page_num is out of range
      - rendered image is empty
    """
    doc = fitz.open(pdf_path)
    total_pages = len(doc)

    if page_num < 1 or page_num > total_pages:
        doc.close()
        raise RuntimeError(
            f"Page {page_num} is out of range. PDF has {total_pages} pages. "
            f"Cannot render for Vision. (This causes the 'from_bytes' error.)"
        )

    page = doc[page_num - 1]
    mat  = fitz.Matrix(dpi / 72, dpi / 72)
    pix  = page.get_pixmap(matrix=mat, alpha=False)

    # Validate pixmap before converting
    if pix.width == 0 or pix.height == 0:
        doc.close()
        raise RuntimeError(f"Page {page_num} rendered to zero-size pixmap. Page may be blank.")

    img_bytes = pix.tobytes("png")
    doc.close()

    if not img_bytes or len(img_bytes) < 500:
        raise RuntimeError(
            f"Page {page_num} produced empty/tiny PNG ({len(img_bytes)} bytes). "
            f"This is the root cause of the 'from_bytes' Vision error."
        )

    return img_bytes


# ─── FLEXIBLE PIPE-ROW PARSER ────────────────────────────────────────────

# Domain configuration resolved from PromptVault at import time.
# Centralised in vault.py to enable vocabulary expansion without agent edits.
_MANUFACTURER_KEYWORDS = PromptVault.get_manufacturer_keywords()
_TAG_PREFIX_MAP        = PromptVault.get_domain_tag_prefix_map()
_HEADER_ALIASES        = PromptVault.get_header_aliases()
_OCR_CONFUSABLES       = PromptVault.get_ocr_confusables()
_EMPTY_MARKERS         = PromptVault.get_empty_markers()

_DIM_PATTERN    = re.compile(r'\d+[\"\']\s*[xX\u00d7]\s*\d+|\d+[\"\']\s*[HhWw]|\d+\s*[Mm][Mm]', re.IGNORECASE)
_CSI_PATTERN    = re.compile(r'^\d{2}[\s\-]\d{2}')
_TAG_PATTERN    = re.compile(r'^[A-Z]{1,6}[\-\.\s]?\d{0,3}[A-Z]?$', re.IGNORECASE)
_TAG_TOKEN_PATTERN = re.compile(r'\b[A-Z]{1,8}(?:[\-\.\s]?[A-Z0-9]{1,4})?\b', re.IGNORECASE)


def _normalize_tag_text(value: str) -> str:
    value = str(value or "").strip().upper()
    return re.sub(r'[\u2010\u2011\u2012\u2013\u2014\uFE58\uFE63\uFF0D]', '-', value)


def _plain_tag(value: str) -> str:
    return re.sub(r'[^A-Z0-9]', '', _normalize_tag_text(value))


def _chars_ocr_equivalent(a: str, b: str) -> bool:
    if a == b:
        return True
    return any(a in group and b in group for group in _OCR_CONFUSABLES)


def _tags_equivalent(candidate: str, target: str) -> bool:
    """
    Conservative OCR-aware tag equality.

    Allows same-length OCR confusions such as SPORT-1/SP0RT-1 or P-E1/P-El,
    but rejects semantic siblings with inserted/deleted letters such as
    TER-1/TERT-1.
    """
    cand_plain = _plain_tag(candidate)
    target_plain = _plain_tag(target)
    if not cand_plain or not target_plain:
        return False
    if cand_plain == target_plain:
        return True
    if len(cand_plain) != len(target_plain):
        return False
    return all(_chars_ocr_equivalent(a, b) for a, b in zip(cand_plain, target_plain))


def _cell_matches_target_tag(cell: str, target: str) -> bool:
    cell = str(cell or "").strip()
    if not cell:
        return False
    if _tags_equivalent(cell, target):
        return True
    # OCR often leaves punctuation/noise around an otherwise clean tag cell.
    for token in _TAG_TOKEN_PATTERN.findall(_normalize_tag_text(cell)):
        if _tags_equivalent(token, target):
            return True
    return False


def _text_has_target_tag(text: str, target: str) -> bool:
    normalized_text = _normalize_tag_text(text)
    return any(_cell_matches_target_tag(token, target) for token in _TAG_TOKEN_PATTERN.findall(normalized_text))


def _line_tag_collision(line: str, target: str) -> bool:
    """
    True when the line appears to contain a finish tag that is not the target.
    Used to prevent TER-1 from accepting TERT-1 evidence after prompt forcing.
    """
    tokens = [
        t for t in _TAG_TOKEN_PATTERN.findall(_normalize_tag_text(line))
        if re.search(r'\d', t) or "-" in t
    ]
    return bool(tokens) and not any(_tags_equivalent(t, target) for t in tokens)


def _filled(value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    upper = value.upper()
    return not any(upper.startswith(marker) for marker in _EMPTY_MARKERS if marker)


def _looks_like_color(cell: str, all_cells: list = None) -> bool:
    """
    Dynamic color detector: accepts color codes, color names, and short
    alphanumeric identifiers without assuming SW/BM/HC-only paint formats.
    """
    value = str(cell or "").strip()
    if not value or len(value) > 80:
        return False
    upper = value.upper()
    if upper in _EMPTY_MARKERS:
        return False
    if re.search(r'\b(?:COLOR|COLOUR|PAINT|PANTONE)\b', upper):
        return True
    if re.search(r'\b[A-Z]{1,6}[-\s]?\d{1,6}[A-Z]?\b', upper):
        return True
    if re.search(r'\b\d{2,6}[A-Z]?\b', upper) and not _DIM_PATTERN.search(upper):
        return True
    return False


def _manufacturer_mentions(text: str) -> list:
    text_l = str(text or "").lower()
    return [brand for brand in _MANUFACTURER_KEYWORDS if brand in text_l]


def _target_line_windows(text: str, target: str, radius: int = 1) -> list:
    lines = str(text or "").splitlines()
    windows = []
    seen = set()
    for idx, line in enumerate(lines):
        if _text_has_target_tag(line, target):
            start = max(0, idx - radius)
            end = min(len(lines), idx + radius + 1)
            for selected in lines[start:end]:
                key = selected.strip()
                if key and key not in seen:
                    windows.append(selected)
                    seen.add(key)
    return windows


def _rank_chunk_for_target(chunk: dict, target: str) -> float:
    """
    Layout-agnostic target ranking.

    Target/OCR-equivalent tag presence is the primary signal. Manufacturer and
    construction finish vocabulary near that tag are secondary signals. Pipes,
    table shape, and other formatting artifacts are intentionally low/neutral
    because weak OCR often destroys them.
    """
    text = chunk.get("text", "") or ""
    vector_score = float(chunk.get("score", 0.0) or 0.0)
    rank = min(vector_score, 1.0)

    target_windows = _target_line_windows(text, target, radius=1)
    has_target_anywhere = bool(target_windows) or _text_has_target_tag(text, target)
    if has_target_anywhere:
        rank += 30.0

    # A tag-looking cell/line is stronger than an incidental prose mention, but
    # this never depends on pipe delimiters being present.
    for line in str(text).splitlines() or [text]:
        if not _text_has_target_tag(line, target):
            continue
        stripped = line.strip()
        if _cell_matches_target_tag(stripped, target):
            rank += 8.0
        if re.search(r'(^|\s|\|)' + re.escape(target.split("-")[0]), stripped, re.IGNORECASE):
            rank += 2.0
        break

    local_text = "\n".join(target_windows) if target_windows else text[:800]
    local_mfr_hits = _manufacturer_mentions(local_text)
    global_mfr_hits = _manufacturer_mentions(text)
    rank += min(len(local_mfr_hits), 3) * 4.0
    rank += min(max(len(global_mfr_hits) - len(local_mfr_hits), 0), 3) * 1.0

    finish_terms = (
        "manufacturer", "product", "series", "color", "colour", "finish", "size",
        "notes", "terrazzo", "rubber", "vinyl", "tile", "paint", "slate", "wood",
        "acoustical", "resilient", "flooring", "base"
    )
    local_lower = local_text.lower()
    rank += min(sum(1 for term in finish_terms if term in local_lower), 6) * 0.75

    if has_target_anywhere and _line_tag_collision(local_text, target):
        rank -= 6.0
    if not has_target_anywhere and _line_tag_collision(text[:1000], target):
        rank -= 4.0
    return rank


def _extract_target_focused_text(chunk: dict, target: str, max_chars: int = 3500) -> str:
    text = chunk.get("text", "") or ""
    lines = text.splitlines()
    pipe_lines = [line for line in lines if "|" in line]
    target_windows = _target_line_windows(text, target, radius=2)
    if not pipe_lines and not target_windows:
        return text[:max_chars]

    selected = []
    header = next((line for line in pipe_lines if _is_header_row(line)), "")
    if header:
        selected.append(header)
    selected.extend(target_windows)

    for idx, line in enumerate(lines):
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        if any(_cell_matches_target_tag(c, target) for c in cells):
            if idx > 0 and "|" in lines[idx - 1] and not _is_header_row(lines[idx - 1]):
                selected.append(lines[idx - 1])
            selected.append(line)
            if idx + 1 < len(lines) and "|" in lines[idx + 1] and not _is_header_row(lines[idx + 1]):
                selected.append(lines[idx + 1])

    if not selected:
        selected = pipe_lines[:8] if pipe_lines else lines[:20]

    compact = "\n".join(dict.fromkeys(s.strip() for s in selected if s.strip()))
    return compact[:max_chars]


def _pack_evidence_for_llm(evidence_chunks: list, target: str, max_chars: int = 22000, max_chunks: int = 14) -> tuple:
    """
    Rerank and budget evidence for Tier 2.

    Vector score remains useful, but exact/fuzzy target-row presence dominates.
    This prevents high-scoring prose from slicing off the actual pipe row.
    """
    ranked = sorted(evidence_chunks, key=lambda c: _rank_chunk_for_target(c, target), reverse=True)
    blocks = []
    used = 0
    selected_ids = []

    for idx, chunk in enumerate(ranked[:max_chunks], start=1):
        body = _extract_target_focused_text(chunk, target)
        if not body.strip():
            continue
        rank = _rank_chunk_for_target(chunk, target)
        chunk_id = chunk.get("chunk_id", f"chunk_{idx}")
        block = (
            f"[Evidence {idx} | Page {chunk.get('page_num','?')} | "
            f"Chunk {chunk_id} | VectorScore {float(chunk.get('score',0) or 0):.2f} | "
            f"TargetRank {rank:.2f}]\n{body.strip()}"
        )
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining > 1200:
                blocks.append(block[:remaining])
                selected_ids.append(chunk_id)
            break
        blocks.append(block)
        selected_ids.append(chunk_id)
        used += len(block)

    return "\n---\n".join(blocks), selected_ids


def _llm_result_supported_by_evidence(result: SpecDetailRow, target: str) -> bool:
    if not result:
        return False
    if not _tags_equivalent(result.finish_tag, target):
        logger.warning(f"[{target}] LLM returned sibling/wrong finish_tag '{result.finish_tag}'. Rejecting.")
        return False
    excerpt = getattr(result, "evidence_excerpt", "") or ""
    if result.status == "confirmed" or result.confidence >= 0.55:
        if not _text_has_target_tag(excerpt, target):
            logger.warning(f"[{target}] LLM evidence excerpt lacks target tag. Rejecting to avoid conflation.")
            return False
        if _line_tag_collision(excerpt, target):
            logger.warning(f"[{target}] LLM evidence excerpt contains only sibling tags. Rejecting.")
            return False
    return True

# These are NOT hardcoded column indices.
# The parser below reads the HEADER ROW of each table to discover
# column positions dynamically — so any document structure works.

_HEADER_ALIASES = {
    # KEY / TAG column
    "key":          "key",
    "tag":          "key",
    "code":         "key",
    "id":           "key",
    "finish code":  "key",
    "finish tag":   "key",
    "material code":"key",
    # MANUFACTURER
    "manufacturer": "manufacturer",
    "mfr":          "manufacturer",
    "brand":        "manufacturer",
    "supplier":     "manufacturer",
    "vendor":       "manufacturer",
    "maker":        "manufacturer",
    # PRODUCT / MODEL / SERIES
    "product":      "product",
    "series":       "product",
    "model":        "product",
    "line":         "product",
    "style":        "product",
    "pattern":      "product",
    "collection":   "product",
    "item":         "product",
    "name":         "product",
    # COLOR
    "color":        "color",
    "colour":       "color",
    "finish":       "color",
    "shade":        "color",
    "glaze":        "color",
    "hue":          "color",
    # SIZE / DIMENSIONS
    "type/size":    "size",
    "size":         "size",
    "dimension":    "size",
    "dimensions":   "size",
    "type":         "size",
    "thickness":    "size",
    "format":       "size",
    # NOTES / AREA / REMARKS
    "notes":        "notes",
    "note":         "notes",
    "remarks":      "notes",
    "remark":       "notes",
    "comment":      "notes",
    "location":     "notes",
    "area":         "notes",
    "room":         "notes",
    "scope":        "notes",
    "application":  "notes",
    "use":          "notes",
    # CSI SECTION
    "specification section": "csi_section",
    "spec section":  "csi_section",
    "section":       "csi_section",
    "csi":           "csi_section",
    "division":      "csi_section",
    # FINISH TYPE / MATERIAL DESC
    "finish type":   "finish_type",
    "material":      "finish_type",
    "description":   "finish_type",
    "material type": "finish_type",
    "type":          "finish_type",
}


def _detect_column_map(header_line: str) -> dict:
    """
    Given a pipe-delimited header row, returns a dict mapping
    field names → column index. Works for ANY document structure.

    Example:
      "SPEC SECTION | FINISH TYPE | KEY | | MANUFACTURER | PRODUCT | COLOR | SIZE | NOTES"
      → {"csi_section":0, "finish_type":1, "key":2, "manufacturer":4,
          "product":5, "color":6, "size":7, "notes":8}
    """
    cells = [c.strip().lower() for c in header_line.split("|")]
    col_map = {}
    for idx, cell in enumerate(cells):
        matched = _HEADER_ALIASES.get(cell)
        if not matched:
            # Try partial match — e.g. "specification section" contains "section"
            for alias, field in _HEADER_ALIASES.items():
                if alias in cell or cell in alias:
                    matched = field
                    break
        if matched and matched not in col_map:
            col_map[matched] = idx
    return col_map


def _is_header_row(line: str) -> bool:
    """
    Detects whether a pipe-delimited line is a header row
    (contains column label words rather than data values).
    """
    cells = [c.strip().lower() for c in line.split("|") if c.strip()]
    if not cells:
        return False
    header_words = {
        "key", "tag", "code", "manufacturer", "mfr", "product", "series",
        "color", "colour", "finish", "size", "notes", "remarks", "section",
        "type", "description", "material", "specification", "model", "area",
        "location", "room", "division", "csi", "brand", "shade", "pattern",
        "style", "collection", "dimensions", "format", "scope", "application"
    }
    hits = sum(1 for c in cells if any(w in c for w in header_words))
    return hits >= 2


def _fallback_column_detection(cells: list, tag_idx: int) -> dict:
    """
    Headerless row fallback.

    Do not guess column semantics in Python. OCR-mashed construction schedules
    frequently violate any fixed positional assumption, and deterministic guesses
    can corrupt rows like SLATE. Preserve the raw row/cell structure and let the
    Tier 2 Headerless Row Interpreter resolve fields from content shape.
    """
    clean_cells = [str(c).strip() for c in cells]
    return {
        "headerless_unparsed": True,
        "tag_cell_index": tag_idx,
        "raw_cells": clean_cells,
        "unparsed_row_text": " | ".join(clean_cells),
    }


def _parse_all_rows_for_tag(chunks: list, tag: str) -> dict:
    """
    Scans ALL pipe-delimited rows across ALL Pinecone chunks.
    Finds the row(s) where the TAG/KEY column matches target_tag.

    DYNAMIC — reads header row to discover column positions.
    Falls back to content-based heuristics if no header found.
    Works for ANY construction document, ANY column order.

    Returns the richest matching row as a dict, or {} if not found.
    """
    tag_upper = tag.strip().upper()

    best_row: dict = {}

    for chunk in chunks:
        text = chunk.get("text", "")
        lines = [l for l in text.split("\n") if "|" in l]
        if not lines:
            continue

        # ── Step 1: Find header row and build column map ──────────────────
        col_map: dict = {}
        for line in lines:
            if _is_header_row(line):
                col_map = _detect_column_map(line)
                break  # use first header found

        # ── Step 2: Scan every pipe row for this tag ──────────────────────
        for line in lines:
            if _is_header_row(line):
                continue  # skip header itself

            cells = [c.strip() for c in line.split("|")]

            # Try to find the tag in any cell
            tag_idx = -1
            for i, cell in enumerate(cells):
                # Check against known KEY column first (if header was found)
                if col_map.get("key") is not None:
                    if i == col_map["key"] and _cell_matches_target_tag(cell, tag_upper):
                        tag_idx = i
                        break
                else:
                    # No header — check if this cell IS the tag in any column
                    if _cell_matches_target_tag(cell, tag_upper) and (
                        _TAG_PATTERN.match(cell.strip()) or len(_plain_tag(cell)) == len(_plain_tag(tag_upper))
                    ):
                        tag_idx = i
                        break

            if tag_idx == -1:
                continue  # tag not in this row

            # ── Step 3: Extract fields using col_map or heuristics ────────
            def _get(idx):
                return cells[idx].strip() if 0 <= idx < len(cells) else ""

            if col_map:
                # Header-guided extraction — reliable
                row = {
                    "tag":          tag_upper,
                    "csi_section":  _get(col_map.get("csi_section", -1)),
                    "finish_type":  _get(col_map.get("finish_type", -1)),
                    "mat_desc":     _get(col_map.get("finish_type", -1)),
                    "manufacturer": _get(col_map.get("manufacturer", -1)),
                    "product":      _get(col_map.get("product", -1)),
                    "color":        _get(col_map.get("color", -1)),
                    "size":         _get(col_map.get("size", -1)),
                    "notes":        _get(col_map.get("notes", -1)),
                }
            else:
                # No header — use content-based heuristics
                row = _fallback_column_detection(cells, tag_idx)
                row["tag"] = tag_upper

            row["page_num"]  = chunk.get("page_num", 1)
            row["chunk_id"]  = chunk.get("chunk_id", "")
            row["raw_line"]  = line.strip()
            row["score"]     = chunk.get("score", 0.5)

            fill_score = sum(1 for f in [
                row.get("manufacturer",""),
                row.get("product",""),
                row.get("color",""),
                row.get("size",""),
            ] if f)
            row["fill_score"] = fill_score

            if fill_score > best_row.get("fill_score", -1):
                best_row = row

    return best_row


def _is_pinecone_data_sufficient(chunks: list, tag: str) -> tuple:
    """
    Parses Pinecone pipe-table rows to find the target tag's data row.

    Returns:
      (sufficient: bool, parsed_fields: dict, best_chunk: dict)

    sufficient = True when at least 2 of these 3 critical fields are non-empty:
      manufacturer, product (model/series), color

    If sufficient is True → caller uses Text LLM with the parsed data as hints.
    If sufficient is False → caller escalates to Vision API.
    """
    parsed = _parse_all_rows_for_tag(chunks, tag)
    if not parsed:
        logger.info(f"[{tag}] Pinecone: tag row NOT found in any chunk. Vision required.")
        return False, {}, {}

    mfr   = parsed.get("manufacturer", "").strip()
    prod  = parsed.get("product", "").strip()
    color = parsed.get("color", "").strip()

    parsed_blob = " | ".join(str(v) for v in parsed.values()).upper()
    if "NOT USED" in parsed_blob:
        parsed["not_used"] = True
        logger.info(f"[{tag}] Pinecone: explicit NOT USED row found. Returning deterministic row.")
        best_chunk = {
            "page_num": parsed.get("page_num", 1),
            "chunk_id": parsed.get("chunk_id", f"pinecone_{tag.lower()}"),
            "score":    parsed.get("score", 0.85),
        }
        return True, parsed, best_chunk

    filled_count = sum([_filled(mfr), _filled(prod), _filled(color)])

    if filled_count >= 2:
        logger.info(
            f"[{tag}] Pinecone: SUFFICIENT data found "
            f"(mfr='{mfr}', prod='{prod}', color='{color}'). "
            f"Vision API NOT needed."
        )
        # Build a fake "best_chunk" dict for SpecDetailRow page reference
        best_chunk = {
            "page_num": parsed.get("page_num", 1),
            "chunk_id": parsed.get("chunk_id", f"pinecone_{tag.lower()}"),
            "score":    parsed.get("score", 0.8),
        }
        return True, parsed, best_chunk

    logger.info(
        f"[{tag}] Pinecone: INSUFFICIENT data "
        f"(only {filled_count}/3 critical fields filled). "
        f"Escalating to Vision API."
    )
    return False, parsed, {}


def _build_specrow_from_pinecone(tag: str, parsed: dict, chunk: dict, source_document: str) -> SpecDetailRow:
    """
    Builds a SpecDetailRow directly from Pinecone parsed data.
    Called only when _is_pinecone_data_sufficient() returns True.
    No LLM call. No Vision call. Zero API cost.
    """
    page_num   = chunk.get("page_num", 1)
    evidence_id = chunk.get("chunk_id", f"pinecone_{tag.lower()}_p{page_num}")
    raw_line   = parsed.get("raw_line", "")

    # CSI section — split division from full section
    csi_raw = parsed.get("csi_section", "").strip()
    csi_div = csi_raw[:2] if len(csi_raw) >= 2 and csi_raw[:2].isdigit() else "N/S"
    csi_sec = csi_raw if csi_raw else "N/S"

    not_used = bool(parsed.get("not_used"))

    # Notes field may contain install info, area info, or product criteria
    notes = parsed.get("notes", "").strip()
    if not_used and "NOT USED" not in notes.upper():
        notes = "NOT USED" if not notes else f"{notes}; NOT USED"

    # Size — from TYPE/SIZE column
    size_raw = parsed.get("size", "").strip()

    # area_scope — try to extract from notes if it mentions rooms/locations
    # Common US patterns: "@ Lobby", "Corridors and...", "Gym Entry"
    area_scope = "N/S"
    if notes and not not_used:
        # Simple heuristic: notes often contain location after "@" or start with a room name
        area_match = re.search(r'@\s*(.+?)(?:\s+and|\s+&|$)', notes, re.IGNORECASE)
        if area_match:
            area_scope = area_match.group(1).strip()
        elif len(notes) < 80:  # Short notes are usually location descriptions
            area_scope = notes

    return SpecDetailRow(
        finish_tag=tag.upper(),
        area_scope=area_scope,
        manufacturer=parsed.get("manufacturer") or "N/S",
        model_series=parsed.get("product") or "N/S",
        finish_color=parsed.get("color") or "N/S",
        size=size_raw or "N/S",
        product_criteria=parsed.get("mat_desc") or "N/S",
        installation_criteria="N/S",
        standards_codes="N/S",
        csi_division=csi_div,
        csi_section=csi_sec,
        source_document=source_document,
        page_reference=str(page_num),
        evidence_id=evidence_id,
        evidence_excerpt=raw_line[:500] if raw_line else f"Pinecone row for {tag}",
        confidence=round(min(chunk.get("score", 0.75) + 0.1, 0.95), 3),
        status="confirmed",
        extraction_method="pinecone_direct",
        finish_type=parsed.get("finish_type", "N/S") or "N/S",
        special_remarks=notes if (not_used or len(notes) > 80) else "N/S",   # short notes go to area_scope; long notes are remarks
    )


# ─── VISION EXTRACTION ────────────────────────────────────────────────────────

def extract_with_vision(
    raw_client: "genai.Client",
    model_id: str,
    target_tag: str,
    pdf_path: str,
    page_num: int,
    evidence_id: str,
    source_document: str,
    hint_text: str = ""
) -> SpecDetailRow:
    """
    TIER 3 — Vision API extraction. Called only when Pinecone + Text LLM both fail.
    Renders PDF page as PNG image and sends to Gemini Vision.

    Guards:
    - Validates page_num before rendering (fixes 'from_bytes' error)
    - Validates image bytes non-empty before API call
    - DPI=150 (was 200) — smaller images, same readability, lower cost
    """
    logger.info(f"  [VISION] Rendering page {page_num} for '{target_tag}'...")

    # ── GUARD: validate before ANY rendering ─────────────────────────────────
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        doc.close()
    except Exception as e:
        raise RuntimeError(f"Cannot open PDF '{pdf_path}': {e}")

    if page_num < 1 or page_num > total_pages:
        raise RuntimeError(
            f"Page {page_num} out of range — PDF has {total_pages} pages. "
            f"This is the root cause of the 'from_bytes' error. "
            f"Fix: router page_map_with_buffer must stay within PDF bounds."
        )

    image_bytes = extract_page_as_image_bytes(pdf_path, page_num)

    if not image_bytes or len(image_bytes) < 500:
        raise RuntimeError(
            f"Page {page_num} rendered to empty/tiny PNG ({len(image_bytes)} bytes). "
            f"Page may be blank or PDF corrupted."
        )
    # ─────────────────────────────────────────────────────────────────────────

    _GEMINI_LIMITER.acquire()

    tag_prefix    = re.match(r'^([A-Z]+)', target_tag.upper())
    material_hint = _TAG_PREFIX_MAP.get(
        tag_prefix.group(1) if tag_prefix else '', 'construction finish material'
    )
    hint_clause = f"\nROUTING HINTS FROM PRIOR SCAN:\n{hint_text}" if hint_text else ""

    prompt = PromptVault.get_extractor_vision_prompt(
        target_tag=target_tag,
        page_num=page_num,
        source_document=source_document,
        hint_clause=hint_clause,
        material_hint=material_hint,
        evidence_id=evidence_id
    )

    response = raw_client.models.generate_content(
        model=model_id,
        contents=[
            prompt,
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SpecDetailRow,
            http_options=types.HttpOptions(timeout=180000),
        ),
    )

    parsed = response.parsed
    if parsed:
        parsed.extraction_method = "vision"
    return parsed


# ─── MAIN EXTRACTOR WORKER ────────────────────────────────────────────────────

class ExtractorWorker:
    def __init__(self, google_api_key: Optional[str] = None):
        setup_gcp_credentials()
        try:
            raw_client = genai.Client(
                vertexai=True,
                project=os.environ.get("GCP_PROJECT_ID"),
                location=os.environ.get("GCP_LOCATION", "us-central1"),
                http_options=types.HttpOptions(timeout=120000)
            )
            model_id = os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-pro")
            self.raw_client = raw_client
            self.model_id = model_id
            self.client = instructor.from_genai(raw_client, model=model_id)
            self.gemini_limiter = _GEMINI_LIMITER  # FIX-6: reuse module-level limiter
            self.dry_run = False
            logger.info("ExtractorWorker initialized with Vertex AI (Gemini 2.5 Pro)")
        except Exception as e:
            self.raw_client = None
            self.model_id = None
            self.client = None
            self.dry_run = True
            logger.warning(f"ExtractorWorker in DRY RUN mode: {e}")

    def extract(
        self,
        target_tag: str,
        evidence_chunks: list,
        source_document: str,
        project_id: str = "WORKER",
        router_metadata: dict = None,
        target_pages: list = None
    ) -> SpecDetailRow:
        """
        3-Tier extraction — cheapest to most expensive:

        TIER 1: Pinecone Direct (FREE — zero API calls)
          Parses pipe-table rows already stored by Loader.
          If manufacturer + at least one of (product, color) found → done.

        TIER 2: Text LLM (cheap — one text API call)
          Pinecone has partial or unclear data.
          Sends all Pinecone text + parsed hints to Gemini text API.

        TIER 3: Vision API (expensive — only when both above fail or page is scanned)
          Renders PDF page as image, sends to Gemini Vision.
          Cost saved: for a 100-tag document with clean Pinecone data,
          Vision is called 0 times. Previously: 100 times.
        """
        router_metadata = router_metadata or {}
        target_pages    = target_pages or []

        # Inject target_tag so the Pydantic validator can recover N/S
        SpecDetailRow._target_tag = target_tag

        if self.dry_run:
            return self._dry_run_response(target_tag, evidence_chunks, source_document)
        if not evidence_chunks:
            logger.warning(f"[{target_tag}] No evidence chunks — routing to fallback.")
            return self._dry_run_response(target_tag, [], source_document)

        # Build router hints — only valid (non-N/S) values
        hints = []
        def _vm(v): return v and v not in ("N/S","","none","None","null","N/A","TBD")
        if _vm(router_metadata.get("manufacturer")):
            hints.append(f"Router manufacturer: {router_metadata['manufacturer']}")
        if _vm(router_metadata.get("product_model")):
            hints.append(f"Router model/series: {router_metadata['product_model']}")
        if _vm(router_metadata.get("color")):
            hints.append(f"Router color: {router_metadata['color']}")
        if _vm(router_metadata.get("material_description")):
            hints.append(f"Material category: {router_metadata['material_description']}")
        hint_text = "\n".join(hints) if hints else ""

        sorted_chunks   = sorted(evidence_chunks, key=lambda x: _rank_chunk_for_target(x, target_tag), reverse=True)
        best_chunk      = sorted_chunks[0]
        vision_page_num = target_pages[0] if target_pages else best_chunk.get("page_num", 1)
        evidence_id     = best_chunk.get("chunk_id", f"ev_{target_tag.lower()}_p{vision_page_num}")
        pdf_path        = best_chunk.get("pdf_path", "")

        # ── TIER 1: Pinecone Direct (FREE) ───────────────────────────────────
        sufficient, parsed_fields, pinecone_chunk = _is_pinecone_data_sufficient(
            evidence_chunks, target_tag
        )
        if sufficient:
            logger.info(f"[{target_tag}] ✅ TIER 1 — Pinecone Direct. Zero API cost.")
            return _build_specrow_from_pinecone(
                tag=target_tag, parsed=parsed_fields,
                chunk=pinecone_chunk, source_document=source_document
            )

        # ── TIER 2: Text LLM ─────────────────────────────────────────────────
        is_scanned = best_chunk.get("metadata", {}).get("extraction_method") == "vision_scanned"
        tier2_result = None

        if not is_scanned:
            logger.info(f"[{target_tag}] TIER 2 — Text LLM...")
            try:
                self.gemini_limiter.acquire()
                tier2_result = self._extract_text_llm(
                    target_tag=target_tag,
                    evidence_chunks=sorted_chunks,
                    parsed_hints=parsed_fields,
                    source_document=source_document,
                    page_num=vision_page_num,
                    evidence_id=evidence_id,
                    hint_text=hint_text,
                    project_id=project_id
                )
                if tier2_result and not _llm_result_supported_by_evidence(tier2_result, target_tag):
                    logger.warning(f"[{target_tag}] TIER 2 rejected by evidence guard. → TIER 3")
                    tier2_result = None
                filled = sum([
                    tier2_result.manufacturer not in ("N/S","",None),
                    tier2_result.model_series  not in ("N/S","",None),
                    tier2_result.finish_color  not in ("N/S","",None),
                ]) if tier2_result else 0
                if tier2_result and tier2_result.confidence >= 0.65 and filled >= 2:
                    logger.info(
                        f"[{target_tag}] ✅ TIER 2 — Text LLM sufficient "
                        f"(conf={tier2_result.confidence:.2f}). Vision NOT needed."
                    )
                    return tier2_result
                logger.info(f"[{target_tag}] TIER 2 weak (conf={getattr(tier2_result,'confidence','?')}). → TIER 3")
            except Exception as e:
                logger.error(f"[{target_tag}] TIER 2 failed: {e}")

        # ── TIER 3: Vision API ────────────────────────────────────────────────
        if pdf_path and os.path.exists(pdf_path):
            try:
                logger.info(f"[{target_tag}] TIER 3 — Vision API page {vision_page_num}...")
                vision_result = extract_with_vision(
                    self.raw_client, self.model_id, target_tag, pdf_path,
                    vision_page_num, evidence_id, source_document, hint_text
                )
                if vision_result and not _llm_result_supported_by_evidence(vision_result, target_tag):
                    logger.warning(f"[{target_tag}] TIER 3 rejected by evidence guard.")
                    vision_result = None
                if vision_result and vision_result.confidence > 0.4:
                    logger.info(f"[{target_tag}] ✅ TIER 3 Vision (conf={vision_result.confidence:.2f}).")
                    return vision_result
                logger.warning(f"[{target_tag}] TIER 3 low confidence — using TIER 2 if available.")
            except Exception as ve:
                logger.error(f"[{target_tag}] TIER 3 Vision FAILED: {ve}. Using TIER 2 result.")
        else:
            logger.warning(f"[{target_tag}] PDF unavailable for Vision ({pdf_path}).")

        return tier2_result or self._dry_run_response(target_tag, evidence_chunks, source_document)

    def _extract_text_llm(
        self,
        target_tag: str,
        evidence_chunks: list,
        parsed_hints: dict,
        source_document: str,
        page_num: int,
        evidence_id: str,
        hint_text: str,
        project_id: str
    ) -> SpecDetailRow:
        """
        TIER 2 — Cheap text LLM extraction.
        Sends Pinecone pipe-table text + any partial parsed data to Gemini text.
        Never sends images. One text API call only.
        """
        tag_prefix    = re.match(r'^([A-Z]+)', target_tag.upper())
        material_type = _TAG_PREFIX_MAP.get(
            tag_prefix.group(1) if tag_prefix else '', 'construction finish material'
        )

        combined_evidence, selected_chunk_ids = _pack_evidence_for_llm(evidence_chunks, target_tag)
        packed_evidence_id = selected_chunk_ids[0] if selected_chunk_ids else evidence_id

        # Include partial Pinecone parse as explicit starting hints
        ph = []
        if parsed_hints.get("manufacturer"): ph.append(f"Manufacturer (from Pinecone): {parsed_hints['manufacturer']}")
        if parsed_hints.get("product"):      ph.append(f"Product/Series (from Pinecone): {parsed_hints['product']}")
        if parsed_hints.get("color"):        ph.append(f"Color (from Pinecone): {parsed_hints['color']}")
        if parsed_hints.get("size"):         ph.append(f"Size (from Pinecone): {parsed_hints['size']}")
        if parsed_hints.get("notes"):        ph.append(f"Notes (from Pinecone): {parsed_hints['notes']}")
        if parsed_hints.get("headerless_unparsed"):
            ph.append("Headerless fallback row detected: Python intentionally did NOT infer column semantics.")
            ph.append(f"Raw row text: {parsed_hints.get('unparsed_row_text', '')}")
            ph.append(f"Raw cells: {json.dumps(parsed_hints.get('raw_cells', []), ensure_ascii=False)}")
        if hint_text:                        ph.append(hint_text)
        all_hints = "\n".join(ph) if ph else "None available"

        result = self.client.chat.completions.create(
            response_model=SpecDetailRow,
            max_retries=3,
            messages=[
                {
                    "role": "system",
                    "content": PromptVault.get_extractor_text_prompt(
                        target_tag=target_tag,
                        material_type=material_type,
                        all_hints=all_hints,
                        combined_evidence=combined_evidence,
                        source_document=source_document,
                        page_num=page_num,
                        packed_evidence_id=packed_evidence_id
                    )
                },
                {
                    "role": "user",
                    "content": (
                        f"Extract ALL specification data for '{target_tag}'.\n\n"
                        f"PINECONE EVIDENCE, reranked by target-row likelihood across ALL retrieved chunks:\n"
                        f"{combined_evidence}\n\n"
                        f"Find the row whose tag/key cell is '{target_tag}' or a conservative OCR-equivalent. "
                        f"Map columns by header labels when available. For headerless/unparsed rows, apply the "
                        f"Headerless Row Interpreter Protocol using only the raw row/cell content. If no such row "
                        f"is present, fail closed with verification_needed instead of borrowing a similar tag's data."
                    )
                }
            ]
        )

        try:
            raw = getattr(result, '_raw_response', None)
            usage = getattr(raw, 'usage_metadata', None)
            if usage:
                token_ledger_db.log_usage(
                    project_id=project_id, agent_name="ExtractorWorker_TextLLM",
                    model_name=self.model_id,
                    input_tokens=getattr(usage, 'prompt_token_count', 0),
                    output_tokens=getattr(usage, 'candidates_token_count', 0)
                )
        except Exception: pass

        return result

    def extract_fallback(
        self,
        target_tag: str,
        pdf_path: str,
        target_pages: list,
        source_document: str,
        project_id: str = "WORKER"
    ) -> SpecDetailRow:
        """Fallback: scan specific pages with Vision when Pinecone is empty."""
        if not target_pages or not os.path.exists(pdf_path):
            logger.warning(f"[{target_tag}] Fallback: no target pages or PDF missing.")
            return self._dry_run_response(target_tag, [], source_document)

        logger.info(f"[{target_tag}] Fallback mode: scanning pages {target_pages}")

        # FIX-4: Inject target_tag before Pydantic instantiation
        SpecDetailRow._target_tag = target_tag

        best_result = None
        best_confidence = 0.0

        for page_num in target_pages:
            try:
                evidence_id = f"fallback_{target_tag.lower()}_p{page_num}"
                result = extract_with_vision(
                    self.raw_client, self.model_id, target_tag, pdf_path,
                    page_num, evidence_id, source_document
                )
                if result and result.confidence > best_confidence:
                    best_confidence = result.confidence
                    best_result = result
                if best_confidence >= 0.7:
                    break
            except Exception as e:
                logger.error(f"[{target_tag}] Fallback page {page_num} failed: {e}")

        return best_result or self._dry_run_response(target_tag, [], source_document)

    def _dry_run_response(self, target_tag: str, evidence_chunks: list, source_document: str) -> dict:
        page = evidence_chunks[0].get("page_num", "N/A") if evidence_chunks else "N/A"
        return {
            "finish_tag": target_tag,
            "area_scope": "[DRY RUN - Add API key]",
            "manufacturer": "[DRY RUN]",
            "model_series": "[DRY RUN]",
            "finish_color": "[DRY RUN]",
            "source_document": source_document,
            "page_reference": str(page),
            "evidence_id": f"dry_run_{target_tag}",
            "evidence_excerpt": "[DRY RUN - No API key provided]",
            "confidence": 0.0,
            "status": "verification_needed",
            "extraction_method": "dry_run"
        }


# ─── DUAL-QUERY RETRIEVAL ────────────────────────────────────────────────────
# FIX-1a + FIX-1b: Dual query + nuclear synthetic fallback chunk

def _retrieve_evidence_for_tag(tag: str, project_id: str, target_pages: list = None) -> list:
    """
    Dual-query retrieval with synthetic fallback guarantee.

    Query A: raw tag string (e.g. "CT-1") — high precision on page-level index
    Query B: expanded context sentence — high recall

    Both embedded in ONE API call (batch). Results unioned and deduplicated.

    If STILL empty after both queries and Plan B global search:
    → Inject synthetic chunk from router's target_pages so Vision always
      runs on the correct page. Zero silent data loss.
    """
    from pinecone import Pinecone

    setup_gcp_credentials()

    # Two queries — embedded in one batch API call
    query_a = tag  # raw tag: "CT-1" scores highly on pages containing CT-1
    query_b = f"specifications for finish tag {tag} manufacturer model color"

    _GEMINI_LIMITER.acquire()

    genai_client = genai.Client(
        vertexai=True,
        project=os.environ.get("GCP_PROJECT_ID"),
        location=os.environ.get("GCP_LOCATION", "us-central1"),
    )
    res = genai_client.models.embed_content(
        model="text-embedding-004",
        contents=[query_a, query_b],   # batch: 2 embeddings, 1 API call
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=768
        )
    )

    pc = Pinecone(api_key=os.environ.get("PINECONE_API_KEY"))
    index = pc.Index(os.environ.get("PINECONE_INDEX", "construction-vertex-index"))

    all_matches = {}  # deduplicate by chunk_id

    for embed_idx, vector in enumerate([
        res.embeddings[0].values,
        res.embeddings[1].values
    ]):
        # FIX-1a: Much lower thresholds when router already knows exact pages
        if target_pages:
            pinecone_filter = {"project_id": project_id, "page_num": {"$in": target_pages}}
            score_threshold = 0.10  # any hit on correct page is valid
            top_k = 20
        else:
            pinecone_filter = {"project_id": project_id}
            score_threshold = 0.35  # lowered from 0.45
            top_k = 15

        results = index.query(
            vector=vector,
            filter=pinecone_filter,
            top_k=top_k,
            include_metadata=True
        )

        for m in results.matches:
            if m.score > score_threshold and m.id not in all_matches:
                all_matches[m.id] = {
                    "text": m.metadata.get("text", ""),
                    "page_num": int(m.metadata.get("page_num", 0)),
                    "chunk_id": m.id,
                    "score": m.score,
                    "pdf_path": m.metadata.get("pdf_path", ""),
                    "metadata": {
                        "extraction_method": m.metadata.get("extraction_method", ""),
                        "chunk_type": m.metadata.get("chunk_type", "")
                    }
                }

    matches = sorted(all_matches.values(), key=lambda x: x["score"], reverse=True)

    # Plan B: global search if page-filtered search failed
    if not matches and target_pages:
        logger.warning(f"[{tag}] Plan B: page filter returned nothing. Global search...")
        for vector in [res.embeddings[0].values, res.embeddings[1].values]:
            results = index.query(
                vector=vector,
                filter={"project_id": project_id},
                top_k=15,
                include_metadata=True
            )
            for m in results.matches:
                if m.score > 0.35 and m.id not in all_matches:
                    all_matches[m.id] = {
                        "text": m.metadata.get("text", ""),
                        "page_num": int(m.metadata.get("page_num", 0)),
                        "chunk_id": m.id,
                        "score": m.score,
                        "pdf_path": m.metadata.get("pdf_path", ""),
                        "metadata": {
                            "extraction_method": m.metadata.get("extraction_method", ""),
                            "chunk_type": m.metadata.get("chunk_type", "")
                        }
                    }
        matches = sorted(all_matches.values(), key=lambda x: x["score"], reverse=True)

    # FIX-1b: NUCLEAR FALLBACK — if STILL empty but router gave pages,
    # inject synthetic chunk. Vision will run on the correct page.
    # Guarantees ZERO silent data loss for router-discovered tags.
    if not matches and target_pages:
        logger.warning(
            f"[{tag}] NUCLEAR FALLBACK: Pinecone returned nothing after dual query + Plan B. "
            f"Injecting synthetic chunk for router page {target_pages[0]}."
        )
        matches = [{
            "text": f"Finish tag {tag} located on page {target_pages[0]}",
            "page_num": target_pages[0],
            "chunk_id": f"router_fallback_{tag.lower()}",
            "score": 0.5,
            "pdf_path": "",   # filled by lambda_handler before extract() is called
            "metadata": {"chunk_type": "router_fallback"}
        }]

    logger.info(f"[{tag}] Retrieval complete: {len(matches)} chunks found.")
    return matches


# ─── SQS HEARTBEAT ────────────────────────────────────────────────────────────

class SQSHeartbeat(threading.Thread):
    def __init__(self, sqs_client, queue_url, receipt_handle, tag, interval=300):
        super().__init__(daemon=True)
        self.sqs = sqs_client
        self.queue_url = queue_url
        self.receipt_handle = receipt_handle
        self.tag = tag
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.sqs.change_message_visibility(
                    QueueUrl=self.queue_url,
                    ReceiptHandle=self.receipt_handle,
                    VisibilityTimeout=900
                )
                logger.info(f"[{self.tag}] SQS Visibility extended (Heartbeat).")
            except Exception as e:
                logger.error(f"[{self.tag}] Heartbeat failed: {e}")
                break

    def stop(self):
        self.stop_event.set()


# ─── LAMBDA HANDLER ───────────────────────────────────────────────────────────

def lambda_handler(event, context):
    """SQS-triggered Lambda handler. Each SQS message = one tag to extract."""
    from supabase import create_client
    from src.database.error_logger import error_logger
    from src.database.supabase_db import check_worker_finalization, init_pipeline_state

    sqs = boto3.client('sqs')
    worker_queue_url = os.environ.get("WORKER_QUEUE_URL")

    for record in event.get("Records", []):
        project_id = "UNKNOWN"
        tag = "UNKNOWN"
        heartbeat = None   # FIX-5: initialize to None — prevents NameError in finally

        try:
            payload = json.loads(record["body"])
            tag = payload["tag"]
            project_id = payload["project_id"]
            target_pages = payload.get("target_pages", [])
            s3_path = payload.get("s3_path", "")
            source_doc = payload.get("source_doc", "UNKNOWN_DOC")
            total_tags = payload.get("total_tags", 0)
            tags_key = payload.get("tags_key", "")
            receipt_handle = record.get("receiptHandle")
            router_metadata = payload.get("router_metadata", {})
            approved_tags = payload.get("approved_tags", [])

            # Validate Fallback Queue early
            fallback_url = os.environ.get("FALLBACK_QUEUE_URL")
            if not fallback_url:
                logger.error(f"[{tag}] FALLBACK_QUEUE_URL not set. Tag will be lost.")
                error_logger.log_error(
                    project_id, f"MissingEnvVar_{tag}",
                    EnvironmentError("FALLBACK_QUEUE_URL not set")
                )
                continue

            # Validate tag is router-approved
            if approved_tags and tag not in approved_tags:
                logger.error(f"[{tag}] NOT in Router approved list. Skipping.")
                continue

            # Start SQS heartbeat
            if worker_queue_url and receipt_handle:
                heartbeat = SQSHeartbeat(sqs, worker_queue_url, receipt_handle, tag)
                heartbeat.start()
                logger.info(f"[{tag}] Heartbeat started.")

            # Retrieve evidence — FIX-1a/1b applied inside this function
            try:
                evidence = _retrieve_evidence_for_tag(tag, project_id, target_pages)
            except Exception as e:
                logger.error(f"[{tag}] Retrieval failed: {e}")
                evidence = []

            # Route to fallback only if evidence is still empty after nuclear fallback
            # (nuclear fallback guarantees matches when target_pages is known,
            #  so empty here means target_pages was also unknown)
            if not evidence:
                logger.warning(f"[{tag}] No evidence and no target_pages. Routing to Fallback Queue.")
                sqs.send_message(
                    QueueUrl=fallback_url,
                    MessageBody=json.dumps({
                        "tag": tag,
                        "project_id": project_id,
                        "target_pages": target_pages,
                        "s3_path": s3_path,
                        "source_doc": source_doc,
                        "total_tags": total_tags,
                        "tags_key": tags_key,
                        "approved_tags": approved_tags,
                        "router_metadata": router_metadata,
                        "is_fallback": True
                    })
                )
                logger.info(f"[{tag}] Routed to Fallback Queue.")
                continue

            # Download PDF to /tmp
            bucket = s3_path.replace("s3://", "").split("/")[0]
            s3_key = "/".join(s3_path.replace("s3://", "").split("/")[1:])
            pdf_filename = os.path.basename(s3_key)
            local_pdf_path = f"/tmp/{pdf_filename}"

            if not os.path.exists(local_pdf_path):
                s3 = boto3.client('s3')
                try:
                    logger.info(f"[{tag}] Downloading {s3_path}...")
                    s3.download_file(bucket, s3_key, local_pdf_path)
                    logger.info(f"[{tag}] Download complete.")
                except ClientError as e:
                    if e.response['Error']['Code'] == '404':
                        logger.warning(f"[{tag}] 404: Checking archive...")
                        from datetime import datetime, timedelta
                        dates_to_check = [
                            datetime.now().strftime('%Y-%m-%d'),
                            (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
                        ]
                        found_in_archive = False
                        for d in dates_to_check:
                            archive_key = f"archive/{d}/{project_id}/{pdf_filename}"
                            try:
                                s3.download_file(bucket, archive_key, local_pdf_path)
                                logger.info(f"[{tag}] Found in archive.")
                                found_in_archive = True
                                break
                            except Exception:
                                continue
                        if not found_in_archive:
                            logger.error(f"[{tag}] PDF not found in original or archive.")
                            raise e
                    else:
                        raise e

            # Attach local pdf_path to evidence chunks (overrides Loader Lambda path)
            for chunk in evidence:
                chunk["pdf_path"] = local_pdf_path

            # FIX-4: Inject target_tag into SpecDetailRow before extraction
            SpecDetailRow._target_tag = tag

            logger.info(f"[{tag}] EXTRACTION START...")
            worker = ExtractorWorker()

            # FIX-2: Pass target_pages to extract() so Vision uses correct page
            result = worker.extract(
                target_tag=tag,
                evidence_chunks=evidence,
                source_document=pdf_filename,
                project_id=project_id,
                router_metadata=router_metadata,
                target_pages=target_pages   # FIX-2: passed here
            )
            logger.info(f"[{tag}] EXTRACTION COMPLETE.")

            if result and not getattr(result, 'extraction_method', '') == 'dry_run':
                sb = create_client(
                    os.environ.get("SUPABASE_URL"),
                    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
                )
                data = result.model_dump() if hasattr(result, 'model_dump') else dict(result)
                data["project_id"] = project_id
                sb.table("spec_detail_ledger").upsert(
                    data, on_conflict="project_id,finish_tag,area_scope"
                ).execute()
                logger.info(f"[{tag}] Saved to spec_detail_ledger.")

                # Finalization check — last worker archives and resets state
                if check_worker_finalization(project_id, total_tags):
                    logger.info(f"[{tag}] LAST WORKER. Archiving and resetting state.")
                    if bucket and s3_key and tags_key:
                        from datetime import datetime
                        archive_prefix = f"archive/{datetime.now().strftime('%Y-%m-%d')}/{project_id}/"
                        for k in [s3_key, tags_key]:
                            try:
                                dest_key = archive_prefix + os.path.basename(k)
                                logger.info(f"Archiving {k} → {dest_key}")
                                boto3.client('s3').copy_object(
                                    Bucket=bucket,
                                    CopySource={'Bucket': bucket, 'Key': k},
                                    Key=dest_key
                                )
                            except Exception as arch_err:
                                logger.error(f"Archive failed for {k}: {arch_err}")
                    init_pipeline_state(project_id, total_chunks=0)
                    logger.info(f"[{tag}] Pipeline state reset.")

            if os.path.exists(local_pdf_path):
                os.remove(local_pdf_path)

        except Exception as e:
            logger.error(f"[{tag}] Fatal error: {e}")
            from src.database.error_logger import error_logger
            error_logger.log_error(project_id, f"ExtractorWorker_{tag}", e)
        finally:
            # FIX-5: heartbeat is always defined (None if not started)
            # so this never throws NameError
            if heartbeat:
                heartbeat.stop()
                heartbeat.join()
                logger.info(f"[{tag}] Heartbeat stopped.")

    return {"statusCode": 200}


# ─── LOCAL TEST ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("TEST 1: Evidence Type Detector")
    print("=" * 60)

    table_text = "LVT-1  Armstrong  NatCreations  Walnut  6x36  CPT-1  Shaw"
    prose_text = "The finish system shall consist of 2 coats of SuperPaint."

    print(f"Table text → {detect_evidence_type(table_text).value}")
    print(f"Prose text → {detect_evidence_type(prose_text).value}")

    print("\n" + "=" * 60)
    print("TEST 2: ExtractorWorker Dry Run")
    print("=" * 60)

    SpecDetailRow._target_tag = "PL-1"
    worker = ExtractorWorker()
    result = worker.extract(
        target_tag="PL-1",
        evidence_chunks=[{
            "text": "Wilsonart PL-1 4880-38 Carbon Mesh applied to vertical cabinet surfaces.",
            "page_num": 407,
            "chunk_id": "ev_pl1_p407",
            "pdf_path": os.environ.get("TEST_PDF_PATH", "/tmp/test.pdf")
        }],
        source_document="awosting_hall_manual.pdf",
        target_pages=[407]
    )
    print(f"Worker result for PL-1: {result}")

    print("\n" + "=" * 60)
    print("DB CLEANUP SQL (run in Supabase SQL Editor):")
    print("=" * 60)
    print("UPDATE spec_detail_ledger SET finish_tag='MARBLE'")
    print("  WHERE finish_tag='MARBLE THRESHOLDS'")
    print("  AND project_id='ENTERPRISE_DEMO_PROJECT';")
    print("")
    print("DELETE FROM spec_detail_ledger")
    print("  WHERE finish_tag='N/S'")
    print("  AND project_id='ENTERPRISE_DEMO_PROJECT';")
