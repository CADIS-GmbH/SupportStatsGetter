import json
import os
import sys
import mariadb
import re

def escape_column_name(column_name):
    """Escape column names for MariaDB compatibility."""
    return re.sub(r'[^0-9a-zA-Z_]', '_', column_name)

def infer_type_from_data(data_values):
    """Infer the SQL column type from multiple Python values."""
    has_str, has_float, has_int, has_text = False, False, False, False
    for value in data_values:
        if isinstance(value, int):
            has_int = True
        elif isinstance(value, float):
            has_float = True
        elif isinstance(value, str):
            if len(value) >= 255:
                has_text = True
            else:
                has_str = True
        elif isinstance(value, list):
            return 'JSON'  # Lists are stored as JSON
        elif isinstance(value, dict):
            return None  # Dicts are expanded to columns

    if has_text:
        return 'TEXT'  # Prefer string if any string is present
    if has_str:
        return 'VARCHAR(255)'  # Prefer string if any string is present
    if has_float:
        return 'FLOAT'  # Use float if any value is a float
    if has_int:
        return 'INT'  # Use integer if all values are int or null
    return 'TEXT'  # Fallback for null or unrecognized types

def flatten_json(nested_json, parent_key='', sep='_'):
    """Flatten a nested JSON object."""
    items = []
    for k, v in nested_json.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_json(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)

def collect_column_data(data):
    """Collect column data types from the entire dataset."""
    column_data = {}
    for record in data:
        flat_record = flatten_json(record)
        for col, value in flat_record.items():
            if col not in column_data:
                column_data[col] = []
            column_data[col].append(value)
    return column_data

def infer_column_types(column_data):
    """Infer column types based on the collected data for each column."""
    return {col: infer_type_from_data(values) for col, values in column_data.items() if infer_type_from_data(values) is not None}

def create_table_if_not_exists(cursor, table_name, columns):
    """Create a table if it doesn't exist."""
    columns_definition = ', '.join(f"`{escape_column_name(col)}` {typ}" for col, typ in columns.items())
    create_table_query = f"CREATE TABLE IF NOT EXISTS `{table_name}` ({columns_definition})"
    cursor.execute(create_table_query)

def insert_into_table(cursor, table_name, data):
    """Insert data into the table."""
    columns = ', '.join(f"`{escape_column_name(col)}`" for col in data.keys())
    placeholders = ', '.join(['%s'] * len(data))
    insert_query = f"INSERT INTO `{table_name}` ({columns}) VALUES ({placeholders})"
    cursor.execute(insert_query, list(data.values()))

def process_json_to_db(json_file, table_name=None, db_config=None):
    # Load JSON file
    with open(json_file, 'r') as f:
        data = json.load(f)

    # Use the filename (without .json) as the table name if not provided
    if table_name is None:
        table_name = os.path.splitext(os.path.basename(json_file))[0]

    # Connect to MariaDB
    conn = mariadb.connect(**db_config)
    cursor = conn.cursor()

    # First pass: Collect all data to infer column types
    column_data = collect_column_data(data)
    column_types = infer_column_types(column_data)

    # Create table if it doesn't exist
    create_table_if_not_exists(cursor, table_name, column_types)

    # Second pass: Insert each record into the table
    for idx, record in enumerate(data):
        try:
            flat_record = flatten_json(record)
            insert_into_table(cursor, table_name, flat_record)

        except mariadb.Error as e:
            print(f"Error inserting record at index {idx}: {e}")
            print(f"Property name: {list(flat_record.keys())}, Record causing the error: {flat_record}")
            conn.rollback()
            cursor.close()
            conn.close()
            sys.exit(1)

    conn.commit()
    cursor.close()
    conn.close()

if __name__ == "__main__":
    # Command-line arguments handling
    if len(sys.argv) < 2:
        print("Usage: python script.py <json_file> [table_name]")
        sys.exit(1)

    json_file = sys.argv[1]
    table_name = sys.argv[2] if len(sys.argv) > 2 else None

    # Database configuration (example)
    db_config = {
        'user': 'your_user',
        'password': 'your_password',
        'host': 'localhost',
        'database': 'your_database'
    }

    # Process the JSON file and insert into MariaDB
    process_json_to_db(json_file, table_name, db_config)
