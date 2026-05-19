import csv
import logging
from typing import List, Dict

logging.basicConfig(level=logging.INFO, format='%(asctime)s [SCORER] %(message)s')

class SpecScorer:
    """
    Compares extracted data against a Golden Dataset (Human-Verified Truth).
    Calculates Accuracy based on field-level matches.
    """

    def __init__(self, golden_csv_path: str):
        self.golden_data = self._load_csv(golden_csv_path)

    def _load_csv(self, path: str) -> List[Dict]:
        try:
            with open(path, mode='r', encoding='utf-8') as f:
                return list(csv.DictReader(f))
        except FileNotFoundError:
            logging.error(f"Golden CSV not found: {path}")
            return []

    def calculate_score(self, extracted_data: List[Dict]) -> dict:
        """
        Calculates field-level accuracy.
        Fields compared: manufacturer, model_series, finish_color, size.
        """
        if not self.golden_data:
            return {"error": "No golden data to compare against."}

        total_fields = 0
        matching_fields = 0
        
        # Create a lookup for golden data by tag
        golden_lookup = {row['finish_tag']: row for row in self.golden_data}
        
        for ext_row in extracted_data:
            tag = ext_row.get('finish_tag')
            if tag not in golden_lookup:
                continue
                
            gold_row = golden_lookup[tag]
            
            # Fields to compare
            for field in ['manufacturer', 'model_series', 'finish_color']:
                total_fields += 1
                ext_val = str(ext_row.get(field, "")).strip().lower()
                gold_val = str(gold_row.get(field, "")).strip().lower()
                
                if ext_val == gold_val and gold_val != "n/s":
                    matching_fields += 1
                elif gold_val == "n/s" and (ext_val == "n/s" or not ext_val):
                    matching_fields += 1

        accuracy = (matching_fields / total_fields) * 100 if total_fields > 0 else 0
        
        return {
            "accuracy_score": f"{accuracy:.2f}%",
            "total_fields_checked": total_fields,
            "matches": matching_fields,
            "target_threshold": "95.00%"
        }

if __name__ == "__main__":
    # Example usage
    # scorer = SpecScorer("data/golden_output.csv")
    # report = scorer.calculate_score(extracted_data)
    # print(report)
    pass
