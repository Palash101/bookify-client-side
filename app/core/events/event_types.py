"""Canonical Pub/Sub event_type strings used by this service."""

# Auth / OTP
CLIENT_LOGIN_OTP = "client.login_otp"

# Booking emails (consumer loads details from DB via booking_id)
CLIENT_BOOKING_CREATED = "client.booking.created"
CLIENT_BOOKING_WAITLIST_JOINED = "client.booking.waitlist_joined"
CLIENT_BOOKING_WAITLIST_PROMOTED = "client.booking.waitlist_promoted"
CLIENT_BOOKING_CONFIRMED = "client.booking.confirmed"
CLIENT_BOOKING_CANCELLED = "client.booking.cancelled"
CLIENT_BOOKING_PENDING_PAYMENT = "client.booking.pending_payment"

# Wallet emails (consumer loads details from DB via user_id)
CLIENT_WALLET_TOPUP_SUCCESS = "client.wallet.topup_success"
CLIENT_WALLET_TOPUP_FAILED = "client.wallet.topup_failed"
CLIENT_WALLET_DEBITED = "client.wallet.debited"

# Package emails (consumer loads details from DB via package_id)
CLIENT_PACKAGE_PURCHASED = "client.package.purchased"
CLIENT_PACKAGE_PURCHASE_FAILED = "client.package.purchase_failed"

# Payment emails (consumer loads details from DB via order_id)
CLIENT_PAYMENT_FAILED = "client.payment.failed"
