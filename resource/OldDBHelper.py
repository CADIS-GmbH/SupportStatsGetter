import json
from datetime import datetime, timezone
import argparse

# Constants
DATE_CUTOFF = datetime(2025, 7, 12, tzinfo=timezone.utc)

def parse_json(input_path):
    with open(input_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def filter_records(data, cutoff_date):
    filtered = []
    for record in data:
        date_str = record.get("DateModified")
        if date_str:
            try:
                date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if date_obj > cutoff_date:
                    filtered.append(record)
            except ValueError:
                # Malformed date, ignore or log as needed
                continue
    return filtered

def save_json(data, output_path):
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def main():
    parser = argparse.ArgumentParser(description="Filter JSON records modified after 2025-07-01.")
    parser.add_argument("input_file", help="Path to input JSON file")
    parser.add_argument("output_file", help="Path to output JSON file")

    args = parser.parse_args()

    print("Loading input...")
    records = parse_json(args.input_file)

    print(f"Filtering {len(records)} records...")
    filtered = filter_records(records, DATE_CUTOFF)

    print(f"Saving {len(filtered)} matching records to {args.output_file}")
    save_json(filtered, args.output_file)

if __name__ == "__main__":
    main()
