import os
import csv
import bcrypt
import mysql.connector
from dotenv import load_dotenv

# 1. Load configuration from your specific path
env_path = "/home/ubuntu/web/webapp/.env"
load_dotenv(dotenv_path=env_path)

# Accessing your .env keys
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DBs_NAME")  # As per your .env: secureapp
SECRET_KEY = os.getenv("APP_SECRET_KEY")

def get_db_connection():
    try:
        return mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
    except mysql.connector.Error as err:
        print(f"Error: Could not connect to database {DB_NAME}. {err}")
        return None

def hash_password(password):
    """Securely hashes password using bcrypt."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def process_value(val):
    """If user types NULL or string is empty, return Python None (SQL NULL)."""
    if val is None or str(val).strip().upper() == "NULL" or str(val).strip() == "":
        return None
    return val

def insert_user(cursor, username, password, join_year, branch, name, is_admin):
    # Updated SQL to include 'name'
    sql = """INSERT INTO users (username, password, join_year, branch, name, is_admin) 
             VALUES (%s, %s, %s, %s, %s, %s)"""
    
    # Apply NULL logic to optional fields
    join_year = process_value(join_year)
    branch = process_value(branch)
    name = process_value(name)
    
    # Encrypt the password
    hashed_pwd = hash_password(password)
    
    try:
        cursor.execute(sql, (username, hashed_pwd, join_year, branch, name, is_admin))
    except mysql.connector.Error as err:
        print(f"Error inserting user '{username}': {err}")

def manual_insert():
    print("\n--- Manual User Entry ---")
    username = input("Enter Username: ")
    password = input("Enter Password: ")
    name = input("Enter Full Name (type NULL for empty): ")
    join_year = input("Enter Join Year (type NULL for empty): ")
    branch = input("Enter Branch (type NULL for empty): ")
    is_admin_input = input("Is Admin? (y/n): ").lower()
    is_admin = 1 if is_admin_input == 'y' else 0

    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        insert_user(cursor, username, password, join_year, branch, name, is_admin)
        conn.commit()
        print(f"Successfully added: {username}")
        cursor.close()
        conn.close()

def mass_insert_csv():
    print("\n--- CSV Mass Upload ---")
    file_path = input("Enter the full path to your CSV file: ")
    if not os.path.exists(file_path):
        print("Error: File not found.")
        return

    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        try:
            with open(file_path, mode='r', encoding='utf-8') as f:
                # Expected CSV Headers: username,password,name,join_year,branch,is_admin
                reader = csv.DictReader(f)
                count = 0
                for row in reader:
                    admin_flag = 1 if str(row.get('is_admin', '0')).lower() in ['1', 'true', 'y'] else 0
                    
                    insert_user(
                        cursor, 
                        row['username'], 
                        row['password'], 
                        row.get('join_year'), 
                        row.get('branch'), 
                        row.get('name'), 
                        admin_flag
                    )
                    count += 1
            conn.commit()
            print(f"Finished. {count} records processed.")
        except Exception as e:
            print(f"CSV Reading Error: {e}")
        finally:
            cursor.close()
            conn.close()

def main():
    if SECRET_KEY:
        print(f"System: App Secret Key detected.")
    
    print("\n--- SecureApp User Manager ---")
    print("1. Manual Insertion")
    print("2. Mass Insertion via CSV")
    choice = input("Select an option (1 or 2): ")

    if choice == '1':
        manual_insert()
    elif choice == '2':
        mass_insert_csv()
    else:
        print("Invalid selection.")

if __name__ == "__main__":
    main()