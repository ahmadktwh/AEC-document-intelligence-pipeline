# ==============================================================================
# PROPRIETARY AND CONFIDENTIAL - STRICTLY RESTRICTED USE ONLY
# ------------------------------------------------------------------------------
# Copyright (c) 2026 Mujeeb Ahmad. All rights reserved.
# Unauthorized commercial reproduction, distribution, replication, or hosting
# of this proprietary software, in whole or in part, is strictly prohibited.
# Licensed solely for authorized technical review and demonstration purposes.
# ==============================================================================

"""
router_agent.py — Advanced 3-Engine Tag Discovery System
"""

import os
import json
import logging
import pandas as pd
import time
import base64
import re
import boto3
from botocore.exceptions import ClientError
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Set
from enum import Enum
from collections import defaultdict

import fitz  # PyMuPDF
import requests
from google import genai
from google.genai import types
import instructor
from pydantic import BaseModel, Field

from src.database.token_ledger import token_ledger_db
from src.core.vault import PromptVault
from src.utils.gcp_helper import setup_gcp_credentials
import threading

import random
from datetime import datetime, timedelta, timezone

from src.utils.rate_limiter import GlobalRateLimiter


# We initialize this per RouterAgent instance to ensure project_id context


# ─────────────────────────────── Logging ────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("RouterV2")


# ══════════════════════════════════════════════════════════════════════════════
#  DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════

class PageType(str, Enum):
    TEXT    = "text"      # Mostly digital/selectable text
    SCANNED = "scanned"   # Image-only, no selectable text
    MIXED   = "mixed"     # Partial text + embedded images (diagrams etc.)
    EMPTY   = "empty"     # Blank or nearly blank page


@dataclass
class PageInfo:
    """Metadata about a single PDF page after classification."""
    page_num: int
    page_type: PageType
    text_density: float       # 0.0–1.0 ratio of text coverage
    image_count: int          # Number of embedded images
    has_table_lines: bool     # Does PyMuPDF detect table-like lines?
    schedule_score: float     # Heuristic: likelihood of finish schedule content (0–1)
    raw_text: str             # Extracted text (empty for scanned pages)


@dataclass
class TagCandidate:
    """A tag found by one or more engines."""
    tag: str
    source_pages: List[int]    = field(default_factory=list)
    found_by: List[str]        = field(default_factory=list)   # engine names
    confidence: float          = 0.0
    context_snippet: str       = ""
    
    # Construction-specific metadata (filled during discovery or verification)
    manufacturer: str          = "N/S"
    product_model: str         = "N/S"
    color: str                = "N/S"
    dimensions: str           = "N/S"
    surface_location: str      = "N/S"
    installation_remarks: str  = "N/S"
    rooms: str                = "N/S"
    csi_section: str          = "N/S"
    material_description: str  = "N/S"


class TagDiscoveryResult(BaseModel):
    """Pydantic schema for LLM-structured output (used with instructor)."""
    finish_tags: List[str] = Field(
        description=(
            "List of unique material/finish tags found. "
            "Tags usually have a prefix and a suffix (e.g., PL-1, LVT-12A, Type A, PT1, GWB). "
            "DO NOT include adhesive product codes (e.g., PL-400), dimensions (e.g., 2x4), or generic room numbers. "
            "Capture any tag that identifies a specific finish material in the schedule."
        )
    )
    tag_pages: Dict[str, List[int]] = Field(
        default_factory=dict,
        description='Mapping of each tag to the page number(s) it appears on, e.g. {"PL-1": [42]}'
    )
    tag_metadata: Dict[str, Dict[str, str]] = Field(
        default_factory=dict,
        description=(
            'Optional metadata per tag extracted from the schedule row. '
            'Key = tag code, value = dict with any of: manufacturer, product_model, color, '
            'dimensions, surface_location, installation_remarks, csi_section, material_description. '
            'E.g. {"LVT-2": {"manufacturer": "Shaw", "color": "Caramel Oak", "dimensions": "6x36"}}'
            'Leave fields empty string "" if not visible — do NOT invent data.'
        )
    )


class VerificationResult(BaseModel):
    """LLM verifier output — validates and normalises a raw tag list."""
    verified_tags: List[str] = Field(
        description=(
            "Tags confirmed as genuine material/finish codes. "
            "Normalise format to standard uppercase. If it consists of letters and numbers, use a dash (e.g., 'pt1' → 'PT-1', 'Type a' → 'TYPE A')."
        )
    )
    rejected_tags: List[str] = Field(
        description="Tags that appear to be false positives (dimensions, product codes, room numbers)."
    )
    notes: str = Field(default="", description="Brief explanation of any rejections.")



# ─────────────────────── Unicode Dash Normalization ────────────────────────
# ROOT CAUSE FIX: Excel, Word, and many CAD/BIM tools export PDFs using
# Unicode hyphen variants (U+2010 HYPHEN, U+2013 EN DASH, etc.) instead of
# the plain ASCII hyphen-minus (U+002D). This means CT‐1 in a PDF looks like
# CT-1 visually but does NOT match regex patterns using the ASCII hyphen [-].
# Every tag in an Excel-exported finish schedule will be invisible without this.
_UNICODE_DASH_MAP = str.maketrans({
    '\u2010': '-',   # HYPHEN (most common in Excel PDFs)
    '\u2011': '-',   # NON-BREAKING HYPHEN
    '\u2012': '-',   # FIGURE DASH
    '\u2013': '-',   # EN DASH (common in Word PDFs)
    '\u2014': '-',   # EM DASH
    '\u2015': '-',   # HORIZONTAL BAR
    '\u2212': '-',   # MINUS SIGN (used in some Revit exports)
    '\uFE58': '-',   # SMALL EM DASH
    '\uFE63': '-',   # SMALL HYPHEN-MINUS (fullwidth variant)
    '\uFF0D': '-',   # FULLWIDTH HYPHEN-MINUS (CJK-origin PDFs)
})


def normalize_unicode_dashes(text: str) -> str:
    """Replace ALL Unicode hyphen/dash variants with ASCII hyphen-minus (U+002D).

    Must be applied to raw PDF text BEFORE any regex matching. Failure to call
    this function is the single most common cause of missed tags in digitally
    exported construction PDFs (Excel, Word, Revit, Bluebeam, AutoCAD).
    """
    return text.translate(_UNICODE_DASH_MAP)


# ─────────────────── Known Finish Tag Prefix Whitelist ──────────────────────
# Domain vocabulary is managed centrally in PromptVault to decouple AEC
# knowledge from pipeline logic and enable production-time vocabulary updates
# without agent redeployment. These module-level names are lazy references
# that resolve at first call — Lambda cold-start safe.
_KNOWN_FINISH_PREFIXES = PromptVault.get_known_finish_prefixes()
_KNOWN_NON_TAG_PREFIXES = PromptVault.get_known_non_tag_prefixes()


def normalize_tag(tag: str) -> str:
    """Universally normalize tags (handles PT-1, PT1, PT.1, Type A, Type IV, P-E1, SP0RT-1).
    Returns empty string for known non-tag patterns so callers can skip them.

    Unicode normalization is applied first so that tags from Excel/Word/Revit PDFs
    that use U+2010 HYPHEN (CT‐1) are treated identically to ASCII hyphen (CT-1).

    Also fixes PDF extraction artifacts:
      "P -9"  (space before hyphen) → "P-9"
      "SP0RT" (zero for letter O)   → kept as-is; caller uses _NON_TAG_WORDS to filter
    """
    import re as _re
    tag = normalize_unicode_dashes(tag).strip().upper()
    # Collapse internal space before/after separator (PDF span-join artifact: "P -9")
    tag = _re.sub(r'\s*([-._])\s*', r'\1', tag).strip()
    # Fix OCR zero-for-letter-O in tag PREFIXES: SP0RT-1 → SPORT-1
    # Apply only to the alphabetic prefix (before the first separator or digit run).
    # Split on separator first to isolate the prefix cleanly.
    if '-' in tag:
        _pfx, _rest = tag.split('-', 1)
        tag = _pfx.replace('0', 'O') + '-' + _rest
    elif '.' in tag:
        _pfx, _rest = tag.split('.', 1)
        tag = _pfx.replace('0', 'O') + '.' + _rest
    else:
        # No separator — replace 0 only in leading alpha run
        _lead = _re.match(r'^([A-Z0-9]+?)(\d+[A-Z]*)$', tag)
        if _lead:
            tag = _lead.group(1).replace('0', 'O') + _lead.group(2)

    # Handle "TYPE A / TYPE I / TYPE IV" format (keep as-is with normalized spacing)
    if tag.startswith("TYPE"):
        return _re.sub(r'\s+', ' ', tag).strip()

    # Split by any non-alphanumeric cluster (e.g., '-', '.', ' ', '_')
    parts = _re.split(r'[^A-Z0-9]+', tag)
    if len(parts) == 2:
        prefix = parts[0]
        suffix = parts[1]

        # Reject known building/drawing reference words
        if prefix in _KNOWN_NON_TAG_PREFIXES:
            return ""

        if suffix.isdigit():
            return f"{prefix}-{int(suffix)}"   # removes leading zeros: PL-01 → PL-1
        else:
            return f"{prefix}-{suffix}"         # keeps alpha suffix: WD-A, CPT-1A

    # Fallback for completely merged tags like PT101 → PT-101, CPT2 → CPT-2
    # Also handles alpha-digit merged suffix: TERT1 → TERT-1
    m = _re.match(r'^([A-Z]+)(\d+[A-Z]{0,2})$', tag)
    if m:
        prefix = m.group(1)
        # Reject known non-tag prefixes in merged form too
        if prefix in _KNOWN_NON_TAG_PREFIXES:
            return ""
        suffix = m.group(2)
        # Strip leading zeros from purely numeric suffixes (DC055 → DC-55)
        if suffix.isdigit():
            suffix = str(int(suffix))
        return f"{prefix}-{suffix}"

    return tag


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 1 — PAGE CLASSIFIER
# ══════════════════════════════════════════════════════════════════════════════

class PageClassifier:
    """
    Classifies every page in the PDF without reading content.

    Text density = chars per pt² of page area.
    If text density < TEXT_THRESHOLD and there are images → SCANNED.
    If text density ≥ TEXT_THRESHOLD and images present    → MIXED.
    If text density ≥ TEXT_THRESHOLD and no images         → TEXT.
    """

    SCHEDULE_KEYWORDS = [
        # ── Schedule section headers ──────────────────────────────────────────
        "finish schedule", "finish legend", "material schedule",
        "flooring schedule", "room finish", "floor finish",
        "floor plan", "finish plan", "material legend",
        "interior finish", "finish key", "room finish schedule",
        "wall finish", "ceiling finish", "finish matrix",
        "floor finish schedule", "material finish", "finish type",
        # ── Flooring materials ────────────────────────────────────────────────
        "carpet tile", "luxury vinyl", "ceramic tile", "porcelain tile",
        "vinyl composition", "vinyl sheet", "rubber flooring",
        "resilient flooring", "paint system", "wall base", "rubber base",
        "terrazzo", "epoxy terrazzo", "access flooring", "raised floor",
        "hardwood flooring", "athletic floor", "rubber sports",
        "vinyl tile", "lvt", "lvp", "vct", "sheet vinyl",
        "polished concrete", "stained concrete", "sealed concrete",
        "cork flooring", "linoleum", "bamboo flooring",
        # ── Wall / Ceiling materials ──────────────────────────────────────────
        "acoustical ceiling", "gypsum board", "gypsum wall",
        "fabric panel", "wall panel", "wallcovering", "fabric wallcovering",
        "vinyl wallcovering", "frp panel", "cement board",
        "drop ceiling", "lay-in ceiling", "suspended ceiling",
        "drywall", "plaster", "stucco",
        # ── Paint & coatings ─────────────────────────────────────────────────
        "interior painting", "exterior painting", "paint color",
        "paint finish", "eggshell", "semi-gloss", "flat finish",
        "epoxy coating", "urethane", "alkyd",
        # ── Grout & setting materials ─────────────────────────────────────────
        "grout type", "cement grout", "epoxy grout", "tile grout",
        "kerapoxy", "mapei", "laticrete",
        # ── Base / trim ───────────────────────────────────────────────────────
        "rubber base", "vinyl base", "cove base", "vented base",
        "stair tread", "tread nosing", "stair nosing",
        # ── Division 10–12 (Specialties, Equipment, Furnishings) ─────────────
        "toilet partition", "locker", "cubicle curtain", "folding partition",
        "tackboard", "markerboard", "chalkboard", "whiteboard",
        "window treatment", "blinds", "shade", "roller shade",
        "casework", "millwork", "solid surface", "laminate", "quartz",
        # ── Common tag prefixes (both hyphenated and plain) ───────────────────
        "cpt", "lvt", "vct", "lvp", "pl-", "pt-", "ct-", "b-", "wb-", "hm-",
        "fl-", "sl-", "cvt-", "bt-", "st-", "nt-", "gt-", "rb-", "stn-",
        "vsf-", "vt-", "tert-", "ter-", "res-", "apc-", "rt-", "p-e",
        "sport-", "wd-", "wd-r", "wd-a", "wd-b", "gwb-", "act-", "frp-",
        "epx-", "pnt-", "lmt-", "qz-",
        # ── US Flooring brands ────────────────────────────────────────────────
        "armstrong", "shaw", "shaw floors", "mohawk", "interface",
        "mannington", "tarkett", "beaulieu", "karndean", "wicanders",
        "polyflor", "nora systems", "nora", "ecore", "altro",
        "patcraft", "milliken", "tandus", "atals", "atlas", "broadloom",
        "connor sports", "robbins sports", "boen", "connor",
        "robbins", "boen", "junckers", "mjm",
        # ── US Tile brands ────────────────────────────────────────────────────
        "daltile", "dal tile", "american olean", "florida tile", "crossville",
        "marazzi", "emser", "porcelanosa", "bedrosians", "walker zanger",
        "ann sacks", "stonepeak", "interceramic",
        # ── US Paint brands ───────────────────────────────────────────────────
        "sherwin", "sherwin-williams", "benjamin moore", "behr", "ppg",
        "pratt lambert", "dunn-edwards", "valspar", "sw-", "bm-",
        "ici paints", "pittsburgh paints",
        # ── US Base / Accessories brands ──────────────────────────────────────
        "roppe", "johnsonite", "burke", "cove base", "pinnacle",
        # ── US Grout / Setting brands ─────────────────────────────────────────
        "mapei", "laticrete", "custom building", "tec", "schluter",
        "ardex", "bostik", "sika", "weber",
        # ── US Ceiling / Wall brands ──────────────────────────────────────────
        "armstrong ceiling", "usg", "certainteed", "georgia-pacific",
        "knauf", "sto", "dryvit", "nucedar", "ecophon",
        # ── US Specialty / Terrazzo ───────────────────────────────────────────
        "nurazzo", "wausau tile", "american terrazzo", "diverzey", "donn",
        # ── CSI MasterFormat identifiers ─────────────────────────────────────
        "division 09", "div 09", "09 65", "09 91", "09 22", "09 30",
        "09 51", "09 64", "09 66", "09 67", "09 68", "09 30 00",
        "division 10", "division 11", "division 12",
        "03 35", "04 20", "06 20",
        # ── General schedule markers ──────────────────────────────────────────
        "acoustical", "gypsum", "frp", "cmu", "act", "gwb",
        "epoxy", "chemical resistant", "thin-set", "adhesive",
        "manufacturer", "product", "color", "finish type", "specification",
        "key", "legend", "schedule", "material", "surface",
    ]

    TEXT_THRESHOLD = 20  # chars per 1000 pt² — below this = scanned
    SCHEDULE_MATCH_MIN = 2  # Minimum keyword hits to flag a page

    def classify_page(self, page: fitz.Page) -> PageInfo:
        text = page.get_text("text") or ""
        blocks = page.get_text("blocks") or []
        images = page.get_images(full=True)
        area = page.rect.width * page.rect.height

        text_density = (len(text) / area * 1000) if area > 0 else 0
        image_count = len(images)

        # Detect table-like horizontal/vertical lines
        drawings = page.get_drawings()
        line_count = sum(
            1 for d in drawings
            if d["type"] == "l" and (
                abs(d["rect"].width) > 40 or abs(d["rect"].height) > 40
            )
        )
        has_table_lines = line_count >= 4

        # Schedule scoring
        text_lower = text.lower()
        kw_hits = sum(1 for kw in self.SCHEDULE_KEYWORDS if kw in text_lower)
        # Normalise based on minimum keyword hits
        schedule_score = min(kw_hits / self.SCHEDULE_MATCH_MIN, 1.0)

        # Page type logic
        if len(text.strip()) < 30:
            if image_count > 0:
                ptype = PageType.SCANNED
            else:
                ptype = PageType.EMPTY
        elif text_density < self.TEXT_THRESHOLD and image_count > 0:
            ptype = PageType.SCANNED
        elif image_count > 0:
            ptype = PageType.MIXED
        else:
            ptype = PageType.TEXT

        return PageInfo(
            page_num=page.number + 1,
            page_type=ptype,
            text_density=round(text_density, 2),
            image_count=image_count,
            has_table_lines=has_table_lines,
            schedule_score=round(schedule_score, 3),
            raw_text=text,
        )

    def classify_all(self, doc: fitz.Document) -> List[PageInfo]:
        log.info(f"Classifying all {len(doc)} pages...")
        results = []
        for i, page in enumerate(doc):
            info = self.classify_page(page)
            if (i + 1) % 50 == 0:
                log.info(f"  Classified {i + 1}/{len(doc)} pages...")
            results.append(info)
        counts = defaultdict(int)
        for p in results:
            counts[p.page_type.value] += 1
        log.info(f"Classification done: {dict(counts)}")
        return results


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 2a — TEXT ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class TextEngine:
    """
    Extracts tags from digital text pages.

    Two strategies:
      1. Table extraction (PyMuPDF 1.23+): preserves row/column structure
      2. Text block extraction: fallback for pages without detectable tables

    Both feed into a simple regex pre-filter BEFORE sending to the LLM,
    reducing token usage significantly.
    """

    # ── Core finish-tag pattern ───────────────────────────────────────────────
    # After normalize_unicode_dashes(), all Unicode hyphens are ASCII '-'.
    # Three sub-patterns cover every format seen in US construction schedules:
    #
    #   1. TYPE keywords    : "TYPE A", "TYPE I", "TYPE IV"
    #   2. Hyphenated       : PREFIX - SUFFIX
    #                         PREFIX = 1–6 letters  (P, CT, CPT, TERT, SPORT)
    #                         separator = - . or _   (ASCII after Unicode norm)
    #                         SUFFIX  = optional 0–2 letters + 1–4 digits + optional 0–2 letters
    #                                   covers: 1 (CT-1), 10 (CT-10), E1 (P-E1), 1A (WD-1A), A (WD-A)
    #   3. Merged (no sep)  : 2–6-letter prefix immediately followed by 1–4 digit suffix
    #                         (CPT1, LVT12, DC055)
    #                         Min 2-letter prefix required to avoid false A5, B3 grid hits.
    #
    # NOTE: spaces as separators are intentionally excluded — "ROOM 10", "LEVEL 2",
    # "DETAIL 3", "PHASE 2" would all match otherwise.
    TAG_PATTERN = re.compile(
        r"\b(?:"
        # 1. Named types: TYPE A, TYPE B, TYPE I, TYPE IV
        r"TYPE\s+(?:[A-Z]{1,3}|[IVX]{1,4})"
        # 2. Hyphenated with optional digit in prefix — covers SP0RT-1 (rubber sports tile),
        #    SPORT-1, CT-1, P-E1, WD-A, TERT-1, APC-2.
        #    Single-letter prefix allowed (P-1, P-E1) — must start with alpha.
        #    Separator [-._] optionally followed by ≤1 space before suffix.
        r"|[A-Z][A-Z0-9]{0,5}\s{0,1}[-._]\s{0,1}[A-Z]{0,2}\d{1,4}[A-Z]{0,2}"
        # 3. Alpha-only suffix with hyphen: WD-A, WD-B, WD-R
        r"|[A-Z]{2,6}[-._][A-Z]{1,3}"
        # 4. Merged — no separator, must start with alpha, ≥2-letter prefix.
        #    Covers: CPT1, LVT12, VCT1, DC055.
        #    Allow digit inside prefix: SP0RT1.
        r"|[A-Z][A-Z0-9]{1,5}\d{1,4}[A-Z]{0,2}"
        r")\b",
        re.IGNORECASE
    )

    # ── Words that look like tag prefixes but are NEVER finish tags ───────────
    # Applied in _mine_text to drop matches before confidence scoring.
    # Prevents: COLOR-1 (from "P-E1 PAINT COLOR-1"), DOORS, etc.
    _NON_TAG_WORDS = {
        'COLOR', 'DOORS', 'DOOR', 'FRAME', 'FRAMES', 'PAINT', 'PAINTED',
        'PANELS', 'ADJACENT', 'OUTSIDE', 'MATCH', 'SAME', 'USED',
        'TBD',    # placeholder
        'TM',     # trademark symbol (appears in rubber tile row)
        'REV', 'PHASE', 'ZONE', 'LEVEL', 'AREA', 'ROOM', 'BLDG',
        'STAIR', 'DETAIL', 'SHEET', 'DIV', 'SECTION', 'SPEC',
        'ACROBAT', 'MARKET', 'ATMOSPHERE', 'RECYCLED', 'FLOORING',
        # English compound-word prefixes that match TAG_PATTERN via hyphen
        # e.g. "CAST-IN-PLACE CONCRETE" → CAST-IN matches [A-Z]{2,6}[-._][A-Z]{1,3}
        'CAST',   # CAST-IN-PLACE
        'BUILT',  # BUILT-IN
        'CUT',    # CUT-OUT
        'WALK',   # WALK-IN
        'ROLL',   # ROLL-IN
        'SLIP',   # SLIP-IN
        'DROP',   # DROP-IN
        'FLUSH',  # FLUSH-IN
        'STAND',  # STAND-ALONE
        'FACE',   # FACE-MOUNTED
        'SEMI',   # SEMI-GLOSS (paint sheen label, not a finish tag)
        'ANTI',   # ANTI-SLIP
        'SELF',   # SELF-LEVELING
        'PRE',    # PRE-FINISHED
        'NON',    # NON-SLIP
    }

    # ── Standalone material codes (no number suffix) ──────────────────────────
    # These are valid finish-schedule identifiers that appear without a numeric
    # suffix — e.g. TERRAZZO, CONC, BRICK, VRB, APC — either as the sole tag
    # in a schedule row or as a category label in a legend/header.
    #
    # CRITICAL negative lookahead (?![-._\d]):
    #   Prevents VCT matching inside "VCT-1", CPT inside "CPT-1", WD inside
    #   "WD-A", TERT inside "TERT-1". The TAG_PATTERN already captures full form.
    #
    # Position gate in _mine_text: m.start() <= STANDALONE_POS_GATE
    #   CONC appears at pos 9 in "CONCRETE CONC SEALED" — needs gate >= 9
    #   MARBLE appears at pos 11 in "THRESHOLDS MARBLE" — needs gate >= 11
    #   TERRAZZO appears at pos 7 in "TERT-1 TERRAZZO TILE" — must block this
    #   so gate is 12: passes CONC(9) and MARBLE(11), blocks mid-description.
    STANDALONE_CODES = re.compile(
        r'\b('
        # ── Resilient / vinyl / carpet (standalone schedule category labels) ──
        r'VCT|LVT|LVP|CPT|CVT|VRB|LMT|QZ|EPX|PNT|HM'
        # ── Wall / ceiling assembly codes ─────────────────────────────────────
        r'|FRP|GWB|ACT|APC|WC|VC|CB'
        # ── Masonry / structural ─────────────────────────────────────────────
        r'|GCMU|CMU|CONC'
        # ── Pure natural material names used as schedule row labels ───────────
        r'|TERRAZZO|STUCCO|PLASTER|BRICK|STONE|MARBLE|GRANITE|SLATE|CORK'
        # ── Roman numeral type grades ─────────────────────────────────────────
        r'|TYPE\s+[IVX]{1,4}'
        # ── WD standalone (wood base / millwork generic reference) ────────────
        r'|WD|WDM'
        r')(?![-._\d])\b',   # do NOT match if immediately followed by separator/digit
        re.IGNORECASE
    )
    STANDALONE_POS_GATE = 20   # chars — tuned: CONC(18) passes, MARBLE(11) passes;
    # TERRAZZO at pos 21+ in "TERT-1 TERRAZZO TILE NURAZZO" blocked by separate check below

    def extract_from_page(
        self, doc: fitz.Document, page_info: PageInfo
    ) -> List[TagCandidate]:
        page = doc[page_info.page_num - 1]
        candidates: Dict[str, TagCandidate] = {}

        # ── Strategy A: Table extraction ──────────────────────────────────
        try:
            tables = page.find_tables()
            for table in tables.tables:
                df = table.to_pandas()
                for col in df.columns:
                    for cell_val in df[col].dropna().astype(str):
                        self._mine_text(
                            cell_val, page_info.page_num, "text_table", candidates
                        )
        except Exception as e:
            log.debug(f"  Table extraction skipped (page {page_info.page_num}): {e}")

        # ── Strategy B: Raw text blocks ───────────────────────────────────
        blocks = page.get_text("blocks")
        for b in blocks:
            block_text = b[4] if len(b) > 4 else ""
            self._mine_text(
                block_text, page_info.page_num, "text_block", candidates
            )

        return list(candidates.values())

    def _mine_text(
        self,
        text: str,
        page_num: int,
        source: str,
        candidates: Dict[str, TagCandidate],
    ) -> None:
        # ── Unicode normalization ─────────────────────────────────────────────
        # CRITICAL: must run BEFORE any regex. Excel/Word/Revit PDFs export
        # hyphens as U+2010 (HYPHEN) not U+002D (ASCII HYPHEN-MINUS), making
        # CT‐1 invisible to pattern [-]. After this call, all dashes are ASCII.
        text = normalize_unicode_dashes(text)
        # Collapse space-around-hyphen PDF extraction artifacts:
        #   "P -9"    → "P-9"    (single-letter prefix, PDF extraction hyphen artifact)
        #   "CT - 1"  → "CT-1"   (scanned PDF where tag spans split across spans)
        #   "VCT -1"  → "VCT-1"  (OCR artifact with space before hyphen)
        #   "CPT  - 1"→ "CPT-1"  (double-space before hyphen from column alignment)
        # Pattern: any 1-6 alpha chars + 1-3 spaces + hyphen + optional space + digit
        text = re.sub(r'\b([A-Z]{1,6})\s{1,3}(-\s*\d)', r'\1\2', text)
        stripped = text.strip()

        # ── TAG_PATTERN matches ───────────────────────────────────────────────
        for m in self.TAG_PATTERN.finditer(stripped):
            tag_raw = normalize_tag(m.group(0))
            if not tag_raw:          # normalize_tag returns "" for known non-tags
                continue

            # Drop known non-tag words immediately — these are English words /
            # label strings that match TAG_PATTERN structure but are never tags.
            # e.g. COLOR-1 (from "P-E1 PAINT COLOR-1"), WDM, TM (trademark), DOORS
            tag_upper = tag_raw.upper()
            tag_prefix_word = tag_upper.split('-')[0]
            if tag_prefix_word in self._NON_TAG_WORDS:
                continue

            # ── Prefix-aware confidence scoring ───────────────────────────────
            # DESIGN PRINCIPLE: _KNOWN_FINISH_PREFIXES is a BOOST signal only.
            # It must NEVER be a blocking gate.
            #
            # Reason: any construction firm can use any prefix convention they
            # choose (MF-1, GL-1, HDF-1, SSF-1, etc.). Blocking an unknown
            # prefix before the LLM verifier sees it silently drops legitimate
            # tags from unfamiliar companies — unacceptable for a pipeline that
            # must work across ALL US construction businesses.
            #
            # The LLM verifier (Stage 4) is the authoritative false-positive
            # filter. Our job here is to score confidence so the verifier gets
            # a ranked list to work with, not to pre-filter it.
            #
            # Confidence ladder (all values >= 0.48, above the 0.45 emit gate):
            #   Known prefix + near start   -> 0.75  (very high certainty)
            #   Known prefix + mid-text     -> 0.60  (likely real, deeper in row)
            #   Unknown prefix + near start -> 0.52  (structural signal: label pos)
            #   Unknown prefix + mid-text   -> 0.48  (borderline: let verifier decide)
            prefix_m = re.match(r'^[A-Z]+', tag_raw, re.IGNORECASE)
            prefix = tag_raw.split('-')[0] if '-' in tag_raw else (
                prefix_m.group(0) if prefix_m else tag_raw
            )
            is_known_prefix = prefix.upper() in _KNOWN_FINISH_PREFIXES
            is_near_start = m.start() <= 20

            # ── Hard-block 1: manufacturer paint color numbers ────────────────
            # SW-7008 (Sherwin-Williams), BM-2070-30 / HC-11 / OC-9 (Benjamin Moore)
            # These match TAG_PATTERN structurally but are NEVER finish tags.
            # BM-OC-9 specifically: after Unicode normalization "BM-OC-9" stays as
            # "BM-OC-9" and normalizes to "BM-OC" or similar — block the BM prefix.
            _sw = tag_upper
            _is_paint_color = (
                (_sw.startswith('SW-') and _sw[3:].replace('-','').isdigit()) or
                (_sw.startswith('BM-') and len(_sw) > 3) or   # all BM- codes
                (_sw.startswith('HC-') and _sw[3:].isdigit()) or
                (_sw.startswith('OC-') and _sw[3:].isdigit())
            )
            if _is_paint_color:
                continue

            # ── Hard-block 2: tile / carpet / terrazzo product color codes ────
            # These appear in the PRODUCT or COLOR columns of finish schedules,
            # never in the TAG column. They are mid-row (pos > 20) and have
            # prefixes that are pure product-line identifiers:
            #   DO1, DO4   → Dal Tile Keystones color codes (prefix DO)
            #   D335, D160 → Dal Tile color codes (single letter D)
            #   Q094, Q192 → Dal Tile Semi-Gloss color codes (prefix Q)
            #   R943, R940 → American Olean Urban Tones color codes (prefix R)
            #   P501, P527 → Dal Tile Veranda color codes (P + 3-digit = product)
            #   DC055,DC018→ Nurazzo terrazzo design-basis codes (prefix DC)
            #   ET47       → Atlas carpet product SKU
            #   S306       → Atlas Sidra series product code
            # Rule: short prefix (1-2 alpha) + purely numeric suffix, mid-row → block
            _suffix = tag_upper.split('-')[-1] if '-' in tag_upper else re.sub(r'^[A-Z]+','',tag_upper)
            _pfx_only = re.match(r'^([A-Z]{1,3})-?(\d+)$', tag_upper)
            if _pfx_only and not is_near_start:
                pfx2, num2 = _pfx_only.group(1), _pfx_only.group(2)
                # P + 3-digit mid-row = product color (P501), not paint tag P-1
                if pfx2 == 'P' and len(num2) >= 3:
                    continue
                # Known product-color-only prefixes
                if pfx2 in {'DO','DC','ET','TM','S','Q','R'} and num2.isdigit():
                    continue
                # Single-letter prefix that isn't a known finish prefix: D4, R3
                if len(pfx2) == 1 and pfx2 not in {'P','W','A'}:
                    continue

            if is_known_prefix and is_near_start:
                base_conf = 0.75
            elif is_known_prefix:
                base_conf = 0.60
            elif is_near_start:
                base_conf = 0.52
            else:
                base_conf = 0.48

            if tag_raw not in candidates:
                candidates[tag_raw] = TagCandidate(
                    tag=tag_raw,
                    source_pages=[page_num],
                    found_by=[source],
                    confidence=base_conf,
                    context_snippet=stripped[:120].strip(),
                )
            else:
                tc = candidates[tag_raw]
                if page_num not in tc.source_pages:
                    tc.source_pages.append(page_num)
                # Each additional page occurrence boosts confidence slightly
                tc.confidence = min(tc.confidence + 0.05, 0.95)

        # -- Space-Separated Known Prefixes Miner --
        # Captures CPT 1, LVT 12, PT 3 and normalizes them to CPT-1, LVT-12, PT-3
        pfx_pattern = rf'\b({"|".join(re.escape(pfx) for pfx in _KNOWN_FINISH_PREFIXES if len(pfx) > 1)})\s+(\d{{1,4}}[A-Z]{{0,2}})\b'
        for m in re.finditer(pfx_pattern, stripped, re.IGNORECASE):
            tag_raw = f"{m.group(1).upper()}-{m.group(2).upper()}"
            if tag_raw not in candidates:
                candidates[tag_raw] = TagCandidate(
                    tag=tag_raw,
                    source_pages=[page_num],
                    found_by=[source],
                    confidence=0.75,
                    context_snippet=stripped[:120].strip(),
                )
            else:
                tc = candidates[tag_raw]
                if page_num not in tc.source_pages:
                    tc.source_pages.append(page_num)
                tc.confidence = min(tc.confidence + 0.05, 0.95)

        # ── STANDALONE_CODES matches ──────────────────────────────────────────
        # Three structural challenges require special handling:
        #
        # A) CSI section header rows push standalones past pos 20:
        #    "03 35 00 CAST-IN-PLACE CONCRETE  CONC  SEALED..."  → CONC at pos 33
        #    Fix: strip the CSI section number + description prefix before measuring
        #    position. Use the stripped version (solo_src) for SOLO matching only;
        #    PAT matching already ran on the full row above.
        #
        # B) Room finish matrix rows have multiple tags per row (GWB at pos 40):
        #    "103  CORRIDOR  VCT-1  RB-1  P-3  GWB" — GWB IS a real ceiling tag.
        #    Fix: if >= 2 hyphenated tags on row, treat ALL standalones as valid tags
        #    (room matrix format). The position gate does not apply.
        #
        # C) Terrazzo / marble description words appear mid-row after the tag:
        #    "TERT-1 TERRAZZO TILE NURAZZO..." — TERRAZZO at pos 7 is a description.
        #    Fix: _desc_only_standalones guard suppresses them when a hyphenated tag
        #    precedes on the same row.
        _CSI_PREFIX_RE = re.compile(
            r'^\s*\d{2}\s+\d{2}\s+\d{2}\s+\S[^\n]{0,60}?\s{2,}', re.I
        )
        csi_stripped = _CSI_PREFIX_RE.sub('', stripped).strip()
        solo_src = csi_stripped   # position measurement uses this

        # Count hyphenated tags on this row (for room-matrix detection)
        n_hyph_tags = len(re.findall(
            r'\b[A-Z]{1,6}-[A-Z]{0,2}\d{1,4}[A-Z]{0,2}\b', stripped, re.I
        ))

        _desc_only_standalones = {'TERRAZZO', 'STUCCO', 'PLASTER', 'STONE',
                                  'GRANITE', 'CORK', 'MARBLE', 'CMU'}

        for m in self.STANDALONE_CODES.finditer(solo_src):
            tag_raw = m.group(0).upper().strip()
            tag_raw = re.sub(r'\s+', ' ', tag_raw)
            if not tag_raw:
                continue

            if n_hyph_tags >= 2:
                # Multi-tag row (room finish matrix): all standalones are real tags.
                # GWB, ACT, VCT etc. in column cells are genuine finish tag references.
                # Position gate does NOT apply — they can appear at any column position.
                pass
            else:
                # Single-tag or no-tag row: enforce position gate on csi-stripped text.
                # Allow core finish standalones if they are preceded by multiple spaces/tabs (padded table cells)
                is_padded_cell = False
                start_idx = m.start()
                if start_idx >= 2:
                    is_padded_cell = solo_src[start_idx-2:start_idx].isspace()
                is_core_code = tag_raw in {'ACT', 'GWB', 'CONC', 'VCT', 'LVT', 'LVP', 'CPT', 'VRB', 'FRP', 'WDM'}
                
                if m.start() > self.STANDALONE_POS_GATE:
                    if not (is_padded_cell and is_core_code):
                        continue
                # Description-word guard: suppress _DO words that appear AFTER a
                # hyphenated tag on the same row (they are material-type descriptions,
                # not standalone tags). e.g. "TERT-1 TERRAZZO TILE" → TERRAZZO blocked.
                if tag_raw in _desc_only_standalones:
                    if re.search(r'\b[A-Z]{1,6}-\d', stripped[:m.start()], re.I):
                        continue
                    if tag_raw == 'CMU' and re.search(r'\bGCMU\b', stripped, re.I):
                        continue

            if tag_raw not in candidates:
                candidates[tag_raw] = TagCandidate(
                    tag=tag_raw,
                    source_pages=[page_num],
                    found_by=[source],
                    confidence=0.55,
                    context_snippet=stripped[:120].strip(),
                )
            else:
                tc = candidates[tag_raw]
                if page_num not in tc.source_pages:
                    tc.source_pages.append(page_num)
                tc.confidence = min(tc.confidence + 0.05, 0.95)


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 2b — VISION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class VisionEngine:
    """
    Sends scanned/image pages to Gemini Vision for tag extraction.

    Batches pages together to reduce API calls.
    Falls back to single-page mode if batch fails.
    """

    BATCH_SIZE = 10         # Pages per Vision API call
    MAX_RETRIES = 3
    RETRY_DELAY = 10.0      # seconds — Increased for 429 recovery

    # Vision prompt is retrieved at call-time from PromptVault to support
    # production-time override via VAULT_ROUTER_VISION_PROMPT env variable
    # without requiring a Lambda redeployment.

    def __init__(self, raw_client, project_id: str = "UNKNOWN", request_id: str = "LOCAL"):
        self.raw_client = raw_client
        self.project_id = project_id
        self.request_id = request_id
        self.limiter = GlobalRateLimiter("GLOBAL_GEMINI_LOCK", rpm_limit=5, token_limit=20000)
        from src.database.supabase_db import supabase
        self.supabase = supabase

    def _claim_batch(self, batch_label: str) -> bool:
        """Attempts to atomically claim a page batch in Supabase to prevent duplication.
        Fails OPEN (returns True) if the table is missing — Vision proceeds without dedup
        rather than silently dropping all Vision processing.
        """
        try:
            self.supabase.table("document_page_claims").insert({
                "project_id": self.project_id,
                "page_batch": batch_label,
                "claimed_by": self.request_id
            }).execute()
            return True
        except Exception as e:
            err_str = str(e).lower()
            # Table doesn't exist → fail OPEN so Vision is not silently disabled
            if "does not exist" in err_str or "42p01" in err_str or "undefined" in err_str:
                log.error(
                    "SETUP ERROR: 'document_page_claims' table missing in Supabase. "
                    "Vision dedup is DISABLED — create the table to enable it. "
                    "Proceeding without claim check."
                )
                return True  # fail open — let Vision proceed
            # Duplicate key = another Lambda already claimed this batch
            log.warning(f"[{self.project_id}] SKIPPING DUPLICATE BATCH: {batch_label} already claimed.")
            return False

    def _page_to_base64(self, doc: fitz.Document, page_num: int, dpi: int = 150) -> str:
        """Render a PDF page to PNG and return base64 string."""
        page = doc[page_num - 1]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        return base64.b64encode(pix.tobytes("png")).decode()

    def extract_batch(
        self, doc: fitz.Document, batch: List[PageInfo]
    ) -> List[TagCandidate]:
        """Sends a batch of pages to Vision for processing."""
        if not self.raw_client:
            return []

        page_nums = [p.page_num for p in batch]
        batch_label = f"{min(page_nums)}-{max(page_nums)}"

        # Deduplication Check: Try to claim this batch globally
        if not self._claim_batch(batch_label):
            return [] # Skip this batch, someone else is doing it

        for attempt in range(self.MAX_RETRIES):
            try:
                # Retrieve vision prompt from vault at call-time — supports
                # env-var overrides without Lambda cold-start caching issues.
                parts = []
                prompt = PromptVault.get_router_vision_prompt(
                    page_nums_str=str(page_nums),
                    page_example=str(page_nums[0] if page_nums else 1),
                )
                parts.append(prompt)
                
                for p_info in batch:
                    img_b64 = self._page_to_base64(doc, p_info.page_num)
                    img_bytes = base64.b64decode(img_b64)
                    parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))

                # Apply strict Global Rate Limiting (1 RPM across all 5 lambdas)
                log.info(f"[{self.project_id}] VisionEngine: Requesting global lock for pages {page_nums}...")
                self.limiter.acquire()

                response = self.raw_client.models.generate_content(
                    model="gemini-2.5-pro",
                    contents=parts,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=TagDiscoveryResult,
                        http_options=types.HttpOptions(timeout=240000) # Higher timeout for batches
                    )
                )
                
                output = response.parsed
                log.info(f"  Vision batch successful: {len(output.finish_tags)} tags found in pages {page_nums}")

                candidates = []
                for tag in output.finish_tags:
                    tag_upper = normalize_tag(tag)
                    if not tag_upper:   # skip rejected non-tags
                        continue
                    # Use mapped pages from Vision, fallback to entire batch if missing
                    tag_source_pages = output.tag_pages.get(tag, output.tag_pages.get(tag_upper, page_nums))
                    # Pull any metadata Vision extracted for this tag
                    raw_meta = output.tag_metadata.get(tag, output.tag_metadata.get(tag_upper, {}))
                    candidates.append(
                        TagCandidate(
                            tag=tag_upper,
                            source_pages=tag_source_pages,
                            found_by=["vision"],
                            confidence=0.75,
                            # Populate metadata fields from Vision's inline extraction
                            manufacturer=raw_meta.get("manufacturer", "N/S") or "N/S",
                            product_model=raw_meta.get("product_model", "N/S") or "N/S",
                            color=raw_meta.get("color", "N/S") or "N/S",
                            dimensions=raw_meta.get("dimensions", "N/S") or "N/S",
                            surface_location=raw_meta.get("surface_location", "N/S") or "N/S",
                            installation_remarks=raw_meta.get("installation_remarks", "N/S") or "N/S",
                            rooms=raw_meta.get("rooms", "N/S") or "N/S",
                            csi_section=raw_meta.get("csi_section", "N/S") or "N/S",
                            material_description=raw_meta.get("material_description", "N/S") or "N/S",
                        )
                    )
                return candidates

            except Exception as e:
                if attempt < self.MAX_RETRIES - 1:
                    wait = self.RETRY_DELAY * (attempt + 1)
                    log.warning(f"  Vision attempt {attempt+1} failed: {e}. Retrying in {wait}s...")
                    time.sleep(wait)
        return []


class HybridEngine:
    """
    Processes 'mixed' pages by combining digital text extraction (TextEngine)
    with visual model extraction (VisionEngine). This ensures we catch tags in
    both digital text blocks and any embedded scans, graphics, or drawings.
    """
    def __init__(self, text_engine: TextEngine, vision_engine: VisionEngine):
        self.text_engine = text_engine
        self.vision_engine = vision_engine

    def extract_from_page(
        self, doc: fitz.Document, page_info: PageInfo
    ) -> List[TagCandidate]:
        candidates: List[TagCandidate] = []
        
        # 1. Extract digital text candidates
        try:
            text_candidates = self.text_engine.extract_from_page(doc, page_info)
            candidates.extend(text_candidates)
        except Exception as e:
            log.error(f"HybridEngine (Text stage) failed on page {page_info.page_num}: {e}")

        # 2. Extract vision candidates
        try:
            vision_candidates = self.vision_engine.extract_batch(doc, [page_info])
            candidates.extend(vision_candidates)
        except Exception as e:
            log.error(f"HybridEngine (Vision stage) failed on page {page_info.page_num}: {e}")

        return candidates


class TagMerger:
    """Merges duplicate TagCandidates discovered by different engines or on different pages."""
    def merge(self, candidates: List[TagCandidate]) -> Dict[str, TagCandidate]:
        unified: Dict[str, TagCandidate] = {}
        for tc in candidates:
            tag = tc.tag
            if tag not in unified:
                # Make a copy to avoid mutating the original
                import copy
                unified[tag] = copy.deepcopy(tc)
            else:
                existing = unified[tag]
                # Merge pages
                for p in tc.source_pages:
                    if p not in existing.source_pages:
                        existing.source_pages.append(p)
                # Merge found_by
                for fb in tc.found_by:
                    if fb not in existing.found_by:
                        existing.found_by.append(fb)
                # Keep highest confidence
                existing.confidence = max(existing.confidence, tc.confidence)
                # Merge metadata fields (prefer non-empty / non-N/S values)
                for field_name in [
                    "manufacturer", "product_model", "color", "dimensions",
                    "surface_location", "installation_remarks", "rooms",
                    "csi_section", "material_description"
                ]:
                    v_existing = getattr(existing, field_name, "N/S") or "N/S"
                    v_new = getattr(tc, field_name, "N/S") or "N/S"
                    if v_existing in ("N/S", "UNKNOWN", "") and v_new not in ("N/S", "UNKNOWN", ""):
                        setattr(existing, field_name, v_new)

        # Apply page-count boost
        for tag, tc in unified.items():
            extra_pages = max(len(tc.source_pages) - 1, 0)
            unified[tag].confidence = min(
                tc.confidence + extra_pages * 0.05, 0.95
            )

        log.info(f"Merged {len(unified)} unique tag candidates from all engines.")
        return unified



# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 4 — LLM VERIFIER
# ══════════════════════════════════════════════════════════════════════════════

class LLMVerifier:
    """
    Final AI-powered verification pass.

    Sends the merged tag list to Gemini with context and asks it to:
      1. Confirm each tag is a genuine material/finish code
      2. Normalise formatting (pl-1 → PL-1)
      3. Reject product codes, dimensions, room numbers

    Processes in batches of 50 to stay within token limits.
    """

    BATCH_SIZE = 50
    MAX_RETRIES = 2

    VERIFY_PROMPT = """You are a senior US construction document specialist with 20+ years of experience reading CSI MasterFormat finish schedules for commercial, institutional, and educational projects.

Below is a list of tag candidates extracted from a construction finish schedule PDF.
Your job is to decide which are GENUINE material/finish tags and which are false positives.

════════════════════════════════════════════════════════════════
 ALWAYS ACCEPT — these are genuine finish tags, no exceptions
════════════════════════════════════════════════════════════════
TILE FAMILIES:
  CT-1 to CT-99    = Ceramic Tile types         (Dal Tile, American Olean, Florida Tile, etc.)
  PT-1 to PT-99    = Porcelain / Paver Tile      (Dal Tile Veranda, Crossville, etc.)
  GT-1 to GT-99    = Grout Types                 (Mapei Kerapoxy series: GT-1=Beige, GT-5=Grey, etc.)
  RT-1, RT-2       = Restored/Replacement Tile   (Rockwood, Summitville, etc.)

FLOORING FAMILIES:
  VT-1 to VT-99    = Vinyl Tile                  (Polyflor, Tarkett, etc.)
  VSF-1 to VSF-99  = Vinyl Sheet Flooring        (Polyflor Perlazzo, Armstrong, etc.)
  LVT-1 to LVT-99  = Luxury Vinyl Tile           (Shaw, Mohawk, Interface, etc.)
  LVP-1 to LVP-99  = Luxury Vinyl Plank
  VCT-1            = Vinyl Composition Tile      (Armstrong, Mannington, etc.)
  CPT-1 to CPT-99  = Carpet tile or broadloom    (Tandus, Interface, Patcraft, Atlas, etc.)
  TERT-1, TERT-2   = Terrazzo Tile (pre-fab)     (Nurazzo, Wausau Tile, etc.)
  TER-1            = Terrazzo poured-in-place    (epoxy or cementitious)
  RES-1            = Resinous / epoxy coating    (Sherwin-Williams, Stonhard, etc.)
  SPORT-1          = Rubber sports tile          (Ecore, Nora, Altro, etc.)
  WD-R             = Restored Wood Floor         (any brand)
  WD-A             = Athletic Wood Floor         (Connor Sports, Robbins, etc.)
  WD-B             = Bio-cushion Wood Floor      (Robbins Bio Cushion Classic, etc.)
  WD-1, WD-2       = Painted / Stained Wood Base (millwork base types)

PAINT TAGS — single-letter prefix P is a STANDARD US convention:
  P-1 to P-99      = Interior paint types        (Sherwin-Williams, Benjamin Moore, PPG, etc.)
  P-E1             = Exterior paint type 1       (do NOT reject single-letter P prefix!)

BASE / NOSING:
  RB-1 to RB-99    = Rubber Base                 (Roppe, Johnsonite, Burke, etc.)
  VRB              = Vented Rubber Base           (Johnsonite Vent-Cove, etc.)
  STN-1, STN-2     = Stair Tread Nosing          (Roppe Pinnacle Vantage, etc.)

CEILING:
  APC-1 to APC-99  = Acoustical Panel Ceiling    (Armstrong Ultima, etc.)

MASONRY / STANDALONE:
  GCMU, CMU, BRICK, CONC, SLATE, MARBLE, TERRAZZO, STUCCO, PLASTER, STONE, GRANITE, CORK, WDM
  VCT, VRB, FRP, GWB, ACT, ACP, LVT, LVP, CPT, WD, PL, GL, SS, SST, MW, QT, T

ALPHA / TYPE VARIANTS (ALWAYS accept):
  WD-A, WD-B, WD-R, WD-1A, CPT-12B, LVT-2A, TYPE A, TYPE B, TYPE I, TYPE IV

════════════════════════════════════════════════════════════════
 ALWAYS REJECT — these are NOT finish tags
════════════════════════════════════════════════════════════════
MANUFACTURER PAINT COLOR CODES (look like tags but are product numbers):
  SW-7008, SW-6227, SW-6472, SW-6501 → Sherwin-Williams color codes (NOT finish tags)
  BM-2070-30, BM-OC-9, HC-11, OC-9  → Benjamin Moore color codes (NOT finish tags)
  BM-OC → partial Benjamin Moore code (OC = Off-White series), reject it
  Any SW-XXXX, BM-XXXX, HC-XX, OC-XX format → manufacturer color number, NOT a finish tag

DAL TILE / AMERICAN OLEAN PRODUCT COLOR CODES:
  DO4, DO1, DO-4, DO-1   → Dal Tile Keystones color codes (appear after the real tag CT-6, CT-9)
  D335, D160, D-335      → Dal Tile color codes
  Q094, Q192, Q-94       → Dal Tile Semi-Gloss color codes
  R943, R940, R-943      → American Olean Urban Tones color codes
  P501, P527, P-501      → Dal Tile Veranda Solids color codes (3-digit number = product, not paint tag)
  RULE: Single/double-letter prefix + numeric suffix appearing AFTER the real tile tag = product color code.

ENGLISH WORDS THAT MATCH TAG PATTERN — always reject:
  COLOR-1   → The word "COLOR" followed by a number (from "P-E1 PAINT COLOR-1")
  DOORS     → Door schedule header
  TM        → Trademark symbol fragment from product name

DRAWING / SHEET REFERENCES:
  A-101, S-202, M-301, E-101, G-001, C-201 → Sheet references (letter + 3-digit)

CSI SPECIFICATION SECTION NUMBERS:
  09-65, 09651, 03-30, 09 30 00, 04 20 00 → Spec sections (NOT finish codes)
  IMPORTANT: "3-1" or "10-1" look like tags but are actually section sub-numbers.

ROOM / LOCATION IDENTIFIERS:
  R-101, ROOM-10, Room 205 → Room numbers used as location references only

STRUCTURAL / DRAWING GRID LABELS:
  A5, B3, C7 → Single letter + single digit = grid coordinate, NOT a finish tag

REVISION / PHASE / ZONE LABELS:
  REV-1, REV-2, PHASE-2, ZONE-3, LEVEL-2, STAIR-1

TERRAZZO PRODUCT DESIGN-BASIS CODES (embedded in product column):
  DC-18, DC-55, DC-055, DC-018 → Nurazzo/terrazzo manufacturer product basis codes,
  NOT finish schedule labels. Reject them — they appear in the PRODUCT column, not the TAG column.

CARPET / FLOORING PRODUCT SKU CODES:
  ET-47, ET47, 07324, 03026, 4351, 23503 → SKUs embedded in product description columns.
  S306 → Atlas Sidra series product code in product column.

════════════════════════════════════════════════════════════════
 SPECIAL NORMALIZATION RULES
════════════════════════════════════════════════════════════════
  SP0RT-1  → The "0" is a zero (PDF OCR artifact), normalize to SPORT-1
  "P -9"   → Space before hyphen is a PDF extraction artifact, normalize to P-9
  - Uppercase all letters: ct-1 → CT-1, cpt2 → CPT-2, pt1 → PT-1
  - Use dash separator: WD A → WD-A, CPT12B → CPT-12B
  - Preserve alpha suffixes: wd-a → WD-A, cpt-12b → CPT-12B
  - Keep standalone codes as-is: ACT, GWB, TERRAZZO, BRICK

TAG CANDIDATES:
{tags_json}

Return ONLY this JSON (no markdown, no backticks):
{{
  "verified_tags": ["CT-1", "CPT-1", "P-1", ...],
  "rejected_tags": ["SW-7008", "BM-2070-30", "DC-55", ...],
  "notes": "brief explanation of any notable rejections"
}}"""

    def __init__(self, llm_client, limiter):
        self.client = llm_client
        self.limiter = limiter
        if self.client is None:
            log.error("FATAL: instructor client failed — verifier will not run and will fall back to passing through all tags unverified.")

    def verify(self, merged: Dict[str, TagCandidate]) -> Dict[str, TagCandidate]:
        if self.client is None or not merged:
            log.warning("LLM Verifier: no client or no candidates — skipping.")
            return merged

        all_tags = list(merged.keys())
        verified_set: Set[str] = set()
        rejected_set: Set[str] = set()

        # Process in batches
        for i in range(0, len(all_tags), self.BATCH_SIZE):
            batch = all_tags[i : i + self.BATCH_SIZE]
            log.info(f"  Verifying batch {i//self.BATCH_SIZE + 1}: {len(batch)} tags...")

            for attempt in range(self.MAX_RETRIES):
                try:
                    prompt = PromptVault.get_router_verifier_prompt(
                        json.dumps(batch, indent=2)
                    )
                    self.limiter.acquire() # Quota protection
                    response: VerificationResult = self.client.chat.completions.create(
                        response_model=VerificationResult,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    verified_set.update(normalize_tag(t) for t in response.verified_tags)
                    rejected_set.update(normalize_tag(t) for t in response.rejected_tags)
                    if response.notes:
                        log.info(f"  Verifier notes: {response.notes}")
                    break
                except Exception as e:
                    log.warning(f"  Verifier attempt {attempt+1} failed: {e}")
                    time.sleep(2.0)
            else:
                # If all retries fail, keep the batch as-is
                log.error(f"  Verifier failed for batch — keeping {len(batch)} tags unverified.")
                verified_set.update(batch)

        # Remove rejected, boost confidence for verified
        final: Dict[str, TagCandidate] = {}
        for tag, tc in merged.items():
            if tag in verified_set:
                tc.confidence = min(tc.confidence + 0.15, 1.0)
                final[tag] = tc
            elif tag in rejected_set:
                log.debug(f"  Rejected: {tag}")
            else:
                # Not mentioned by verifier → keep with existing confidence
                final[tag] = tc

        log.info(
            f"Verification: {len(final)} kept, "
            f"{len(merged) - len(final)} rejected."
        )
        return final


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ORCHESTRATOR — RouterAgentV2
# ══════════════════════════════════════════════════════════════════════════════

class RouterAgentV2:
    """
    3-Engine Tag Discovery System.

    Scan every page → classify → run correct engine(s) → merge → verify → emit.

    Parameters
    ----------
    google_api_key : str, optional
        Gemini API key. Falls back to GOOGLE_API_KEY env var.
    schedule_score_threshold : float
        Min schedule score (0–1) to include a page for engine processing.
        Lower = more pages processed (safer but slower).
        Default 0.1 means: include even low-confidence pages.
    vision_dpi : int
        DPI for rendering scanned pages as images. 150 is a good balance.
    dry_run : bool
        If True, skips LLM calls and returns regex-only results.
    """

    def __init__(
        self,
        project_id: str = "UNKNOWN",
        request_id: str = "LOCAL",
        google_api_key: Optional[str] = None,
        schedule_score_threshold: float = 0.1,
        vision_dpi: int = 150,
        dry_run: bool = False,
    ):
        self.project_id = project_id
        self.request_id = request_id
        self.schedule_score_threshold = schedule_score_threshold
        self.vision_dpi = vision_dpi
        self.dry_run = dry_run

        # Model Selection: Priority to Environment Variable
        self.model_name = os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-pro")

        if not dry_run:
            # Setup GCP Credentials for Vertex AI
            setup_gcp_credentials()

            try:
                # Initialize Vertex AI Client
                self.raw_client = genai.Client(
                    vertexai=True,
                    project=os.environ.get("GCP_PROJECT_ID"),
                    location=os.environ.get("GCP_LOCATION", "us-central1")
                )
                self.llm_client = instructor.from_genai(
                    self.raw_client, model=self.model_name
                )
                log.info(f"LLM client ready (Vertex AI | {self.model_name} via instructor)")
            except Exception as e:
                self.raw_client = None
                self.llm_client = None
                log.warning(f"Failed to initialize Vertex AI client: {e}. Running in DRY RUN mode.")
                self.dry_run = True
        else:
            self.raw_client = None
            self.llm_client = None
            log.warning("Running in DRY RUN mode (regex-only)")
            self.dry_run = True

        # Build engine instances
        self.classifier   = PageClassifier()
        self.text_engine  = TextEngine()
        self.vision_engine = VisionEngine(self.raw_client, self.project_id, self.request_id)
        self.hybrid_engine = HybridEngine(self.text_engine, self.vision_engine)
        self.merger        = TagMerger()
        self.verifier      = LLMVerifier(self.llm_client, limiter=self.vision_engine.limiter)

    # ─────────────────────────── Public API ───────────────────────────────────

    def run(self, pdf_path: str) -> dict:
        """
        Full pipeline. Returns:
        {
            "tags"       : ["PL-1", "LVT-2", ...],   # sorted
            "confidence" : {"PL-1": 0.95, ...},
            "page_map"   : {"PL-1": [3, 7, 12], ...},
            "found_by"   : {"PL-1": ["text_table", "vision"], ...},
            "stats"      : {page classification breakdown, timing, etc.}
        }
        """
        t_start = time.time()
        log.info(f"{'='*60}")
        log.info(f"RouterAgentV2 starting: {pdf_path}")
        log.info(f"{'='*60}")

        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        log.info(f"PDF opened: {total_pages} pages total")

        # ── Stage 1: Classify ALL pages ───────────────────────────────────
        all_page_info: List[PageInfo] = self.classifier.classify_all(doc)

        # ── Filter: Which pages go to which engine? ───────────────────────
        non_empty_pages = [p for p in all_page_info if p.page_type != PageType.EMPTY]
        
        # 1. Text Engine: Fast and free. Run on ALL non-empty pages.
        text_pages = non_empty_pages
        
        # 2. Vision Engine: Run on high-scoring scanned/mixed pages.
        vision_candidates = [
            p for p in non_empty_pages 
            if p.page_type in (PageType.SCANNED, PageType.MIXED) 
            and p.schedule_score >= self.schedule_score_threshold
        ]
        
        # Safety net: if no high-scoring scanned pages, include ALL scanned/mixed pages.
        # A construction PDF may have low keyword scores but still have finish schedule images.
        if not vision_candidates:
            scanned_only = [p for p in non_empty_pages if p.page_type in (PageType.SCANNED, PageType.MIXED)]
            if scanned_only:
                vision_candidates = scanned_only  # ALL pages — do NOT slice to last N
                log.warning(
                    f"No high-scoring scanned pages found. "
                    f"Including ALL {len(scanned_only)} scanned/mixed pages as safety net."
                )

        log.info(f"Processing {len(text_pages)} pages via TextEngine and {len(vision_candidates)} pages via VisionEngine.")

        # ── Stage 2: Run engines ──────────────────────────────────────────
        all_candidates: List[TagCandidate] = []
        page_stats = defaultdict(int)

        # 1. Process Text pages (fast)
        for p_info in text_pages:
            page_stats[p_info.page_type.value] += 1
            found = self.text_engine.extract_from_page(doc, p_info)
            all_candidates.extend(found)

        # 2. Process Vision pages (Hybrid/Scanned)
        scanned_pages = [p for p in vision_candidates if p.page_type == PageType.SCANNED]
        mixed_pages = [p for p in vision_candidates if p.page_type == PageType.MIXED]

        # 3. Process Mixed pages (Hybrid)
        for p_info in mixed_pages:
            page_stats[p_info.page_type.value] += 1
            if not self.dry_run:
                found = self.hybrid_engine.extract_from_page(doc, p_info)
                all_candidates.extend(found)
            else:
                log.debug(f"  DRY RUN: skipping Hybrid engine for page {p_info.page_num}")

        # 3. Process SCANNED pages in batches of 10
        batch_size = self.vision_engine.BATCH_SIZE
        for i in range(0, len(scanned_pages), batch_size):
            batch = scanned_pages[i : i + batch_size]
            for p_info in batch:
                page_stats[p_info.page_type.value] += 1
            
            log.info(f"  Processing Vision batch {i//batch_size + 1}/{(len(scanned_pages)-1)//batch_size + 1} ({len(batch)} pages)")
            if not self.dry_run:
                found = self.vision_engine.extract_batch(doc, batch)
                all_candidates.extend(found)
            else:
                log.debug(f"  DRY RUN: skipping Vision batch {i//batch_size + 1}")

        log.info(f"Engines produced {len(all_candidates)} raw candidates.")

        # ── Stage 3: Merge ────────────────────────────────────────────────
        merged = self.merger.merge(all_candidates)

        # ── Stage 4: LLM Verify ───────────────────────────────────────────
        if not self.dry_run and merged:
            final = self.verifier.verify(merged)
        else:
            final = merged

        # Apply minimum confidence gate — blocks product-code false positives that
        # scored 0.25–0.40 in prefix-aware scoring while safely passing all known-
        # prefix tags (which start at 0.55–0.70). Set at 0.45 to give one page-count
        # boost of 0.05 to standalone codes (0.50) a safety margin.
        MIN_EMIT_CONFIDENCE = 0.45
        final = {
            tag: tc for tag, tc in final.items() 
            if tc.confidence >= MIN_EMIT_CONFIDENCE
        }
        log.info(f"After confidence gate ({MIN_EMIT_CONFIDENCE}): {len(final)} tags remain.")

        # ── Stage 5: Format output ────────────────────────────────────────
        elapsed = round(time.time() - t_start, 2)
        
        def safe_sort_key(tag):
            # Handles PL-1, PL-1A, or malformed tags without crashing
            parts = tag.split("-")
            prefix = parts[0]
            if len(parts) > 1:
                # Extract numeric part safely
                num_str = "".join(filter(str.isdigit, parts[1]))
                suffix = int(num_str) if num_str else 0
                alpha = "".join(filter(str.isalpha, parts[1]))
                return (prefix, suffix, alpha)
            return (prefix, 0, "")

        tags_sorted = sorted(final.keys(), key=safe_sort_key)

        result = {
            "tags": tags_sorted,
            "confidence": {t: round(final[t].confidence, 3) for t in tags_sorted},
            "page_map": {t: sorted(final[t].source_pages) for t in tags_sorted},
            # Include ±1 page buffer to handle Router page_num inaccuracy:
            "page_map_with_buffer": {
                t: sorted(set(
                    p + offset 
                    for p in final[t].source_pages 
                    for offset in [-1, 0, 1]
                    if p + offset > 0
                ))
                for t in tags_sorted
            },
            "found_by": {t: final[t].found_by for t in tags_sorted},
            "metadata": {
                t: {
                    "manufacturer": final[t].manufacturer,
                    "product_model": final[t].product_model,
                    "color": final[t].color,
                    "dimensions": final[t].dimensions,
                    "surface_location": final[t].surface_location,
                    "installation_remarks": final[t].installation_remarks,
                    "rooms": final[t].rooms,
                    "csi_section": final[t].csi_section,
                    "material_description": final[t].material_description,
                }
                for t in tags_sorted
            },
            "stats": {
                "total_pages": total_pages,
                "candidate_pages": len(non_empty_pages),
                "page_types": dict(page_stats),
                "raw_candidates": len(all_candidates),
                "after_merge": len(merged),
                "after_verification": len(final),
                "elapsed_seconds": elapsed,
                "dry_run": self.dry_run,
            },
        }

        self._print_summary(result)
        doc.close()
        return result

    def _print_summary(self, result: dict) -> None:
        s = result["stats"]
        log.info(f"{'='*60}")
        log.info("ROUTER AGENT V2 — RESULTS")
        log.info(f"{'='*60}")
        log.info(f"Total tags found    : {len(result['tags'])}")
        log.info(f"Tags                : {result['tags']}")
        log.info(f"Pages scanned       : {s['candidate_pages']} / {s['total_pages']}")
        log.info(f"Page breakdown      : {s['page_types']}")
        log.info(f"Raw candidates      : {s['raw_candidates']}")
        log.info(f"After merge         : {s['after_merge']}")
        log.info(f"After verification  : {s['after_verification']}")
        log.info(f"Elapsed             : {s['elapsed_seconds']}s")
        log.info(f"{'='*60}")
        log.info("CONFIDENCE SCORES:")
        for tag in result["tags"]:
            conf = result["confidence"][tag]
            pages = result["page_map"][tag]
            engines = ", ".join(result["found_by"][tag])
            log.info(f"  {tag:10} | conf={conf:.2f} | pages={pages} | via={engines}")
        log.info(f"{'='*60}")


# ══════════════════════════════════════════════════════════════════════════════
#  PIPELINE EMITTER — send results downstream
# ══════════════════════════════════════════════════════════════════════════════

class PipelineEmitter:
    """
    Emits verified tags to the downstream pipeline.

    Currently supports:
      - JSON file output
      - Dict return (for in-process usage)

    Extend this class to add: REST API POST, message queue, database insert, etc.
    """

    def emit_to_file(self, result: dict, output_path: str) -> None:
        """Write result dict to a JSON file."""
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        log.info(f"Pipeline output written to: {output_path}")

    def emit_tags_only(self, result: dict) -> List[str]:
        """Return just the verified tag list — for passing to next agent."""
        return result["tags"]

    def emit_high_confidence(
        self, result: dict, min_confidence: float = 0.7
    ) -> List[str]:
        """Return only tags above a confidence threshold."""
        return [
            t for t in result["tags"]
            if result["confidence"].get(t, 0) >= min_confidence
        ]


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def lambda_handler(event, context):
    """
    AWS Lambda entry point for Router Agent (Tag Discovery).
    Triggered by S3 Ping. Downloads PDF, finds tags, and uploads tags.json to S3.
    """
    try:
        # Detect if it's an EventBridge event or direct S3
        if 'Records' in event:
            record = event['Records'][0]
            bucket = record['s3']['bucket']['name']
            key = record['s3']['object']['key']
        else:
            # EventBridge S3 notification
            bucket = event['detail']['bucket']['name']
            key = event['detail']['object']['key']
            
        # --- LOOP PREVENTION & FILTERING ---
        if not key.lower().endswith(".pdf"):
            logging.info(f"Skipping non-PDF file: {key}")
            return {"statusCode": 200, "body": f"Skipped non-PDF: {key}"}

        project_id = key.split("/")[-1].split(".")[0].upper()
        
        local_pdf = f"/tmp/{os.path.basename(key)}"
        output_key = f"tags/{project_id}_tags.json"
        local_output = f"/tmp/{project_id}_tags.json"
        
        s3 = boto3.client('s3')
        
        # Deduplication: Skip if already done
        try:
            s3.head_object(Bucket=bucket, Key=output_key)
            logging.info(f"[{project_id}] tags.json already exists. Skipping duplicate run.")
            return {"statusCode": 200, "body": "Already completed"}
        except ClientError as e:
            if e.response['Error']['Code'] != '404':
                raise
        # 404 = not found = proceed normally

        logging.info(f"Downloading s3://{bucket}/{key} for Tag Discovery...")
        s3.download_file(bucket, key, local_pdf)
        
        # Run Router
        request_id = getattr(context, 'aws_request_id', 'LOCAL_CLI')
        router = RouterAgentV2(project_id=project_id, request_id=request_id)
        result = router.run(local_pdf)
        
        # Save locally then upload
        emitter = PipelineEmitter()
        emitter.emit_to_file(result, local_output)
        
        logging.info(f"Uploading discovered tags to s3://{bucket}/{output_key}")
        s3.upload_file(local_output, bucket, output_key)
        
        # Cleanup
        for f in [local_pdf, local_output]:
            if os.path.exists(f): os.remove(f)

        # Sync and Trigger
        check_in_pipeline(project_id, "router_done", bucket, key)
            
        return {"statusCode": 200, "body": f"Tags discovered and uploaded for {key}"}
    except Exception as e:
        from src.database.error_logger import error_logger
        proj_id = key.split("/")[-1].split(".")[0].upper() if 'key' in locals() else "UNKNOWN"
        error_logger.log_error(proj_id, "Router", e)
        return {"statusCode": 500, "body": str(e)}

def check_in_pipeline(project_id: str, component: str, bucket: str, key: str):
    """
    State-Sync Machine: Ensures Orchestrator only starts when BOTH Loader and Router finish.
    Uses atomic check-and-set via supabase_db helper to prevent double-triggering.
    """
    from src.database.supabase_db import supabase as sb, try_trigger_orchestrator
    
    # Step 1: Mark this component as done
    try:
        sb.table("pipeline_state").update({
            component: True
        }).eq("project_id", project_id).execute()
        logging.info(f"[{project_id}] {component} marked as DONE.")
    except Exception as e:
        logging.error(f"Failed to update pipeline state for {component}: {e}")
        return
    # Step 2: Atomic attempt to trigger orchestrator
    if try_trigger_orchestrator(project_id):
        logging.info(f"PIPELINE SYNC COMPLETE for {project_id}. Waking up Orchestrator...")
        
        try:
            # Use Asynchronous Lambda Invoke instead of requests.post to avoid 10s timeouts
            orchestrator_lambda = os.environ.get("ORCHESTRATOR_FUNCTION_NAME", "Construction-Orchestrator")
            import boto3
            client = boto3.client('lambda')
            
            payload = {
                "project_id": project_id,
                "s3_path": f"s3://{bucket}/{key}"
            }
            
            client.invoke(
                FunctionName=orchestrator_lambda,
                InvocationType='Event', # Asynchronous
                Payload=json.dumps(payload).encode()
            )
            logging.info(f"Orchestrator successfully triggered via async invoke for {project_id}")
        except Exception as e:
            logging.error(f"Failed to trigger Orchestrator via async invoke: {e}")

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) > 1:
        PDF_PATH = sys.argv[1]
        OUTPUT_JSON = sys.argv[2] if len(sys.argv) > 2 else "tags_output.json"
        router = RouterAgentV2()
        result = router.run(PDF_PATH)
        emitter = PipelineEmitter()
        emitter.emit_to_file(result, OUTPUT_JSON)
    else:
        print("Usage: python router_agent.py <path_to_pdf> [output_json]")