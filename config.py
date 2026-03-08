import os
from dotenv import load_dotenv

load_dotenv()

# App
APP_NAME = "GrabIt"
APP_URL = os.getenv("APP_URL", "http://localhost:8001")
SECRET_KEY = os.getenv("SECRET_KEY", "grabit-secret-key-2024")

# Facebook OAuth
FB_APP_ID = os.getenv("FB_APP_ID", "")
FB_APP_SECRET = os.getenv("FB_APP_SECRET", "")

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "pk_test_placeholder")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# Business Rules
MIN_ITEM_PRICE = 80.00       # Minimum item price in dollars
MAX_DELIVERY_MILES = 15      # Maximum delivery radius

# Delivery fee tiers: (max_miles, total_fee, runner_payout, platform_cut)
DELIVERY_TIERS = [
    (5,  10.00, 6.00,  4.00),
    (10, 20.00, 13.00, 7.00),
    (15, 30.00, 20.00, 10.00),
]

SERVICE_FEE_RATE = 0.05      # 5% of item price, 100% to platform

# Launch area center (Bethesda/DC)
LAUNCH_LAT = 38.9807
LAUNCH_LNG = -77.1000


def get_delivery_fee(distance_miles: float) -> dict:
    """Return fee breakdown for given distance."""
    for max_miles, total_fee, runner_payout, platform_cut in DELIVERY_TIERS:
        if distance_miles <= max_miles:
            return {
                "delivery_fee": total_fee,
                "runner_payout": runner_payout,
                "platform_cut": platform_cut,
            }
    return None  # Out of range


def calculate_order_totals(
    item_price: float,
    distance_miles: float,
    heavy_item: bool = False,
) -> dict | None:
    """Calculate all fees for an order. Returns None if out of range or below min price.

    If heavy_item is True the delivery fee and runner payout are doubled (platform
    keeps the same percentage); runners_needed is set to 2.
    """
    if item_price < MIN_ITEM_PRICE:
        return None
    if distance_miles > MAX_DELIVERY_MILES or distance_miles <= 0:
        return None

    fees = get_delivery_fee(distance_miles)
    if not fees:
        return None

    delivery_fee  = fees["delivery_fee"]
    runner_payout = fees["runner_payout"]
    platform_cut  = fees["platform_cut"]
    runners_needed = 1

    if heavy_item:
        delivery_fee  = round(delivery_fee  * 2, 2)
        runner_payout = round(runner_payout * 2, 2)
        runners_needed = 2
        # platform_cut stays the same — only the runner portion doubles

    service_fee     = round(item_price * SERVICE_FEE_RATE, 2)
    total           = round(item_price + delivery_fee + service_fee, 2)
    platform_profit = round(platform_cut + service_fee, 2)

    return {
        "item_price":     round(item_price, 2),
        "distance_miles": distance_miles,
        "delivery_fee":   delivery_fee,
        "runner_payout":  runner_payout,
        "platform_cut":   platform_cut,
        "service_fee":    service_fee,
        "total":          total,
        "platform_profit": platform_profit,
        "heavy_item":     heavy_item,
        "runners_needed": runners_needed,
    }
