from flask import Flask, render_template, request, redirect, url_for, flash
from datetime import datetime, date
import sqlite3
import os
import csv
import urllib.request
import io
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

app = Flask(__name__)

SECRET_KEY = os.environ.get("SECRET_KEY")

if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY environment variable is not set."
    )

app.config["SECRET_KEY"] = SECRET_KEY

# Database file name
DATABASE = os.environ.get("DATABASE_PATH", "inventory.db")

# Folder where product images will be uploaded
UPLOAD_FOLDER = "static/uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

PRODUCT_CATEGORIES = [
    "Bakery",
    "Beer and Cider",
    "Biscuits",
    "Canned Food",
    "Cereals",
    "Chilled Food",
    "Chocolate",
    "Cleaning Supplies",
    "Coffee and Tea",
    "Confectionery",
    "Crisps",
    "Dairy",
    "Frozen Food",
    "Fruit and Vegetables",
    "Groceries",
    "Health and Beauty",
    "Household",
    "Ice Cream",
    "Meat and Poultry",
    "Minerals",
    "Personal Care",
    "Pet Care",
    "Ready Meals",
    "Sauces and Condiments",
    "Snacks",
    "Spirits",
    "Tobacco",
    "Water",
    "Wine"
]

REQUIRED_CSV_COLUMNS = {
    "name",
    "category",
    "quantity",
    "expiry_date"
}

MAX_CSV_FILE_SIZE = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def create_table():
    """
    Creates the database tables if they do not already exist.
    """
    conn = get_db_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            expiry_date TEXT NOT NULL,
            image_path TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,

            movement_type TEXT NOT NULL
                CHECK (movement_type IN ('IN', 'OUT')),

            quantity INTEGER NOT NULL
                CHECK (quantity > 0),

            note TEXT,
            movement_date TEXT NOT NULL,

            FOREIGN KEY (product_id)
                REFERENCES products (id)
                ON DELETE SET NULL
        )
    """)

    conn.commit()
    conn.close()

def get_product_status(product):
    """
    Checks the product quantity and expiry date,
    then returns a clear stock/expiry status.
    """
    quantity = product["quantity"]
    expiry_date = datetime.strptime(product["expiry_date"], "%Y-%m-%d").date()
    today = date.today()

    days_left = (expiry_date - today).days

    if days_left < 0:
        return "Expired"
    elif days_left <= 7:
        return "Expiring Soon"
    elif quantity <= 5:
        return "Low Stock"
    else:
        return "In Stock"

def clean_import_text(value):
    """
    Removes unnecessary spaces from imported text.
    """
    return " ".join(
        str(value or "").strip().split()
    )


def normalize_import_category(category):
    """
    Matches imported categories to the standard category list.
    Custom categories are still allowed.
    """
    category = clean_import_text(category)

    category_lookup = {
        item.casefold(): item
        for item in PRODUCT_CATEGORIES
    }

    return category_lookup.get(
        category.casefold(),
        category
    )


def validate_import_row(row, row_number):
    """
    Validates one product row from a CSV file.
    """
    errors = []

    name = clean_import_text(row.get("name"))
    category = normalize_import_category(
        row.get("category")
    )
    quantity_text = clean_import_text(
        row.get("quantity")
    )
    expiry_date = clean_import_text(
        row.get("expiry_date")
    )

    if not name:
        errors.append("product name is missing")

    elif len(name) > 100:
        errors.append(
            "product name exceeds 100 characters"
        )

    if not category:
        errors.append("category is missing")

    elif len(category) > 50:
        errors.append(
            "category exceeds 50 characters"
        )

    try:
        quantity = int(quantity_text)

        if quantity < 0:
            errors.append(
                "quantity cannot be negative"
            )

    except ValueError:
        quantity = None

        errors.append(
            "quantity must be a whole number"
        )

    try:
        parsed_expiry_date = datetime.strptime(
            expiry_date,
            "%Y-%m-%d"
        )

        if (
            parsed_expiry_date.strftime("%Y-%m-%d")
            != expiry_date
        ):
            raise ValueError

    except ValueError:
        errors.append(
            "expiry date must use YYYY-MM-DD"
        )

    product_data = {
        "name": name,
        "category": category,
        "quantity": quantity,
        "expiry_date": expiry_date
    }

    return product_data, errors


def import_products_from_reader(
    reader,
    source_name
):
    """
    Imports validated rows from a CSV reader.
    """
    fieldnames = reader.fieldnames

    if not fieldnames:
        return (
            "Import failed: The CSV file is empty.",
            False
        )

    normalized_headers = [
        clean_import_text(header).lower()
        for header in fieldnames
    ]

    non_empty_headers = [
        header
        for header in normalized_headers
        if header
    ]

    if len(non_empty_headers) != len(
        set(non_empty_headers)
    ):
        return (
            "Import failed: The CSV contains "
            "duplicate column names.",
            False
        )

    missing_columns = (
        REQUIRED_CSV_COLUMNS
        - set(normalized_headers)
    )

    if missing_columns:
        missing_text = ", ".join(
            sorted(missing_columns)
        )

        return (
            "Import failed: Missing required "
            f"columns: {missing_text}.",
            False
        )

    reader.fieldnames = normalized_headers

    conn = get_db_connection()

    imported_count = 0
    skipped_count = 0
    invalid_count = 0
    processed_count = 0

    row_error_messages = []
    csv_product_keys = set()

    try:
        for row_number, row in enumerate(
            reader,
            start=2
        ):
            row_has_content = any(
                clean_import_text(value)
                for key, value in row.items()
                if key is not None
            )

            if not row_has_content:
                continue

            processed_count += 1

            if processed_count > MAX_IMPORT_ROWS:
                conn.rollback()

                return (
                    "Import failed: The CSV cannot "
                    f"contain more than "
                    f"{MAX_IMPORT_ROWS} product rows.",
                    False
                )

            product_data, row_errors = (
                validate_import_row(
                    row,
                    row_number
                )
            )

            if row_errors:
                invalid_count += 1

                if len(row_error_messages) < 5:
                    row_error_messages.append(
                        f"Row {row_number}: "
                        + ", ".join(row_errors)
                        + "."
                    )

                continue

            name = product_data["name"]
            category = product_data["category"]
            quantity = product_data["quantity"]
            expiry_date = product_data[
                "expiry_date"
            ]

            duplicate_key = (
                name.casefold(),
                expiry_date
            )

            if duplicate_key in csv_product_keys:
                skipped_count += 1
                continue

            csv_product_keys.add(duplicate_key)

            existing_product = conn.execute("""
                SELECT id
                FROM products
                WHERE name = ? COLLATE NOCASE
                AND expiry_date = ?
            """, (
                name,
                expiry_date
            )).fetchone()

            if existing_product:
                skipped_count += 1
                continue

            cursor = conn.execute("""
                INSERT INTO products (
                    name,
                    category,
                    quantity,
                    expiry_date,
                    image_path
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                name,
                category,
                quantity,
                expiry_date,
                ""
            ))

            product_id = cursor.lastrowid

            if quantity > 0:
                conn.execute("""
                    INSERT INTO stock_movements (
                        product_id,
                        movement_type,
                        quantity,
                        note,
                        movement_date
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    product_id,
                    "IN",
                    quantity,
                    f"Initial stock imported from "
                    f"{source_name}",
                    date.today().strftime(
                        "%Y-%m-%d"
                    )
                ))

            imported_count += 1

        conn.commit()

    except sqlite3.Error:
        conn.rollback()

        return (
            "Import failed: A database error "
            "occurred while importing products.",
            False
        )

    finally:
        conn.close()

    if processed_count == 0:
        return (
            "Import failed: The CSV contains "
            "no product rows.",
            False
        )

    message = (
        "Import complete. "
        f"Imported: {imported_count}, "
        f"skipped duplicates: {skipped_count}, "
        f"invalid rows: {invalid_count}."
    )

    successful = (
        imported_count > 0
        or skipped_count > 0
        )

    if not successful:
        message = (
            "Import failed: No valid products were imported. "
        f"Invalid rows: {invalid_count}."
    )

    if row_error_messages:
        message += " " + " ".join(row_error_messages)

    return message, successful


def import_products_from_csv():
    """
    Imports products from data/products.csv.
    """
    csv_file_path = "data/products.csv"

    if not os.path.exists(csv_file_path):
        return (
            "Import failed: data/products.csv "
            "was not found.",
            False
        )
    try:
        if os.path.getsize(csv_file_path) > MAX_CSV_FILE_SIZE:
            return (
            "Import failed: The CSV file cannot exceed 2 MB.",
            False
        )
    except OSError:
            return (
        "Import failed: The CSV file could not be accessed.",
        False
    )

    try:
        with open(
            csv_file_path,
            "r",
            newline="",
            encoding="utf-8-sig"
        ) as file:
            reader = csv.DictReader(file)

            return import_products_from_reader(
                reader,
                "local CSV"
            )

    except UnicodeDecodeError:
        return (
            "Import failed: The CSV must use "
            "UTF-8 encoding.",
            False
        )

    except csv.Error:
        return (
            "Import failed: The CSV file is "
            "not formatted correctly.",
            False
        )

    except OSError:
        return (
            "Import failed: The CSV file could "
            "not be opened.",
            False
        )


def is_valid_github_csv_url(csv_url):
    """
    Allows only HTTPS raw GitHub CSV links.
    """
    try:
        parsed_url = urlparse(csv_url)

        return (
            parsed_url.scheme == "https"
            and parsed_url.hostname
            == "raw.githubusercontent.com"
            and parsed_url.path.lower().endswith(
                ".csv"
            )
            and not parsed_url.username
            and not parsed_url.password
        )

    except ValueError:
        return False


def import_products_from_github(csv_url):
    """
    Downloads and imports a raw GitHub CSV file.
    """
    csv_url = csv_url.strip()

    if not csv_url:
        return (
            "Import failed: Enter a GitHub CSV URL.",
            False
        )

    if len(csv_url) > 2048:
        return (
            "Import failed: The URL is too long.",
            False
        )

    if not is_valid_github_csv_url(csv_url):
        return (
            "Import failed: Use a valid HTTPS "
            "raw.githubusercontent.com URL "
            "ending in .csv.",
            False
        )

    try:
        github_request = urllib.request.Request(
            csv_url,
            headers={
                "User-Agent": "StockTrack/1.0"
            }
        )

        with urllib.request.urlopen(
            github_request,
            timeout=10
        ) as response:
            final_url = response.geturl()

            if not is_valid_github_csv_url(
                final_url
            ):
                return (
                    "Import failed: GitHub redirected "
                    "to an unsupported URL.",
                    False
                )

            csv_content = response.read(
                MAX_CSV_FILE_SIZE + 1
            )

        if len(csv_content) > MAX_CSV_FILE_SIZE:
            return (
                "Import failed: The CSV file cannot "
                "exceed 2 MB.",
                False
            )

        csv_text = csv_content.decode(
            "utf-8-sig"
        )

        reader = csv.DictReader(
            io.StringIO(
                csv_text,
                newline=""
            )
        )

        return import_products_from_reader(
            reader,
            "GitHub CSV"
        )

    except HTTPError as error:
        return (
            "Import failed: GitHub returned "
            f"HTTP status {error.code}.",
            False
        )

    except URLError:
        return (
            "Import failed: The GitHub file "
            "could not be reached.",
            False
        )

    except TimeoutError:
        return (
            "Import failed: The GitHub request "
            "timed out.",
            False
        )

    except UnicodeDecodeError:
        return (
            "Import failed: The CSV must use "
            "UTF-8 encoding.",
            False
        )

    except csv.Error:
        return (
            "Import failed: The downloaded file "
            "is not a valid CSV.",
            False
        )

    except Exception:
        return (
            "Import failed: An unexpected error "
            "occurred.",
            False
        )

@app.route("/")
def home():
    return redirect(url_for("dashboard"))

@app.route("/add", methods=["GET", "POST"])
def add_product():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        quantity_text = request.form.get("quantity", "").strip()
        expiry_date = request.form.get("expiry_date", "").strip()

        form_data = {
            "name": name,
            "category": category,
            "quantity": quantity_text,
            "expiry_date": expiry_date
        }

        errors = []

        # Validate product name
        if not name:
            errors.append("Product name is required.")
        elif len(name) > 100:
            errors.append("Product name cannot exceed 100 characters.")

        # Validate category
        if not category:
            errors.append("Category is required.")
        elif len(category) > 50:
            errors.append("Category cannot exceed 50 characters.")

        # Validate quantity
        try:
            quantity = int(quantity_text)

            if quantity < 0:
                errors.append("Quantity cannot be negative.")

        except ValueError:
            quantity = None
            errors.append("Quantity must be a whole number.")

        # Validate expiry date
        try:
            parsed_expiry_date = datetime.strptime(
            expiry_date,
            "%Y-%m-%d"
            )

            if (
                parsed_expiry_date.strftime("%Y-%m-%d")
                != expiry_date
            ):
                raise ValueError

        except ValueError:
            errors.append(
        "Expiry date must use YYYY-MM-DD."
        )

        if errors:
            for error in errors:
                flash(error, "error")

            return render_template(
                "add_product.html",
                form_data=form_data
            )

        conn = get_db_connection()

        try:
            existing_product = conn.execute("""
                SELECT id
                FROM products
                WHERE LOWER(name) = LOWER(?)
                AND expiry_date = ?
            """, (name, expiry_date)).fetchone()

            if existing_product:
                flash(
                    "A product with this name and expiry date already exists.",
                    "error"
                )

                return render_template(
                    "add_product.html",
                    form_data=form_data
                )

            cursor = conn.execute("""
                INSERT INTO products (
                    name,
                    category,
                    quantity,
                    expiry_date,
                    image_path
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                name,
                category,
                quantity,
                expiry_date,
                ""
            ))

            product_id = cursor.lastrowid

            # Record the initial quantity in stock history
            if quantity > 0:
                conn.execute("""
                    INSERT INTO stock_movements (
                        product_id,
                        movement_type,
                        quantity,
                        note,
                        movement_date
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    product_id,
                    "IN",
                    quantity,
                    "Initial stock",
                    date.today().strftime("%Y-%m-%d")
                ))

            conn.commit()

        except sqlite3.Error:
            conn.rollback()
            flash(
                "The product could not be added due to a database error.",
                "error"
            )

            return render_template(
                "add_product.html",
                form_data=form_data
            ), 500

        finally:
            conn.close()

        flash("Product added successfully.", "success")
        return redirect(url_for("view_products"))

    return render_template(
        "add_product.html",
        form_data={}
    )

@app.route("/products")
def view_products():
    conn = get_db_connection()

    product_rows = conn.execute("""
        SELECT *
        FROM products
    """).fetchall()

    conn.close()

    all_products = []

    total_quantity = 0
    low_stock_count = 0
    expired_count = 0
    expiring_soon_count = 0

    category_map = {
        category.casefold(): category
        for category in PRODUCT_CATEGORIES
    }

    for product_row in product_rows:
        product = dict(product_row)

        product["status"] = get_product_status(product_row)

        all_products.append(product)

        category = product["category"].strip()

        if category:
            formatted_category = category.title()

            category_map.setdefault(
                formatted_category.casefold(),
                formatted_category
            )

        total_quantity += int(product["quantity"] or 0)

        if product["status"] == "Low Stock":
            low_stock_count += 1

        elif product["status"] == "Expired":
            expired_count += 1

        elif product["status"] == "Expiring Soon":
            expiring_soon_count += 1

    categories = sorted(
        category_map.values(),
        key=str.casefold
    )

    search_term = request.args.get("q", "").strip()
    category_filter = request.args.get("category", "").strip()
    status_filter = request.args.get("status", "").strip()
    sort_option = request.args.get(
        "sort",
        "expiry_soonest"
    ).strip()

    products = all_products.copy()

    if search_term:
        search_lower = search_term.casefold()

        products = [
            product
            for product in products
            if search_lower in product["name"].casefold()
            or search_lower in product["category"].casefold()
        ]

    if category_filter:
        selected_category = category_filter.casefold()

        products = [
            product
            for product in products
            if product["category"].strip().casefold()
            == selected_category
        ]

    if status_filter:
        products = [
            product
            for product in products
            if product["status"] == status_filter
        ]

    if sort_option == "name_az":
        products.sort(
            key=lambda product: product["name"].casefold()
        )

    elif sort_option == "name_za":
        products.sort(
            key=lambda product: product["name"].casefold(),
            reverse=True
        )

    elif sort_option == "quantity_low":
        products.sort(
            key=lambda product: int(
                product["quantity"] or 0
            )
        )

    elif sort_option == "quantity_high":
        products.sort(
            key=lambda product: int(
                product["quantity"] or 0
            ),
            reverse=True
        )

    elif sort_option == "newest":
        products.sort(
            key=lambda product: int(product["id"]),
            reverse=True
        )

    else:
        products.sort(
            key=lambda product:
                product["expiry_date"] or "9999-12-31"
        )

    return render_template(
        "products.html",
        products=products,
        categories=categories,
        total_products=len(all_products),
        total_quantity=total_quantity,
        low_stock_count=low_stock_count,
        expired_count=expired_count,
        expiring_soon_count=expiring_soon_count,
        search_term=search_term,
        category_filter=category_filter,
        status_filter=status_filter,
        sort_option=sort_option
    )

@app.route("/import")
def import_products():
    message, successful = (
        import_products_from_csv()
    )

    flash(
        message,
        "success" if successful else "error"
    )

    return redirect(
        url_for("view_products")
    )


@app.route(
    "/import-github",
    methods=["GET", "POST"]
)
def import_github():
    message = ""

    csv_url = request.args.get(
        "csv_url",
        ""
    ).strip()

    if request.method == "POST":
        csv_url = request.form.get(
            "csv_url",
            ""
        ).strip()

        message, successful = (
            import_products_from_github(
                csv_url
            )
        )

    return render_template(
        "import_github.html",
        message=message,
        csv_url=csv_url
    )

@app.route("/scan-qr")
def scan_qr():
    return render_template("scan_qr.html")

@app.route("/edit/<int:product_id>", methods=["GET", "POST"])
def edit_product(product_id):
    conn = get_db_connection()

    product = conn.execute("""
        SELECT *
        FROM products
        WHERE id = ?
    """, (product_id,)).fetchone()

    if product is None:
        conn.close()
        return "Product not found.", 404

    if request.method == "POST":
        name = " ".join(
            request.form.get("name", "").strip().split()
        )

        category = " ".join(
            request.form.get("category", "").strip().split()
        )

        expiry_date = request.form.get(
            "expiry_date",
            ""
        ).strip()

        product_data = dict(product)
        product_data["name"] = name
        product_data["category"] = category
        product_data["expiry_date"] = expiry_date

        errors = []

        if not name:
            errors.append("Product name is required.")
        elif len(name) > 100:
            errors.append(
                "Product name cannot exceed 100 characters."
            )

        if not category:
            errors.append("Category is required.")
        elif len(category) > 50:
            errors.append(
                "Category cannot exceed 50 characters."
            )

        try:
            parsed_expiry_date = datetime.strptime(
            expiry_date,
            "%Y-%m-%d"
        )

            if (
                parsed_expiry_date.strftime("%Y-%m-%d")
                != expiry_date
        ):
                raise ValueError

        except ValueError:
            errors.append(
        "Expiry date must use YYYY-MM-DD."
    )

        category_lookup = {
            item.casefold(): item
            for item in PRODUCT_CATEGORIES
        }

        if category:
            category = category_lookup.get(
                category.casefold(),
                category
            )

            product_data["category"] = category

        if not errors:
            duplicate_product = conn.execute("""
                SELECT id
                FROM products
                WHERE name = ? COLLATE NOCASE
                AND expiry_date = ?
                AND id != ?
            """, (
                name,
                expiry_date,
                product_id
            )).fetchone()

            if duplicate_product:
                errors.append(
                    "Another product with this name and "
                    "expiry date already exists."
                )

        if errors:
            conn.close()

            for error in errors:
                flash(error, "error")

            return render_template(
                "edit_product.html",
                product=product_data,
                categories=PRODUCT_CATEGORIES
            )

        try:
            conn.execute("""
                UPDATE products
                SET name = ?,
                    category = ?,
                    expiry_date = ?
                WHERE id = ?
            """, (
                name,
                category,
                expiry_date,
                product_id
            ))

            conn.commit()

        except sqlite3.Error:
            conn.rollback()

            flash(
                "The product could not be updated due "
                "to a database error.",
                "error"
            )

            return render_template(
                "edit_product.html",
                product=product_data,
                categories=PRODUCT_CATEGORIES
            ), 500

        finally:
            conn.close()

        flash(
            "Product updated successfully.",
            "success"
        )

        return redirect(
            url_for("view_products")
        )

    product_data = dict(product)
    conn.close()

    return render_template(
        "edit_product.html",
        product=product_data,
        categories=PRODUCT_CATEGORIES
    )

@app.route("/delete/<int:product_id>", methods=["POST"])
def delete_product(product_id):
    conn = get_db_connection()

    product = conn.execute("""
        SELECT id
        FROM products
        WHERE id = ?
    """, (product_id,)).fetchone()

    if product is None:
        conn.close()
        return "Product not found.", 404

    conn.execute("""
        DELETE FROM products
        WHERE id = ?
    """, (product_id,))

    conn.commit()
    conn.close()

    return redirect(url_for("view_products"))

@app.route("/dashboard")
def dashboard():
    conn = get_db_connection()

    products = conn.execute("""
        SELECT *
        FROM products
        ORDER BY id DESC
    """).fetchall()

    recent_movements = conn.execute("""
        SELECT
            stock_movements.id,
            stock_movements.movement_type,
        stock_movements.quantity,
        stock_movements.note,
        stock_movements.movement_date,
        COALESCE(
            products.name,
            'Deleted Product'
        ) AS product_name
    FROM stock_movements
    LEFT JOIN products
        ON stock_movements.product_id = products.id
    ORDER BY
        stock_movements.movement_date DESC,
        stock_movements.id DESC
    LIMIT 6
    """).fetchall()

    conn.close()

    total_products = len(products)
    total_quantity = 0
    low_stock_count = 0
    expired_count = 0
    expiring_soon_count = 0
    in_stock_count = 0

    dashboard_products = []

    for product_row in products:
        status = get_product_status(product_row)

        product = dict(product_row)
        product["status"] = status

        dashboard_products.append(product)

        total_quantity += int(product_row["quantity"] or 0)

        if status == "Low Stock":
            low_stock_count += 1
        elif status == "Expired":
            expired_count += 1
        elif status == "Expiring Soon":
            expiring_soon_count += 1
        elif status == "In Stock":
            in_stock_count += 1

    recent_products = dashboard_products[:6]

    return render_template(
        "dashboard.html",
        total_products=total_products,
        total_quantity=total_quantity,
        low_stock_count=low_stock_count,
        expired_count=expired_count,
        expiring_soon_count=expiring_soon_count,
        in_stock_count=in_stock_count,
        recent_products=recent_products,
        recent_movements=recent_movements
    )

@app.route("/filter/<status_type>")
def filter_products(status_type):
    conn = get_db_connection()

    products = conn.execute("""
        SELECT * FROM products
        ORDER BY expiry_date ASC
    """).fetchall()

    conn.close()

    filtered_products = []

    for product in products:
        product_data = dict(product)
        product_status = get_product_status(product)
        product_data["status"] = product_status

        if status_type == "expired" and product_status == "Expired":
            filtered_products.append(product_data)

        elif status_type == "expiring-soon" and product_status == "Expiring Soon":
            filtered_products.append(product_data)

        elif status_type == "low-stock" and product_status == "Low Stock":
            filtered_products.append(product_data)

    return render_template(
        "filtered_products.html",
        products=filtered_products,
        status_type=status_type
    )

@app.route(
    "/stock-update/<int:product_id>",
    methods=["GET", "POST"]
)
def stock_update(product_id):
    conn = get_db_connection()

    product_row = conn.execute("""
        SELECT *
        FROM products
        WHERE id = ?
    """, (product_id,)).fetchone()

    if product_row is None:
        conn.close()
        return "Product not found.", 404

    product = dict(product_row)

    form_data = {
        "movement_type": "IN",
        "quantity": "",
        "note": ""
    }

    if request.method == "POST":
        movement_type = request.form.get(
            "movement_type",
            ""
        ).strip().upper()

        quantity_text = request.form.get(
            "quantity",
            ""
        ).strip()

        note = request.form.get(
            "note",
            ""
        ).strip()

        form_data = {
            "movement_type": movement_type,
            "quantity": quantity_text,
            "note": note
        }

        errors = []

        if movement_type not in {"IN", "OUT"}:
            errors.append(
                "Select a valid stock movement type."
            )

        try:
            quantity = int(quantity_text)

            if quantity <= 0:
                errors.append(
                    "Movement quantity must be greater than zero."
                )

        except ValueError:
            quantity = None

            errors.append(
                "Movement quantity must be a whole number."
            )

        if len(note) > 250:
            errors.append(
                "The stock movement note cannot exceed 250 characters."
            )

        current_quantity = int(product["quantity"])

        if (
            not errors
            and movement_type == "OUT"
            and quantity > current_quantity
        ):
            errors.append(
                "Stock Out quantity cannot exceed the available stock."
            )

        if errors:
            conn.close()

            for error in errors:
                flash(error, "error")

            return render_template(
                "stock_update.html",
                product=product,
                form_data=form_data
            )

        if movement_type == "IN":
            new_quantity = current_quantity + quantity
        else:
            new_quantity = current_quantity - quantity

        movement_date = date.today().strftime(
            "%Y-%m-%d"
        )

        try:
            conn.execute("""
                UPDATE products
                SET quantity = ?
                WHERE id = ?
            """, (
                new_quantity,
                product_id
            ))

            conn.execute("""
                INSERT INTO stock_movements (
                    product_id,
                    movement_type,
                    quantity,
                    note,
                    movement_date
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                product_id,
                movement_type,
                quantity,
                note or None,
                movement_date
            ))

            conn.commit()

        except sqlite3.Error:
            conn.rollback()

            flash(
                "The stock update could not be saved "
                "due to a database error.",
                "error"
            )

            return render_template(
                "stock_update.html",
                product=product,
                form_data=form_data
            ), 500

        finally:
            conn.close()

        flash(
            f"Stock updated successfully. "
            f"New quantity: {new_quantity} units.",
            "success"
        )

        return redirect(
            url_for("view_products")
        )

    conn.close()

    return render_template(
        "stock_update.html",
        product=product,
        form_data=form_data
    )

@app.route("/stock-history")
def stock_history():
    search_term = request.args.get("q", "").strip()
    movement_filter = request.args.get("type", "").strip().upper()

    conn = get_db_connection()

    query = """
        SELECT
            stock_movements.id,
            stock_movements.product_id,
            stock_movements.movement_type,
            stock_movements.quantity,
            stock_movements.note,
            stock_movements.movement_date,
            COALESCE(products.name, 'Deleted Product') AS product_name
        FROM stock_movements
        LEFT JOIN products
            ON stock_movements.product_id = products.id
    """

    conditions = []
    parameters = []

    if search_term:
        search_value = f"%{search_term.lower()}%"

        conditions.append("""
            (
                LOWER(COALESCE(products.name, '')) LIKE ?
                OR LOWER(COALESCE(stock_movements.note, '')) LIKE ?
            )
        """)

        parameters.extend([
            search_value,
            search_value
        ])

    if movement_filter in ["IN", "OUT"]:
        conditions.append("""
            stock_movements.movement_type = ?
        """)

        parameters.append(movement_filter)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += """
        ORDER BY
            DATE(stock_movements.movement_date) DESC,
            stock_movements.id DESC
    """

    movements = conn.execute(
        query,
        parameters
    ).fetchall()

    summary = conn.execute("""
        SELECT
            COUNT(*) AS total_movements,

            COALESCE(
                SUM(
                    CASE
                        WHEN movement_type = 'IN' THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS stock_in_count,

            COALESCE(
                SUM(
                    CASE
                        WHEN movement_type = 'OUT' THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS stock_out_count,

            COALESCE(
                SUM(
                    CASE
                        WHEN movement_type = 'IN' THEN quantity
                        ELSE 0
                    END
                ),
                0
            ) AS total_units_in,

            COALESCE(
                SUM(
                    CASE
                        WHEN movement_type = 'OUT' THEN quantity
                        ELSE 0
                    END
                ),
                0
            ) AS total_units_out

        FROM stock_movements
    """).fetchone()

    conn.close()

    net_stock_movement = (
        summary["total_units_in"]
        - summary["total_units_out"]
    )

    return render_template(
        "stock_history.html",
        movements=movements,
        search_term=search_term,
        movement_filter=movement_filter,
        total_movements=summary["total_movements"],
        stock_in_count=summary["stock_in_count"],
        stock_out_count=summary["stock_out_count"],
        total_units_in=summary["total_units_in"],
        total_units_out=summary["total_units_out"],
        net_stock_movement=net_stock_movement
    )

# Initialize database tables when the application starts
create_table()


if __name__ == "__main__":
    app.run(debug=False)