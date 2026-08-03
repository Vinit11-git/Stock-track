from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4

import app as stocktrack

stocktrack.app.config["TESTING"] = True


class TestFailure(AssertionError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise TestFailure(message)


def unique(prefix: str) -> str:
    return f"{prefix} {uuid4().hex[:8]}"


def connection():
    return stocktrack.get_db_connection()


def scalar(query: str, parameters: tuple = ()):
    conn = connection()
    try:
        return conn.execute(query, parameters).fetchone()[0]
    finally:
        conn.close()


def one(query: str, parameters: tuple = ()):
    conn = connection()
    try:
        return conn.execute(query, parameters).fetchone()
    finally:
        conn.close()


def all_rows(query: str, parameters: tuple = ()):
    conn = connection()
    try:
        return conn.execute(query, parameters).fetchall()
    finally:
        conn.close()


def cleanup_product(product_id: int | None) -> None:
    if product_id is None:
        return

    conn = connection()
    try:
        conn.execute(
            "DELETE FROM stock_movements WHERE product_id = ?",
            (product_id,),
        )
        conn.execute(
            "DELETE FROM products WHERE id = ?",
            (product_id,),
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_movements(movement_ids: list[int]) -> None:
    if not movement_ids:
        return

    placeholders = ",".join("?" for _ in movement_ids)
    conn = connection()

    try:
        conn.execute(
            f"DELETE FROM stock_movements WHERE id IN ({placeholders})",
            tuple(movement_ids),
        )
        conn.commit()
    finally:
        conn.close()


def create_product(client, name: str, quantity: int = 5) -> int:
    response = client.post(
        "/add",
        data={
            "name": name,
            "category": "Dairy",
            "quantity": str(quantity),
            "expiry_date": "2032-12-31",
        },
        follow_redirects=False,
    )

    require(response.status_code == 302, "Valid product creation failed.")

    product = one(
        "SELECT id FROM products WHERE name = ?",
        (name,),
    )

    require(product is not None, "Created product was not found.")
    return product["id"]


def test_invalid_stock_updates() -> None:
    name = unique("Invalid Stock Test")
    product_id = None

    try:
        with stocktrack.app.test_client() as client:
            product_id = create_product(client, name, 5)

            movements_before = scalar(
                "SELECT COUNT(*) FROM stock_movements WHERE product_id = ?",
                (product_id,),
            )

            responses = [
                client.post(
                    f"/stock-update/{product_id}",
                    data={"movement_type": "OUT", "quantity": "6", "note": ""},
                ),
                client.post(
                    f"/stock-update/{product_id}",
                    data={"movement_type": "IN", "quantity": "0", "note": ""},
                ),
                client.post(
                    f"/stock-update/{product_id}",
                    data={"movement_type": "IN", "quantity": "-2", "note": ""},
                ),
                client.post(
                    f"/stock-update/{product_id}",
                    data={"movement_type": "INVALID", "quantity": "1", "note": ""},
                ),
            ]

        quantity_after = scalar(
            "SELECT quantity FROM products WHERE id = ?",
            (product_id,),
        )

        movements_after = scalar(
            "SELECT COUNT(*) FROM stock_movements WHERE product_id = ?",
            (product_id,),
        )

        require(all(r.status_code == 200 for r in responses), "An invalid request was accepted.")
        require(quantity_after == 5, "Invalid stock update changed quantity.")
        require(movements_before == movements_after, "Invalid movement was recorded.")

    finally:
        cleanup_product(product_id)


def test_delete_preserves_history_and_dashboard() -> None:
    name = unique("Delete History Test")
    product_id = None
    movement_ids: list[int] = []

    try:
        with stocktrack.app.test_client() as client:
            product_id = create_product(client, name, 6)

            client.post(
                f"/stock-update/{product_id}",
                data={
                    "movement_type": "OUT",
                    "quantity": "1",
                    "note": "Delete history test",
                },
                follow_redirects=False,
            )

            movement_ids = [
                row["id"]
                for row in all_rows(
                    "SELECT id FROM stock_movements WHERE product_id = ?",
                    (product_id,),
                )
            ]

            delete_response = client.post(
                f"/delete/{product_id}",
                follow_redirects=False,
            )

            dashboard_response = client.get("/dashboard")

        placeholders = ",".join("?" for _ in movement_ids)
        remaining = all_rows(
            f"SELECT product_id FROM stock_movements WHERE id IN ({placeholders})",
            tuple(movement_ids),
        )

        require(delete_response.status_code == 302, "Delete did not redirect.")
        require(scalar("SELECT COUNT(*) FROM products WHERE id = ?", (product_id,)) == 0,
                "Product was not deleted.")
        require(len(remaining) == len(movement_ids), "Stock history was deleted.")
        require(all(row["product_id"] is None for row in remaining),
                "Deleted history product_id was not set to NULL.")
        require(dashboard_response.status_code == 200, "Dashboard failed after deletion.")
        require(b"Deleted Product" in dashboard_response.data,
                "Dashboard does not show Deleted Product.")

        product_id = None

    finally:
        cleanup_product(product_id)
        cleanup_movements(movement_ids)


def test_stock_history_search_and_filters() -> None:
    name = unique("History Search Test")
    product_id = None

    try:
        with stocktrack.app.test_client() as client:
            product_id = create_product(client, name, 5)

            client.post(
                f"/stock-update/{product_id}",
                data={
                    "movement_type": "OUT",
                    "quantity": "2",
                    "note": "History filter note",
                },
                follow_redirects=False,
            )

            search_response = client.get(
                "/stock-history",
                query_string={"q": name},
            )
            in_response = client.get(
                "/stock-history",
                query_string={"type": "IN"},
            )
            out_response = client.get(
                "/stock-history",
                query_string={"type": "OUT"},
            )

        encoded_name = name.encode("utf-8")

        require(search_response.status_code == 200, "History search failed.")
        require(in_response.status_code == 200, "IN history filter failed.")
        require(out_response.status_code == 200, "OUT history filter failed.")
        require(encoded_name in search_response.data, "Search result is missing the product.")
        require(encoded_name in in_response.data, "IN filter is missing the product.")
        require(encoded_name in out_response.data, "OUT filter is missing the product.")

    finally:
        cleanup_product(product_id)


def test_filter_routes() -> None:
    with stocktrack.app.test_client() as client:
        for route in (
            "/filter/expired",
            "/filter/expiring-soon",
            "/filter/low-stock",
        ):
            response = client.get(route)
            require(response.status_code == 200, f"{route} returned {response.status_code}.")


def test_local_csv_import() -> None:
    csv_path = Path("data/products.csv")
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    original_exists = csv_path.exists()
    original_content = csv_path.read_bytes() if original_exists else None

    valid_name = unique("Local CSV Test")
    invalid_name = unique("Invalid Local CSV Test")
    product_id = None

    try:
        with csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=["name", "category", "quantity", "expiry_date"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "name": valid_name,
                    "category": "dairy",
                    "quantity": "9",
                    "expiry_date": "2035-03-20",
                }
            )
            writer.writerow(
                {
                    "name": invalid_name,
                    "category": "Dairy",
                    "quantity": "-1",
                    "expiry_date": "2035-03-20",
                }
            )

        with stocktrack.app.test_client() as client:
            first = client.get("/import", follow_redirects=False)
            second = client.get("/import", follow_redirects=False)

        products = all_rows(
            "SELECT id, category, quantity FROM products WHERE name = ? COLLATE NOCASE",
            (valid_name,),
        )

        require(products, "Valid local CSV row was not imported.")
        product_id = products[0]["id"]

        movement = one(
            """
            SELECT movement_type, quantity, note
            FROM stock_movements
            WHERE product_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (product_id,),
        )

        require(first.status_code == 302, "First local CSV import failed.")
        require(second.status_code == 302, "Second local CSV import failed.")
        require(len(products) == 1, "Duplicate local CSV product was inserted.")
        require(products[0]["category"] == "Dairy", "Local category was not normalized.")
        require(products[0]["quantity"] == 9, "Local quantity is incorrect.")
        require(scalar("SELECT COUNT(*) FROM products WHERE name = ?", (invalid_name,)) == 0,
                "Invalid local CSV row was imported.")
        require(
            movement is not None
            and movement["movement_type"] == "IN"
            and movement["quantity"] == 9
            and "local CSV" in (movement["note"] or ""),
            "Local opening stock history is incorrect.",
        )

    finally:
        cleanup_product(product_id)

        if original_exists:
            csv_path.write_bytes(original_content)
        elif csv_path.exists():
            csv_path.unlink()


class FakeResponse:
    def __init__(self, content: bytes, final_url: str):
        self.content = content
        self.final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def geturl(self) -> str:
        return self.final_url

    def read(self, size: int = -1) -> bytes:
        return self.content if size < 0 else self.content[:size]


def test_github_import_and_duplicate_protection() -> None:
    name = unique("GitHub CSV Test")
    product_id = None
    github_url = (
        "https://raw.githubusercontent.com/"
        "example/stocktrack/main/products.csv"
    )

    content = (
        "name,category,quantity,expiry_date\n"
        f"{name},minerals,11,2035-04-25\n"
    ).encode("utf-8")

    try:
        with stocktrack.app.test_client() as client:
            with patch.object(
                stocktrack.urllib.request,
                "urlopen",
                return_value=FakeResponse(content, github_url),
            ):
                first = client.post(
                    "/import-github",
                    data={"csv_url": github_url},
                )
                second = client.post(
                    "/import-github",
                    data={"csv_url": github_url},
                )

            invalid_url = client.post(
                "/import-github",
                data={"csv_url": "https://example.com/products.csv"},
            )

        products = all_rows(
            "SELECT id, category, quantity FROM products WHERE name = ? COLLATE NOCASE",
            (name,),
        )

        require(products, "Valid GitHub CSV row was not imported.")
        product_id = products[0]["id"]

        movement = one(
            """
            SELECT movement_type, quantity, note
            FROM stock_movements
            WHERE product_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (product_id,),
        )

        require(first.status_code == 200, "First GitHub import failed.")
        require(second.status_code == 200, "Second GitHub import failed.")
        require(len(products) == 1, "Duplicate GitHub product was inserted.")
        require(products[0]["category"] == "Minerals", "GitHub category was not normalized.")
        require(products[0]["quantity"] == 11, "GitHub quantity is incorrect.")
        require(
            movement is not None
            and movement["movement_type"] == "IN"
            and movement["quantity"] == 11
            and "GitHub CSV" in (movement["note"] or ""),
            "GitHub opening stock history is incorrect.",
        )
        require(invalid_url.status_code == 200, "Invalid GitHub URL form failed.")
        require(b"raw.githubusercontent.com" in invalid_url.data,
                "Invalid GitHub URL message was not displayed.")

    finally:
        cleanup_product(product_id)


def test_pages_missing_ids_and_final_route_scan() -> None:
    missing_id = scalar("SELECT COALESCE(MAX(id), 0) + 999999 FROM products")

    expected_200 = [
        "/dashboard",
        "/add",
        "/products",
        "/import-github",
        "/scan-qr",
        "/stock-history",
        "/products?q=test",
        "/products?category=Dairy",
        "/products?status=In%20Stock",
        "/products?sort=name_az",
        "/products?sort=quantity_high",
        "/products?sort=newest",
    ]

    with stocktrack.app.test_client() as client:
        home = client.get("/", follow_redirects=False)
        require(home.status_code == 302, "/ did not return 302.")
        require(urlparse(home.headers.get("Location", "")).path == "/dashboard",
                "/ did not redirect to /dashboard.")

        for route in expected_200:
            response = client.get(route)
            require(response.status_code == 200, f"{route} returned {response.status_code}.")

        require(client.get(f"/edit/{missing_id}").status_code == 404,
                "Missing Edit ID did not return 404.")
        require(client.get(f"/stock-update/{missing_id}").status_code == 404,
                "Missing Stock Update ID did not return 404.")
        require(client.post(f"/delete/{missing_id}").status_code == 404,
                "Missing Delete ID did not return 404.")


TESTS = [
    ("Invalid stock updates", test_invalid_stock_updates),
    ("Delete preserves history and dashboard activity", test_delete_preserves_history_and_dashboard),
    ("Stock history search and filters", test_stock_history_search_and_filters),
    ("Filter routes", test_filter_routes),
    ("Local CSV import", test_local_csv_import),
    ("GitHub import and duplicate protection", test_github_import_and_duplicate_protection),
    ("Pages, missing IDs and final route scan", test_pages_missing_ids_and_final_route_scan),
]


def main() -> None:
    passed = 0
    failed = 0

    print("\nStockTrack remaining automated tests\n")

    for name, test_function in TESTS:
        try:
            test_function()
        except Exception as error:
            failed += 1
            print(f"[FAIL] {name}: {error}")
        else:
            passed += 1
            print(f"[PASS] {name}")

    print("\nSummary")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
