# ==============================================================================
# PROPRIETARY AND CONFIDENTIAL — STRICTLY RESTRICTED USE ONLY
# ------------------------------------------------------------------------------
# Copyright (c) 2026 Mujeeb Ahmad. All rights reserved.
# Unauthorized commercial reproduction, distribution, replication, or hosting
# of this proprietary software, in whole or in part, is strictly prohibited.
# Licensed solely for authorized technical review and demonstration purposes.
# ==============================================================================

"""
src/core/vault.py — Secure Prompt Vault & Domain Configuration Registry

Single authoritative source for all LLM prompts and AEC domain heuristics
used across the extraction pipeline. Separating prompt authorship from pipeline
logic achieves two critical objectives:

  1. IP Shielding: PhD-level enterprise prompts are injected from secure
     environment variables at runtime. The repository exposes only high-fidelity
     demonstration templates that showcase architectural intent without leaking
     production tuning parameters.

  2. Configuration Decoupling: Agents reference PromptVault class methods
     rather than inline constants, so prompt upgrades, A/B tests, and domain
     vocabulary expansions are applied pipeline-wide without touching agent code.

Production Override Pattern:
    Set any of the VAULT_* environment variables to a fully-formatted template
    string. The vault will prefer the env-var value over the built-in fallback.
    Template placeholders must match the keyword arguments documented on each
    class method.
"""

import os
import logging

logger = logging.getLogger("PromptVault")


class PromptVault:
    """
    Centralized prompt and domain configuration registry for the AEC
    document intelligence pipeline.

    All methods follow the same contract:
      - Check for a VAULT_* environment variable first (production override).
      - Fall back to the built-in high-fidelity demonstration template.
      - Log which source was used so ops teams can audit prompt provenance.

    Domain knowledge methods (get_domain_*) return Python data structures
    (dicts, sets, frozensets) rather than strings. These are not overridable
    via environment variables — they represent the compiled AEC vocabulary
    that underpins the pipeline's CSI MasterFormat awareness.
    """

    # ══════════════════════════════════════════════════════════════════════════
    #  LLM PROMPT METHODS
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def get_extractor_vision_prompt(
        cls,
        target_tag: str,
        page_num: int,
        source_document: str,
        hint_clause: str,
        material_hint: str,
        evidence_id: str,
    ) -> str:
        """
        Multimodal extraction prompt for Tier 3 Vision API calls.

        Instructs Gemini Vision to scan a rendered page image, locate the
        target finish tag in schedule tables, legends, or callouts, and
        extract all associated specification fields with strict evidence
        anchoring — never inferring or hallucinating absent data.

        Production override: VAULT_EXTRACTOR_VISION_PROMPT
        """
        prod = os.environ.get("VAULT_EXTRACTOR_VISION_PROMPT")
        if prod:
            logger.info("PromptVault: loaded enterprise Extractor Vision prompt from secure vault.")
            return prod.format(
                target_tag=target_tag,
                page_num=page_num,
                source_document=source_document,
                hint_clause=hint_clause,
                material_hint=material_hint,
                evidence_id=evidence_id,
            )

        return f"""You are a Senior US Commercial Construction Specification Analyst
with 20 years reading CSI MasterFormat finish schedules for schools,
hospitals, government buildings, and offices.

TARGET TAG: '{target_tag}' ({material_hint})
SOURCE: Page {page_num} of {source_document}
EVIDENCE ID: {evidence_id}
{hint_clause}

YOUR TASK:
Scan this ENTIRE page image. Find EVERY location where '{target_tag}' appears —
in schedule tables, legends, keynotes, callouts, annotations, or plan notes.
Extract ALL data associated with '{target_tag}'.

FIELD EXTRACTION GUIDE:
- finish_tag       : Return exactly '{target_tag}' in JSON only after you locate a source
                     row/callout for '{target_tag}' or a conservative OCR-equivalent spelling.
                     OCR-equivalent means same cleaned length with only 0/O, 1/I/L, 5/S, 2/Z
                     substitutions. TER-1 is NOT TERT-1.
- area_scope       : WHERE this tag is applied — room name, room number, zone, floor, area.
                     Often in the NOTES column or last column of the schedule table.
                     If visible in multiple rooms, list all: "Lobby, Corridor 101, Staff Lounge"
- manufacturer     : Brand/company name — e.g. Armstrong, Shaw, Roppe, Sherwin-Williams,
                     Dal Tile, Mapei, Polyflor, Johnsonite, Connor Sports, Nurazzo, Tandus.
- model_series     : Product line/series — e.g. "Mystique PUR", "Pinnacle", "Kerapoxy",
                     "Veranda Solids", "Urban Tones", "Perlazzo PUR", "Bio Cushion Classic".
- finish_color     : EXACT color name AND code if both present — e.g. "Beola 3560",
                     "SW-7008 Alabaster", "Chianti Q094", "Burnt Umber 194", "Deep Purple DO44".
- size             : Physical dimensions WITH units — e.g. '3" x 6"', '24"x24"',
                     "608mm x 608mm", '13" x 13" x 3/8"', '6" straight base'.
- product_criteria : Finish sheen (MATTE, SEMI GLOSS, GLOSS), backing type, wear layer,
                     tile pattern, technical grade — capture exactly as written.
- installation_criteria: Install method, bond pattern (RUNNING BOND, STACK BOND,
                     CAMOUFLAGE, 1/4 TURN, BRICK MONOLITHIC), adhesive, substrate,
                     weld rod, built-up base details.
- standards_codes  : ASTM, ANSI, UL, NFPA, LEED references if visible.
- csi_division     : 2-digit division number — e.g. "09" for finishes, "04" for masonry.
- csi_section      : Full section — e.g. "09 65 00", "09 91 23", "09 68 13".
- evidence_excerpt : Copy the EXACT row text verbatim from the schedule. Do NOT paraphrase.

[IP Shielding Active: Enterprise extraction parameters enforced.]

STATUS RULES:
- 'confirmed'          : Tag found, fields extracted with high confidence.
- 'verification_needed': Tag found but 1+ fields unclear or cut off.
- 'reference_only'     : Tag appears only in a legend or note, not a primary schedule row.
- 'excluded'           : ONLY if '{target_tag}' DOES NOT EXIST ANYWHERE in this document.
                         Do NOT use 'excluded' just because tag is not on THIS page.
                         If not on this page → confidence=0.1, status='verification_needed'.

CRITICAL RULES:
1. Extract ONLY '{target_tag}' data or an OCR-equivalent spelling of that exact tag.
2. Do NOT mix data from adjacent rows or semantically similar sibling tags.
3. Copy evidence_excerpt VERBATIM — the exact words from the document.
4. Use 'N/S' for fields genuinely absent. NEVER invent manufacturer, model, or color.
5. confidence: be honest — 0.0 to 1.0. Lower if any field is unclear.
6. If you see only a sibling tag (for example TERT-1 while target is TER-1), fail closed:
   status='verification_needed', confidence<=0.25, manufacturer/model/color='N/S'.
7. Set extraction_method='vision' exactly.

IF TAG NOT VISIBLE ON THIS PAGE:
Return: finish_tag='{target_tag}', confidence=0.1, status='verification_needed',
evidence_excerpt='Tag {target_tag} not visible on page {page_num}.'"""

    @classmethod
    def get_extractor_text_prompt(
        cls,
        target_tag: str,
        material_type: str,
        all_hints: str,
        combined_evidence: str,
        source_document: str,
        page_num: int,
        packed_evidence_id: str,
    ) -> str:
        """
        Text-only LLM extraction prompt for Tier 2 cognitive extraction.

        Instructs the LLM to resolve a finish tag from Pinecone-retrieved
        pipe-delimited chunks, applying header-guided column mapping when
        available and the Headerless Row Interpreter Protocol when not.

        Production override: VAULT_EXTRACTOR_TEXT_PROMPT
        """
        prod = os.environ.get("VAULT_EXTRACTOR_TEXT_PROMPT")
        if prod:
            logger.info("PromptVault: loaded enterprise Extractor Text prompt from secure vault.")
            return prod.format(
                target_tag=target_tag,
                material_type=material_type,
                all_hints=all_hints,
                combined_evidence=combined_evidence,
                source_document=source_document,
                page_num=page_num,
                packed_evidence_id=packed_evidence_id,
            )

        return f"""You are a PhD-level US Commercial Construction Specification Analyst.

TARGET TAG: '{target_tag}' ({material_type})

PIPE TABLE INTERPRETATION:
- Prefer rows whose KEY/TAG/FINISH TAG cell equals '{target_tag}'.
- Accept OCR-equivalent tag cells only when the cleaned strings are the same length and differ only by 0/O, 1/I/L, 5/S, or 2/Z. Example: SPORT-1 may appear as SP0RT-1.
- Reject near-neighbor tags with inserted/deleted letters. TER-1 is NOT TERT-1.
- Use header labels when present.

HEADERLESS ROW INTERPRETER PROTOCOL:
- When processing an unparsed fallback row block without column headers, use construction engineering content-shape, not fixed column indexes.
- Evaluate each string/token to judge whether it is CSI section, finish/material type, manufacturer, product series/model, color/name/code, notes, type/size, installation criteria, or area/scope.
- Manufacturer usually looks like a brand/company; product series like a named collection; color may be a plain color name, color number, or token such as COLOR-1; size/type contains dimensions, base profiles, tile/plank formats, or material configuration.
- Do NOT rely on a fixed column index or assume product follows manufacturer.
- If a field is genuinely missing from the raw row text, output 'N/S'.
- You are strictly forbidden from guessing, hallucinating, or cross-borrowing data from neighboring sibling rows.

KNOWN PARTIAL DATA (trust these unless evidence contradicts):
{all_hints}

RULES:
1. Extract ONLY '{target_tag}' or an OCR-equivalent spelling of that exact tag.
2. Do NOT use data from sibling tags, adjacent rows, or semantically similar materials.
3. If the evidence only contains a sibling tag, return status='verification_needed', confidence<=0.25, manufacturer/model/color='N/S', and explain in special_remarks.
4. finish_tag must be exactly '{target_tag}' in the JSON, but only after the source row has been proven to be this target or its OCR-equivalent.
5. evidence_excerpt: copy the EXACT matching pipe row verbatim. Do not paste instructions.
6. area_scope: extract from NOTES column (rooms, floors, areas).
7. installation_criteria: look for pattern names (RUNNING BOND, STACK BOND), backing type, adhesive method in notes.
8. product_criteria: finish sheen (MATTE, SEMI GLOSS), wear layer, backing.
9. Use 'N/S' only if genuinely absent. Never invent data.
10. source_document='{source_document}', page_reference='{page_num}', evidence_id='{packed_evidence_id}', extraction_method='text'.

Extract ALL specification data for '{target_tag}'.

PINECONE EVIDENCE, reranked by target-row likelihood across ALL retrieved chunks:
{combined_evidence}

Find the row whose tag/key cell is '{target_tag}' or a conservative OCR-equivalent. Map columns by header labels when available. For headerless/unparsed rows, apply the Headerless Row Interpreter Protocol using only the raw row/cell content. If no such row is present, fail closed with verification_needed instead of borrowing a similar tag's data."""

    @classmethod
    def get_router_verifier_prompt(cls, tags_json: str) -> str:
        """
        LLM verifier prompt used by RouterAgentV2's LLMVerifier stage.

        Validates a raw list of tag candidates produced by the Text and
        Vision engines, rejecting manufacturer color codes, drawing references,
        CSI section numbers, and other false-positive patterns while normalising
        casing, separators, and OCR substitutions in genuine tags.

        Production override: VAULT_ROUTER_VERIFIER_PROMPT
        """
        prod = os.environ.get("VAULT_ROUTER_VERIFIER_PROMPT")
        if prod:
            logger.info("PromptVault: loaded enterprise Router Verifier prompt from secure vault.")
            return prod.format(tags_json=tags_json)

        return f"""You are a senior US construction document specialist with 20+ years of experience reading CSI MasterFormat finish schedules for commercial, institutional, and educational projects.

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

    @classmethod
    def get_router_vision_prompt(cls, page_nums_str: str, page_example: str) -> str:
        """
        Multimodal batch-scan prompt for RouterAgentV2's VisionEngine stage.

        Instructs Gemini Vision to scan a batch of rendered PDF pages and
        identify all unique finish/material tag codes present — without
        extracting product detail data (that is the Extractor's responsibility).

        Production override: VAULT_ROUTER_VISION_PROMPT
        """
        prod = os.environ.get("VAULT_ROUTER_VISION_PROMPT")
        if prod:
            logger.info("PromptVault: loaded enterprise Router Vision prompt from secure vault.")
            return prod.format(page_nums_str=page_nums_str, page_example=page_example)

        return f"""You are a Construction Document Analyst specialising in finish schedules for US commercial construction projects.

Look at these images (pages) from a construction document set. These pages are from page numbers: {page_nums_str}

YOUR TASK:
Find ALL unique material/finish tags visible anywhere on these pages. A "finish tag" is the SHORT CODE in the first column of a finish schedule row that identifies a specific material or surface type. It is NOT the product name, manufacturer, color name, or specification section number.

════════════════════════════════════════════════
 WHAT IS A FINISH TAG — accept ALL these formats
════════════════════════════════════════════════

FORMAT 1 — Standard hyphenated (PREFIX-NUMBER):
  CT-1, CT-10, CT-12     → Ceramic Tile types
  PT-1, PT-2             → Porcelain/Paver Tile types
  GT-1 through GT-8      → Grout Types
  CPT-1 through CPT-6    → Carpet types (broadloom or tile)
  VT-1, VT-2, VT-3       → Vinyl Tile types
  VSF-1, VSF-2           → Vinyl Sheet Flooring
  VCT-1                  → Vinyl Composition Tile
  LVT-1, LVT-2           → Luxury Vinyl Tile
  TERT-1, TERT-2         → Terrazzo Tile (prefabricated)
  TER-1                  → Terrazzo (poured epoxy)
  RES-1                  → Resinous / broadcast epoxy coating
  SPORT-1                → Rubber sports tile
  RB-1, RB-2, RB-3       → Rubber Base
  STN-1, STN-2           → Stair Tread Nosing
  APC-2, APC-3           → Acoustical Panel Ceiling
  P-1 through P-19       → PAINT TYPES (very common, single letter prefix P is valid)
  P-E1                   → Exterior Paint type 1

FORMAT 2 — No-separator merged (PREFIX immediately followed by NUMBER):
  CPT1, LVT12, PT1, VCT1

FORMAT 3 — Alpha-only or alpha suffix:
  WD-A, WD-B, WD-R       → Wood floor subtypes (A=athletic, B=bio-cushion, R=restored)
  WD-1, WD-2             → Wood Base types
  CPT-12B, LVT-2A        → Variants with letter suffix

FORMAT 4 — Named types:
  TYPE A, TYPE B, TYPE I, TYPE II, TYPE IV

FORMAT 5 — Standalone material codes (no number):
  CONC   = Sealed/Polished Concrete    VCT = Vinyl Composition Tile
  SLATE  = Slate                       VRB = Vented Rubber Base
  BRICK  = Brick                       WD  = Wood (generic reference)
  CMU    = Concrete Masonry Unit       GCMU = Ground-Face CMU
  MARBLE = Marble                      TERRAZZO = Terrazzo
  ACT    = Acoustical Ceiling Tile     GWB = Gypsum Wallboard
  FRP    = Fiber Reinforced Panel      CPT = Carpet (generic)

════════════════════════════════════════════════
 WHERE TO LOOK
════════════════════════════════════════════════
  - The FIRST COLUMN of any finish schedule table (this is almost always the tag)
  - Material legends and finish keys
  - Room finish schedule matrices (rows = rooms, columns = floor/base/wall/ceiling tags)
  - Floor plan callouts and keynotes
  - Wall section annotations and detail notes
  - Ceiling plan notation
  - Door/window schedule "finish" columns

════════════════════════════════════════════════
 REJECT THESE — they look like tags but are NOT
════════════════════════════════════════════════
  - Paint color product numbers: SW-7008, BM-2070-30, HC-11, OC-9 (manufacturer color codes)
  - Drawing/sheet reference numbers: A-101, S-202, M-301, G-001 (sheet numbers)
  - CSI specification section numbers: 09-65-00, 09651, 03 35 13 (division numbers)
  - Room/space identifiers appearing as locations: R-101, Room 205
  - Structural grid labels: A5, B3, C7 (single letter + single digit = grid coordinate)
  - Revision codes: REV-1, REV-2
  - Phase/zone labels: PHASE-2, ZONE-3, LEVEL-2
  - Terrazzo product design basis codes like DC-055, DC-018 (these are product SKUs, not tags)
  - Carpet product SKUs embedded in product column: ET47, 07324, 03026 (in the product/model column)
  - Product adhesive codes: PL-400, XA-1004 (3+ digit numbers after the dash)

RULE: The tag is almost always the FIRST token in its row, not embedded in the middle of product description text.

════════════════════════════════════════════════
 OUTPUT RULES
════════════════════════════════════════════════
  - List each unique tag only ONCE
  - For each tag, record the page number(s) from {page_nums_str} where it appears
  - Normalize to uppercase with hyphen separator: ct-1 → CT-1, cpt2 → CPT-2

Return ONLY a JSON object (no markdown, no backticks):
{{"finish_tags": ["CT-1", "CPT-1", "P-1"], "tag_pages": {{"CT-1": [{page_example}], "CPT-1": [{page_example}], "P-1": [{page_example}]}}}}

If no finish tags found: {{"finish_tags": [], "tag_pages": {{}}}}"""

    @classmethod
    def get_fallback_hybrid_prompt(
        cls,
        tag: str,
        project_id: str,
        p_num: int,
        hint_clause: str,
        combined_context: str,
    ) -> str:
        """
        Hybrid deep-scan prompt for the FallbackAgent's Vision + Markdown pass.

        Unlike the standard extractor prompt which works from pre-embedded
        Pinecone chunks, this prompt operates on raw page image + plumber
        markdown tables + raw OCR text simultaneously — the most expensive
        and most capable modality, reserved for tags that fail all upstream
        retrieval tiers.

        Production override: VAULT_FALLBACK_HYBRID_PROMPT
        """
        prod = os.environ.get("VAULT_FALLBACK_HYBRID_PROMPT")
        if prod:
            logger.info("PromptVault: loaded enterprise Fallback Hybrid prompt from secure vault.")
            return prod.format(
                tag=tag,
                project_id=project_id,
                p_num=p_num,
                hint_clause=hint_clause,
                combined_context=combined_context,
            )

        return f"""[HYBRID DEEP SCAN PROTOCOL]
Target Tag: {tag}
Project: {project_id}
Page: {p_num}

You are a Senior AEC Specification Analyst executing a deep visual and structural scan.
Locate and extract EVERY technical detail for '{tag}'.
{hint_clause}

STRUCTURED DATA (PDFPlumber Tables + Raw OCR):
{combined_context}

VISUAL DATA: (See attached page image)

STRICT JSON SCHEMA:
{{
    "finish_tag": "{tag}",
    "area_scope": "...",
    "manufacturer": "...",
    "model_series": "...",
    "finish_color": "...",
    "size": "...",
    "product_criteria": "...",
    "installation_criteria": "...",
    "standards_codes": "...",
    "csi_division": "...",
    "csi_section": "...",
    "source_document": "...",
    "page_reference": "{p_num}",
    "evidence_excerpt": "...",
    "confidence": 0.0,
    "status": "confirmed",
    "extraction_method": "vision"
}}

RULES:
1. Cross-reference the image and the structured text — the image is ground truth.
2. Use 'N/S' for fields genuinely absent. Never guess or fabricate.
3. evidence_excerpt: copy the EXACT row text verbatim from the source.
4. Set confidence honestly — 0.0 to 1.0.
5. If tag is not present on this page, set status='verification_needed', confidence=0.1."""

    # ══════════════════════════════════════════════════════════════════════════
    #  DOMAIN CONFIGURATION — AEC VOCABULARY REGISTRIES
    # ══════════════════════════════════════════════════════════════════════════

    @classmethod
    def get_domain_tag_prefix_map(cls) -> dict:
        """
        Returns the canonical AEC finish tag prefix → material category mapping.

        Used by ExtractorWorker to derive material context hints for LLM prompts
        and by the Router's TextEngine to assign baseline confidence scores to
        candidate tags whose prefix is a known CSI MasterFormat abbreviation.

        Entries ordered by CSI division: 03 Concrete → 04 Masonry → 06 Wood →
        07 Thermal → 08 Openings → 09 Finishes → 11-12 Equipment/Furnishings.
        """
        return {
            # ── Carpet ──────────────────────────────────────────────────────
            'CPT':   'Carpet Tile or Carpet Broadloom',
            'CT':    'Ceramic Tile',
            # ── Luxury Vinyl ─────────────────────────────────────────────────
            'LVT':   'Luxury Vinyl Tile',
            'LVP':   'Luxury Vinyl Plank',
            'VT':    'Vinyl Tile',
            'VCT':   'Vinyl Composition Tile',
            'VSF':   'Vinyl Sheet Flooring',
            'SV':    'Sheet Vinyl',
            # ── Tile / Stone ─────────────────────────────────────────────────
            'PT':    'Paver Tile or Paint (check context)',
            'PC':    'Porcelain Tile',
            'QT':    'Quarry Tile',
            'ST':    'Stone Tile or Slate',
            'MT':    'Mosaic Tile',
            'GT':    'Grout',
            'RT':    'Restored or Glazed Wall Tile',
            # ── Terrazzo / Resinous ──────────────────────────────────────────
            'TERT':  'Terrazzo Tile (prefabricated)',
            'TER':   'Epoxy or Poured Terrazzo',
            'RES':   'Resinous Flooring (epoxy/polymer)',
            'EPX':   'Epoxy Flooring',
            'EF':    'Epoxy Floor Coating',
            # ── Sports / Specialty Flooring ──────────────────────────────────
            'SPORT': 'Sports Rubber Flooring',
            'WDA':   'Wood Athletic Floor',
            'WDB':   'Wood Athletic Floor',
            'WDR':   'Restored Wood Floor',
            'WD':    'Wood Base, Wood Door, or Wood Element',
            # ── Concrete / Masonry ───────────────────────────────────────────
            'CONC':  'Sealed or Stained Concrete',
            'SL':    'Sealed Concrete',
            'CMU':   'Concrete Masonry Unit',
            'GCMU':  'Ground Face CMU',
            'BRICK': 'Brick',
            'SLATE': 'Slate Treads',
            # ── Wall Base / Stair ─────────────────────────────────────────────
            'RB':    'Resilient Rubber Base',
            'VRB':   'Vented Rubber Base',
            'WB':    'Wall Base',
            'STN':   'Stair Tread Nosing',
            # ── Paint / Coating ──────────────────────────────────────────────
            'P':     'Paint',
            'PNT':   'Paint System',
            'EP':    'Exterior Paint',
            'SP':    'Special Paint or Coating',
            'WP':    'Waterproof Coating',
            # ── Ceiling ──────────────────────────────────────────────────────
            'APC':   'Acoustical Ceiling Panel',
            'ACT':   'Acoustical Ceiling Tile',
            'GWB':   'Gypsum Wall Board',
            'FRP':   'Fiberglass Reinforced Panel',
            # ── Wall / Surface ───────────────────────────────────────────────
            'WC':    'Wall Covering',
            'VC':    'Vinyl Wall Covering',
            'MS':    'Metal Surface',
            'MTL':   'Metal Panel',
            # ── Door / Window / Hardware ─────────────────────────────────────
            'D':     'Door',
            'HM':    'Hollow Metal Door or Frame',
            'W':     'Window',
            'GL':    'Glass',
            'FR':    'Fire-Rated Element',
            # ── Millwork / Casework ──────────────────────────────────────────
            'WDM':   'Wood Millwork',
            'LMT':   'Laminate',
            'QZ':    'Quartz Surface',
            'PL':    'Plastic Laminate',
            'SS':    'Solid Surface or Stainless Steel',
            # ── MEP / Equipment ──────────────────────────────────────────────
            'AHU':   'Air Handling Unit',
            'FCU':   'Fan Coil Unit',
            'L':     'Lighting Fixture',
            'DL':    'Downlight',
            'EL':    'Emergency Light',
            'SD':    'Smoke Detector',
            # ── General ──────────────────────────────────────────────────────
            'SST':   'Stainless Steel',
            'TBD':   'To Be Determined',
        }

    @classmethod
    def get_manufacturer_keywords(cls) -> set:
        """
        Returns a set of lowercase AEC manufacturer brand names.

        Used by ExtractorWorker's evidence ranking function to identify chunks
        that contain manufacturer proximity signals — chunks where the target
        tag and a known brand name appear within a small line window are ranked
        higher than prose-only chunks for LLM evidence packaging.
        """
        return {
            'armstrong', 'shaw', 'mohawk', 'interface', 'mannington', 'tarkett',
            'daltile', 'american olean', 'crossville', 'sherwin', 'benjamin moore',
            'ppg', 'behr', 'roppe', 'johnsonite', 'burke', 'usg', 'certainteed',
            'mapei', 'laticrete', 'wilsonart', 'formica', 'georgia-pacific', 'nora',
            'polyflor', 'patcraft', 'milliken', 'tandus', 'wicanders', 'karndean',
            'emser', 'marazzi', 'porcelanosa', 'bedrosians', 'florida tile',
            'connor', 'robbins', 'boen', 'nurazzo', 'wausau', 'shaw floors',
            'pratt lambert', 'dunn-edwards', 'valspar', 'atlas', 'atals',
            'trendwyth', 'rockwood', 'stonhard', 'ecore', 'altro',
        }

    @classmethod
    def get_header_aliases(cls) -> dict:
        """
        Returns the pipe-table column header alias → semantic field mapping.

        Used by ExtractorWorker's dynamic column detection engine to resolve
        column positions from ANY document's header row without relying on
        fixed column indexes — critical for construction schedules where column
        ordering varies by architect, firm, and project type.
        """
        return {
            # KEY / TAG column
            "key":              "key",
            "tag":              "key",
            "code":             "key",
            "id":               "key",
            "finish code":      "key",
            "finish tag":       "key",
            "material code":    "key",
            # MANUFACTURER
            "manufacturer":     "manufacturer",
            "mfr":              "manufacturer",
            "brand":            "manufacturer",
            "supplier":         "manufacturer",
            "vendor":           "manufacturer",
            "maker":            "manufacturer",
            # PRODUCT / MODEL / SERIES
            "product":          "product",
            "series":           "product",
            "model":            "product",
            "line":             "product",
            "style":            "product",
            "pattern":          "product",
            "collection":       "product",
            "item":             "product",
            "name":             "product",
            # COLOR
            "color":            "color",
            "colour":           "color",
            "finish":           "color",
            "shade":            "color",
            "glaze":            "color",
            "hue":              "color",
            # SIZE / DIMENSIONS
            "type/size":        "size",
            "size":             "size",
            "dimension":        "size",
            "dimensions":       "size",
            "type":             "size",
            "thickness":        "size",
            "format":           "size",
            # NOTES / AREA / REMARKS
            "notes":            "notes",
            "note":             "notes",
            "remarks":          "notes",
            "remark":           "notes",
            "comment":          "notes",
            "location":         "notes",
            "area":             "notes",
            "room":             "notes",
            "scope":            "notes",
            "application":      "notes",
            "use":              "notes",
            # CSI SECTION
            "specification section": "csi_section",
            "spec section":     "csi_section",
            "section":          "csi_section",
            "csi":              "csi_section",
            "division":         "csi_section",
            # FINISH TYPE / MATERIAL DESC
            "finish type":      "finish_type",
            "material":         "finish_type",
            "description":      "finish_type",
            "material type":    "finish_type",
        }

    @classmethod
    def get_ocr_confusables(cls) -> tuple:
        """
        Returns a tuple of frozensets representing OCR character confusion groups.

        Characters within the same frozenset are visually indistinguishable
        in low-DPI scans and are treated as equivalent by the tag equality
        comparator — enabling conservative OCR-aware fuzzy matching that
        recovers SP0RT-1 from SPORT-1 without over-matching TER-1 to TERT-1.
        """
        return (
            frozenset(("0", "O")),
            frozenset(("1", "I", "L")),
            frozenset(("5", "S")),
            frozenset(("2", "Z")),
        )

    @classmethod
    def get_empty_markers(cls) -> set:
        """
        Returns the set of string values that represent absent/void fields
        in an AEC finish schedule.

        These sentinel values are used throughout the extraction pipeline to
        distinguish between "we found this field and it says N/S" vs "the LLM
        hallucinated a plausible-sounding placeholder value."
        """
        return {"", "TBD", "N/S", "N/A", "NA", "NONE", "NULL", "NOT USED", "SAME AS", "CUSTOM COLOR"}

    @classmethod
    def get_known_finish_prefixes(cls) -> set:
        """
        Returns the whitelist of known US commercial finish tag prefixes.

        Tags whose prefix appears in this set receive elevated base confidence
        (0.55) during TextEngine mining. Tags with unrecognized prefixes —
        which are more likely to be product SKUs, drawing references, or
        manufacturer color codes — receive low base confidence (0.30) and
        are typically filtered before the LLM verification stage.

        Organized by CSI MasterFormat division.
        """
        return {
            # ── Tile ─────────────────────────────────────────────────────────
            'CT', 'PT', 'RT', 'BT', 'AT', 'ST',
            # ── Resilient / Vinyl Flooring ────────────────────────────────────
            'VT', 'VCT', 'LVT', 'LVP', 'CVT', 'VSF', 'LMT',
            # ── Carpet ───────────────────────────────────────────────────────
            'CPT',
            # ── Terrazzo / Epoxy / Resinous ──────────────────────────────────
            'TERT', 'TER', 'RES', 'EPX',
            # ── Wood / Athletic Flooring ──────────────────────────────────────
            'WD',
            # ── Sports / Rubber Flooring ──────────────────────────────────────
            'SPORT',
            # ── Paint ────────────────────────────────────────────────────────
            'P', 'PNT', 'EP',
            # ── Grout ────────────────────────────────────────────────────────
            'GT',
            # ── Wall / Ceiling Assemblies ─────────────────────────────────────
            'GWB', 'ACT', 'APC', 'ACP', 'FRP', 'WC', 'VC', 'WP', 'CB', 'FC',
            # ── Plastic Laminate / Millwork / Solid Surface / Glazing ─────────
            'PL', 'PLAM', 'MW', 'GL', 'SS', 'SST',
            # ── Base / Nosing / Accessories ───────────────────────────────────
            'RB', 'VRB', 'STN', 'SB', 'NT', 'TB', 'QT', 'T',
            # ── Masonry ──────────────────────────────────────────────────────
            'GCMU', 'CMU',
            # ── Specialty / Misc ──────────────────────────────────────────────
            'HM', 'QZ', 'SC', 'FL', 'WDM',
        }

    @classmethod
    def get_known_non_tag_prefixes(cls) -> set:
        """
        Returns the blacklist of word prefixes that appear as WORD-NUMBER
        combinations in US construction documents but are NEVER finish tags.

        normalize_tag() returns an empty string for any candidate whose
        prefix matches this set, preventing drawing grid labels, room numbers,
        and revision codes from entering the tag candidate pool.
        """
        return {
            "ROOM", "FLOOR", "LEVEL", "STAIR", "PHASE", "STEP",
            "GRID", "DETAIL", "SECTION", "SHEET", "DIV", "DIVISION",
            "AREA", "ZONE", "UNIT", "SUITE", "BLDG", "WING",
            "ELEV", "ELEVATION", "PLAN", "COLUMN", "AXIS", "LINE",
            "MARK", "REF", "TAG", "NOTE", "KEYNOTE", "ITEM",
            "DOOR", "WINDOW", "FRAME", "PANEL",
        }
