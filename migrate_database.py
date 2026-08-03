import os
import sqlite3
import sys


DATABASE = "inventory.db"
BACKUP_DATABASE = "inventory_backup.db"


def table_exists(connection, table_name):
    result = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        AND name = ?
        """,
        (table_name,)
    ).fetchone()

    return result is not None


def migration_already_applied(connection):
    columns = connection.execute(
        "PRAGMA table_info(stock_movements)"
    ).fetchall()

    product_id_column = next(
        (
            column
            for column in columns
            if column[1] == "product_id"
        ),
        None
    )

    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(stock_movements)"
    ).fetchall()

    has_nullable_product_id = (
        product_id_column is not None
        and product_id_column[3] == 0
    )

    has_set_null_foreign_key = any(
        foreign_key[2] == "products"
        and foreign_key[3] == "product_id"
        and foreign_key[6].upper() == "SET NULL"
        for foreign_key in foreign_keys
    )

    return (
        has_nullable_product_id
        and has_set_null_foreign_key
    )


def create_indexes(connection):
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_products_name_expiry
        ON products (
            name COLLATE NOCASE,
            expiry_date
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_products_category
        ON products (
            category COLLATE NOCASE
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_stock_movements_product_id
        ON stock_movements (
            product_id
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_stock_movements_date
        ON stock_movements (
            movement_date DESC,
            id DESC
        )
        """
    )


def migrate_database():
    if not os.path.exists(DATABASE):
        print(
            f"Migration stopped: {DATABASE} was not found."
        )
        sys.exit(1)

    if not os.path.exists(BACKUP_DATABASE):
        print(
            "Migration stopped: inventory_backup.db "
            "was not found."
        )
        sys.exit(1)

    connection = sqlite3.connect(DATABASE)

    try:
        connection.execute("PRAGMA foreign_keys = OFF")

        if not table_exists(
            connection,
            "stock_movements"
        ):
            raise RuntimeError(
                "The stock_movements table was not found."
            )

        invalid_movements = connection.execute(
            """
            SELECT COUNT(*)
            FROM stock_movements
            WHERE movement_type NOT IN ('IN', 'OUT')
            OR quantity <= 0
            """
        ).fetchone()[0]

        if invalid_movements > 0:
            raise RuntimeError(
                f"Found {invalid_movements} invalid stock "
                "movement record(s). Migration was cancelled."
            )

        connection.execute("BEGIN IMMEDIATE")

        if migration_already_applied(connection):
            create_indexes(connection)
            connection.commit()

            print(
                "Database migration was already completed."
            )
            print("Database indexes are ready.")
            return

        connection.execute(
            """
            DROP TABLE IF EXISTS stock_movements_new
            """
        )

        connection.execute(
            """
            CREATE TABLE stock_movements_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                product_id INTEGER,

                movement_type TEXT NOT NULL
                    CHECK (
                        movement_type IN ('IN', 'OUT')
                    ),

                quantity INTEGER NOT NULL
                    CHECK (quantity > 0),

                note TEXT,

                movement_date TEXT NOT NULL,

                FOREIGN KEY (product_id)
                    REFERENCES products (id)
                    ON DELETE SET NULL
            )
            """
        )

        connection.execute(
            """
            INSERT INTO stock_movements_new (
                id,
                product_id,
                movement_type,
                quantity,
                note,
                movement_date
            )
            SELECT
                stock_movements.id,

                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM products
                        WHERE products.id =
                            stock_movements.product_id
                    )
                    THEN stock_movements.product_id
                    ELSE NULL
                END,

                stock_movements.movement_type,
                stock_movements.quantity,
                stock_movements.note,
                stock_movements.movement_date

            FROM stock_movements
            """
        )

        old_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM stock_movements
            """
        ).fetchone()[0]

        new_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM stock_movements_new
            """
        ).fetchone()[0]

        if old_count != new_count:
            raise RuntimeError(
                "Movement record count did not match "
                "after copying the data."
            )

        connection.execute(
            """
            DROP TABLE stock_movements
            """
        )

        connection.execute(
            """
            ALTER TABLE stock_movements_new
            RENAME TO stock_movements
            """
        )

        create_indexes(connection)

        connection.commit()

        connection.execute("PRAGMA foreign_keys = ON")

        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

        if foreign_key_errors:
            print(
                "Migration completed, but foreign-key "
                "problems were detected:"
            )

            for error in foreign_key_errors:
                print(error)

            sys.exit(1)

        print("Database migration completed successfully.")
        print(
            f"Preserved {new_count} stock movement record(s)."
        )
        print(
            "Deleting a product can now preserve its "
            "stock movement history."
        )
        print("Database indexes were created successfully.")

    except (sqlite3.Error, RuntimeError) as error:
        connection.rollback()

        print("Migration failed.")
        print(error)
        print(
            "Your original database should remain unchanged."
        )

        sys.exit(1)

    finally:
        connection.close()


if __name__ == "__main__":
    migrate_database()