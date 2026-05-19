from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum

class RowStatus(str, Enum):
    CONFIRMED = "confirmed"
    VERIFICATION = "verification"
    REFERENCE_ONLY = "reference only"
    EXCLUDED = "excluded"

class SpecDetailRow(BaseModel):
    """
    Representing a single, first-class product specification row for the Strict Spec Ledger.
    NO MERGING ALLOWED. Each row must represent one unique product/material.
    """
    area_scope: str = Field(..., description="The room or area where this material is applied (e.g., Suite C & D)")
    csi_division: str = Field(..., description="The CSI division (e.g., 09 for Finishes)")
    csi_section: Optional[str] = Field("N/S", description="The specific CSI section number (e.g., 09 65 00)")
    finish_tag: str = Field(..., description="The unique finish tag (e.g., PL1, LVT-1, PT-1)")
    manufacturer: str = Field("N/S", description="The brand or manufacturer name (e.g., Wilsonart, Mohawk)")
    model_series: str = Field("N/S", description="The specific product model or series (e.g., Living Local)")
    finish_color: str = Field("N/S", description="The color name or code (e.g., 4880-38 Carbon Mesh)")
    size: Optional[str] = Field("N/S", description="Physical dimensions (e.g., 12x24, 7.75 in. x 52 in.)")
    product_criteria: Optional[str] = Field("N/S", description="Technical specs like wear layer, VOC, thickness")
    installation_criteria: Optional[str] = Field("N/S", description="How it's installed (e.g., Glue down, Ashlar)")
    standards_codes: Optional[str] = Field("N/S", description="References like ASTM, UL, NFPA, NOA")
    finish_type: str = Field("N/S", description="Surface finish sheen or texture if specified")
    special_remarks: str = Field("N/S", description="Any notes, basis-of-design callouts, or remarks")
    source_document: str = Field(..., description="Name of the PDF file source")
    page_reference: str = Field(..., description="Page or Sheet number where evidence was found (e.g., A400)")
    evidence_id: str = Field(..., description="Unique ID linking back to the Pinecone chunk")
    evidence_excerpt: str = Field(..., description="The exact raw text snippet from the document")
    confidence: float = Field(..., ge=0.0, le=1.0, description="AI confidence score")
    status: RowStatus = Field(default=RowStatus.CONFIRMED)
    extraction_method: str = Field("text", description="The extraction method used")

    class Config:
        use_enum_values = True
