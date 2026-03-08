from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional
import os
import secrets
import stripe

from database import init_db, get_db, Order, Runner, RunnerSession
from config import (
    APP_NAME, APP_URL, SECRET_KEY,
    STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY,
    STRIPE_WEBHOOK_SECRET, MIN_ITEM_PRICE, MAX_DELIVERY_MILES,
    calculate_order_totals, FB_APP_ID, FB_APP_SECRET
)

try:
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    PASSLIB_AVAILABLE = True
except ImportError:
    PASSLIB_AVAILABLE = False

# ── App setup ──────────────────────────────────────────────────────────────────
app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

init_db()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# ── Facebook OAuth ─────────────────────────────────────────────────────────────
oauth = OAuth()
if FB_APP_ID and FB_APP_SECRET:
    oauth.register(
        name="facebook",
        client_id=FB_APP_ID,
        client_secret=FB_APP_SECRET,
        access_token_url="https://graph.facebook.com/oauth/access_token",
        authorize_url="https://www.facebook.com/dialog/oauth",
        api_base_url="https://graph.facebook.com/",
        client_kwargs={"scope": "email public_profile"},
    )

# ── Helpers ────────────────────────────────────────────────────────────────────

STATUS_LABELS = {
    "pending":   "Pending Payment",
    "paid":      "Awaiting Runner",
    "accepted":  "Runner Accepted",
    "picked_up": "Item Picked Up",
    "delivered": "Delivered",
}

STATUS_COLORS = {
    "pending":   "#6c757d",
    "paid":      "#FF6B35",
    "accepted":  "#0d6efd",
    "picked_up": "#fd7e14",
    "delivered": "#198754",
}


def tmpl(name: str, request: Request, **ctx):
    ctx.setdefault("app_name", APP_NAME)
    ctx.setdefault("status_labels", STATUS_LABELS)
    ctx.setdefault("status_colors", STATUS_COLORS)
    ctx.setdefault("fb_user", request.session.get("fb_user"))
    ctx.setdefault("fb_login_enabled", bool(FB_APP_ID and FB_APP_SECRET))
    return templates.TemplateResponse(name, {"request": request, **ctx})


def hash_password(password: str) -> str:
    if PASSLIB_AVAILABLE:
        return pwd_context.hash(password)
    # Fallback: store a salted sha256 (not production-grade, but works without passlib)
    import hashlib, os as _os
    salt = _os.urandom(16).hex()
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"sha256${salt}${h}"


def verify_password(password: str, hashed: str) -> bool:
    if PASSLIB_AVAILABLE:
        return pwd_context.verify(password, hashed)
    # Fallback sha256 verifier
    import hashlib
    try:
        _, salt, h = hashed.split("$")
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except Exception:
        return False


def get_runner_from_cookie(request: Request, db: Session) -> Optional[Runner]:
    token = request.cookies.get("runner_token")
    if not token:
        return None
    session = db.query(RunnerSession).filter(RunnerSession.token == token).first()
    if not session:
        return None
    return db.query(Runner).filter(Runner.id == session.runner_id).first()


# ── Home ───────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return tmpl("index.html", request)


# ── Facebook Auth ───────────────────────────────────────────────────────────────
@app.get("/auth/facebook")
async def fb_login(request: Request):
    if not (FB_APP_ID and FB_APP_SECRET):
        raise HTTPException(status_code=503, detail="Facebook login not configured")
    redirect_uri = str(request.url_for("fb_callback"))
    return await oauth.facebook.authorize_redirect(request, redirect_uri)


@app.get("/auth/facebook/callback", name="fb_callback")
async def fb_callback(request: Request):
    try:
        token = await oauth.facebook.authorize_access_token(request)
        resp = await oauth.facebook.get(
            "me?fields=id,name,email,picture.width(100)", token=token
        )
        user = resp.json()
        request.session["fb_user"] = {
            "id": user.get("id"),
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "picture": user.get("picture", {}).get("data", {}).get("url", ""),
        }
    except Exception:
        pass
    return RedirectResponse(url="/order")


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")


# ── Order form ─────────────────────────────────────────────────────────────────
@app.get("/order", response_class=HTMLResponse)
async def order_page(request: Request):
    return tmpl("order.html", request,
                min_price=MIN_ITEM_PRICE,
                max_miles=MAX_DELIVERY_MILES)


@app.post("/order")
async def create_order(
    request: Request,
    fb_link: str = Form(...),
    item_title: str = Form(...),
    item_price: float = Form(...),
    distance_miles: float = Form(...),
    buyer_name: str = Form(...),
    buyer_email: str = Form(...),
    buyer_phone: str = Form(""),
    delivery_address: str = Form(...),
    pickup_address: str = Form(""),
    heavy_item: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    heavy = heavy_item == "on"

    # Validate
    errors = []
    if item_price < MIN_ITEM_PRICE:
        errors.append(f"Item price must be at least ${MIN_ITEM_PRICE:.0f}.")
    if distance_miles <= 0 or distance_miles > MAX_DELIVERY_MILES:
        errors.append(f"Delivery distance must be between 0.1 and {MAX_DELIVERY_MILES} miles.")

    if errors:
        return tmpl("order.html", request,
                    errors=errors,
                    min_price=MIN_ITEM_PRICE,
                    max_miles=MAX_DELIVERY_MILES,
                    form={
                        "fb_link": fb_link, "item_title": item_title,
                        "item_price": item_price, "distance_miles": distance_miles,
                        "buyer_name": buyer_name, "buyer_email": buyer_email,
                        "buyer_phone": buyer_phone, "delivery_address": delivery_address,
                        "pickup_address": pickup_address, "heavy_item": heavy,
                    })

    totals = calculate_order_totals(item_price, distance_miles, heavy_item=heavy)
    if not totals:
        errors.append("Unable to calculate fees. Please check your inputs.")
        return tmpl("order.html", request, errors=errors,
                    min_price=MIN_ITEM_PRICE, max_miles=MAX_DELIVERY_MILES)

    order = Order(
        fb_link=fb_link,
        item_title=item_title,
        item_price=totals["item_price"],
        buyer_name=buyer_name,
        buyer_email=buyer_email,
        buyer_phone=buyer_phone,
        delivery_address=delivery_address,
        pickup_address=pickup_address,
        distance_miles=totals["distance_miles"],
        delivery_fee=totals["delivery_fee"],
        service_fee=totals["service_fee"],
        total=totals["total"],
        runner_payout=totals["runner_payout"],
        platform_profit=totals["platform_profit"],
        heavy_item=totals["heavy_item"],
        runners_needed=totals["runners_needed"],
        status="pending",
        payment_status="unpaid",
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    return RedirectResponse(url=f"/pay/{order.id}", status_code=302)


# ── Quote API (AJAX) ────────────────────────────────────────────────────────────
@app.get("/api/quote")
async def get_quote(
    item_price: float = 0,
    distance_miles: float = 0,
    heavy_item: bool = False,
):
    if item_price < MIN_ITEM_PRICE:
        return JSONResponse({"error": f"Minimum item price is ${MIN_ITEM_PRICE:.0f}"}, status_code=400)
    if distance_miles <= 0 or distance_miles > MAX_DELIVERY_MILES:
        return JSONResponse({"error": f"Distance must be 0.1–{MAX_DELIVERY_MILES} miles"}, status_code=400)
    totals = calculate_order_totals(item_price, distance_miles, heavy_item=heavy_item)
    if not totals:
        return JSONResponse({"error": "Unable to calculate"}, status_code=400)
    return JSONResponse(totals)


# ── Payment page ───────────────────────────────────────────────────────────────
@app.get("/pay/{order_id}", response_class=HTMLResponse)
async def pay_page(order_id: str, request: Request, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.payment_status == "paid":
        return RedirectResponse(url=f"/confirmation/{order_id}")
    return tmpl("pay.html", request, order=order,
                stripe_publishable_key=STRIPE_PUBLISHABLE_KEY)


@app.post("/pay/{order_id}/checkout")
async def create_checkout(order_id: str, request: Request, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if not STRIPE_SECRET_KEY:
        # Demo mode — skip real Stripe, mark as paid
        order.payment_status = "paid"
        order.status = "paid"
        order.stripe_payment_id = "demo_" + order.id[:8]
        db.commit()
        return RedirectResponse(url=f"/confirmation/{order_id}", status_code=302)

    try:
        amount_cents = int(order.total * 100)
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": amount_cents,
                    "product_data": {
                        "name": f"GrabIt — {order.item_title}",
                        "description": (
                            f"Item: ${order.item_price:.2f} | "
                            f"Delivery: ${order.delivery_fee:.2f} | "
                            f"Service fee: ${order.service_fee:.2f}"
                        ),
                    },
                },
                "quantity": 1,
            }],
            customer_email=order.buyer_email,
            mode="payment",
            success_url=f"{APP_URL}/confirmation/{order_id}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_URL}/pay/{order_id}",
            metadata={"order_id": order_id},
        )
        return RedirectResponse(url=session.url, status_code=302)
    except Exception as e:
        return tmpl("pay.html", request, order=order,
                    stripe_publishable_key=STRIPE_PUBLISHABLE_KEY,
                    error=f"Payment error: {str(e)}")


# ── Stripe webhook ─────────────────────────────────────────────────────────────
@app.post("/webhook/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        order_id = session_obj.get("metadata", {}).get("order_id")
        if order_id:
            order = db.query(Order).filter(Order.id == order_id).first()
            if order:
                order.stripe_payment_id = session_obj.get("payment_intent")
                order.payment_status = "paid"
                order.status = "paid"
                db.commit()

    return JSONResponse({"status": "ok"})


# ── Confirmation ───────────────────────────────────────────────────────────────
@app.get("/confirmation/{order_id}", response_class=HTMLResponse)
async def confirmation(
    order_id: str,
    request: Request,
    session_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # If arriving from Stripe success URL, verify and mark paid
    if session_id and order.payment_status != "paid":
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            if sess.payment_status == "paid":
                order.stripe_payment_id = sess.payment_intent
                order.payment_status = "paid"
                order.status = "paid"
                db.commit()
        except Exception:
            pass

    runner = None
    if order.runner_id:
        runner = db.query(Runner).filter(Runner.id == order.runner_id).first()

    return tmpl("confirmation.html", request, order=order, runner=runner)


# ── Public runner landing (redirect to login or dashboard) ─────────────────────
@app.get("/runner", response_class=HTMLResponse)
async def runner_page(request: Request, db: Session = Depends(get_db)):
    current_runner = get_runner_from_cookie(request, db)
    if current_runner:
        return RedirectResponse(url="/runner/dashboard", status_code=302)
    return RedirectResponse(url="/runner/login", status_code=302)


# ── Runner registration ────────────────────────────────────────────────────────
@app.get("/runner/register", response_class=HTMLResponse)
async def runner_register_page(request: Request):
    return tmpl("runner_register.html", request)


@app.post("/runner/register")
async def runner_register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    password: str = Form(...),
    bio: str = Form(""),
    db: Session = Depends(get_db),
):
    errors = []
    if len(password) < 6:
        errors.append("Password must be at least 6 characters.")
    existing = db.query(Runner).filter(Runner.email == email).first()
    if existing:
        errors.append("An account with that email already exists.")

    if errors:
        return tmpl("runner_register.html", request, errors=errors,
                    form={"name": name, "email": email, "phone": phone, "bio": bio})

    runner = Runner(
        name=name,
        email=email,
        phone=phone,
        password_hash=hash_password(password),
        bio=bio,
        is_active=True,
        is_approved=False,  # admin approval required
    )
    db.add(runner)
    db.commit()
    return RedirectResponse(url="/runner/login?registered=1", status_code=302)


# ── Runner login ───────────────────────────────────────────────────────────────
@app.get("/runner/login", response_class=HTMLResponse)
async def runner_login_page(request: Request, db: Session = Depends(get_db)):
    # Already logged in?
    current_runner = get_runner_from_cookie(request, db)
    if current_runner:
        return RedirectResponse(url="/runner/dashboard", status_code=302)
    return tmpl("runner_login.html", request)


@app.post("/runner/login")
async def runner_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    runner = db.query(Runner).filter(Runner.email == email).first()
    if not runner or not runner.password_hash or not verify_password(password, runner.password_hash):
        return tmpl("runner_login.html", request,
                    error="Invalid email or password.",
                    form={"email": email})

    # Create session token
    token = secrets.token_urlsafe(32)
    session_obj = RunnerSession(runner_id=runner.id, token=token)
    db.add(session_obj)
    db.commit()

    response = RedirectResponse(url="/runner/dashboard", status_code=302)
    response.set_cookie("runner_token", token, httponly=True, max_age=60 * 60 * 24 * 7)
    return response


# ── Runner logout ──────────────────────────────────────────────────────────────
@app.get("/runner/logout")
async def runner_logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("runner_token")
    if token:
        db.query(RunnerSession).filter(RunnerSession.token == token).delete()
        db.commit()
    response = RedirectResponse(url="/runner/login", status_code=302)
    response.delete_cookie("runner_token")
    return response


# ── Runner dashboard ───────────────────────────────────────────────────────────
@app.get("/runner/dashboard", response_class=HTMLResponse)
async def runner_dashboard(request: Request, db: Session = Depends(get_db)):
    current_runner = get_runner_from_cookie(request, db)
    if not current_runner:
        return RedirectResponse(url="/runner/login", status_code=302)

    available = (
        db.query(Order)
        .filter(Order.payment_status == "paid", Order.status == "paid")
        .order_by(Order.created_at.asc())
        .all()
    )
    active = (
        db.query(Order)
        .filter(
            Order.runner_id == current_runner.id,
            Order.status.in_(["accepted", "picked_up"]),
        )
        .order_by(Order.updated_at.desc())
        .all()
    )
    completed = (
        db.query(Order)
        .filter(
            Order.runner_id == current_runner.id,
            Order.status == "delivered",
        )
        .order_by(Order.updated_at.desc())
        .limit(20)
        .all()
    )

    return tmpl("runner_dashboard.html", request,
                runner=current_runner,
                available=available,
                active=active,
                completed=completed)


# ── Runner job actions (authenticated) ────────────────────────────────────────
@app.post("/runner/accept/{order_id}")
async def accept_order(
    order_id: str,
    request: Request,
    runner_id: str = Form(None),
    db: Session = Depends(get_db),
):
    # Support both cookie-authenticated runners and legacy runner_id form field
    current_runner = get_runner_from_cookie(request, db)

    if current_runner:
        resolved_runner = current_runner
    elif runner_id:
        resolved_runner = db.query(Runner).filter(Runner.id == runner_id).first()
        if not resolved_runner:
            raise HTTPException(status_code=404, detail="Runner not found")
    else:
        raise HTTPException(status_code=401, detail="Not authenticated")

    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or order.status != "paid":
        raise HTTPException(status_code=400, detail="Order not available")

    order.runner_id = resolved_runner.id
    order.status = "accepted"
    order.updated_at = datetime.utcnow()
    db.commit()

    if current_runner:
        return RedirectResponse(url="/runner/dashboard", status_code=302)
    return RedirectResponse(url="/runner", status_code=302)


@app.post("/runner/pickup/{order_id}")
async def mark_picked_up(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or order.status != "accepted":
        raise HTTPException(status_code=400, detail="Invalid status transition")

    # Verify the logged-in runner owns this order (if cookie present)
    current_runner = get_runner_from_cookie(request, db)
    if current_runner and order.runner_id != current_runner.id:
        raise HTTPException(status_code=403, detail="Not your order")

    order.status = "picked_up"
    order.updated_at = datetime.utcnow()
    db.commit()

    if current_runner:
        return RedirectResponse(url="/runner/dashboard", status_code=302)
    return RedirectResponse(url="/runner", status_code=302)


@app.post("/runner/deliver/{order_id}")
async def mark_delivered(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or order.status != "picked_up":
        raise HTTPException(status_code=400, detail="Invalid status transition")

    current_runner = get_runner_from_cookie(request, db)
    if current_runner and order.runner_id != current_runner.id:
        raise HTTPException(status_code=403, detail="Not your order")

    order.status = "delivered"
    order.updated_at = datetime.utcnow()

    # Credit runner earnings
    if order.runner_id:
        runner = db.query(Runner).filter(Runner.id == order.runner_id).first()
        if runner:
            runner.total_earnings   = round(runner.total_earnings + order.runner_payout, 2)
            runner.total_deliveries = runner.total_deliveries + 1
    db.commit()

    if current_runner:
        return RedirectResponse(url="/runner/dashboard", status_code=302)
    return RedirectResponse(url="/runner", status_code=302)


# ── Admin ──────────────────────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, db: Session = Depends(get_db)):
    orders = db.query(Order).order_by(Order.created_at.desc()).all()
    runners = db.query(Runner).order_by(Runner.created_at.desc()).all()

    # Earnings summary
    paid_orders = [o for o in orders if o.payment_status == "paid"]
    total_revenue = round(sum(o.total for o in paid_orders), 2)
    total_platform_profit = round(sum(o.platform_profit for o in paid_orders), 2)
    total_runner_payouts = round(sum(o.runner_payout for o in paid_orders if o.status == "delivered"), 2)
    delivered_count = sum(1 for o in orders if o.status == "delivered")
    pending_count = sum(1 for o in orders if o.status in ("pending", "paid"))

    runner_map = {r.id: r for r in runners}

    return tmpl("admin.html", request,
                orders=orders,
                runners=runners,
                runner_map=runner_map,
                total_revenue=total_revenue,
                total_platform_profit=total_platform_profit,
                total_runner_payouts=total_runner_payouts,
                delivered_count=delivered_count,
                pending_count=pending_count,
                order_count=len(orders))


@app.post("/admin/orders/{order_id}/status")
async def update_order_status(
    order_id: str,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404)
    valid = {"pending", "paid", "accepted", "picked_up", "delivered"}
    if status not in valid:
        raise HTTPException(status_code=400, detail="Invalid status")
    order.status = status
    order.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url="/admin", status_code=302)


# ── Admin: approve runner ──────────────────────────────────────────────────────
@app.post("/admin/runners/{runner_id}/approve")
async def approve_runner(runner_id: str, db: Session = Depends(get_db)):
    runner = db.query(Runner).filter(Runner.id == runner_id).first()
    if not runner:
        raise HTTPException(status_code=404)
    runner.is_approved = True
    db.commit()
    return RedirectResponse(url="/admin", status_code=302)
