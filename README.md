# StockTrack

StockTrack is a Flask-based inventory and expiry management system designed for small retail shops.

It helps businesses track product quantities, expiry dates, stock movements, and inventory status from a simple responsive dashboard.

## Features

- Add and edit products
- Product validation
- Inventory search, filtering, and sorting
- Stock In and Stock Out
- Stock movement history
- Low-stock detection
- Expired and expiring-soon product tracking
- Dashboard statistics and recent activity
- Local CSV product import
- GitHub raw CSV import
- QR-code scanning for GitHub CSV links
- Duplicate product protection
- Persistent stock history after product deletion
- Responsive sidebar interface

## Technology Stack

- Python
- Flask
- SQLite
- HTML
- CSS
- JavaScript
- html5-qrcode
- Waitress

## Database

StockTrack uses two main tables:

### products

- id
- name
- category
- quantity
- expiry_date
- image_path

### stock_movements

- id
- product_id
- movement_type
- quantity
- note
- movement_date

Stock quantities are not edited directly from the Edit Product page.

All quantity changes are performed through Stock In or Stock Out so that inventory history remains accurate.

## Installation

Clone the repository:

```bash
git clone https://github.com/Vinit11-git/Stock-track.git
cd Stock-track